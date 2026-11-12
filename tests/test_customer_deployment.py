"""Offline customer deployment regressions; every synthesis has explicit teardown."""
import contextlib
import io
import json
import os
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch

import aws_cdk as cdk
from aws_cdk import assertions
from botocore.httpsession import URLLib3Session

from h100_spot_controller import __version__
from h100_spot_controller.infrastructure import (
    CapacityControllerStack, LAMBDA_ASSET_EXCLUDES, deployment_name_prefix,
)

ROOT = Path(__file__).resolve().parents[1]
NAME_PROPERTIES = {
    "AWS::DynamoDB::Table": "TableName", "AWS::SNS::Topic": "TopicName",
    "AWS::IAM::Role": "RoleName", "AWS::IAM::Policy": "PolicyName",
    "AWS::Logs::LogGroup": "LogGroupName", "AWS::Lambda::Function": "FunctionName",
    "AWS::Events::Rule": "Name", "AWS::CloudWatch::Dashboard": "DashboardName",
    "AWS::CloudWatch::Alarm": "AlarmName",
}


def statements(template):
    for resource in template["Resources"].values():
        if resource["Type"] == "AWS::IAM::Policy":
            yield from resource["Properties"]["PolicyDocument"]["Statement"]


def has_action(statement, action):
    actions = statement.get("Action", [])
    return action in (actions if isinstance(actions, list) else [actions])


class CustomerDeploymentTests(unittest.TestCase):
    def cleanup_temporary_synth(self):
        path = Path(self.temp.name)
        self.temp.cleanup()
        self.assertFalse(path.exists(), "owned synthesis directory must be removed")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hyd-customer-synth-")
        self.addCleanup(self.cleanup_temporary_synth)
        environment_patch = patch.dict(os.environ, {
            "AWS_ACCESS_KEY_ID": "offline-test", "AWS_SECRET_ACCESS_KEY": "offline-test",
            "AWS_SESSION_TOKEN": "offline-test", "AWS_EC2_METADATA_DISABLED": "true",
            "AWS_DEFAULT_REGION": "ap-south-2",
        })
        environment_patch.start()
        self.addCleanup(environment_patch.stop)
        network_patch = patch.object(URLLib3Session, "send", side_effect=AssertionError("AWS SDK network forbidden in offline tests"))
        network_patch.start()
        self.addCleanup(network_patch.stop)
        self.app = cdk.App(outdir=str(Path(self.temp.name) / "assembly"))

    def synth(self, namespace=None, target=None, identifier="Customer"):
        self.app = cdk.App(outdir=str(Path(self.temp.name) / identifier))
        stack = CapacityControllerStack(
            self.app, identifier, deployment_namespace=namespace, target_id_default=target,
            env=cdk.Environment(account="111111111111", region="ap-south-2"),
        )
        return assertions.Template.from_stack(stack).to_json()

    def test_two_customer_names_never_collide(self):
        first = self.synth("customer-a", "customer-a-hyd-b300", "CustomerA")
        second = self.synth("customer-b", "customer-b-hyd-b300", "CustomerB")
        all_names = []
        for namespace, template in (("customer-a", first), ("customer-b", second)):
            names = [r["Properties"][NAME_PROPERTIES[r["Type"]]] for r in template["Resources"].values() if r["Type"] in NAME_PROPERTIES]
            self.assertTrue(all(n.startswith(f"Phoenix-Codex-Local-Spot-{namespace}-") for n in names))
            all_names.append(set(names))
        self.assertFalse(all_names[0] & all_names[1])
        self.app.synth()

    def test_legacy_names_and_mutation_scope_stay_identical(self):
        template = self.synth()
        expected = {
            "State", "Notifications", "Controller-Role", "Controller-Policy", "Collector-Role", "Collector-Policy",
            "Reconciler", "Collector", "Spot-Event-Handler", "Reconciler-Logs", "Collector-Logs", "Spot-Event-Handler-Logs",
            "Reconcile-Schedule", "Signal-Schedule", "SPS-Schedule", "Spot-Lifecycle-Events",
            "Capacity-Dashboard", "No-Eligible-Region", "Stale-Region-Signal",
        }
        actual = {r["Properties"][NAME_PROPERTIES[r["Type"]]] for r in template["Resources"].values() if r["Type"] in NAME_PROPERTIES}
        self.assertEqual({f"Phoenix-Codex-Local-Spot-{suffix}" for suffix in expected}, actual)
        for statement in statements(template):
            self.assertNotIn("customer-id", str(statement))
        for resource in template["Resources"].values():
            if resource["Type"] == "AWS::Lambda::Function":
                self.assertNotIn("CUSTOMER_ID", resource["Properties"]["Environment"]["Variables"])

    def test_namespace_validation_and_required_customer_target(self):
        for namespace in ("", "UPPER", "customer_a", "-start", "end-", "a" * 13, "a/b", "a b", 123):
            with self.subTest(namespace=namespace), self.assertRaises(ValueError):
                deployment_name_prefix(namespace)
        self.assertEqual("Phoenix-Codex-Local-Spot-a", deployment_name_prefix("a"))
        self.assertTrue(deployment_name_prefix("a" * 12).endswith("a" * 12))
        template = self.synth("customer-a")
        self.assertNotIn("Default", template["Parameters"]["TargetId"])
        self.assertEqual(1, template["Parameters"]["TargetId"]["MinLength"])

    def test_customer_env_and_serial_writer(self):
        template = self.synth("customer-a", "customer-a-hyd-b300")
        functions = [r["Properties"] for r in template["Resources"].values() if r["Type"] == "AWS::Lambda::Function"]
        for function in functions:
            env = function["Environment"]["Variables"]
            self.assertEqual("customer-a", env["CUSTOMER_ID"])
            self.assertEqual({"Ref": "TargetId"}, env["TARGET_ID"])
        reconciler = next(f for f in functions if f["FunctionName"].endswith("-Reconciler"))
        self.assertEqual(1, reconciler["ReservedConcurrentExecutions"])

    def test_capacity_iam_binds_customer_target_and_reviewed_launch_inputs(self):
        template = self.synth("customer-a", "customer-a-hyd-b300")
        policy_statements = list(statements(template))
        import re
        role_pattern = template["Parameters"]["LaunchInstanceRoleArns"]["AllowedPattern"]
        template_pattern = template["Parameters"]["LaunchTemplateArns"]["AllowedPattern"]
        self.assertIsNotNone(re.fullmatch(role_pattern, "arn:aws:iam::111111111111:role/reviewed-instance"))
        self.assertIsNotNone(re.fullmatch(template_pattern, "arn:aws:ec2:ap-south-2:111111111111:launch-template/lt-0123456789abcdef0"))
        for pattern in (role_pattern, template_pattern):
            self.assertIsNone(re.fullmatch(pattern, "*"))
            self.assertIsNone(re.fullmatch(pattern, "arn:aws:iam::111111111111:role/*"))
        for action in ("ec2:CreateFleet", "ec2:CreateTags"):
            found = [s for s in policy_statements if has_action(s, action)]
            self.assertTrue(found)
            for statement in found:
                conditions = statement["Condition"]["StringEquals"]
                self.assertEqual("customer-a", conditions["aws:RequestTag/customer-id"])
                self.assertEqual({"Ref": "TargetId"}, conditions["aws:RequestTag/capacity-target-id"])
        for action in ("ec2:ModifyFleet", "ec2:DeleteFleets", "ec2:TerminateInstances"):
            for statement in [s for s in policy_statements if has_action(s, action)]:
                conditions = statement["Condition"]["StringEquals"]
                self.assertEqual("customer-a", conditions["ec2:ResourceTag/customer-id"])
                self.assertEqual({"Ref": "TargetId"}, conditions["ec2:ResourceTag/capacity-target-id"])
        launch = [s for s in policy_statements if has_action(s, "ec2:RunInstances")]
        self.assertEqual(2, len(launch))
        self.assertTrue(all("LaunchTemplateArns" in str(s["Condition"]["ArnEquals"]["ec2:LaunchTemplate"]) for s in launch))
        new_resources = next(s for s in launch if "StringEquals" in s["Condition"])
        self.assertIn("aws:RequestTag/customer-id", new_resources["Condition"]["StringEquals"])
        self.assertNotEqual("*", new_resources["Resource"])
        passed = next(s for s in policy_statements if has_action(s, "iam:PassRole"))
        self.assertIn("LaunchInstanceRoleArns", str(passed["Resource"]))
        self.assertEqual("ec2.amazonaws.com", passed["Condition"]["StringEquals"]["iam:PassedToService"])

    def test_controller_preflight_reads_exist(self):
        template = self.synth("customer-a", "customer-a-hyd-b300")
        role = next(key for key, r in template["Resources"].items() if r["Type"] == "AWS::IAM::Role" and r["Properties"]["RoleName"].endswith("-Controller-Role"))
        policies = [r for r in template["Resources"].values() if r["Type"] == "AWS::IAM::Policy" and {"Ref": role} in r["Properties"]["Roles"]]
        for action in ("servicequotas:GetServiceQuota", "ec2:DescribeSpotInstanceRequests", "ec2:DescribeSecurityGroups"):
            self.assertIn(action, str(policies))
        self.assertNotIn("ec2:DescribeFleetHistory", str(policies))  # Runtime uses DescribeFleets.Errors, not history reads.

    def test_asset_manifest_and_staged_files_exclude_private_tools(self):
        fixture = Path(self.temp.name) / "asset-input"
        fixture.mkdir()
        (fixture / "runtime_handler.py").write_text("# offline fixture\n")
        for directory in (".agents", "openspec", "scripts", "privateinputs", "testoutputs", "config", "cdk.out-test"):
            (fixture / directory).mkdir()
            (fixture / directory / "marker-private.txt").write_text("not-for-lambda\n")
        self.assertTrue({".agents", "openspec", "scripts", "privateinputs", "testoutputs"}.issubset(LAMBDA_ASSET_EXCLUDES))
        original = cdk.aws_lambda.Code.from_asset
        with patch("h100_spot_controller.infrastructure.lambda_.Code.from_asset", side_effect=lambda path, **kwargs: original(str(fixture), **kwargs)):
            self.synth("customer-a", "customer-a-hyd-b300")
        assembly = self.app.synth()
        manifests = list(Path(assembly.directory).glob("*.assets.json"))
        self.assertTrue(manifests)
        zip_assets = 0
        for manifest in manifests:
            payload = json.loads(manifest.read_text())
            for asset in payload.get("files", {}).values():
                if asset["source"].get("packaging") != "zip":
                    continue  # CloudFormation template file is also an asset.
                zip_assets += 1
                staged = Path(assembly.directory) / asset["source"]["path"]
                self.assertTrue((staged / "runtime_handler.py").is_file())
                self.assertFalse(list(staged.rglob("marker-private.txt")))
        self.assertGreater(zip_assets, 0)

    def test_app_context_routes_customer_region_and_target(self):
        outdir = Path(self.temp.name) / "app-output"
        context = {"customer_id": "customer-a", "target_id": "customer-a-hyd-b300", "region": "ap-south-2"}
        app = cdk.App(context=context, outdir=str(outdir))
        with patch("aws_cdk.App", return_value=app):
            runpy.run_path(str(ROOT / "app.py"), run_name="__main__")
        template = json.loads((outdir / "Phoenix-Codex-Local-Spot-customer-a-Controller.template.json").read_text())
        self.assertEqual("customer-a-hyd-b300", template["Parameters"]["TargetId"]["Default"])
        manifest = json.loads((outdir / "manifest.json").read_text())
        artifact = manifest["artifacts"]["Phoenix-Codex-Local-Spot-customer-a-Controller"]
        self.assertTrue(artifact["environment"].endswith("/ap-south-2"))

    def test_documented_operator_options_are_real_parser_options(self):
        from h100_spot_controller.cli import main
        for command, required in {
            "validate-target": ["path"], "dry-run": ["--profile"], "capacity-review": ["--table", "--profile"],
            "target-review": ["--table", "--profile"], "target-put": ["--execute", "--enable-capacity", "--expected-version"],
            "cleanup": ["--execute", "--terminate-instances", "--table"],
        }.items():
            output = io.StringIO()
            with self.subTest(command=command), patch("sys.argv", ["h100-spot-controller", command, "--help"]), contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as exit_code:
                main()
            self.assertEqual(0, exit_code.exception.code)
            for option in required:
                self.assertIn(option, output.getvalue())

    def test_hyd_example_is_disabled_and_customer_scoped(self):
        from h100_spot_controller.config import load_target
        target = load_target(ROOT / "config" / "hyd-b300.example.yaml")
        self.assertFalse(target.enabled)
        self.assertEqual((1, 1), (target.desired_instance_count, target.maximum_instance_count))
        self.assertEqual("hyd-b300", target.acquisition_profile)
        self.assertEqual("all-approved", target.placement_strategy)
        self.assertEqual("customer-a", target.ownership_tags["customer-id"])
        self.assertEqual(["ap-south-2"], [region.region for region in target.candidate_regions])
        self.assertEqual(["p6-b300.48xlarge"], [kind.name for kind in target.instance_types])

    def test_release_guard_blocks_sdk_requests_without_blocking_jsii(self):
        import boto3
        release = runpy.run_path(str(ROOT / "scripts" / "release-check.py"))
        client = boto3.client("ec2", region_name="ap-south-2", endpoint_url="http://127.0.0.1:9")
        with patch.object(URLLib3Session, "send", release["blocked_aws_request"]):
            with self.assertRaisesRegex(AssertionError, "ALL botocore HTTP"):
                client.describe_instances()
        self.synth("customer-a", "customer-a-hyd-b300")

    def test_release_metadata_consistency(self):
        import tomllib
        metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.assertEqual("0.2.0", __version__)
        self.assertEqual(__version__, metadata["project"]["version"])
        self.assertEqual("h100_spot_controller.cli:main", metadata["project"]["scripts"]["h100-spot-controller"])


if __name__ == "__main__":
    unittest.main()
