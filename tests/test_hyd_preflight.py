"""Offline HYD contract/preflight tests: only in-memory EC2/Pricing fakes."""

import base64
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from h100_spot_controller.config import ConfigurationError, load_target, target_from_mapping
from h100_spot_controller.discovery import discover_region
from h100_spot_controller.dryrun import integration_dry_run
from h100_spot_controller.fleet import fleet_request
from h100_spot_controller.preflight import (
    G_AND_VT_SPOT_VCPU_QUOTA_CODE, P_SPOT_VCPU_QUOTA_CODE,
    collect_hyd_preflight, collect_spot_quota, spot_quota_code,
)


def hyd_mapping():
    return {
        "target_id": "customer-a-hyd", "enabled": False,
        "desired_instance_count": 1, "maximum_instance_count": 1,
        "acquisition_profile": "hyd-b300", "placement_strategy": "all-approved",
        "active_region": "ap-south-2", "ownership_tags": {"customer-id": "customer-a"},
        "instance_types": [{"name": "p6-b300.48xlarge", "spot_price_cap_usd": "20"}],
        "candidate_regions": [{
            "region": "ap-south-2", "launch_template_id": "lt-test", "launch_template_version": "1",
            "ami_id": "ami-test", "iam_instance_profile_arn": "arn:aws:iam::123456789012:instance-profile/test",
            "security_group_ids": ["sg-test"], "bootstrap_contract_version": "b300-v1",
            "user_data_sha256": sha256(b"bootstrap").hexdigest(), "root_volume_encrypted": True,
            "standard_placements": [{"subnet_id": "subnet-a", "zone_id": "aps2-az1"}, {"subnet_id": "subnet-b", "zone_id": "aps2-az2"}],
            "local_zone_placements": [],
        }],
    }


class FakePricing:
    def __init__(self, price="55.04"):
        self.price = price

    def get_products(self, **kwargs):
        if self.price is None:
            raise RuntimeError("pricing unavailable")
        return {"PriceList": [json.dumps({"terms": {"OnDemand": {"term": {"priceDimensions": {"hour": {"unit": "Hrs", "pricePerUnit": {"USD": self.price}}}}}}})]}


class FakeQuota:
    def __init__(self, value=384):
        self.value = value
        self.calls = []

    def get_service_quota(self, **kwargs):
        self.calls.append(kwargs)
        if self.value is None:
            raise RuntimeError("quota unavailable")
        return {"Quota": {"Value": self.value, "QuotaCode": kwargs["QuotaCode"], "Adjustable": True}}


class FakeEC2:
    def __init__(self, target):
        self.calls = []
        self.failures = set()
        self.metadata = {"p6-b300.48xlarge": {"InstanceType": "p6-b300.48xlarge", "GpuInfo": {"Gpus": [{"Name": "B300", "Manufacturer": "NVIDIA", "Count": 8}]}, "SupportedUsageClasses": ["on-demand", "spot"], "VCpuInfo": {"DefaultVCpus": 192}}}
        self.zones = [{"ZoneName": "ap-south-2a", "ZoneId": "aps2-az1", "ZoneType": "availability-zone", "State": "available"}, {"ZoneName": "ap-south-2b", "ZoneId": "aps2-az2", "ZoneType": "availability-zone", "State": "available"}]
        self.offering_pages = [{"InstanceTypeOfferings": [{"InstanceType": "p6-b300.48xlarge", "Location": "aps2-az1"}]}, {"InstanceTypeOfferings": [{"InstanceType": "p6-b300.48xlarge", "Location": "aps2-az2"}]}]
        self.subnets = [{"SubnetId": "subnet-a", "AvailabilityZoneId": "aps2-az1", "State": "available", "VpcId": "vpc-test", "AvailableIpAddressCount": 10}, {"SubnetId": "subnet-b", "AvailabilityZoneId": "aps2-az2", "State": "available", "VpcId": "vpc-test", "AvailableIpAddressCount": 10}]
        self.groups = [{"GroupId": "sg-test", "VpcId": "vpc-test"}]
        tags = [{"Key": key, "Value": value} for key, value in {**target.tags, "bootstrap-contract-version": "b300-v1"}.items()]
        self.launch = {"LaunchTemplateVersions": [{"LaunchTemplateData": {
            "ImageId": "ami-test", "IamInstanceProfile": {"Arn": "arn:aws:iam::123456789012:instance-profile/test"},
            "SecurityGroupIds": ["sg-test"], "MetadataOptions": {"HttpTokens": "required"},
            "UserData": base64.b64encode(b"bootstrap").decode(),
            "BlockDeviceMappings": [{"DeviceName": "/dev/xvda", "Ebs": {"Encrypted": True, "DeleteOnTermination": True}}],
            "TagSpecifications": [{"ResourceType": kind, "Tags": tags} for kind in ("instance", "volume")],
        }}]}
        self.instance_pages = [{"Reservations": []}]
        self.request_pages = [{"SpotInstanceRequests": []}]

    def _call(self, operation, kwargs):
        self.calls.append((operation, kwargs))
        if operation in self.failures:
            raise RuntimeError(f"{operation} unavailable")

    def describe_instance_types(self, **kwargs):
        self._call("describe_instance_types", kwargs)
        return {"InstanceTypes": [self.metadata[name] for name in kwargs["InstanceTypes"] if name in self.metadata]}

    def describe_availability_zones(self, **kwargs):
        self._call("describe_availability_zones", kwargs)
        return {"AvailabilityZones": self.zones}

    def describe_subnets(self, **kwargs):
        self._call("describe_subnets", kwargs)
        return {"Subnets": self.subnets}

    def describe_security_groups(self, **kwargs):
        self._call("describe_security_groups", kwargs)
        return {"SecurityGroups": self.groups}

    def describe_launch_template_versions(self, **kwargs):
        self._call("describe_launch_template_versions", kwargs)
        return self.launch

    def get_spot_placement_scores(self, **kwargs):
        self._call("get_spot_placement_scores", kwargs)
        return {"SpotPlacementScores": [{"Region": "ap-south-2", "Score": 1}]}

    def get_paginator(self, operation):
        ec2 = self

        class Paginator:
            def paginate(self, **kwargs):
                ec2._call(operation, kwargs)
                if operation == "describe_instance_type_offerings":
                    return iter(ec2.offering_pages)
                if operation == "describe_instances":
                    return iter(ec2.instance_pages)
                if operation == "describe_spot_instance_requests":
                    return iter(ec2.request_pages)
                if operation == "describe_fleets":
                    return iter([{"Fleets": []}])
                raise AssertionError(f"unexpected read {operation}")

        return Paginator()


class HydPreflightTests(unittest.TestCase):
    def setUp(self):
        # Fail even an accidental real SDK request before it touches the network.
        network = patch("botocore.httpsession.URLLib3Session.send", side_effect=AssertionError("AWS network forbidden in offline tests"))
        network.start()
        self.addCleanup(network.stop)
        self.target = target_from_mapping(hyd_mapping())
        self.ec2 = FakeEC2(self.target)
        self.quota = FakeQuota()
        self.pricing = FakePricing()

    def preflight(self):
        return collect_hyd_preflight(self.target, self.ec2, self.quota, self.pricing)

    def test_generic_defaults_and_existing_examples_are_backward_compatible(self):
        for name in ("target.example.yaml", "validation-target.example.yaml", "validation-target.auto-initial.example.yaml"):
            with self.subTest(name=name):
                target = load_target(Path("config") / name)
                self.assertEqual("generic", target.acquisition_profile)
                self.assertEqual("sequential", target.placement_strategy)

    def test_disabled_hyd_example_validates_without_aws(self):
        target = load_target("config/hyd-b300.example.yaml")
        self.assertFalse(target.enabled)
        self.assertEqual((1, 1), (target.desired_instance_count, target.maximum_instance_count))
        self.assertEqual("customer-a", target.tags["customer-id"])
        self.assertEqual("all-approved", target.placement_strategy)
        self.assertEqual(2, len(target.candidate_regions[0].standard_placements))
        self.assertEqual((), target.candidate_regions[0].local_zone_placements)
        self.assertEqual("p6-b300.48xlarge", target.instance_types[0].name)

    def test_hyd_rejects_profile_escapes(self):
        mutations = []
        for key, value in (("active_region", "us-east-1"), ("integration_mode", "existing-eks"), ("capacity_rebalancing", True), ("acquisition_profile", "unrecognized"), ("placement_strategy", "random"), ("accelerator_profile", "functional-validation")):
            raw = hyd_mapping(); raw[key] = value; mutations.append(raw)
        for mode in ("auto_initial", "recommend"):
            raw = hyd_mapping(); raw["region_selection"] = {"mode": mode}; mutations.append(raw)
        for customer in (None, "", "   "):
            raw = hyd_mapping(); raw["ownership_tags"] = {"customer-id": customer}; mutations.append(raw)
        raw = hyd_mapping(); raw["instance_types"][0]["name"] = "p5.48xlarge"; mutations.append(raw)
        raw = hyd_mapping(); raw["candidate_regions"][0]["region"] = "us-east-1"; mutations.append(raw)
        raw = hyd_mapping(); raw["candidate_regions"].append({**deepcopy(raw["candidate_regions"][0]), "region": "us-east-1"}); mutations.append(raw)
        raw = hyd_mapping(); raw["candidate_regions"][0]["local_zone_placements"] = [{"subnet_id": "subnet-lz", "zone_id": "aps2-lz1"}]; mutations.append(raw)
        for raw in mutations:
            with self.subTest(raw=raw), self.assertRaises(ConfigurationError):
                target_from_mapping(raw)

    def test_valid_b300_spot_is_eligibility_not_stock_or_headroom(self):
        report = self.preflight()
        self.assertTrue(report["ready"], report)
        self.assertEqual([], report["error_codes"])
        self.assertEqual("unknown", report["spot_capacity_status"])
        self.assertFalse(report["aws_write"])
        self.assertEqual(192, report["quota"]["required_vcpus"])
        self.assertEqual(384, report["quota"]["total_quota_vcpus"])
        self.assertEqual("unknown", report["quota"]["headroom_status"])
        self.assertIsNone(report["quota"]["available_headroom_vcpus"])
        self.assertEqual(P_SPOT_VCPU_QUOTA_CODE, self.quota.calls[0]["QuotaCode"])

    def test_unsupported_spot_is_distinct_from_unverified_support(self):
        item = self.ec2.metadata["p6-b300.48xlarge"]
        item["SupportedUsageClasses"] = ["on-demand"]
        unsupported = self.preflight()
        self.assertFalse(unsupported["ready"])
        self.assertIn("HYD_SPOT_UNSUPPORTED", unsupported["error_codes"])
        item.pop("SupportedUsageClasses")
        unknown = self.preflight()
        self.assertIn("HYD_SPOT_SUPPORT_UNKNOWN", unknown["error_codes"])
        self.assertNotIn("HYD_SPOT_UNSUPPORTED", unknown["error_codes"])

    def test_wrong_model_count_manufacturer_failclosed(self):
        for key, value in (("Name", "H100"), ("Count", 4), ("Manufacturer", "unknown")):
            with self.subTest(key=key):
                self.ec2 = FakeEC2(self.target)
                self.ec2.metadata["p6-b300.48xlarge"]["GpuInfo"]["Gpus"][0][key] = value
                report = self.preflight()
                self.assertFalse(report["ready"])
                self.assertIn("HYD_GPU_IDENTITY_INVALID", report["error_codes"])

    def test_missing_metadata_or_vcpu_blocks(self):
        for field in ("GpuInfo", "VCpuInfo"):
            with self.subTest(field=field):
                self.ec2 = FakeEC2(self.target)
                self.ec2.metadata["p6-b300.48xlarge"].pop(field)
                self.assertFalse(self.preflight()["ready"])
        self.ec2.metadata = {}
        self.assertIn("HYD_METADATA_UNVERIFIED", self.preflight()["error_codes"])

    def test_each_mandatory_api_failure_is_classified_and_blocks(self):
        for operation in ("describe_instance_types", "describe_availability_zones", "describe_instance_type_offerings", "describe_subnets", "describe_security_groups", "describe_launch_template_versions"):
            with self.subTest(operation=operation):
                self.ec2 = FakeEC2(self.target)
                self.ec2.failures.add(operation)
                report = self.preflight()
                self.assertFalse(report["ready"])
                self.assertTrue(any(item["status"] == "unknown" for item in report["checks"]))

    def test_quota_failed_or_missing_read_blocks(self):
        self.quota = FakeQuota(None)
        self.assertIn("HYD_QUOTA_UNVERIFIED", self.preflight()["error_codes"])
        self.quota = None
        self.assertFalse(self.preflight()["ready"])

    def test_quota_total_insufficient_even_without_usage(self):
        self.quota = FakeQuota(191)
        report = self.preflight()
        self.assertIn("HYD_QUOTA_INSUFFICIENT", report["error_codes"])
        self.assertFalse(report["ready"])
        self.assertEqual(192, report["quota"]["required_vcpus"])

    def test_negative_nonfinite_quota_is_unverified(self):
        for value in (-1, "NaN", "Infinity"):
            with self.subTest(value=value):
                self.quota = FakeQuota(value)
                self.assertIn("HYD_QUOTA_UNVERIFIED", self.preflight()["error_codes"])

    def test_missing_offering_blocks_and_all_offering_pages_are_read(self):
        report = self.preflight()
        self.assertTrue(report["ready"])
        self.ec2.offering_pages = self.ec2.offering_pages[:1]
        report = self.preflight()
        self.assertFalse(report["ready"])
        self.assertIn("HYD_OFFERING_UNAVAILABLE", report["error_codes"])

    def test_local_or_unavailable_or_unknown_zone_blocks(self):
        for key, value in (("ZoneType", "local-zone"), ("State", "unavailable"), ("State", None), ("ZoneName", "us-east-1a")):
            with self.subTest(key=key, value=value):
                self.ec2 = FakeEC2(self.target)
                self.ec2.zones[0][key] = value
                self.assertIn("HYD_STANDARD_AZ_INVALID", self.preflight()["error_codes"])

    def test_subnet_az_state_vpc_addresses_are_mandatory(self):
        for key, value in (("AvailabilityZoneId", "aps2-az3"), ("State", "pending"), ("VpcId", "vpc-other"), ("AvailableIpAddressCount", 0), ("AvailableIpAddressCount", None)):
            with self.subTest(key=key, value=value):
                self.ec2 = FakeEC2(self.target)
                self.ec2.subnets[0][key] = value
                self.assertIn("HYD_SUBNET_INVALID", self.preflight()["error_codes"])
        self.ec2.subnets = []
        self.assertFalse(self.preflight()["ready"])

    def test_security_groups_must_match_subnet_vpc(self):
        self.ec2.groups[0]["VpcId"] = "vpc-foreign"
        self.assertIn("HYD_SECURITY_GROUP_VPC_INVALID", self.preflight()["error_codes"])

    def test_launch_contract_requires_customer_tags_and_safe_inputs(self):
        data = self.ec2.launch["LaunchTemplateVersions"][0]["LaunchTemplateData"]
        data["TagSpecifications"][0]["Tags"] = [item for item in data["TagSpecifications"][0]["Tags"] if item["Key"] != "customer-id"]
        self.assertIn("HYD_LAUNCH_CONTRACT_INVALID", self.preflight()["error_codes"])

    def test_launch_conflicting_type_market_and_fixed_placement_block(self):
        for key, value in (("InstanceType", "g6e.xlarge"), ("InstanceRequirements", {"VCpuCount": {"Min": 1}}), ("InstanceMarketOptions", {"MarketType": "on-demand"}), ("Placement", {"AvailabilityZone": "ap-south-2a"}), ("NetworkInterfaces", [{"SubnetId": "subnet-unapproved", "Groups": ["sg-test"]}])):
            with self.subTest(key=key):
                self.ec2 = FakeEC2(self.target)
                self.ec2.launch["LaunchTemplateVersions"][0]["LaunchTemplateData"][key] = value
                self.assertIn("HYD_LAUNCH_CONTRACT_INVALID", self.preflight()["error_codes"])

    def test_pricing_failure_zero_ambiguous_or_missing_blocks(self):
        for price in (None, "0", "NaN"):
            with self.subTest(price=price):
                self.pricing = FakePricing(price)
                self.assertIn("HYD_PRICING_UNVERIFIED", self.preflight()["error_codes"])

    def test_effective_cap_is_minimum_in_both_directions(self):
        self.assertEqual("20", self.preflight()["effective_spot_caps_usd"]["p6-b300.48xlarge"])
        self.pricing = FakePricing("10")
        self.assertEqual("10", self.preflight()["effective_spot_caps_usd"]["p6-b300.48xlarge"])

    def test_usage_is_paginated_unique_and_requests_do_not_doublecount(self):
        running = {"InstanceId": "i-running", "InstanceType": "p6-b300.48xlarge", "InstanceLifecycle": "spot", "State": {"Name": "running"}}
        pending = {**running, "InstanceId": "i-pending", "State": {"Name": "pending"}}
        other = {**running, "InstanceId": "i-other", "InstanceType": "p5.4xlarge"}
        self.ec2.metadata["p5.4xlarge"] = {"InstanceType": "p5.4xlarge", "VCpuInfo": {"DefaultVCpus": 16}}
        self.ec2.instance_pages = [{"Reservations": [{"Instances": [running, pending]}]}, {"Reservations": [{"Instances": [running, other]}]}]
        self.ec2.request_pages = [{"SpotInstanceRequests": [{"SpotInstanceRequestId": "sir-fulfilled", "InstanceId": "i-running", "LaunchSpecification": {"InstanceType": "p6-b300.48xlarge"}}]}, {"SpotInstanceRequests": [{"SpotInstanceRequestId": "sir-open", "LaunchSpecification": {"InstanceType": "p6-b300.48xlarge"}}]}]
        usage = self.preflight()["quota"]["usage"]
        self.assertEqual(208, usage["known_running_vcpus"])
        self.assertEqual(["i-pending"], usage["pending_instance_ids"])
        self.assertEqual(["sir-open"], usage["unfulfilled_request_ids"])
        self.assertEqual("unknown", usage["pending_demand_status"])
        self.assertIsNone(usage["available_headroom_vcpus"])

    def test_usage_failure_does_not_invent_headroom_or_fail_eligibility(self):
        self.ec2.failures.update({"describe_instances", "describe_spot_instance_requests"})
        report = self.preflight()
        self.assertTrue(report["ready"])
        self.assertEqual("unknown", report["quota"]["usage"]["status"])
        self.assertIsNone(report["quota"]["known_running_vcpus"])
        self.assertEqual("unknown", report["quota"]["headroom_status"])

    def test_g6e_selects_g_vt_quota_and_aws_vcpu_metadata(self):
        target = replace(self.target, acquisition_profile="generic", instance_types=(replace(self.target.instance_types[0], name="g6e.xlarge"),))
        self.ec2.metadata["g6e.xlarge"] = {"InstanceType": "g6e.xlarge", "VCpuInfo": {"DefaultVCpus": 4}}
        report = collect_spot_quota(target, self.ec2, self.quota)
        self.assertEqual(G_AND_VT_SPOT_VCPU_QUOTA_CODE, report["quota_code"])
        self.assertEqual(4, report["required_vcpus"])
        self.assertEqual(G_AND_VT_SPOT_VCPU_QUOTA_CODE, self.quota.calls[-1]["QuotaCode"])
        self.assertIsNone(spot_quota_code("c7i.large"))
        self.assertEqual(G_AND_VT_SPOT_VCPU_QUOTA_CODE, spot_quota_code("vt1.3xlarge"))

    def test_hyd_dryrun_uses_shared_preflight_and_parent_preview_helper(self):
        report = integration_dry_run(self.target, {"ap-south-2": self.ec2}, self.pricing, datetime.now(timezone.utc), {"ap-south-2": self.quota})
        region = report["regions"]["ap-south-2"]
        self.assertTrue(region["hyd_preflight"]["ready"])
        self.assertEqual(1, region["sps"]["scores"][0]["Score"])
        self.assertNotIn("g_and_vt_spot_vcpu_quota", region)
        self.assertEqual(P_SPOT_VCPU_QUOTA_CODE, region["spot_vcpu_quota"]["quota_code"])
        self.assertEqual(fleet_request(self.target, (), {"p6-b300.48xlarge": Decimal("55.04")}), region["request_preview"])
        self.assertEqual(2, len(region["request_preview"]["LaunchTemplateConfigs"][0]["Overrides"]))
        self.assertEqual(1, region["request_preview"]["TargetCapacitySpecification"]["TotalTargetCapacity"])
        self.assertIsNone(report["manual_failover_plan_preview"])
        self.assertIsNone(report["automatic_failover_plan_preview"])
        self.assertFalse(report["aws_write"])

    def test_hyd_dryrun_mandatory_failure_has_no_ready_preview(self):
        self.ec2.failures.add("describe_instance_types")
        report = integration_dry_run(self.target, {"ap-south-2": self.ec2}, self.pricing, datetime.now(timezone.utc), {"ap-south-2": self.quota})
        self.assertFalse(report["regions"]["ap-south-2"]["hyd_preflight"]["ready"])
        self.assertIsNone(report["regions"]["ap-south-2"]["request_preview"])

    def test_pricing_ambiguity_blocks(self):
        class AmbiguousPricing:
            def get_products(self, **kwargs):
                return {"PriceList": FakePricing("10").get_products()["PriceList"] + FakePricing("11").get_products()["PriceList"]}
        self.pricing = AmbiguousPricing()
        self.assertIn("HYD_PRICING_UNVERIFIED", self.preflight()["error_codes"])

    def test_disabled_missing_launch_inputs_cannot_be_ready(self):
        inputs = replace(self.target.candidate_regions[0], ami_id=None, root_volume_encrypted=False)
        self.target = replace(self.target, candidate_regions=(inputs,))
        self.assertIn("HYD_LAUNCH_CONTRACT_INVALID", self.preflight()["error_codes"])

    def test_generic_all_approved_cannot_expose_local_zones(self):
        raw = hyd_mapping()
        raw["acquisition_profile"] = "generic"
        raw["candidate_regions"][0]["local_zone_placements"] = [{"zone_id": "aps2-lz1", "subnet_id": "subnet-lz"}]
        with self.assertRaises(ConfigurationError):
            target_from_mapping(raw)

    def test_quota_required_vcpus_scales_with_machine_count(self):
        self.target = replace(self.target, desired_instance_count=2, maximum_instance_count=2)
        report = self.preflight()
        self.assertTrue(report["ready"])
        self.assertEqual(384, report["quota"]["required_vcpus"])
        self.assertEqual(384, report["quota"]["total_quota_vcpus"])
        self.assertIsNone(report["quota"]["available_headroom_vcpus"])

    def test_no_paginator_with_continuation_is_not_complete_evidence(self):
        class IncompleteClient:
            def describe_availability_zones(self, **kwargs):
                return {"AvailabilityZones": []}
            def describe_instance_type_offerings(self, **kwargs):
                return {"InstanceTypeOfferings": [], "NextToken": "more"}
        with self.assertRaises(ValueError):
            discover_region(IncompleteClient(), "ap-south-2", ["p6-b300.48xlarge"])

    def test_discovery_paginates_offerings(self):
        discovery = discover_region(self.ec2, "ap-south-2", ["p6-b300.48xlarge"])
        self.assertEqual({"aps2-az1", "aps2-az2"}, set(discovery.offered_instance_types_by_zone))
        self.assertEqual(1, sum(operation == "describe_instance_type_offerings" for operation, _ in self.ec2.calls))


if __name__ == "__main__":
    unittest.main()
