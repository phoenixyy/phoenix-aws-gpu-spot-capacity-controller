"""Offline-only disposable CPU template, package manifest and durable run receipts.

No SDK clients, discovery, credential reads or cloud actions live in this module.
Runtime IAM must be supplied from an operator-reviewed Autopilot baseline; this
module deliberately cannot invent a policy when that tool is unavailable.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import tempfile
import zipfile

# Offline fixture only, deliberately not a live deployment account.
# ApprovedScope prevents this example from creating resources in real accounts.
ACCOUNT = "000000000000"
REGION = "ap-northeast-2"
PREFIX = "Phoenix-Codex-Local-Spot-"
BOOTSTRAP_VERSION = "cpu-validation-v1"
USER_DATA = "#!/bin/sh\nexit 0\n"
USER_DATA_SHA256 = hashlib.sha256(USER_DATA.encode()).hexdigest()
MANAGED_BY = "h100-spot-capacity-controller"
IDENTIFIER = re.compile(r"[a-z][a-z0-9-]{0,27}\Z")


def _identity(run_id: str, target_id: str) -> None:
    if not IDENTIFIER.fullmatch(run_id) or not IDENTIFIER.fullmatch(target_id):
        raise ValueError("run_id and target_id require lowercase safe identifiers, max 28 characters")


def ownership_tags(run_id: str, target_id: str) -> dict[str, str]:
    _identity(run_id, target_id)
    return {"managed-by": MANAGED_BY, "purpose": "cpu-spot-validation",
            "validation-run-id": run_id, "capacity-target-id": target_id,
            "account-id": ACCOUNT, "region": REGION}


def _ref(name):
    return {"Ref": name}


def _att(name, attribute="Arn"):
    return {"Fn::GetAtt": [name, attribute]}


def build_cpu_template(*, runtime_policy: dict, run_id: str, target_id: str) -> dict:
    """Return a parameterized template; never synthesize production infrastructure.

    The policy is a mandatory, fully scoped identity document supplied by root.
    Building is not IAM approval or deployment authorization. An explicitly
    supported CloudFormation Condition suppresses every resource/output outside
    the approved account/Region; root must separately verify deployment identity.
    """
    _identity(run_id, target_id)
    if (not isinstance(runtime_policy, dict) or runtime_policy.get("Version") != "2012-10-17"
            or not isinstance(runtime_policy.get("Statement"), list) or not runtime_policy["Statement"]):
        raise ValueError("supply the complete Autopilot-derived, root-reviewed runtime policy; no default")
    tags = [{"Key": k, "Value": v} for k, v in ownership_tags(run_id, target_id).items()]
    name = PREFIX + run_id
    target_tags = tags + [{"Key": "Name", "Value": PREFIX + target_id},
                          {"Key": "bootstrap-contract-version", "Value": BOOTSTRAP_VERSION}]
    resources = {}

    def resource(logical, kind, properties, why, must, **extra):
        resources[logical] = {"Type": "AWS::" + kind, "Condition": "ApprovedScope", "DeletionPolicy": "Delete",
                              "UpdateReplacePolicy": "Delete", "Properties": properties,
                              "Metadata": {"com.aws.cloudformation.Context": {"why": why, "must": must}}, **extra}

    def trust(service):
        # Standard execution-role service trust. Do not add SourceArn to services
        # which do not populate it on execution-role AssumeRole calls.
        return {"Version": "2012-10-17", "Statement": [{"Effect": "Allow",
                "Principal": {"Service": service}, "Action": "sts:AssumeRole"}]}

    resource("StateTable", "DynamoDB::Table", {
        "TableName": name + "-state", "BillingMode": "PAY_PER_REQUEST",
        "AttributeDefinitions": [{"AttributeName": key, "AttributeType": "S"} for key in ("pk", "sk")],
        "KeySchema": [{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
        "SSESpecification": {"SSEEnabled": True}, "Tags": tags},
        "test-only runtime state and writer claims; deliberately disposable",
        ["never substitute a production table", "export recovery evidence before deletion"])
    resource("RuntimeLogGroup", "Logs::LogGroup", {
        "LogGroupName": "/aws/lambda/" + name + "-reconcile", "RetentionInDays": 1,
        "Tags": tags}, "short-lived real handler evidence", ["test logs only; preserve concise receipt first"])
    resource("InstanceRole", "IAM::Role", {
        "RoleName": name + "-instance", "AssumeRolePolicyDocument": trust("ec2.amazonaws.com"),
        "Tags": tags}, "permissionless instance identity; no SSM or bootstrap service access",
        ["no managed or inline permissions", "PassRole only this role"])
    resource("InstanceProfile", "IAM::InstanceProfile", {
        "InstanceProfileName": name + "-profile", "Roles": [_ref("InstanceRole")]},
        "dedicated profile for the pinned launch contract", ["one run-owned role only"])
    resource("SecurityGroup", "EC2::SecurityGroup", {
        "GroupName": name + "-sg", "GroupDescription": "Disposable CPU validation; no ingress or internet egress",
        "VpcId": _ref("DefaultVpcId"),
        # No ingress property/rule: a NEW dedicated SG defaults to deny inbound.
        # Omit empty [] to satisfy the unchanged official description guard;
        # actual CPU preflight must still verify IpPermissions == [].
        # An empty egress list permits EC2's default allow-all. A loopback-only
        # rule suppresses that default without adding a networking dependency.
        "SecurityGroupEgress": [{"IpProtocol": "-1", "CidrIp": "127.0.0.1/32",
                                 "Description": "Suppress default outbound; no external destination"}],
        "Tags": tags}, "isolated fixture in reviewed shared default VPC; no route changes",
        ["no inbound rules", "never reuse a production SG", "no internet egress"])
    resource("LaunchTemplate", "EC2::LaunchTemplate", {
        "LaunchTemplateName": name + "-lt",
        "TagSpecifications": [{"ResourceType": "launch-template", "Tags": tags}],
        "LaunchTemplateData": {
            "ImageId": _ref("ApprovedAmiId"), "InstanceType": "c6i.large",
            "IamInstanceProfile": {"Arn": _att("InstanceProfile")},
            "NetworkInterfaces": [{"DeviceIndex": 0, "AssociatePublicIpAddress": False,
                                   "DeleteOnTermination": True, "Groups": [_ref("SecurityGroup")]}],
            "MetadataOptions": {"HttpEndpoint": "enabled", "HttpTokens": "required", "HttpPutResponseHopLimit": 1},
            "BlockDeviceMappings": [{"DeviceName": _ref("RootDeviceName"), "Ebs": {
                "Encrypted": True, "DeleteOnTermination": True, "VolumeSize": _ref("RootVolumeGiB"),
                "VolumeType": "gp3"}}],
            "UserData": {"Fn::Base64": USER_DATA},
            "TagSpecifications": [{"ResourceType": kind, "Tags": target_tags} for kind in ("instance", "volume")]}},
        "fixed small CPU; no downloads, SSM, GPU bootstrap or public IPv4",
        ["pin numeric version 1; never latest/default", "approved AMI root size/device only",
         "subnet selection belongs to Fleet overrides", "IMDSv2 and delete-on-termination encrypted root"])
    resource("RuntimeRole", "IAM::Role", {
        "RoleName": name + "-runtime", "AssumeRolePolicyDocument": trust("lambda.amazonaws.com"),
        "Tags": tags, "Policies": [{"PolicyName": "ReviewedCpuRuntime", "PolicyDocument": copy.deepcopy(runtime_policy)}]},
        "root-reviewed Autopilot-derived permissions; no generated permissive fallback",
        ["exact LT, profile role, AMI, subnets, SG and table scope", "mutations require run ownership",
         "review policy and baseline before deployment"])
    resource("Reconciler", "Lambda::Function", {
        "FunctionName": name + "-reconcile", "Runtime": "python3.12", "Handler": "runtime_handler.reconcile",
        "Architectures": ["x86_64"], "MemorySize": 256, "Timeout": 60,
        "ReservedConcurrentExecutions": 1, "Role": _att("RuntimeRole"),
        "Code": {"S3Bucket": _ref("StagingBucket"), "S3Key": _ref("LambdaZipKey")},
        "Environment": {"Variables": {"STATE_TABLE_NAME": _ref("StateTable"), "TARGET_ID": target_id,
                                       "VALIDATION_RUN_ID": run_id}},
        "Tags": tags}, "real reconciler, not a raw Fleet demo; one reserved writer",
        ["persist disabled target before enablement", "no collectors, dashboard or SNS"],
        DependsOn=["RuntimeLogGroup"])
    resource("ReconcileRule", "Events::Rule", {
        "Name": name + "-tick", "State": "DISABLED", "ScheduleExpression": "rate(1 minute)",
        "Targets": [{"Id": "Reconcile", "Arn": _att("Reconciler"),
                     "Input": json.dumps({"target_id": target_id}, separators=(",", ":")),
                     "RetryPolicy": {"MaximumEventAgeInSeconds": 60, "MaximumRetryAttempts": 0}}]},
        "operator-controlled tick; disabled on creation", ["never enable before fresh approved target window"])
    resource("RuleInvokePermission", "Lambda::Permission", {
        "Action": "lambda:InvokeFunction", "FunctionName": _ref("Reconciler"),
        "Principal": "events.amazonaws.com", "SourceArn": _att("ReconcileRule"),
        "SourceAccount": _ref("AWS::AccountId")}, "only this schedule may invoke via EventBridge",
        ["retain exact SourceArn and SourceAccount"])
    parameters = {
        "ApprovedAmiId": {"Type": "AWS::EC2::Image::Id", "Description": "Root-reviewed x86_64 AL2023 image, not a dynamic latest reference"},
        "DefaultVpcId": {"Type": "AWS::EC2::VPC::Id", "Description": "Reviewed default VPC only; never modify routes/subnets"},
        "ApprovedSubnetIds": {"Type": "List<AWS::EC2::Subnet::Id>", "Description": "Reviewed standard-AZ default-VPC placements; read-only"},
        "StagingBucket": {"Type": "String", "AllowedPattern": "[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]"},
        "LambdaZipKey": {"Type": "String", "AllowedPattern": re.escape(name) + r"/[A-Za-z0-9._/-]+\.zip"},
        "RootDeviceName": {"Type": "String", "Default": "/dev/xvda", "AllowedValues": ["/dev/xvda", "/dev/sda1"]},
        "RootVolumeGiB": {"Type": "Number", "Default": 8, "AllowedValues": [8], "Description": "Stop if approved AMI minimum exceeds 8 GiB"}}
    outputs = {
        "AccountId": _ref("AWS::AccountId"), "Region": _ref("AWS::Region"), "RunId": run_id, "TargetId": target_id,
        "StateTableName": _ref("StateTable"), "FunctionName": _ref("Reconciler"), "RuntimeRoleArn": _att("RuntimeRole"),
        "RuleName": _ref("ReconcileRule"), "LogGroupName": _ref("RuntimeLogGroup"),
        "LaunchTemplateId": _ref("LaunchTemplate"), "LaunchTemplateVersion": "1",
        "LaunchTemplateLatestVersion": _att("LaunchTemplate", "LatestVersionNumber"),
        "SecurityGroupId": _ref("SecurityGroup"), "InstanceProfileArn": _att("InstanceProfile"),
        "InstanceRoleArn": _att("InstanceRole"), "AmiId": _ref("ApprovedAmiId"), "VpcId": _ref("DefaultVpcId"),
        "SubnetIds": {"Fn::Join": [",", _ref("ApprovedSubnetIds")]},
        "BootstrapContractVersion": BOOTSTRAP_VERSION, "UserDataSha256": USER_DATA_SHA256,
        "InstanceType": "c6i.large", "RootVolumeGiB": _ref("RootVolumeGiB")}
    return {"AWSTemplateFormatVersion": "2010-09-09",
            "Description": "Disposable bounded CPU Spot control-path validation using the real runtime; no production resources or network bootstrap.",
            "Metadata": {"AWSToolsMetrics": {"AWSAgentToolkit": "aws-cloudformation@3"},
                         "com.aws.cloudformation.Context": {"must": ["root reviews IAM before deployment", "test-only deletion policies", "one machine; <=30 minutes; <=USD0.10/hour; USD1 review budget is not billing cap"],
                                                            "ref": [{"at": "docs/cpu-test-runbook.md", "has": "IAM baseline recipe, run and cleanup gates"}]}},
            "Parameters": parameters,
            "Conditions": {"ApprovedScope": {"Fn::And": [
                {"Fn::Equals": [_ref("AWS::AccountId"), ACCOUNT]},
                {"Fn::Equals": [_ref("AWS::Region"), REGION]}]}},
            "Resources": resources, "Outputs": {k: {"Condition": "ApprovedScope", "Value": v,
                                                     "Description": "CPU validation " + k} for k, v in outputs.items()}}


def runtime_source_files(root: Path) -> tuple[Path, ...]:
    """Resolve only the local transitive imports reachable from runtime_handler."""
    root = root.resolve(strict=True)
    package = root / "h100_spot_controller"
    pending = [root / "runtime_handler.py", package / "__init__.py", package / "handlers.py"]
    found = set()
    while pending:
        path = pending.pop()
        if path in found:
            continue
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
            raise ValueError("runtime source must be a regular file inside the project")
        found.add(path)
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                names = []
                if node.level:
                    if node.level != 1:
                        raise ValueError("unexpected relative import depth")
                    names = [node.module.split(".")[0]] if node.module else [alias.name for alias in node.names]
                elif node.module == "h100_spot_controller":
                    names = [alias.name for alias in node.names if (package / (alias.name + ".py")).is_file()]
                elif node.module and node.module.startswith("h100_spot_controller."):
                    names = [node.module.split(".")[1]]
                pending.extend(package / (name + ".py") for name in names)
    forbidden = {"cli.py", "infrastructure.py", "cpu_test_deployment.py"}
    if any(p.name in forbidden for p in found):
        raise ValueError("runtime import graph unexpectedly includes deployment/CLI tooling")
    return tuple(sorted(found))


def build_cpu_package(root: Path, destination: Path) -> dict:
    """Write a new deterministic zip, using existing pure PyYAML only; no install.

    Lambda supplies boto3/botocore. Do not zip whole trees, dependencies, private
    inputs, configuration files, credentials or local compiled extensions.
    Existing destinations are never replaced. Failed builds remove only their
    exclusive task-owned output and TemporaryDirectory always cleans staging.
    """
    root = root.resolve(strict=True)
    destination = destination.parent.resolve(strict=True) / destination.name
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("refuse to replace an existing package")
    sources = {str(p.relative_to(root)): p for p in runtime_source_files(root)}
    yaml_spec = importlib.util.find_spec("yaml")
    if not yaml_spec or not yaml_spec.origin:
        raise ValueError("existing PyYAML is required; package builder never installs dependencies")
    yaml_root = Path(yaml_spec.origin).parent.resolve(strict=True)
    yaml_sources = sorted(yaml_root.glob("*.py"))
    if not yaml_sources or any(p.is_symlink() for p in yaml_sources):
        raise ValueError("PyYAML pure source files unavailable or symlinked")
    sources.update({"yaml/" + p.name: p for p in yaml_sources})
    members = []
    # Read all inputs before exclusive destination creation.
    contents = {name: path.read_bytes() for name, path in sources.items()}
    for name, data in sorted(contents.items()):
        members.append({"path": name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})
    manifest = {"schema_version": 1, "handler": "runtime_handler.reconcile", "runtime": "python3.12",
                "dependencies": ["PyYAML (existing pure Python source)"], "sdk": "Lambda-provided boto3/botocore",
                "files": members}
    contents["cpu-package-manifest.json"] = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    created = False
    try:
        with tempfile.TemporaryDirectory(prefix="phoenix-cpu-package-") as stage:
            stage_path = Path(stage).resolve(strict=True)
            archive = stage_path / "lambda.zip"
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as package:
                for name, data in sorted(contents.items()):
                    info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = 0o644 << 16
                    package.writestr(info, data)
            with destination.open("xb") as output:
                created = True
                output.write(archive.read_bytes())
                output.flush()
                os.fsync(output.fileno())
        return {**manifest, "zip_sha256": hashlib.sha256(destination.read_bytes()).hexdigest()}
    except BaseException:
        if created:
            assert destination.resolve() == destination and destination.is_file() and not destination.is_symlink()
            destination.unlink()
        raise


class ReceiptError(ValueError):
    """Unsafe replay, ownership conflict or invalid durable receipt."""


class RunReceipt:
    """Atomic local evidence journal, not a cloud executor or ownership oracle.

    Persist begin_mutation BEFORE the cloud call. An interrupted call remains
    uncertain. Only authoritative root observations may resolve it; never replay
    creation or delete fixtures merely because their names share a prefix.
    """
    KINDS = {"stack", "function", "table", "log-group", "rule", "role", "instance-profile",
             "launch-template", "security-group", "staging-bucket", "staging-object", "fleet", "instance", "volume"}

    def __init__(self, path: Path, run_id: str, target_id: str):
        _identity(run_id, target_id)
        self.path = path.parent.resolve(strict=True) / path.name
        if self.path.is_symlink():
            raise ReceiptError("receipt symlinks are forbidden")
        self.tags = ownership_tags(run_id, target_id)
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
            if (self.data.get("schema_version") != 1 or self.data.get("ownership") != self.tags
                    or not isinstance(self.data.get("operations"), dict) or not isinstance(self.data.get("resources"), dict)):
                raise ReceiptError("receipt identity/schema mismatch")
        else:
            self.data = {"schema_version": 1, "ownership": self.tags, "operations": {}, "resources": {}}
            with self.path.open("x", encoding="utf-8") as handle:
                os.chmod(self.path, 0o600)
                json.dump(self.data, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            self._sync_directory()

    def _sync_directory(self):
        fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _save(self, data):
        if self.path.is_symlink() or not self.path.is_file():
            raise ReceiptError("receipt path changed")
        # Single local writer only; do not silently replace a newer journal.
        if json.loads(self.path.read_text(encoding="utf-8")) != self.data:
            raise ReceiptError("receipt changed concurrently; reopen and reconcile")
        fd, temp = tempfile.mkstemp(prefix=".cpu-receipt-", dir=self.path.parent)
        temp_path = Path(temp).absolute()
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            # Both move endpoints are exact task-owned absolute paths; reject
            # symlinked parents/targets before the atomic receipt replacement.
            if (temp_path.resolve(strict=True) != temp_path or self.path.resolve(strict=True) != self.path
                    or temp_path.parent != self.path.parent or not temp_path.name.startswith(".cpu-receipt-")):
                raise ReceiptError("receipt move endpoint escaped task-owned absolute paths")
            os.replace(temp_path, self.path)
            self.data = data
            self._sync_directory()
        finally:
            if temp_path.exists():
                assert temp_path.parent == self.path.parent and temp_path.name.startswith(".cpu-receipt-")
                temp_path.unlink()

    def begin_mutation(self, operation_id: str, action: str, planned_ids: list[str]) -> None:
        if not operation_id or not action or not planned_ids or not all(isinstance(i, str) and i for i in planned_ids):
            raise ReceiptError("record operation, action and explicit names/IDs before mutation")
        if operation_id in self.data["operations"]:
            raise ReceiptError("operation already recorded; recover, never blindly replay")
        if action.lower().startswith(("delete", "terminate", "modify", "update")):
            owned = set(self.cleanup_candidates())
            if not set(planned_ids).issubset(owned):
                raise ReceiptError("destructive intent contains foreign/unconfirmed resource IDs")
        if any(i["status"] == "uncertain" for i in self.data["operations"].values()):
            raise ReceiptError("unresolved write blocks further mutations")
        data = copy.deepcopy(self.data)
        data["operations"][operation_id] = {"action": action, "planned_ids": planned_ids[:], "status": "uncertain"}
        self._save(data)

    def resolve_mutation(self, operation_id: str, *, status: str, observed_ids: list[str], evidence: str) -> None:
        if status not in {"confirmed", "not-applied"} or not evidence:
            raise ReceiptError("authoritative result/absence evidence is required")
        prior = self.data["operations"].get(operation_id)
        if prior is None or prior["status"] != "uncertain":
            raise ReceiptError("only an uncertain recorded operation may be resolved")
        if (status == "confirmed" and not observed_ids) or (status == "not-applied" and observed_ids):
            raise ReceiptError("result IDs contradict status")
        if not all(isinstance(i, str) and i for i in observed_ids):
            raise ReceiptError("invalid observed IDs")
        data = copy.deepcopy(self.data)
        data["operations"][operation_id].update(status=status, observed_ids=observed_ids[:], evidence=evidence)
        self._save(data)

    def record_resource(self, resource_id: str, kind: str, *, observed_tags: dict[str, str],
                        creation_operation: str, evidence: str) -> None:
        if not resource_id or kind not in self.KINDS or not evidence:
            raise ReceiptError("explicit resource ID, supported kind and evidence required")
        if any(observed_tags.get(k) != v for k, v in self.tags.items()):
            raise ReceiptError("foreign or uncertain ownership; no destructive recovery")
        if resource_id.startswith("arn:"):
            fields = resource_id.split(":", 5)
            if len(fields) != 6 or fields[4] not in {"", ACCOUNT} or fields[3] not in {"", REGION}:
                raise ReceiptError("resource ARN escapes approved account/Region")
        operation = self.data["operations"].get(creation_operation)
        if not operation or operation["status"] != "confirmed" or resource_id not in operation["observed_ids"]:
            raise ReceiptError("ownership tags alone are insufficient; require confirmed run creation")
        item = {"kind": kind, "creation_operation": creation_operation, "evidence": evidence,
                "tags": self.tags.copy(), "status": "present"}
        if resource_id in self.data["resources"] and self.data["resources"][resource_id] != item:
            raise ReceiptError("resource evidence conflict")
        data = copy.deepcopy(self.data)
        data["resources"][resource_id] = item
        self._save(data)

    def cleanup_candidates(self) -> tuple[str, ...]:
        if any(o["status"] == "uncertain" for o in self.data["operations"].values()):
            raise ReceiptError("uncertain cloud write blocks destructive cleanup")
        candidates = []
        for resource_id, item in self.data["resources"].items():
            operation = self.data["operations"].get(item.get("creation_operation"), {})
            if (item.get("tags") != self.tags or item.get("kind") not in self.KINDS
                    or operation.get("status") != "confirmed" or resource_id not in operation.get("observed_ids", [])):
                raise ReceiptError("receipt resource lacks confirmed run ownership")
            if item.get("status") == "present":
                candidates.append(resource_id)
        return tuple(sorted(candidates))

    def mark_deleted(self, resource_id: str, evidence: str) -> None:
        if resource_id not in self.cleanup_candidates() or not evidence:
            raise ReceiptError("confirmed absence/terminal evidence for an owned resource required")
        data = copy.deepcopy(self.data)
        data["resources"][resource_id].update(status="deleted", deletion_evidence=evidence)
        self._save(data)
