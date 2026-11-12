"""Read-only HYD eligibility evidence; neither support nor price predicts stock."""

from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
from typing import Any

from .config import CapacityTarget, ConfigurationError
from .discovery import discover_region
from .launch_contract import inspect_launch_contract
from .pricing import ondemand_caps
from .gpu import describe_cpu_instance_types

P_SPOT_VCPU_QUOTA_CODE = "L-7212CCBC"
G_AND_VT_SPOT_VCPU_QUOTA_CODE = "L-3819A6DF"
STANDARD_SPOT_VCPU_QUOTA_CODE = "L-34B43A08"


def _error_code(error: Exception) -> str:
    return getattr(error, "response", {}).get("Error", {}).get("Code", type(error).__name__)


def _pages(ec2: Any, operation: str, **request: Any) -> Any:
    if hasattr(ec2, "get_paginator"):
        return ec2.get_paginator(operation).paginate(**request)
    page = getattr(ec2, operation)(**request)
    if page.get("NextToken"):
        raise ValueError(f"{operation} inventory is incomplete without a paginator")
    return [page]


def spot_quota_code(instance_type: str) -> str | None:
    """Choose by instance family, not the accelerator-profile name."""
    family = instance_type.split(".", 1)[0].lower()
    if family.startswith("p"):
        return P_SPOT_VCPU_QUOTA_CODE
    if family.startswith(("g", "vt")):
        return G_AND_VT_SPOT_VCPU_QUOTA_CODE
    if instance_type == "c6i.large":
        return STANDARD_SPOT_VCPU_QUOTA_CODE
    return None


def _vcpu_count(metadata: dict[str, Any]) -> int:
    count = metadata.get("VCpuInfo", {}).get("DefaultVCpus")
    if type(count) is not int or count <= 0:
        raise ValueError("positive DefaultVCpus metadata is required")
    return count


def _usage_evidence(ec2: Any, quota_code: str, metadata: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Unique running instances are a lower bound, not available quota headroom.

    A fulfilled Spot request and its instance are the same consumption. Requests
    are never added to instance usage; unfulfilled and Fleet demand remain unknown.
    """
    result: dict[str, Any] = {
        "status": "unknown", "known_running_vcpus": None,
        "known_running_instance_ids": [], "pending_instance_ids": [],
        "unfulfilled_request_ids": [], "headroom_status": "unknown",
        "available_headroom_vcpus": None,
        "pending_demand_status": "unknown",
        "note": "Running consumption is a lower bound; pending/Fleet demand and headroom are not established.",
    }
    try:
        instances = {
            instance["InstanceId"]: instance
            for page in _pages(ec2, "describe_instances", Filters=[
                {"Name": "instance-lifecycle", "Values": ["spot"]},
                {"Name": "instance-state-name", "Values": ["pending", "running"]},
            ])
            for reservation in page.get("Reservations", [])
            for instance in reservation.get("Instances", [])
            if instance.get("InstanceId") and instance.get("InstanceLifecycle") == "spot"
            and spot_quota_code(instance.get("InstanceType", "")) == quota_code
        }
        running = {key: item for key, item in instances.items() if item.get("State", {}).get("Name") == "running"}
        missing_types = sorted({item["InstanceType"] for item in running.values()} - set(metadata))
        usage_metadata = dict(metadata)
        if missing_types:
            response = ec2.describe_instance_types(InstanceTypes=missing_types)
            usage_metadata.update({item["InstanceType"]: item for item in response.get("InstanceTypes", [])})
        result["known_running_vcpus"] = sum(_vcpu_count(usage_metadata[item["InstanceType"]]) for item in running.values())
        result["known_running_instance_ids"] = sorted(running)
        result["pending_instance_ids"] = sorted(key for key, item in instances.items() if item.get("State", {}).get("Name") == "pending")
        result["status"] = "known-running-only"
    except Exception as error:
        result["error_code"] = _error_code(error)
    try:
        requests = {
            item["SpotInstanceRequestId"]: item
            for page in _pages(ec2, "describe_spot_instance_requests", Filters=[{"Name": "state", "Values": ["open", "active"]}])
            for item in page.get("SpotInstanceRequests", [])
            if item.get("SpotInstanceRequestId")
            and spot_quota_code(item.get("LaunchSpecification", {}).get("InstanceType", "")) == quota_code
        }
        result["unfulfilled_request_ids"] = sorted(key for key, item in requests.items() if not item.get("InstanceId"))
        result["request_inventory_status"] = "observed"
    except Exception as error:
        result["request_inventory_status"] = "unknown"
        result["request_inventory_error_code"] = _error_code(error)
    return result


def collect_spot_quota(
    target: CapacityTarget, ec2: Any, quota_client: Any | None,
    metadata: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Report family quota totals, required vCPUs and incomplete usage separately."""
    names = tuple(item.name for item in target.instance_types)
    codes = {spot_quota_code(name) for name in names}
    if None in codes or len(codes) != 1:
        return {"status": "unknown", "error_code": "SPOT_QUOTA_FAMILY_UNSUPPORTED", "headroom_status": "unknown"}
    code = codes.pop()
    result: dict[str, Any] = {
        "status": "unknown", "quota_code": code, "required_vcpus": None,
        "total_quota_vcpus": None, "headroom_status": "unknown",
        "available_headroom_vcpus": None,
    }
    try:
        if quota_client is None:
            raise ValueError("Service Quotas client is required")
        quota = quota_client.get_service_quota(ServiceCode="ec2", QuotaCode=code)["Quota"]
        value = Decimal(str(quota["Value"]))
        if not value.is_finite() or value < 0 or quota.get("QuotaCode", code) != code:
            raise ValueError("invalid quota evidence")
        result.update({"total_quota_vcpus": float(value), "value_vcpus": float(value), "adjustable": bool(quota.get("Adjustable", False))})
        if metadata is None:
            response = ec2.describe_instance_types(InstanceTypes=list(names))
            metadata = {item["InstanceType"]: item for item in response.get("InstanceTypes", [])}
        # Each machine can use any approved type; use the largest vCPU requirement.
        result["vcpus_per_instance_type"] = {name: _vcpu_count(metadata[name]) for name in names}
        result["required_vcpus"] = target.desired_instance_count * max(result["vcpus_per_instance_type"].values())
        result["total_sufficient"] = value >= result["required_vcpus"]
        result["status"] = "ok"
    except Exception as error:
        result["error_code"] = _error_code(error)
    result["usage"] = _usage_evidence(ec2, code, metadata or {})
    result["known_running_vcpus"] = result["usage"]["known_running_vcpus"]
    return result


class _LaunchRead:
    """Capture the existing inspector's response without issuing a second read."""

    def __init__(self, ec2: Any):
        self.ec2 = ec2
        self.data: dict[str, Any] = {}
        self.version_number: int | None = None

    def describe_launch_template_versions(self, **kwargs: Any) -> dict[str, Any]:
        response = self.ec2.describe_launch_template_versions(**kwargs)
        versions = response.get("LaunchTemplateVersions", [])
        if len(versions) == 1:
            self.data = versions[0].get("LaunchTemplateData", {})
            self.version_number = versions[0].get("VersionNumber")
        return response


def _collect_single_type_preflight(target: CapacityTarget, ec2: Any, quota_client: Any, pricing: Any, *, cpu: bool = False) -> dict[str, Any]:
    """Fail closed on mandatory reads, leave usage/stock uncertainty explicit.

    ``ready`` means eligibility prerequisites passed, never that Spot is in stock
    or quota headroom is reserved. This helper creates no clients and makes no
    writes. Failed evidence does not authorize cancelling existing maintain demand.
    """
    prefix = "CPU" if cpu else "HYD"
    report: dict[str, Any] = {
        "ready": False, "checks": [], "error_codes": [], "quota": {},
        ("cpu_metadata" if cpu else "gpu_metadata"): {}, "on_demand_caps_usd": {}, "effective_spot_caps_usd": {},
        "spot_capacity_status": "unknown", "aws_write": False,
    }

    def check(name: str, status: str, evidence: Any, code: str | None = None) -> None:
        entry = {"name": name, "status": status, "evidence": evidence}
        if code:
            entry["error_code"] = code
            report["error_codes"].append(code)
        report["checks"].append(entry)

    try:
        target.validate()
        if (cpu and target.accelerator_profile != "cpu-validation") or (not cpu and target.acquisition_profile != "hyd-b300"):
            raise ConfigurationError("HYD preflight requires acquisition_profile hyd-b300")
    except ConfigurationError as error:
        check("configuration", "fail", {"message": str(error)}, f"{prefix}_CONFIGURATION_INVALID")
        return report
    region = target.active_region if cpu else "ap-south-2"
    name = "c6i.large" if cpu else "p6-b300.48xlarge"
    check("configuration", "pass", {"region": region, "instance_type": name})
    inputs = target.region_inputs(region)
    metadata: dict[str, dict[str, Any]] = {}
    try:
        if cpu:
            metadata = describe_cpu_instance_types(ec2, (name,))
        else:
            response = ec2.describe_instance_types(InstanceTypes=[name])
            metadata = {item["InstanceType"]: item for item in response.get("InstanceTypes", [])}
        item = metadata[name]
        gpus = item.get("GpuInfo", {}).get("Gpus", [])
        valid_gpu = bool(gpus) and all(
            str(gpu.get("Manufacturer", "")).upper() == "NVIDIA"
            and str(gpu.get("Name", "")).upper() == "B300"
            and type(gpu.get("Count")) is int and gpu["Count"] > 0 for gpu in gpus
        ) and sum(gpu["Count"] for gpu in gpus) == 8
        if cpu:
            report["cpu_metadata"] = {name: {"model": "CPU", "count_per_machine": 0, "vcpus_per_machine": 2, "memory_mib": 4096, "architectures": ["x86_64"]}}
            check("cpu_identity", "pass", report["cpu_metadata"])
        else:
            report["gpu_metadata"] = {name: {"manufacturer": "NVIDIA" if valid_gpu else "unverified", "model": "B300" if valid_gpu else "unverified", "count_per_machine": sum(gpu.get("Count", 0) for gpu in gpus)}}
            check("gpu_identity", "pass" if valid_gpu else "fail", {"gpus": gpus}, None if valid_gpu else f"{prefix}_GPU_IDENTITY_INVALID")
        usage_classes = item.get("SupportedUsageClasses")
        if usage_classes is None:
            check("spot_support", "unknown", {}, f"{prefix}_SPOT_SUPPORT_UNKNOWN")
        else:
            supported = "spot" in usage_classes
            check("spot_support", "pass" if supported else "fail", {"supported_usage_classes": usage_classes}, None if supported else f"{prefix}_SPOT_UNSUPPORTED")
        check("vcpu_metadata", "pass", {"vcpus_per_machine": _vcpu_count(item)})
    except Exception as error:
        check("metadata", "unknown", {"api_error_code": _error_code(error)}, f"{prefix}_METADATA_UNVERIFIED")

    try:
        discovery = discover_region(ec2, inputs.region, [name])
        zones = {item.zone_id: item for item in discovery.zones}
        approved = [item.zone_id for item in inputs.standard_placements]
        valid = all(zone_id in zones and zones[zone_id].zone_type == "availability-zone" and zones[zone_id].state == "available" and zones[zone_id].zone_name.startswith(region) for zone_id in approved)
        offering_valid = all(name in discovery.offered_instance_types_by_zone.get(zone_id, ()) for zone_id in approved)
        check("standard_zones", "pass" if valid else "fail", {"approved_zone_ids": approved, "zones": [asdict(item) for item in discovery.zones]}, None if valid else f"{prefix}_STANDARD_AZ_INVALID")
        check("offerings", "pass" if offering_valid else "fail", discovery.offered_instance_types_by_zone, None if offering_valid else f"{prefix}_OFFERING_UNAVAILABLE")
    except Exception as error:
        check("placements_discovery", "unknown", {"api_error_code": _error_code(error)}, f"{prefix}_PLACEMENTS_UNVERIFIED")

    try:
        subnets = {item["SubnetId"]: item for item in ec2.describe_subnets(SubnetIds=sorted(item.subnet_id for item in inputs.standard_placements)).get("Subnets", [])}
        evidence = []
        for placement in inputs.standard_placements:
            subnet = subnets.get(placement.subnet_id, {})
            evidence.append({"subnet_id": placement.subnet_id, "configured_zone_id": placement.zone_id, "zone_id": subnet.get("AvailabilityZoneId"), "state": subnet.get("State"), "vpc_id": subnet.get("VpcId"), "available_ip_count": subnet.get("AvailableIpAddressCount")})
        valid = all(item["zone_id"] == item["configured_zone_id"] and item["state"] == "available" and bool(item["vpc_id"]) and type(item["available_ip_count"]) is int and item["available_ip_count"] >= target.desired_instance_count for item in evidence)
        vpcs = {item["vpc_id"] for item in evidence}
        valid = valid and len(vpcs) == 1
        check("subnets", "pass" if valid else "fail", evidence, None if valid else f"{prefix}_SUBNET_INVALID")
        if not inputs.security_group_ids:
            check("security_group_vpc", "fail", {}, f"{prefix}_SECURITY_GROUP_VPC_INVALID")
        else:
            groups = ec2.describe_security_groups(GroupIds=list(inputs.security_group_ids)).get("SecurityGroups", [])
            group_valid = valid and {item.get("GroupId") for item in groups} == set(inputs.security_group_ids) and all(item.get("VpcId") in vpcs for item in groups)
            if cpu:
                group_valid = group_valid and all(item.get("IpPermissions") == [] for item in groups)
            check("security_group_vpc", "pass" if group_valid else "fail", {"groups": [{"group_id": item.get("GroupId"), "vpc_id": item.get("VpcId")} for item in groups]}, None if group_valid else f"{prefix}_SECURITY_GROUP_VPC_INVALID")
    except Exception as error:
        check("subnet_vpc", "unknown", {"api_error_code": _error_code(error)}, f"{prefix}_SUBNET_UNVERIFIED")

    try:
        reader = _LaunchRead(ec2)
        inspection = inspect_launch_contract(reader, target, inputs)
        violations = list(inspection.violations)
        data = reader.data
        if not all((inputs.ami_id, inputs.iam_instance_profile_arn, inputs.security_group_ids, inputs.bootstrap_contract_version, inputs.user_data_sha256, inputs.root_volume_encrypted)):
            violations.append("approved-launch-inputs-missing")
        if data.get("InstanceType", name) != name or data.get("InstanceRequirements"):
            violations.append("instance-type-conflict")
        if data.get("InstanceMarketOptions", {}).get("MarketType", "spot") != "spot":
            violations.append("purchase-option-conflict")
        if data.get("Placement", {}).get("AvailabilityZone") or data.get("SubnetId") or any(item.get("SubnetId") or item.get("AvailabilityZone") for item in data.get("NetworkInterfaces", [])):
            violations.append("fixed-placement-conflict")
        if cpu:
            if reader.version_number != int(inputs.launch_template_version):
                violations.append("pinned-version-mismatch")
            interfaces = data.get("NetworkInterfaces", [])
            if (len(interfaces) != 1 or interfaces[0].get("DeviceIndex") != 0
                    or interfaces[0].get("AssociatePublicIpAddress") is not False):
                violations.append("private-network-interface-required")
            if inputs.bootstrap_contract_version != "cpu-validation-v1":
                violations.append("cpu-bootstrap-contract-mismatch")
        valid = not violations
        check("launch_contract", "pass" if valid else "fail", {**asdict(inspection), "valid": valid, "violations": violations}, None if valid else f"{prefix}_LAUNCH_CONTRACT_INVALID")
    except Exception as error:
        check("launch_contract", "unknown", {"api_error_code": _error_code(error)}, f"{prefix}_LAUNCH_CONTRACT_UNVERIFIED")

    if cpu:
        try:
            images = ec2.describe_images(ImageIds=[inputs.ami_id]).get("Images", [])
            valid = (len(images) == 1 and images[0].get("ImageId") == inputs.ami_id
                     and images[0].get("State") == "available"
                     and images[0].get("Architecture") == "x86_64"
                     and images[0].get("RootDeviceType") == "ebs")
            check("ami", "pass" if valid else "fail", {"image_id": inputs.ami_id}, None if valid else "CPU_AMI_INVALID")
        except Exception as error:
            check("ami", "unknown", {"api_error_code": _error_code(error)}, "CPU_AMI_UNVERIFIED")

    try:
        caps = ondemand_caps(pricing, inputs.region, (name,))
        configured_cap = target.instance_types[0].spot_price_cap_usd
        effective = caps[name] if configured_cap is None else min(caps[name], configured_cap)
        report["on_demand_caps_usd"] = {name: str(caps[name])}
        report["effective_spot_caps_usd"] = {name: str(effective)}
        check("pricing", "pass", {"on_demand_ceiling_usd": str(caps[name]), "configured_spot_cap_usd": str(configured_cap) if configured_cap is not None else None, "effective_spot_cap_usd": str(effective)})
    except Exception as error:
        check("pricing", "unknown", {"api_error_code": _error_code(error)}, f"{prefix}_PRICING_UNVERIFIED")

    quota = collect_spot_quota(target, ec2, quota_client, metadata)
    report["quota"] = quota
    if quota["status"] != "ok":
        check("spot_quota", "unknown", quota, f"{prefix}_QUOTA_UNVERIFIED")
    else:
        sufficient = quota["total_sufficient"]
        check("spot_quota", "pass" if sufficient else "fail", quota, None if sufficient else f"{prefix}_QUOTA_INSUFFICIENT")
    report["ready"] = all(item["status"] == "pass" for item in report["checks"])
    return report


def collect_hyd_preflight(target: CapacityTarget, ec2: Any, quota_client: Any, pricing: Any) -> dict[str, Any]:
    return _collect_single_type_preflight(target, ec2, quota_client, pricing)


def collect_cpu_preflight(target: CapacityTarget, ec2: Any, quota_client: Any, pricing: Any) -> dict[str, Any]:
    return _collect_single_type_preflight(target, ec2, quota_client, pricing, cpu=True)
