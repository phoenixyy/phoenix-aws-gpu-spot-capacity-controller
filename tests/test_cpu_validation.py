"""Offline-only CPU isolation, eligibility and immutable acquisition regression."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from h100_spot_controller.cli import main
from h100_spot_controller.config import ConfigurationError, load_target, target_from_mapping
from h100_spot_controller.dryrun import integration_dry_run
from h100_spot_controller.failover import build_failover_plan, target_configuration_version
from h100_spot_controller.fleet import _same_launch_template_configs, fleet_request, observe_fleet_capacity, reconcile_existing_fleet
from h100_spot_controller.gpu import GpuMetadataError, describe_gpu_instance_types, target_with_gpu_metadata
from h100_spot_controller.handlers import reconcile
from h100_spot_controller.metrics import metric_data
from h100_spot_controller.outcomes import ReconciliationOutcome
from h100_spot_controller.preflight import collect_cpu_preflight, STANDARD_SPOT_VCPU_QUOTA_CODE
from h100_spot_controller.reconciliation import reconcile_target
from h100_spot_controller.state import DynamoStateStore, InMemoryStateStore, StateConflict
from tests.test_hyd_preflight import FakePricing, FakeQuota, hyd_mapping
from tests.test_reliable_acquisition import RuntimeEC2

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def cpu_mapping(enabled=True):
    raw = hyd_mapping()
    raw.update(target_id="isolated-cpu-run", enabled=enabled, accelerator_profile="cpu-validation",
               acquisition_profile="generic", active_region="ap-northeast-2",
               validation_run_id="run-1", validation_started_at=NOW.isoformat(),
               validation_expires_at=(NOW + timedelta(minutes=30)).isoformat(),
               validation_review_budget_usd="1.00",
               ownership_tags={"purpose": "cpu-spot-validation", "validation-run-id": "run-1"},
               instance_types=[{"name": "c6i.large", "spot_price_cap_usd": "0.10"}])
    inputs = raw["candidate_regions"][0]
    inputs.update(region="ap-northeast-2", bootstrap_contract_version="cpu-validation-v1")
    inputs["standard_placements"] = [{"subnet_id": "subnet-a", "zone_id": "apne2-az1"},
                                      {"subnet_id": "subnet-b", "zone_id": "apne2-az2"}]
    return raw


class CPUClient(RuntimeEC2):
    def __init__(self, target):
        super().__init__(target)
        self.metadata = {"c6i.large": {"InstanceType": "c6i.large", "VCpuInfo": {"DefaultVCpus": 2},
                         "MemoryInfo": {"SizeInMiB": 4096}, "ProcessorInfo": {"SupportedArchitectures": ["x86_64"]},
                         "SupportedUsageClasses": ["on-demand", "spot"]}}
        self.zones = [{"ZoneName": f"ap-northeast-2{letter}", "ZoneId": f"apne2-az{index}",
                       "ZoneType": "availability-zone", "State": "available"}
                      for index, letter in ((1, "a"), (2, "b"))]
        self.offering_pages = [{"InstanceTypeOfferings": [{"InstanceType": "c6i.large", "Location": f"apne2-az{index}"}]} for index in (1, 2)]
        for index, subnet in enumerate(self.subnets, 1):
            subnet["AvailabilityZoneId"] = f"apne2-az{index}"
        self.groups[0]["IpPermissions"] = []
        version = self.launch["LaunchTemplateVersions"][0]
        version["VersionNumber"] = 1
        data = version["LaunchTemplateData"]
        data.pop("SecurityGroupIds")
        data["NetworkInterfaces"] = [{"DeviceIndex": 0, "AssociatePublicIpAddress": False, "Groups": ["sg-test"]}]
        for specification in data["TagSpecifications"]:
            specification["Tags"] = [{"Key": k, "Value": v} for k, v in {**target.tags, "bootstrap-contract-version": "cpu-validation-v1"}.items()]
        self.images = [{"ImageId": "ami-test", "State": "available", "Architecture": "x86_64", "RootDeviceType": "ebs"}]

    def describe_images(self, **kwargs):
        self._call("describe_images", kwargs)
        return {"Images": self.images}

    def set_capacity(self, fleet_id, count, state="running"):
        super().set_capacity(fleet_id, count, state)
        for item in self.instances.values():
            item["InstanceType"] = "c6i.large"
            item["Placement"]["AvailabilityZoneId"] = "apne2-az1"


class CPUValidationTests(unittest.TestCase):
    def setUp(self):
        for location in ("botocore.httpsession.URLLib3Session.send", "botocore.endpoint.Endpoint._send"):
            blocked = patch(location, side_effect=AssertionError("offline CPU tests forbid AWS HTTP"))
            blocked.start()
            self.addCleanup(blocked.stop)
        self.raw = cpu_mapping()
        self.target = target_from_mapping(self.raw)
        self.ec2 = CPUClient(self.target)
        self.store = InMemoryStateStore()
        self.pricing = FakePricing("0.096")
        self.quota = FakeQuota(1152)

    def run_target(self, seconds=0, target=None, store=None, **kwargs):
        return reconcile_target(self.ec2, target or self.target, store or self.store,
                                NOW + timedelta(seconds=seconds), pricing=self.pricing, quota_client=self.quota, **kwargs)

    def preflight(self):
        return collect_cpu_preflight(self.target, self.ec2, self.quota, self.pricing)

    def test_disabled_example_and_canonical_timestamp_normalization(self):
        example = load_target("config/cpu-validation.example.yaml")
        self.assertFalse(example.enabled)
        self.assertIsNone(example.validation_started_at)
        self.assertEqual(("CPU", 0, 0), (example.instance_types[0].accelerator_model, example.instance_types[0].accelerator_count, example.instance_types[0].h100_gpu_count))
        raw = cpu_mapping(); raw["validation_started_at"] = "2026-01-01T00:00:00Z"
        self.assertEqual(self.target.validation_started_at, target_from_mapping(raw).validation_started_at)

    def test_config_rejects_every_profile_escape(self):
        changes = [("acquisition_profile", "hyd-b300"), ("accelerator_profile", "gpu-production"),
                   ("integration_mode", "existing-eks"), ("desired_instance_count", 2),
                   ("maximum_instance_count", 2), ("active_region", None),
                   ("capacity_rebalancing", True), ("excess_instance_termination", True),
                   ("validation_run_id", None), ("validation_run_id", " "),
                   ("validation_review_budget_usd", "1.01"), ("validation_review_budget_usd", "NaN"),
                   ("validation_review_budget_usd", "0")]
        for key, value in changes:
            with self.subTest(key=key, value=value):
                raw = cpu_mapping(); raw[key] = value
                with self.assertRaises(ConfigurationError): target_from_mapping(raw)
        for mode in ("recommend", "auto_initial"):
            raw = cpu_mapping(); raw["region_selection"] = {"mode": mode}
            with self.assertRaises(ConfigurationError): target_from_mapping(raw)
        for tag, value in (("purpose", "production"), ("validation-run-id", "different")):
            raw = cpu_mapping(); raw["ownership_tags"][tag] = value
            with self.assertRaises(ConfigurationError): target_from_mapping(raw)
        raw = cpu_mapping(); raw["candidate_regions"].append(deepcopy(raw["candidate_regions"][0]))
        raw["candidate_regions"][1]["region"] = "us-east-1"
        with self.assertRaises(ConfigurationError): target_from_mapping(raw)
        raw = cpu_mapping(); raw["candidate_regions"][0]["local_zone_placements"] = [{"subnet_id": "subnet-lz", "zone_id": "apne2-lz1"}]
        with self.assertRaises(ConfigurationError): target_from_mapping(raw)
        for version in ("$Latest", "$Default", "0", "-1"):
            raw = cpu_mapping(); raw["candidate_regions"][0]["launch_template_version"] = version
            with self.assertRaises(ConfigurationError): target_from_mapping(raw)
        for name in ("c6i.xlarge", "t3.micro", "g6e.xlarge"):
            raw = cpu_mapping(); raw["instance_types"][0]["name"] = name
            with self.assertRaises(ConfigurationError): target_from_mapping(raw)
        for cap in (None, "0", "0.11", "NaN", "Infinity"):
            raw = cpu_mapping(); raw["instance_types"][0]["spot_price_cap_usd"] = cap
            with self.assertRaises(ConfigurationError): target_from_mapping(raw)

    def test_config_rejects_unsafe_or_implicit_windows(self):
        for value in (None, "2026-01-01T00:00:00", "2026-01-01T01:00:00+01:00", "bad",
                      NOW.isoformat(), (NOW + timedelta(minutes=30, seconds=1)).isoformat(),
                      (NOW - timedelta(seconds=1)).isoformat()):
            with self.subTest(expiry=value):
                raw = cpu_mapping(); raw["validation_expires_at"] = value
                with self.assertRaises(ConfigurationError): target_from_mapping(raw)

    def test_gpu_and_hyd_do_not_accept_cpu_substitution(self):
        with self.assertRaises(GpuMetadataError): describe_gpu_instance_types(self.ec2, ("c6i.large",))
        raw = hyd_mapping(); raw["instance_types"][0]["name"] = "c6i.large"
        with self.assertRaises(ConfigurationError): target_from_mapping(raw)
        raw = cpu_mapping(); raw["accelerator_profile"] = "gpu-production"
        with self.assertRaises(ConfigurationError): target_from_mapping(raw)

    def test_metadata_is_strict_and_failed_reads_do_not_fallback_to_gpu(self):
        self.assertEqual({}, target_with_gpu_metadata(self.target, self.ec2)[1])
        for key, value in (("GpuInfo", {}), ("FpgaInfo", {}), ("SupportedUsageClasses", ["on-demand"]),
                           ("VCpuInfo", {"DefaultVCpus": True}), ("VCpuInfo", {"DefaultVCpus": 4}),
                           ("MemoryInfo", {"SizeInMiB": 8192}), ("ProcessorInfo", {"SupportedArchitectures": ["arm64"]})):
            with self.subTest(key=key):
                self.ec2 = CPUClient(self.target); self.ec2.metadata["c6i.large"][key] = value
                self.assertFalse(self.preflight()["ready"])
                with self.assertRaises(GpuMetadataError): target_with_gpu_metadata(self.target, self.ec2)
        self.ec2 = CPUClient(self.target); self.ec2.metadata = {}
        self.assertFalse(self.preflight()["ready"])

    def test_cpu_standard_quota_and_zero_accelerators_are_explicit(self):
        report = self.preflight()
        self.assertTrue(report["ready"], report)
        self.assertNotIn("gpu_metadata", report)
        self.assertEqual(0, report["cpu_metadata"]["c6i.large"]["count_per_machine"])
        self.assertEqual(STANDARD_SPOT_VCPU_QUOTA_CODE, report["quota"]["quota_code"])
        self.assertEqual(2, report["quota"]["required_vcpus"])
        self.assertIsNone(report["quota"]["available_headroom_vcpus"])
        self.assertEqual("unknown", report["spot_capacity_status"])
        self.assertEqual("0.096", report["effective_spot_caps_usd"]["c6i.large"])

    def test_mandatory_failed_reads_block_direct_capacity_repeatedly(self):
        for operation in ("describe_instance_types", "describe_availability_zones", "describe_instance_type_offerings",
                          "describe_subnets", "describe_security_groups", "describe_launch_template_versions", "describe_images"):
            with self.subTest(operation=operation):
                self.ec2 = CPUClient(self.target); self.ec2.failures.add(operation)
                for seconds in (0, 1):
                    self.assertEqual("preflight_blocked", self.run_target(seconds, store=InMemoryStateStore()).kind)
                self.assertEqual([], self.ec2.creates)
        self.ec2 = CPUClient(self.target)
        for quota in (None, FakeQuota(None), FakeQuota(1)):
            self.quota = quota
            self.assertEqual("preflight_blocked", self.run_target().kind)
        self.assertEqual([], self.ec2.creates)

    def test_offering_subnet_security_group_launch_and_ami_mismatches_block(self):
        mutations = [lambda ec2: ec2.offering_pages.pop(),
                     lambda ec2: ec2.subnets[0].update(AvailabilityZoneId="wrong"),
                     lambda ec2: ec2.subnets[0].update(VpcId="foreign"),
                     lambda ec2: ec2.subnets[0].update(AvailableIpAddressCount=0),
                     lambda ec2: ec2.groups[0].update(VpcId="foreign"),
                     lambda ec2: ec2.groups[0].update(IpPermissions=[{}]),
                     lambda ec2: ec2.launch["LaunchTemplateVersions"][0].update(VersionNumber=2),
                     lambda ec2: ec2.launch["LaunchTemplateVersions"][0]["LaunchTemplateData"].update(InstanceType="t3.micro"),
                     lambda ec2: ec2.launch["LaunchTemplateVersions"][0]["LaunchTemplateData"]["NetworkInterfaces"][0].update(AssociatePublicIpAddress=True),
                     lambda ec2: ec2.images[0].update(Architecture="arm64")]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                self.ec2 = CPUClient(self.target); mutate(self.ec2)
                self.assertFalse(self.preflight()["ready"])
                self.assertEqual("preflight_blocked", self.run_target(store=InMemoryStateStore()).kind)
                self.assertEqual([], self.ec2.creates)

    def test_pricing_and_unknown_quota_fail_closed(self):
        for value in (None, "0", "NaN"):
            self.pricing = FakePricing(value)
            self.assertFalse(self.preflight()["ready"])
            self.assertEqual("preflight_blocked", self.run_target().kind)
        self.assertEqual([], self.ec2.creates)

    def test_disabled_preview_has_no_demand_and_live_preview_shows_budget(self):
        report = integration_dry_run(self.target, {self.target.active_region: self.ec2}, self.pricing, NOW,
                                     {self.target.active_region: self.quota})
        self.assertFalse(report["aws_write"])
        self.assertEqual("1.00", report["validation_review_budget_usd"])
        self.assertFalse(report["billing_hard_cap"])
        self.assertEqual(30, report["maximum_duration_minutes"])
        self.assertIsNone(report["manual_failover_plan_preview"])
        self.assertEqual(self.target.validation_expires_at, report["regions"][self.target.active_region]["request_preview"]["ValidUntil"])
        self.target = replace(self.target, enabled=False, validation_started_at=None, validation_expires_at=None)
        report = integration_dry_run(self.target, {self.target.active_region: self.ec2}, self.pricing, NOW,
                                     {self.target.active_region: self.quota})
        self.assertIsNone(report["regions"][self.target.active_region]["request_preview"])
        self.assertEqual([], self.ec2.creates)

    def test_no_cpu_failover_or_gpu_metrics_even_for_bad_observation_inputs(self):
        with self.assertRaisesRegex(ValueError, "CPU"): build_failover_plan(self.target, 1, (), NOW)
        metrics = metric_data(ReconciliationOutcome("healthy", self.target.target_id, self.target.active_region, 1, 1),
                              8, 1, accelerator_profile="cpu-validation", realized_accelerator_count=8,
                              accelerator_counts_by_model={"B300": 8})
        self.assertEqual(0, next(item["Value"] for item in metrics if item["MetricName"] == "RealizedAcceleratorCount"))
        self.assertFalse(any(item["MetricName"] == "RealizedAcceleratorModelCount" for item in metrics))

    def test_frozen_expiry_is_json_replay_safe_and_botocore_accepts_timestamp(self):
        import boto3
        from botocore.stub import Stubber
        request = fleet_request(self.target, (), {"c6i.large": Decimal("0.096")}, "same-epoch")
        self.assertEqual(request, json.loads(json.dumps(request)))
        self.assertTrue(request["TerminateInstancesWithExpiration"])
        client = boto3.client("ec2", region_name="ap-northeast-2", aws_access_key_id="fake", aws_secret_access_key="fake")
        with Stubber(client) as stub:
            stub.add_response("create_fleet", {"FleetId": "fleet-cpu"}, request)
            self.assertEqual("fleet-cpu", client.create_fleet(**request)["FleetId"])
        self.run_target()
        state = self.store.get(self.target.target_id)
        class Table:
            def get_item(inner, **kwargs): return {"Item": state.as_item()}
        class Dynamo:
            def Table(inner, name): return Table()
        restored = DynamoStateStore("offline", Dynamo()).get(self.target.target_id)
        self.assertEqual(state, restored)
        self.assertEqual(self.ec2.creates[0], json.loads(restored.creation_request_json))

    def test_gpu_defaults_do_not_gain_expiry_or_change_config_hash(self):
        target = target_from_mapping(hyd_mapping())
        request = fleet_request(target, (), {"p6-b300.48xlarge": Decimal("55")})
        self.assertNotIn("ValidUntil", request)
        self.assertNotIn("TerminateInstancesWithExpiration", request)
        from dataclasses import asdict
        from hashlib import sha256
        contract = asdict(target)
        for key in ("validation_run_id", "validation_started_at", "validation_expires_at", "validation_review_budget_usd"):
            contract.pop(key)
        for item in contract["instance_types"]: item.update(accelerator_model="unknown", accelerator_count=0, h100_gpu_count=0)
        expected = int(sha256(json.dumps(contract, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()[:15], 16)
        self.assertEqual(expected, target_configuration_version(target))

    def test_direct_and_handler_expiry_barriers_preserve_intent(self):
        self.run_target(); before = self.store.get(self.target.target_id)
        for seconds, expected in ((-1, "validation_not_started"), (1800, "validation_expired"), (1801, "validation_expired")):
            self.assertEqual(expected, self.run_target(seconds).kind)
            with patch("h100_spot_controller.handlers.utc_now", return_value=NOW + timedelta(seconds=seconds)):
                result = reconcile({"target": self.raw}, None, clients={self.target.active_region: self.ec2},
                                   store=self.store, pricing=self.pricing, quota_clients={self.target.active_region: self.quota})
            self.assertEqual(expected, result["status"])
            self.assertFalse(result["aws_write"])
        self.assertEqual(before, self.store.get(self.target.target_id))
        self.assertEqual(1, len(self.ec2.creates)); self.assertEqual([], self.ec2.modifies)

    def test_expired_first_observation_pins_identity_and_cannot_be_renewed(self):
        self.assertEqual("validation_expired", self.run_target(1800).kind)
        fresh = replace(self.target, validation_started_at=(NOW + timedelta(minutes=30)).isoformat(),
                        validation_expires_at=(NOW + timedelta(minutes=60)).isoformat())
        self.assertEqual("creation_conflict", self.run_target(1801, target=fresh).kind)
        self.assertEqual([], self.ec2.creates)

    def test_changed_deadline_run_or_contract_is_never_adopted(self):
        self.run_target()
        for target in (replace(self.target, validation_expires_at=(NOW + timedelta(minutes=20)).isoformat()),
                       replace(self.target, validation_run_id="other", ownership_tags={"purpose": "cpu-spot-validation", "validation-run-id": "other"}),
                       replace(self.target, instance_types=(replace(self.target.instance_types[0], spot_price_cap_usd=Decimal("0.09")),))):
            self.assertEqual("creation_conflict", self.run_target(1, target=target).kind)
        self.assertEqual(1, len(self.ec2.creates))
        self.assertEqual([], self.ec2.modifies)

    def test_timeout_or_ambiguous_result_replays_exact_request_without_extension(self):
        self.ec2.timeout_after_accept = True
        with self.assertRaises(TimeoutError): self.run_target()
        self.ec2.hide_discovery = True
        self.run_target(1)
        self.assertEqual(self.ec2.creates[0], self.ec2.creates[1])
        self.assertEqual(1, len(self.ec2.records))
        before = self.store.get(self.target.target_id).creation_request_json
        self.assertEqual("validation_expired", self.run_target(1800).kind)
        self.assertEqual(before, self.store.get(self.target.target_id).creation_request_json)
        self.assertEqual(2, len(self.ec2.creates))
        # A returned response without FleetId is just as unresolved as a timeout.
        self.ec2 = CPUClient(self.target); self.store = InMemoryStateStore()
        self.ec2.ambiguous_response = True; self.ec2.hide_discovery = True
        self.assertEqual("creation_pending", self.run_target().kind)
        frozen = self.store.get(self.target.target_id).creation_request_json
        self.assertEqual("creation_pending", self.run_target(1).kind)
        self.assertEqual(self.ec2.creates[0], self.ec2.creates[1])
        self.assertEqual("validation_expired", self.run_target(1800).kind)
        self.assertEqual(frozen, self.store.get(self.target.target_id).creation_request_json)
        self.assertEqual(2, len(self.ec2.creates))

    def test_final_state_failure_hidden_response_uses_same_frozen_identity(self):
        class FailCompletion(InMemoryStateStore):
            def put_if_version(inner, state, expected):
                if state.creation_status == "accepted" and not hasattr(inner, "failed"):
                    inner.failed = True; raise StateConflict("offline completion failure")
                super().put_if_version(state, expected)
        store = FailCompletion()
        with self.assertRaises(StateConflict): self.run_target(store=store)
        self.ec2.hide_discovery = True
        self.run_target(1, store=store)
        self.assertEqual(self.ec2.creates[0], self.ec2.creates[1])
        self.assertEqual({}, store.writers)

    def test_configuration_change_and_elapsed_expiry_between_read_and_write_block(self):
        calls = []
        def guard():
            calls.append(1)
            return len(calls) == 1
        result = self.run_target(configuration_guard=guard)
        self.assertEqual("creation_conflict", result.kind)
        self.assertEqual([], self.ec2.creates)
        self.store = InMemoryStateStore()
        with patch("h100_spot_controller.reconciliation.monotonic", side_effect=[0, 1800]):
            result = self.run_target()
        self.assertEqual("validation_expired", result.kind)
        self.assertEqual([], self.ec2.creates)
        self.assertEqual("pending", self.store.get(self.target.target_id).creation_status)

    def test_terminal_fleet_never_rotates_cpu_generation(self):
        self.run_target()
        self.ec2.records["fleet-1"]["FleetState"] = "deleted"
        self.assertEqual("CPU_TERMINAL_FLEET_REQUIRES_CLEANUP", self.run_target(1).error_code)
        self.assertEqual(1, len(self.ec2.creates))

    def test_residual_states_and_wrong_owned_type_block(self):
        for state in ("pending", "running", "stopping", "stopped", "shutting-down"):
            self.ec2.set_capacity("orphan", 1, state)
            self.assertEqual("orphaned_capacity", self.run_target(store=InMemoryStateStore()).kind)
        self.assertEqual([], self.ec2.creates)
        self.ec2.instances = {}; self.run_target(); self.ec2.set_capacity("fleet-1", 1)
        self.ec2.instances["i-fleet-1-0"]["InstanceType"] = "p6-b300.48xlarge"
        self.assertEqual("OWNED_INSTANCE_TYPE_MISMATCH", self.run_target(1).error_code)

    def test_existing_fleet_expiry_mismatch_and_unsafe_modify_block(self):
        self.run_target()
        record = self.ec2.records["fleet-1"]
        record["ValidUntil"] = (NOW + timedelta(minutes=20)).isoformat()
        self.assertEqual("CPU_FLEET_EXPIRY_MISMATCH", self.run_target(1).error_code)
        record["ValidUntil"] = self.target.validation_expires_at
        record["TargetCapacitySpecification"]["TotalTargetCapacity"] = 0
        self.assertEqual("creation_conflict", self.run_target(2).kind)
        self.assertEqual([], self.ec2.modifies)
        self.assertEqual("validation_expired", reconcile_existing_fleet(self.ec2, self.target, record, (), 0, now=NOW + timedelta(minutes=30)).kind)

    def test_handler_runs_same_cpu_reconciler_and_zero_gpu_observation(self):
        with patch("h100_spot_controller.handlers.utc_now", return_value=NOW):
            result = reconcile({"target": self.raw}, None, clients={self.target.active_region: self.ec2},
                               store=self.store, pricing=self.pricing, quota_clients={self.target.active_region: self.quota})
        self.assertEqual("shortfall", result["status"], result)
        self.assertTrue(result["aws_write"])
        self.ec2.set_capacity("fleet-1", 1)
        capacity = observe_fleet_capacity(self.ec2, self.ec2.records["fleet-1"], self.target)
        self.assertEqual(0, capacity.realized_accelerator_count)
        self.assertEqual(0, capacity.realized_h100_gpu_count)
        with patch("h100_spot_controller.handlers.utc_now", return_value=NOW + timedelta(seconds=1)):
            result = reconcile({"target": self.raw}, None, clients={self.target.active_region: self.ec2},
                               store=self.store, pricing=self.pricing, quota_clients={self.target.active_region: self.quota})
        self.assertEqual("healthy", result["status"], result)
        self.assertFalse(result["aws_write"])

    def test_deployed_cpu_target_and_run_binding_fail_closed(self):
        for target_id, run_id in ((self.target.target_id, ""), ("foreign", "run-1"), (self.target.target_id, "foreign")):
            with self.subTest(target_id=target_id, run_id=run_id), patch.dict(os.environ, {
                    "STATE_TABLE_NAME": "isolated", "TARGET_ID": target_id,
                    "VALIDATION_RUN_ID": run_id, "CUSTOMER_ID": ""}):
                result = reconcile({"target": self.raw}, None, clients={self.target.active_region: self.ec2},
                                   store=self.store, pricing=self.pricing, quota_clients={self.target.active_region: self.quota})
                self.assertEqual("configuration_error", result["status"])
                self.assertFalse(result["aws_write"])
        with patch.dict(os.environ, {"STATE_TABLE_NAME": "isolated", "TARGET_ID": self.target.target_id,
                                    "VALIDATION_RUN_ID": "run-1", "CUSTOMER_ID": ""}), \
                patch("h100_spot_controller.handlers.utc_now", return_value=NOW):
            result = reconcile({"target": self.raw}, None, clients={self.target.active_region: self.ec2},
                               store=self.store, pricing=self.pricing, quota_clients={self.target.active_region: self.quota})
        self.assertEqual("shortfall", result["status"], result)

    def test_first_expired_handler_pins_deadline_and_blocks_renewal(self):
        with patch("h100_spot_controller.handlers.utc_now", return_value=NOW + timedelta(minutes=30)):
            result = reconcile({"target": self.raw}, None, clients={self.target.active_region: self.ec2},
                               store=self.store, pricing=self.pricing, quota_clients={self.target.active_region: self.quota})
        self.assertEqual("validation_expired", result["status"])
        pinned = self.store.get(self.target.target_id)
        self.assertEqual(self.target.validation_expires_at, pinned.validation_expires_at)
        fresh = replace(self.target, validation_started_at=(NOW + timedelta(minutes=30)).isoformat(),
                        validation_expires_at=(NOW + timedelta(minutes=60)).isoformat())
        self.assertEqual("creation_conflict", self.run_target(1801, target=fresh).kind)
        self.assertEqual([], self.ec2.creates)

    def test_fulfillment_in_second_approved_zone_keeps_frozen_priorities(self):
        self.run_target(); self.ec2.set_capacity("fleet-1", 1)
        self.ec2.instances["i-fleet-1-0"]["Placement"]["AvailabilityZoneId"] = "apne2-az2"
        self.assertEqual("healthy", self.run_target(1).kind)
        self.assertEqual([], self.ec2.modifies)

    def test_aws_trailing_zero_price_representation_preserves_cpu_frozen_identity(self):
        self.pricing = FakePricing("0.0960000000")
        self.run_target(); self.ec2.set_capacity("fleet-1", 1)
        before = self.store.get(self.target.target_id).creation_request_json
        request = json.loads(before)
        self.assertEqual("0.0960000000", request["LaunchTemplateConfigs"][0]["Overrides"][0]["MaxPrice"])
        record = self.ec2.records["fleet-1"]
        for override in record["LaunchTemplateConfigs"][0]["Overrides"]:
            override["MaxPrice"] = "0.096"
        # Default generic/GPU behavior is unchanged; only CPU asks for numbers.
        self.assertFalse(_same_launch_template_configs(record, request))
        self.assertTrue(_same_launch_template_configs(record, request, numeric_price_comparison=True))
        self.assertEqual("healthy", self.run_target(1).kind)
        self.assertEqual(before, self.store.get(self.target.target_id).creation_request_json)
        self.assertEqual(request["ClientToken"], self.ec2.creates[0]["ClientToken"])
        self.assertEqual(1, len(self.ec2.creates)); self.assertEqual([], self.ec2.modifies)
        record["LaunchTemplateConfigs"][0]["Overrides"][0]["MaxPrice"] = "0.095"
        self.assertEqual("CPU_FROZEN_FLEET_CONTRACT_CHANGED", self.run_target(2).error_code)
        self.assertEqual(before, self.store.get(self.target.target_id).creation_request_json)
        self.assertEqual([], self.ec2.modifies)

    def test_cpu_price_comparison_rejects_missing_invalid_and_nonfinite_caps(self):
        request = fleet_request(self.target, (), {"c6i.large": "0.0960000000"})
        for value in (None, "", "invalid", "NaN", "sNaN", "Infinity", "-Infinity", "0", "-0.1", True):
            with self.subTest(value=value):
                record = deepcopy(request)
                record["LaunchTemplateConfigs"][0]["Overrides"][0]["MaxPrice"] = value
                self.assertFalse(_same_launch_template_configs(record, request, numeric_price_comparison=True))
                # Equal malformed values on both sides must still fail closed.
                self.assertFalse(_same_launch_template_configs(record, deepcopy(record), numeric_price_comparison=True))
        record = deepcopy(request)
        record["LaunchTemplateConfigs"][0]["Overrides"][0].pop("MaxPrice")
        self.assertFalse(_same_launch_template_configs(record, request, numeric_price_comparison=True))

    def test_cli_local_validation_and_temp_fixture_cleanup(self):
        with tempfile.TemporaryDirectory(prefix="cpu-validation-unit-") as temporary:
            path = Path(temporary) / "target.json"
            # YAML loader accepts JSON, with no real account or credential fixture.
            path.write_text(json.dumps(cpu_mapping(False)))
            output = io.StringIO()
            with patch("sys.argv", ["controller", "validate-target", str(path)]), patch("sys.stdout", output):
                self.assertEqual(0, main())
            report = json.loads(output.getvalue())
            self.assertEqual("cpu-validation", report["accelerator_profile"])
            self.assertFalse(report["aws_write"])
        self.assertFalse(Path(temporary).exists())


if __name__ == "__main__":
    unittest.main()
