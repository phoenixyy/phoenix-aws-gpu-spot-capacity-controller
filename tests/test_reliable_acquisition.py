"""Offline crash, concurrency and repeated interruption acceptance tests."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import io
import json
from threading import Event, Thread
import unittest
from unittest.mock import patch

from h100_spot_controller.config import target_from_mapping
from h100_spot_controller.failover import build_failover_plan, target_configuration_version
from h100_spot_controller.fleet import fleet_request, observe_fleet_capacity
from h100_spot_controller.gpu import target_with_gpu_metadata
from h100_spot_controller.handlers import reconcile, spot_event
from h100_spot_controller.outcomes import classify_error_code, classify_fleet_errors
from h100_spot_controller.reconciliation import reconcile_target
from h100_spot_controller.state import DynamoStateStore, InMemoryStateStore, StateConflict, VersionedState
from tests.test_hyd_preflight import FakeEC2, FakePricing, FakeQuota, hyd_mapping

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class RuntimeEC2(FakeEC2):
    def __init__(self, target):
        super().__init__(target)
        self.target = target
        self.records = {}
        self.instances = {}
        self.creates = []
        self.modifies = []
        self.hide_discovery = False
        self.hide_direct = False
        self.create_errors = []
        self.timeout_after_accept = False
        self.ambiguous_response = False
        self.entered = None
        self.release = None

    def get_paginator(self, operation):
        if operation not in {"describe_fleets", "describe_instances", "describe_fleet_instances"}:
            return super().get_paginator(operation)
        ec2 = self

        class Paginator:
            def paginate(self, **kwargs):
                return iter([getattr(ec2, operation)(**kwargs)])

        return Paginator()

    def describe_fleets(self, **kwargs):
        self._call("describe_fleets", kwargs)
        if kwargs.get("FleetIds"):
            records = [] if self.hide_direct else [self.records[key] for key in kwargs["FleetIds"] if key in self.records]
        else:
            records = [] if self.hide_discovery else list(self.records.values())
        return {"Fleets": deepcopy(records)}

    def describe_instances(self, **kwargs):
        self._call("describe_instances", kwargs)
        items = list(self.instances.values())
        if "InstanceIds" in kwargs:
            items = [item for item in items if item["InstanceId"] in kwargs["InstanceIds"]]
        for constraint in kwargs.get("Filters", []):
            name, values = constraint["Name"], constraint["Values"]
            if name.startswith("tag:"):
                items = [item for item in items if dict((tag["Key"], tag["Value"]) for tag in item["Tags"]).get(name[4:]) in values]
            elif name == "instance-state-name":
                items = [item for item in items if item["State"]["Name"] in values]
            elif name == "instance-lifecycle":
                items = [item for item in items if item.get("InstanceLifecycle") in values]
        return {"Reservations": [{"Instances": deepcopy(items)}]}

    def describe_fleet_instances(self, **kwargs):
        return {"ActiveInstances": [{"InstanceId": key} for key, item in self.instances.items()
                                    if item.get("_fleet") == kwargs["FleetId"]]}

    def create_fleet(self, **kwargs):
        self.creates.append(deepcopy(kwargs))
        if self.entered:
            self.entered.set()
            if not self.release.wait(5):
                raise AssertionError("test worker did not release fake capacity write")
        matching = next((item for item in self.records.values() if item.get("ClientToken") == kwargs["ClientToken"]), None)
        if matching is None:
            key = f"fleet-{len(self.records) + 1}"
            matching = {"FleetId": key, "FleetState": "active", **deepcopy(kwargs),
                        "Tags": deepcopy(kwargs["TagSpecifications"][0]["Tags"])}
            self.records[key] = matching
        if self.timeout_after_accept:
            self.timeout_after_accept = False
            raise TimeoutError("AWS accepted, response lost")
        if self.ambiguous_response:
            return {"Errors": deepcopy(self.create_errors)}
        return {"FleetId": matching["FleetId"], "Errors": deepcopy(self.create_errors)}

    def modify_fleet(self, **kwargs):
        self.modifies.append(deepcopy(kwargs))
        self.records[kwargs["FleetId"]].update({key: value for key, value in kwargs.items() if key != "FleetId"})
        return {"Return": True}

    def set_capacity(self, fleet_id, count, state="running"):
        self.instances = {key: item for key, item in self.instances.items() if item.get("_fleet") != fleet_id}
        for index in range(count):
            key = f"i-{fleet_id}-{index}"
            self.instances[key] = {
                "InstanceId": key, "InstanceType": "p6-b300.48xlarge", "InstanceLifecycle": "spot",
                "State": {"Name": state}, "Placement": {"AvailabilityZoneId": "aps2-az1"},
                "Tags": [{"Key": k, "Value": v} for k, v in self.target.tags.items()], "_fleet": fleet_id,
            }


class ReliableAcquisitionTests(unittest.TestCase):
    def setUp(self):
        network = patch("botocore.httpsession.URLLib3Session.send", side_effect=AssertionError("AWS network forbidden"))
        network.start()
        self.addCleanup(network.stop)
        raw = hyd_mapping()
        raw["enabled"] = True
        self.raw = raw
        self.target = target_from_mapping(raw)
        self.store = InMemoryStateStore()
        self.ec2 = RuntimeEC2(self.target)
        self.caps = {"p6-b300.48xlarge": 55.04}

    def run_reconcile(self, seconds=0, target=None, store=None):
        return reconcile_target(self.ec2, target or self.target, store or self.store,
                                NOW + timedelta(seconds=seconds), self.caps)

    def test_intent_is_saved_before_cloud_write_and_result_identity_is_saved(self):
        original = self.ec2.create_fleet

        def check(**kwargs):
            intent = self.store.get(self.target.target_id)
            self.assertEqual("pending", intent.creation_status)
            self.assertEqual(kwargs, json.loads(intent.creation_request_json))
            self.assertIsNone(intent.owned_fleet_id)
            return original(**kwargs)

        self.ec2.create_fleet = check
        result = self.run_reconcile()
        self.assertEqual("shortfall", result.kind)
        self.assertEqual("fleet-1", self.store.get(self.target.target_id).owned_fleet_id)
        self.assertEqual("accepted", self.store.get(self.target.target_id).creation_status)
        self.assertEqual(2, len(self.ec2.creates[0]["LaunchTemplateConfigs"][0]["Overrides"]))
        self.assertEqual(1, self.ec2.creates[0]["TargetCapacitySpecification"]["TotalTargetCapacity"])

    def test_intent_commit_failure_prevents_cloud_write(self):
        class BrokenStore(InMemoryStateStore):
            def put_if_version(self, *args):
                raise StateConflict("state unavailable")

        store = BrokenStore()
        with self.assertRaises(StateConflict):
            self.run_reconcile(store=store)
        self.assertEqual([], self.ec2.creates)
        self.assertEqual({}, store.writers)

    def test_cloud_success_then_state_failure_recovers_discovered_identity(self):
        class FailCompletion(InMemoryStateStore):
            def put_if_version(self, state, expected):
                if state.creation_status == "accepted" and not hasattr(self, "failed"):
                    self.failed = True
                    raise StateConflict("lost completion")
                super().put_if_version(state, expected)

        store = FailCompletion()
        with self.assertRaises(StateConflict):
            self.run_reconcile(store=store)
        self.assertEqual("pending", store.get(self.target.target_id).creation_status)
        self.run_reconcile(1, store=store)
        self.assertEqual(1, len(self.ec2.creates))
        self.assertEqual("fleet-1", store.get(self.target.target_id).owned_fleet_id)

    def test_cloud_success_then_state_failure_hidden_result_replays_same_token(self):
        class FailCompletion(InMemoryStateStore):
            def put_if_version(self, state, expected):
                if state.creation_status == "accepted" and not hasattr(self, "failed"):
                    self.failed = True
                    raise StateConflict("lost completion")
                super().put_if_version(state, expected)

        store = FailCompletion()
        with self.assertRaises(StateConflict):
            self.run_reconcile(store=store)
        self.ec2.hide_discovery = True
        self.run_reconcile(1, store=store)
        self.assertEqual(2, len(self.ec2.creates))
        self.assertEqual(self.ec2.creates[0], self.ec2.creates[1])
        self.assertEqual(1, len(self.ec2.records))

    def test_timeout_after_accept_recovers_without_competing_creation(self):
        self.ec2.timeout_after_accept = True
        with self.assertRaises(TimeoutError):
            self.run_reconcile()
        self.run_reconcile(1)
        self.assertEqual(1, len(self.ec2.creates))
        self.assertEqual("accepted", self.store.get(self.target.target_id).creation_status)

    def test_concurrent_writer_cannot_submit_another_request(self):
        entered, released = Event(), Event()
        self.ec2.entered, self.ec2.release = entered, released
        results = []
        failures = []

        def worker():
            try:
                results.append(self.run_reconcile())
            except BaseException as error:
                failures.append(error)

        thread = Thread(target=worker)
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            second = self.run_reconcile(1)
            self.assertEqual("writer_busy", second.kind)
            self.assertEqual(1, len(self.ec2.creates))
        finally:
            released.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual([], failures)
        self.assertEqual(1, len(results))
        self.assertEqual({}, self.store.writers)

    def test_ambiguous_request_does_not_change_for_config_or_price_updates(self):
        self.ec2.ambiguous_response = True
        self.ec2.hide_discovery = True
        self.assertEqual("creation_pending", self.run_reconcile().kind)
        before = self.store.get(self.target.target_id).creation_request_json
        grown = replace(self.target, desired_instance_count=2, maximum_instance_count=2)
        self.assertEqual("creation_conflict", self.run_reconcile(1, target=grown).kind)
        self.caps["p6-b300.48xlarge"] = 10
        self.assertEqual("creation_conflict", self.run_reconcile(2).kind)
        self.assertEqual(before, self.store.get(self.target.target_id).creation_request_json)
        self.assertEqual(1, len(self.ec2.creates))

    def test_ambiguous_intent_expires_without_rotating_token(self):
        self.ec2.ambiguous_response = True
        self.ec2.hide_discovery = True
        self.run_reconcile()
        result = self.run_reconcile(3600)
        self.assertEqual("CREATION_REPLAY_WINDOW_EXPIRED", result.error_code)
        self.assertEqual(1, len(self.ec2.creates))

    def test_known_fleet_temporarily_hidden_is_resolved_by_identity(self):
        self.run_reconcile()
        self.ec2.hide_discovery = True
        self.assertEqual("shortfall", self.run_reconcile(1).kind)
        self.assertEqual(1, len(self.ec2.creates))
        self.assertTrue(any(op == "describe_fleets" and args.get("FleetIds") == ["fleet-1"] for op, args in self.ec2.calls))

    def test_known_fleet_invisible_everywhere_blocks_new_generation(self):
        self.run_reconcile()
        self.ec2.hide_discovery = self.ec2.hide_direct = True
        result = self.run_reconcile(1)
        self.assertEqual("KNOWN_FLEET_NOT_VISIBLE", result.error_code)
        self.assertEqual(1, len(self.ec2.creates))

    def test_terminal_fleet_with_zero_instances_gets_a_new_generation(self):
        self.run_reconcile()
        self.ec2.records["fleet-1"]["FleetState"] = "deleted"
        self.run_reconcile(1)
        self.assertEqual(2, len(self.ec2.creates))
        self.assertNotEqual(self.ec2.creates[0]["ClientToken"], self.ec2.creates[1]["ClientToken"])
        self.assertEqual("fleet-2", self.store.get(self.target.target_id).owned_fleet_id)

    def test_all_residual_states_block_creation_even_without_runtime_state(self):
        for name in ("pending", "running", "stopping", "stopped", "shutting-down"):
            with self.subTest(state=name):
                self.ec2.set_capacity("fleet-orphan", 1, name)
                result = self.run_reconcile(store=InMemoryStateStore())
                self.assertEqual("orphaned_capacity", result.kind)
                self.assertEqual([], self.ec2.creates)

    def test_deleted_running_fleet_with_residual_blocks_replacement(self):
        self.run_reconcile()
        self.ec2.records["fleet-1"]["FleetState"] = "deleted_running"
        self.ec2.set_capacity("fleet-1", 1)
        result = self.run_reconcile(1)
        self.assertEqual("orphaned_capacity", result.kind)
        self.assertEqual(1, len(self.ec2.creates))

    def test_duplicate_owned_fleets_stop_all_capacity_mutations(self):
        self.run_reconcile()
        duplicate = deepcopy(self.ec2.records["fleet-1"])
        duplicate["FleetId"] = "fleet-other"
        self.ec2.records["fleet-other"] = duplicate
        result = self.run_reconcile(1)
        self.assertEqual("MULTIPLE_OWNED_FLEETS", result.error_code)
        self.assertEqual(1, len(self.ec2.creates))
        self.assertEqual([], self.ec2.modifies)

    def test_recovered_pending_fleet_must_match_client_token(self):
        self.ec2.timeout_after_accept = True
        with self.assertRaises(TimeoutError):
            self.run_reconcile()
        self.ec2.records["fleet-1"]["ClientToken"] = "unrelated"
        self.assertEqual("PENDING_FLEET_TOKEN_MISMATCH", self.run_reconcile(1).error_code)
        self.assertEqual(1, len(self.ec2.creates))

    def test_metadata_does_not_change_operator_configuration_fingerprint(self):
        hydrated, _ = target_with_gpu_metadata(self.target, self.ec2)
        self.assertEqual(target_configuration_version(self.target), target_configuration_version(hydrated))

    def test_price_policy_does_not_change_token_if_effective_cap_unchanged(self):
        first = fleet_request(self.target, (), {"p6-b300.48xlarge": 55})
        higher = fleet_request(self.target, (), {"p6-b300.48xlarge": 60})
        lower = fleet_request(self.target, (), {"p6-b300.48xlarge": 10})
        self.assertEqual(first["ClientToken"], higher["ClientToken"])
        self.assertNotEqual(first["ClientToken"], lower["ClientToken"])
        self.assertEqual("20", first["LaunchTemplateConfigs"][0]["Overrides"][0]["MaxPrice"])

    def test_embedded_errors_keep_fleet_identity_and_are_classified(self):
        for code, kind in (("InsufficientInstanceCapacity", "shortfall"),
                           ("MaxSpotInstanceCountExceeded", "quota_error"),
                           ("UnauthorizedOperation", "authorization_error"),
                           ("InvalidAMIID.NotFound", "configuration_error")):
            with self.subTest(code=code):
                store = InMemoryStateStore()
                self.ec2.records = {}
                self.ec2.create_errors = [{"ErrorCode": code}]
                result = self.run_reconcile(store=store)
                self.assertEqual(kind, result.kind)
                self.assertEqual("fleet-1", store.get(self.target.target_id).owned_fleet_id)
                self.assertEqual((code,), result.pool_error_codes)

    def test_multiple_interruptions_reuse_original_maintain_fleet(self):
        self.run_reconcile()
        hydrated, _ = target_with_gpu_metadata(self.target, self.ec2)
        for cycle in range(3):
            self.ec2.set_capacity("fleet-1", 1)
            self.assertEqual("healthy", self.run_reconcile(60 * cycle + 1, target=hydrated).kind)
            self.ec2.set_capacity("fleet-1", 0)
            self.assertEqual("shortfall", self.run_reconcile(60 * cycle + 2, target=hydrated).kind)
        self.assertEqual(1, len(self.ec2.creates))
        self.assertEqual([], self.ec2.modifies)

    def invoke_handler(self, seconds=0, quota=None):
        with patch.dict("os.environ", {"CUSTOMER_ID": "customer-a", "TARGET_ID": self.target.target_id}), patch("h100_spot_controller.handlers.utc_now", return_value=NOW + timedelta(seconds=seconds)):
            return reconcile({"target": self.raw}, None, clients={"ap-south-2": self.ec2}, store=self.store,
                             pricing=FakePricing(), quota_clients={"ap-south-2": quota or FakeQuota()})

    def test_runtime_hyd_preflight_blocks_unsupported_spot_before_state_or_cloud_write(self):
        self.ec2.metadata["p6-b300.48xlarge"]["SupportedUsageClasses"] = ["on-demand"]
        result = self.invoke_handler()
        self.assertEqual("preflight_blocked", result["status"])
        self.assertFalse(result["aws_write"])
        self.assertIn("HYD_SPOT_UNSUPPORTED", result["preflight"]["error_codes"])
        self.assertIsNone(self.store.get(self.target.target_id))
        self.assertEqual([], self.ec2.creates)

    def test_single_region_handler_keeps_reconciling_after_long_shortfall(self):
        self.assertEqual("shortfall", self.invoke_handler()["status"])
        self.invoke_handler(60)
        result = self.invoke_handler(7200)
        self.assertEqual("shortfall", result["status"])
        self.assertEqual(1, len(self.ec2.creates))
        self.assertEqual(["ap-south-2"], [region.region for region in self.target.candidate_regions])

    def test_runtime_rejects_wrong_customer_identity(self):
        with patch.dict("os.environ", {"CUSTOMER_ID": "customer-b", "TARGET_ID": self.target.target_id}):
            result = reconcile({"target": self.raw}, None, clients={"ap-south-2": self.ec2}, store=self.store, pricing=FakePricing())
        self.assertEqual("configuration_error", result["status"])
        self.assertEqual([], self.ec2.creates)
        self.assertEqual([], self.ec2.calls)

    def test_hyd_manual_failover_is_explicitly_rejected(self):
        with self.assertRaisesRegex(ValueError, "does not permit"):
            build_failover_plan(self.target, 1, (), NOW)

    def test_event_region_and_ownership_are_validated_without_capacity_writes(self):
        self.ec2.set_capacity("fleet-1", 1)
        instance_id = next(iter(self.ec2.instances))

        class Targets:
            def get_mapping(inner, target_id):
                return self.raw

        event = {"target_id": self.target.target_id, "region": "ap-south-2", "detail-type": "EC2 Spot Instance Interruption Warning", "detail": {"instance-id": instance_id}}
        with patch.dict("os.environ", {"CUSTOMER_ID": "customer-a", "TARGET_ID": self.target.target_id}):
            result = spot_event(event, None, clients={"ap-south-2": self.ec2}, target_store=Targets())
            self.assertEqual("spot_interruption", result["status"])
            foreign = spot_event({**event, "region": "us-east-1"}, None, clients={"ap-south-2": self.ec2}, target_store=Targets())
            self.assertEqual("ignored_unapproved_region", foreign["status"])
            self.ec2.instances[instance_id]["Tags"] = []
            unowned = spot_event(event, None, clients={"ap-south-2": self.ec2}, target_store=Targets())
            self.assertEqual("ignored_unowned_instance", unowned["status"])
        self.assertEqual([], self.ec2.creates)
        self.assertEqual([], self.ec2.modifies)

    def test_expired_writer_release_cannot_delete_a_new_owner(self):
        self.assertTrue(self.store.acquire_writer("x", "old", NOW))
        self.assertFalse(self.store.acquire_writer("x", "new", NOW + timedelta(seconds=1)))
        self.assertTrue(self.store.acquire_writer("x", "new", NOW + timedelta(seconds=181)))
        self.store.release_writer("x", "old")
        self.assertEqual("new", self.store.writers["x"][0])

    def test_old_state_and_intent_roundtrip_are_dynamodb_safe(self):
        import boto3
        from boto3.dynamodb.types import TypeSerializer, TypeDeserializer
        from types import SimpleNamespace
        client = boto3.client("dynamodb", region_name="ap-south-2", aws_access_key_id="test", aws_secret_access_key="test")
        serializer, deserializer = TypeSerializer(), TypeDeserializer()

        class Table:
            meta = SimpleNamespace(client=client)
            item = {"pk": "TARGET#old", "sk": "STATE", "version": 1,
                    "active_region": "ap-south-2", "active_zone_ids": ["aps2-az1"]}
            conditions = []

            def get_item(inner, **kwargs):
                return {"Item": deepcopy(inner.item)}

            def put_item(inner, **kwargs):
                inner.conditions.append(kwargs["ConditionExpression"])
                inner.item = {key: deserializer.deserialize(serializer.serialize(value)) for key, value in kwargs["Item"].items()}

        table = Table()
        dynamodb = SimpleNamespace(Table=lambda name: table)
        store = DynamoStateStore("offline", dynamodb)
        old = store.get("old")
        self.assertIsNone(old.creation_request_json)
        self.run_reconcile()
        current = self.store.get(self.target.target_id)
        store.put_if_version(current, 1)
        restored = store.get(self.target.target_id)
        self.assertEqual(current, restored)
        self.assertEqual(json.loads(current.creation_request_json), json.loads(restored.creation_request_json))

    def test_fleet_instance_pagination_does_not_hide_capacity(self):
        self.run_reconcile()
        self.ec2.set_capacity("fleet-1", 2)
        original = self.ec2.get_paginator
        ids = list(self.ec2.instances)

        class Pages:
            def paginate(inner, **kwargs):
                return iter([{"ActiveInstances": [{"InstanceId": ids[0]}]}, {"ActiveInstances": [{"InstanceId": ids[1]}]}])

        self.ec2.get_paginator = lambda operation: Pages() if operation == "describe_fleet_instances" else original(operation)
        hydrated, _ = target_with_gpu_metadata(self.target, self.ec2)
        observed = observe_fleet_capacity(self.ec2, self.ec2.records["fleet-1"], hydrated)
        self.assertEqual(2, sum(observed.by_zone.values()))
        self.assertEqual(16, observed.realized_accelerator_count)

    def test_unpageable_fleet_instances_use_direct_describe(self):
        self.run_reconcile()
        self.ec2.set_capacity("fleet-1", 1)

        class NotPageable(Exception):
            pass
        NotPageable.__name__ = "OperationNotPageableError"

        def reject(operation):
            if operation == "describe_fleet_instances":
                raise NotPageable("not pageable")
            return self.ec2.get_paginator(operation)

        self.ec2.get_paginator = reject
        hydrated, _ = target_with_gpu_metadata(self.target, self.ec2)
        observed = observe_fleet_capacity(self.ec2, self.ec2.records["fleet-1"], hydrated)
        self.assertEqual(1, sum(observed.by_zone.values()))

    def test_error_classifier_distinguishes_transient_from_quota_and_capacity(self):
        self.assertEqual("rate_limited", classify_error_code("RequestLimitExceeded")[0])
        self.assertEqual("quota_error", classify_error_code("MaxSpotInstanceCountExceeded")[0])
        self.assertEqual("shortfall", classify_error_code("InsufficientInstanceCapacity")[0])
        self.assertEqual("authorization_error", classify_fleet_errors([{"ErrorCode": "UnauthorizedOperation"}, {"ErrorCode": "InsufficientInstanceCapacity"}])[0])

    def test_configuration_guard_under_writer_blocks_stale_enabled_snapshot(self):
        result = reconcile_target(self.ec2, self.target, self.store, NOW, self.caps,
                                  configuration_guard=lambda: False)
        self.assertEqual("TARGET_CONFIGURATION_CHANGED", result.error_code)
        self.assertEqual([], self.ec2.creates)
        self.assertIsNone(self.store.get(self.target.target_id))
        self.assertEqual({}, self.store.writers)

    def test_disable_between_initial_read_and_writer_claim_prevents_creation(self):
        test = self

        class Targets:
            calls = 0

            def get_mapping(inner, target_id):
                inner.calls += 1
                raw = deepcopy(test.raw)
                if inner.calls > 1:
                    raw["enabled"] = False
                return raw

        targets = Targets()
        with patch.dict("os.environ", {"CUSTOMER_ID": "customer-a", "TARGET_ID": self.target.target_id}):
            result = reconcile({"target_id": self.target.target_id}, None,
                               clients={"ap-south-2": self.ec2}, store=self.store, pricing=FakePricing(),
                               quota_clients={"ap-south-2": FakeQuota()}, target_store=targets)
        self.assertEqual("creation_conflict", result["status"])
        self.assertEqual("TARGET_CONFIGURATION_CHANGED", result["outcome"]["error_code"])
        self.assertFalse(result["capacity_write_attempted"])
        self.assertEqual([], self.ec2.creates)
        self.assertIsNone(self.store.get(self.target.target_id))
        self.assertEqual(2, targets.calls)

    def test_disabled_runtime_does_not_require_pricing_or_launch_prerequisites(self):
        self.raw["enabled"] = False
        with patch.dict("os.environ", {"CUSTOMER_ID": "customer-a", "TARGET_ID": self.target.target_id}):
            result = reconcile({"target": self.raw}, None, clients={"ap-south-2": self.ec2},
                               store=self.store, pricing=FakePricing(None))
        self.assertEqual("disabled", result["status"])
        self.assertFalse(result["capacity_write_attempted"])
        self.assertEqual([], self.ec2.creates)

    def test_runtime_exposes_uncertain_capacity_write_after_response_loss(self):
        self.ec2.timeout_after_accept = True
        result = self.invoke_handler()
        self.assertEqual("dependency_error", result["status"])
        self.assertIsNone(result["aws_write"])
        self.assertTrue(result["capacity_write_attempted"])
        self.assertEqual("uncertain", result["capacity_writes"][0]["status"])
        self.assertEqual("pending", self.store.get(self.target.target_id).creation_status)

    def test_hyd_requires_immutable_numeric_launch_version(self):
        for version in ("$Latest", "$Default", "0", "-1", "1.0"):
            raw = deepcopy(self.raw)
            raw["candidate_regions"][0]["launch_template_version"] = version
            with self.subTest(version=version), self.assertRaisesRegex(ValueError, "pinned positive numeric"):
                target_from_mapping(raw)

    def test_active_fleet_and_extra_orphan_capacity_stop_mutations(self):
        self.run_reconcile()
        self.ec2.set_capacity("fleet-1", 1)
        self.ec2.instances["i-orphan"] = deepcopy(next(iter(self.ec2.instances.values())))
        self.ec2.instances["i-orphan"].update(InstanceId="i-orphan", _fleet="deleted-running")
        result = self.run_reconcile(1)
        self.assertEqual("OWNED_INSTANCES_OUTSIDE_FLEET", result.error_code)
        self.assertEqual([], self.ec2.modifies)
        self.assertEqual(1, len(self.ec2.creates))

    def test_orphan_within_current_maximum_still_blocks_future_full_fulfillment(self):
        target = replace(self.target, desired_instance_count=2, maximum_instance_count=2)
        self.run_reconcile(target=target)
        self.ec2.set_capacity("orphan", 1)
        result = self.run_reconcile(1, target=target)
        self.assertEqual("OWNED_INSTANCES_OUTSIDE_FLEET", result.error_code)
        self.assertEqual([], self.ec2.modifies)
        self.assertEqual(1, len(self.ec2.creates))

    def test_explicitly_approved_shrink_is_not_misclassified_as_orphan(self):
        grown = replace(self.target, desired_instance_count=2, maximum_instance_count=2)
        self.run_reconcile(target=grown)
        self.ec2.set_capacity("fleet-1", 2)
        smaller = replace(self.target, excess_instance_termination=True)
        self.run_reconcile(1, target=smaller)
        self.assertEqual(1, len(self.ec2.modifies))
        self.assertEqual("termination", self.ec2.modifies[0]["ExcessCapacityTerminationPolicy"])
        self.assertEqual(1, self.ec2.modifies[0]["TargetCapacitySpecification"]["TotalTargetCapacity"])

    def test_hyd_never_counts_a_different_owned_gpu_type_as_b300(self):
        self.run_reconcile()
        self.ec2.set_capacity("fleet-1", 1)
        next(iter(self.ec2.instances.values()))["InstanceType"] = "p5.48xlarge"
        result = self.run_reconcile(1)
        self.assertEqual("OWNED_INSTANCE_TYPE_MISMATCH", result.error_code)
        self.assertEqual([], self.ec2.modifies)

    def test_non_spot_fleet_or_instance_cannot_be_adopted_as_b300_spot(self):
        self.run_reconcile()
        self.ec2.records["fleet-1"]["TargetCapacitySpecification"]["DefaultTargetCapacityType"] = "on-demand"
        self.assertEqual("FLEET_NOT_SPOT_ONLY", self.run_reconcile(1).error_code)
        self.ec2.records["fleet-1"]["TargetCapacitySpecification"]["DefaultTargetCapacityType"] = "spot"
        self.ec2.set_capacity("fleet-1", 1)
        next(iter(self.ec2.instances.values()))["InstanceLifecycle"] = "on-demand"
        self.assertEqual("OWNED_INSTANCE_NOT_SPOT", self.run_reconcile(2).error_code)
        self.assertEqual([], self.ec2.modifies)
        self.assertEqual(1, len(self.ec2.creates))

    def test_current_describe_fleets_errors_validate_against_real_sdk_shape(self):
        import boto3
        from botocore.stub import Stubber
        from h100_spot_controller.fleet import reconcile_existing_fleet
        client = boto3.client("ec2", region_name="ap-south-2", aws_access_key_id="test", aws_secret_access_key="test")
        record = {"FleetId": "fleet-1234567890abcdef0", "FleetState": "active", "Type": "maintain",
                  "ClientToken": "approved-token", "ActivityStatus": "error",
                  "Tags": [{"Key": k, "Value": v} for k, v in self.target.tags.items()],
                  "LaunchTemplateConfigs": fleet_request(self.target, (), self.caps)["LaunchTemplateConfigs"],
                  "TargetCapacitySpecification": {"TotalTargetCapacity": 1, "DefaultTargetCapacityType": "spot"},
                  "Errors": [{"ErrorCode": "MaxSpotInstanceCountExceeded", "ErrorMessage": "quota", "Lifecycle": "spot"}]}
        with Stubber(client) as stubber:
            stubber.add_response("describe_fleets", {"Fleets": [record]}, {"FleetIds": [record["FleetId"]]})
            observed = client.describe_fleets(FleetIds=[record["FleetId"]])["Fleets"][0]
        result = reconcile_existing_fleet(client, self.target, observed, ("aps2-az1", "aps2-az2"), 0, self.caps)
        self.assertEqual("quota_error", result.kind)
        self.assertEqual(("MaxSpotInstanceCountExceeded",), result.pool_error_codes)

    def test_dynamo_writer_conditions_enforce_owner_and_expiry(self):
        import boto3
        from boto3.dynamodb.conditions import Attr
        from types import SimpleNamespace
        client = boto3.client("dynamodb", region_name="ap-south-2", aws_access_key_id="test", aws_secret_access_key="test")
        missing = object()

        def evaluate(condition, item):
            if isinstance(condition, Attr):
                return item.get(condition.name, missing)
            if not hasattr(condition, "get_expression"):
                return condition
            expression = condition.get_expression()
            values = [evaluate(value, item) for value in expression["values"]]
            operator = expression["operator"]
            if operator == "attribute_not_exists":
                return values[0] is missing
            if operator == "OR":
                return any(values)
            if operator == "=":
                return values[0] == values[1]
            if operator == "<=":
                return values[0] is not missing and values[0] <= values[1]
            raise AssertionError(operator)

        class Table:
            meta = SimpleNamespace(client=client)
            rows = {}

            def put_item(inner, **kwargs):
                item = kwargs["Item"]
                key = (item["pk"], item["sk"])
                if not evaluate(kwargs["ConditionExpression"], inner.rows.get(key, {})):
                    raise client.exceptions.ConditionalCheckFailedException({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
                inner.rows[key] = deepcopy(item)

            def delete_item(inner, **kwargs):
                key = (kwargs["Key"]["pk"], kwargs["Key"]["sk"])
                if not evaluate(kwargs["ConditionExpression"], inner.rows.get(key, {})):
                    raise client.exceptions.ConditionalCheckFailedException({"Error": {"Code": "ConditionalCheckFailedException"}}, "DeleteItem")
                inner.rows.pop(key)

        table = Table()
        store = DynamoStateStore("test", SimpleNamespace(Table=lambda name: table))
        self.assertTrue(store.acquire_writer("target", "first", NOW))
        self.assertFalse(store.acquire_writer("target", "second", NOW + timedelta(seconds=1)))
        self.assertTrue(store.acquire_writer("target", "second", NOW + timedelta(seconds=181)))
        store.release_writer("target", "first")
        self.assertEqual("second", table.rows[("TARGET#target", "WRITER")]["owner"])
        store.release_writer("target", "second")
        self.assertEqual({}, table.rows)

    def test_hyd_state_region_is_explicit_despite_profile_defaults(self):
        from types import SimpleNamespace
        from h100_spot_controller.cli import _state_region
        self.assertEqual("ap-south-2", _state_region(SimpleNamespace(state_region=None), self.target))
        with self.assertRaisesRegex(ValueError, "state table must"):
            _state_region(SimpleNamespace(state_region="us-east-1"), self.target)

    def test_target_put_serializes_configuration_updates_and_uses_hyd_state_region(self):
        from h100_spot_controller.cli import main
        session = unittest.mock.MagicMock()
        writers = InMemoryStateStore()
        table = session.resource.return_value.Table.return_value
        raw = deepcopy(self.raw)
        raw["enabled"] = False
        target = target_from_mapping(raw)
        with patch("sys.argv", ["h100-spot-controller", "target-put", "config/hyd-b300.example.yaml", "--table", "isolated", "--execute"]), patch("boto3.Session", return_value=session), patch("h100_spot_controller.cli.load_target", return_value=target), patch("h100_spot_controller.cli.load_target_mapping", return_value=raw), patch("h100_spot_controller.cli.describe_gpu_instance_types", return_value={}), patch("h100_spot_controller.cli.DynamoStateStore", return_value=writers), patch("sys.stdout", new_callable=io.StringIO):
            table.put_item.side_effect = lambda **kwargs: self.assertIn(target.target_id, writers.writers)
            self.assertEqual(0, main())
        self.assertEqual("ap-south-2", session.resource.call_args.kwargs["region_name"])
        self.assertEqual({}, writers.writers)
        self.assertEqual(1, table.put_item.call_count)

    def test_target_file_change_cannot_bypass_explicit_capacity_enable(self):
        from h100_spot_controller.cli import main
        session = unittest.mock.MagicMock()
        reviewed = replace(self.target, enabled=False)
        with patch("sys.argv", ["h100-spot-controller", "target-put", "config/hyd-b300.example.yaml", "--table", "isolated", "--execute"]), patch("boto3.Session", return_value=session), patch("h100_spot_controller.cli.load_target", return_value=reviewed), patch("h100_spot_controller.cli.load_target_mapping", return_value=self.raw), patch("h100_spot_controller.cli.describe_gpu_instance_types", return_value={}), patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as error:
                main()
            self.assertEqual(2, error.exception.code)
        session.resource.assert_not_called()

    def test_hyd_cleanup_requires_persisted_disabled_target_and_releases_writer(self):
        from h100_spot_controller.cli import main
        session = unittest.mock.MagicMock()
        writers = InMemoryStateStore()
        reviewed = replace(self.target, enabled=False)
        persisted = unittest.mock.MagicMock()
        persisted.get_mapping.return_value = self.raw  # still enabled in the actual table
        with patch("sys.argv", ["h100-spot-controller", "cleanup", "config/hyd-b300.example.yaml", "--table", "isolated", "--execute"]), patch("boto3.Session", return_value=session), patch("h100_spot_controller.cli.load_target", return_value=reviewed), patch("h100_spot_controller.cli.DynamoStateStore", return_value=writers), patch("h100_spot_controller.cli.DynamoTargetStore", return_value=persisted), patch("h100_spot_controller.cli.ec2_client") as factory, patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as error:
                main()
            self.assertEqual(2, error.exception.code)
            factory.assert_not_called()
        self.assertEqual({}, writers.writers)

    def test_cli_preflight_returns_nonzero_for_blocked_evidence(self):
        from h100_spot_controller.cli import main
        report = {"aws_write": False, "preflight": {"ready": False, "error_codes": ["HYD_SPOT_UNSUPPORTED"]}}
        session = unittest.mock.MagicMock()
        with patch("sys.argv", ["h100-spot-controller", "preflight", "config/hyd-b300.example.yaml"]), patch("boto3.Session", return_value=session), patch("h100_spot_controller.cli.integration_dry_run", return_value=report), patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(2, main())
            self.assertFalse(json.loads(output.getvalue())["preflight"]["ready"])
