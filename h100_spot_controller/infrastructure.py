"""CDK foundation; synthesis is local and deployment remains operator-authorized."""

import re

from aws_cdk import CfnOutput, CfnParameter, Duration, RemovalPolicy, Stack, Tags
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_sns as sns
from constructs import Construct

from .config import RESOURCE_NAME_PREFIX


LAMBDA_ASSET_EXCLUDES = [
    ".venv",
    ".cpu-validation-work",
    ".git",
    ".codex",
    ".agents",
    "cdk.out*",
    "__pycache__",
    "*.pyc",
    ".gitignore",
    "AGENTS.md",
    "app.py",
    "deployment",
    "pyproject.toml",
    "requirements.lock",
    "tests",
    "openspec",
    "docs",
    "config",
    "scripts",
    "privateinputs",
    "testoutputs",
    ".aws",
    ".env*",
    "*.egg-info",
]


def deployment_name_prefix(namespace: str | None) -> str:
    """Reject unsafe names rather than silently normalizing customer identity."""
    if namespace is None:
        return RESOURCE_NAME_PREFIX
    if not isinstance(namespace, str) or re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,10}[a-z0-9])?", namespace) is None:
        raise ValueError("deployment namespace must be 1-12 lowercase letters/digits/hyphens, with alphanumeric ends")
    return f"{RESOURCE_NAME_PREFIX}-{namespace}"


class CapacityControllerStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, deployment_namespace: str | None = None,
                 target_id_default: str | None = None, **kwargs: object) -> None:
        name_prefix = deployment_name_prefix(deployment_namespace)
        if target_id_default is not None and (not isinstance(target_id_default, str) or not target_id_default.strip()):
            raise ValueError("target_id must be a nonempty string")
        super().__init__(scope, construct_id, **kwargs)
        Tags.of(self).add("managed-by", "h100-spot-capacity-controller")
        Tags.of(self).add("Name", f"{name_prefix}-Controller")
        if deployment_namespace is not None:
            Tags.of(self).add("customer-id", deployment_namespace)
        target_id = CfnParameter(self, "TargetId", type="String", min_length=1,
                                 default=target_id_default or ("h100-training" if deployment_namespace is None else None),
                                 description="Persisted capacity target identifier; must match the reviewed target")
        launch_instance_role_arns = CfnParameter(
            self, "LaunchInstanceRoleArns", type="CommaDelimitedList",
            allowed_pattern=r"arn:[a-z0-9-]+:iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]+" if deployment_namespace is not None else None,
            description="Exact IAM role ARN(s) referenced by the approved per-Region launch templates; no wildcards",
        )
        launch_template_arns = CfnParameter(
            self, "LaunchTemplateArns", type="CommaDelimitedList",
            allowed_pattern=r"arn:[a-z0-9-]+:ec2:[a-z0-9-]+:[0-9]{12}:launch-template/lt-[0-9a-f]+" if deployment_namespace is not None else None,
            description="Exact approved per-Region Launch Template ARN(s) that EC2 Fleet may use to run instances; no wildcards",
        )
        reconcile_schedule = CfnParameter(self, "ReconcileScheduleExpression", type="String", default="rate(1 minute)")
        signal_schedule = CfnParameter(self, "SignalScheduleExpression", type="String", default="rate(5 minutes)")
        sps_schedule = CfnParameter(self, "SpsScheduleExpression", type="String", default="rate(15 minutes)")
        self.state_table = dynamodb.Table(
            self, "State", partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING), encryption=dynamodb.TableEncryption.AWS_MANAGED,
            table_name=f"{name_prefix}-State",
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(point_in_time_recovery_enabled=True),
            time_to_live_attribute="ttl", removal_policy=RemovalPolicy.RETAIN,
        )
        self.notifications = sns.Topic(self, "Notifications", topic_name=f"{name_prefix}-Notifications")
        self.notifications.apply_removal_policy(RemovalPolicy.RETAIN)
        role = iam.Role(
            self, "ControllerRole", assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            role_name=f"{name_prefix}-Controller-Role",
        )
        self.state_table.grant_read_write_data(role)
        role.add_to_policy(iam.PolicyStatement(
            actions=["dynamodb:TransactWriteItems"], resources=[self.state_table.table_arn],
        ))
        self.notifications.grant_publish(role)
        role.add_to_policy(iam.PolicyStatement(actions=[
            "ec2:DescribeAvailabilityZones", "ec2:DescribeFleets", "ec2:DescribeFleetInstances", "ec2:DescribeInstances",
            "ec2:DescribeInstanceTypes", "ec2:DescribeInstanceTypeOfferings", "ec2:DescribeLaunchTemplateVersions", "ec2:DescribeSpotPriceHistory", "ec2:DescribeSubnets",
            "ec2:DescribeSecurityGroups", "ec2:DescribeSpotInstanceRequests",
            "ec2:GetSpotPlacementScores", "pricing:GetProducts", "eks:DescribeCluster", "servicequotas:GetServiceQuota",
            "cloudwatch:PutMetricData",
        ], resources=["*"]))
        request_tags = {"aws:RequestTag/managed-by": "h100-spot-capacity-controller"}
        resource_tags = {"ec2:ResourceTag/managed-by": "h100-spot-capacity-controller"}
        if deployment_namespace is not None:
            request_tags.update({"aws:RequestTag/customer-id": deployment_namespace,
                                 "aws:RequestTag/capacity-target-id": target_id.value_as_string})
            resource_tags.update({"ec2:ResourceTag/customer-id": deployment_namespace,
                                  "ec2:ResourceTag/capacity-target-id": target_id.value_as_string})
        role.add_to_policy(iam.PolicyStatement(
            actions=["ec2:CreateFleet"], resources=["*"],
            conditions={"StringEquals": request_tags},
        ))
        launch_conditions = {"ArnEquals": {"ec2:LaunchTemplate": launch_template_arns.value_as_list}}
        if deployment_namespace is None:
            # Preserve the existing deployment's policy contract exactly.
            role.add_to_policy(iam.PolicyStatement(
                actions=["ec2:RunInstances"], resources=["*"], conditions=launch_conditions,
            ))
        else:
            # Existing launch inputs have no request tags. Require tags only on
            # newly created instances/volumes, not images, subnets or SGs.
            def ec2_arn(resource: str) -> str:
                return f"arn:{self.partition}:ec2:{self.region}:{self.account}:{resource}/*"

            role.add_to_policy(iam.PolicyStatement(
                actions=["ec2:RunInstances"],
                resources=[ec2_arn("instance"), ec2_arn("volume")],
                conditions={**launch_conditions, "StringEquals": request_tags},
            ))
            role.add_to_policy(iam.PolicyStatement(
                actions=["ec2:RunInstances"],
                resources=[f"arn:{self.partition}:ec2:{self.region}::image/*",
                           f"arn:{self.partition}:ec2:{self.region}::snapshot/*",
                           *[ec2_arn(kind) for kind in ("network-interface", "subnet", "security-group", "launch-template")]],
                conditions=launch_conditions,
            ))
        role.add_to_policy(iam.PolicyStatement(
            actions=["ec2:CreateTags"], resources=["*"],
            conditions={"StringEquals": {
                **request_tags,
                "ec2:CreateAction": "CreateFleet",
            }},
        ))
        role.add_to_policy(iam.PolicyStatement(
            actions=["ec2:CreateTags"], resources=["*"],
            conditions={"StringEquals": {
                **request_tags,
                "ec2:CreateAction": "RunInstances",
            }},
        ))
        role.add_to_policy(iam.PolicyStatement(
            actions=["ec2:ModifyFleet", "ec2:DeleteFleets", "ec2:TerminateInstances"], resources=["*"],
            conditions={"StringEquals": resource_tags},
        ))
        role.add_to_policy(iam.PolicyStatement(
            actions=["iam:PassRole"], resources=launch_instance_role_arns.value_as_list,
            conditions={"StringEquals": {"iam:PassedToService": "ec2.amazonaws.com"}} if deployment_namespace is not None else None,
        ))
        default_policy = role.node.try_find_child("DefaultPolicy")
        if not isinstance(default_policy, iam.Policy):
            raise RuntimeError("controller role default policy was not created")
        default_policy_resource = default_policy.node.default_child
        if not isinstance(default_policy_resource, iam.CfnPolicy):
            raise RuntimeError("controller role default policy has no CfnPolicy resource")
        default_policy_resource.policy_name = f"{name_prefix}-Controller-Policy"
        collector_role = iam.Role(
            self, "CollectorRole", assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            role_name=f"{name_prefix}-Collector-Role",
        )
        self.state_table.grant_read_write_data(collector_role)
        collector_role.add_to_policy(iam.PolicyStatement(actions=[
            "ec2:DescribeAvailabilityZones", "ec2:DescribeInstanceTypes",
            "ec2:DescribeInstanceTypeOfferings", "ec2:DescribeLaunchTemplateVersions",
            "ec2:DescribeSpotPriceHistory", "ec2:DescribeSubnets", "ec2:DescribeSecurityGroups",
            "ec2:GetSpotPlacementScores", "pricing:GetProducts",
            "servicequotas:GetServiceQuota", "cloudwatch:PutMetricData",
        ], resources=["*"]))
        collector_default_policy = collector_role.node.try_find_child("DefaultPolicy")
        if not isinstance(collector_default_policy, iam.Policy):
            raise RuntimeError("collector role default policy was not created")
        collector_policy_resource = collector_default_policy.node.default_child
        if not isinstance(collector_policy_resource, iam.CfnPolicy):
            raise RuntimeError("collector role default policy has no CfnPolicy resource")
        collector_policy_resource.policy_name = f"{name_prefix}-Collector-Policy"
        code = lambda_.Code.from_asset(".", exclude=LAMBDA_ASSET_EXCLUDES)
        runtime_environment = {
            "STATE_TABLE_NAME": self.state_table.table_name,
            "TARGET_ID": target_id.value_as_string,
            "NOTIFICATION_TOPIC_ARN": self.notifications.topic_arn,
        }
        if deployment_namespace is not None:
            runtime_environment["CUSTOMER_ID"] = deployment_namespace

        def retained_log_group(name: str, function_role: iam.IRole = role) -> logs.LogGroup:
            group = logs.LogGroup(
                self, f"{name}Logs", log_group_name=f"{name_prefix}-{name}-Logs",
                retention=logs.RetentionDays.ONE_MONTH, removal_policy=RemovalPolicy.RETAIN,
            )
            group.grant_write(function_role)
            return group

        reconciler = lambda_.Function(self, "Reconciler", function_name=f"{name_prefix}-Reconciler", runtime=lambda_.Runtime.PYTHON_3_12, handler="runtime_handler.reconcile", code=code, role=role, reserved_concurrent_executions=1, memory_size=512, timeout=Duration.minutes(1), log_group=retained_log_group("Reconciler"), environment=runtime_environment)
        collector = lambda_.Function(self, "Collector", function_name=f"{name_prefix}-Collector", runtime=lambda_.Runtime.PYTHON_3_12, handler="runtime_handler.collect", code=code, role=collector_role, memory_size=512, timeout=Duration.minutes(5), log_group=retained_log_group("Collector", collector_role), environment=runtime_environment)
        spot_event_handler = lambda_.Function(self, "SpotEventHandler", function_name=f"{name_prefix}-Spot-Event-Handler", runtime=lambda_.Runtime.PYTHON_3_12, handler="runtime_handler.spot_event", code=code, role=role, memory_size=512, timeout=Duration.minutes(1), log_group=retained_log_group("Spot-Event-Handler"), environment=runtime_environment)
        scheduled_event = events.RuleTargetInput.from_object({"target_id": target_id.value_as_string})
        price_event = events.RuleTargetInput.from_object({"target_id": target_id.value_as_string, "collection": "price-and-local"})
        sps_event = events.RuleTargetInput.from_object({"target_id": target_id.value_as_string, "collection": "sps"})
        events.Rule(self, "ReconcileSchedule", rule_name=f"{name_prefix}-Reconcile-Schedule", schedule=events.Schedule.expression(reconcile_schedule.value_as_string), targets=[targets.LambdaFunction(reconciler, event=scheduled_event)])
        events.Rule(self, "SignalSchedule", rule_name=f"{name_prefix}-Signal-Schedule", schedule=events.Schedule.expression(signal_schedule.value_as_string), targets=[targets.LambdaFunction(collector, event=price_event)])
        events.Rule(self, "SpsSchedule", rule_name=f"{name_prefix}-SPS-Schedule", schedule=events.Schedule.expression(sps_schedule.value_as_string), targets=[targets.LambdaFunction(collector, event=sps_event)])
        events.Rule(self, "SpotLifecycleEvents", rule_name=f"{name_prefix}-Spot-Lifecycle-Events", event_pattern=events.EventPattern(source=["aws.ec2"], detail_type=["EC2 Spot Instance Interruption Warning", "EC2 Instance Rebalance Recommendation"]), targets=[targets.LambdaFunction(spot_event_handler)])
        capacity_dimensions = ({"TargetId": target_id.value_as_string, "Region": self.region,
                                "AcceleratorProfile": "gpu-production"} if deployment_namespace is not None else None)
        operational_dimensions = ({"TargetId": target_id.value_as_string, "Region": self.region}
                                  if deployment_namespace is not None else None)
        cloudwatch.Dashboard(self, "CapacityDashboard", dashboard_name=f"{name_prefix}-Capacity-Dashboard", widgets=[
            [cloudwatch.GraphWidget(title="Machine capacity", left=[
                cloudwatch.Metric(namespace="H100SpotCapacityController", metric_name="DesiredMachineCapacity", statistic="Maximum", dimensions_map=capacity_dimensions),
                cloudwatch.Metric(namespace="H100SpotCapacityController", metric_name="FulfilledMachineCapacity", statistic="Maximum", dimensions_map=capacity_dimensions),
                cloudwatch.Metric(namespace="H100SpotCapacityController", metric_name="MachineShortfall", statistic="Maximum", dimensions_map=capacity_dimensions),
            ])],
            [cloudwatch.GraphWidget(title="Accelerator and Zone state", left=[
                cloudwatch.Metric(namespace="H100SpotCapacityController", metric_name="RealizedAcceleratorCount", statistic="Maximum", dimensions_map=capacity_dimensions),
                cloudwatch.Metric(namespace="H100SpotCapacityController", metric_name="RealizedAcceleratorModelCount", statistic="Maximum"),
                cloudwatch.Metric(namespace="H100SpotCapacityController", metric_name="RealizedAcceleratorCount", statistic="Maximum", dimensions_map=capacity_dimensions),
                cloudwatch.Metric(namespace="H100SpotCapacityController", metric_name="ActiveZoneCount", statistic="Maximum", dimensions_map=capacity_dimensions),
            ])],
            [cloudwatch.GraphWidget(title="Placement and price signals", left=[
                cloudwatch.Metric(namespace="H100SpotCapacityController", metric_name="SpotPlacementScore", statistic="Maximum"),
                cloudwatch.Metric(namespace="H100SpotCapacityController", metric_name="SpotPriceUsd", statistic="Maximum"),
                cloudwatch.Metric(namespace="H100SpotCapacityController", metric_name="LocalZoneEligible", statistic="Minimum"),
                cloudwatch.Metric(namespace="H100SpotCapacityController", metric_name="CapacitySignalError", statistic="Sum"),
            ])],
            [cloudwatch.GraphWidget(title="SPS Region selection", left=[
                cloudwatch.Metric(namespace="H100SpotCapacityController", metric_name="EligibleRegionCount", statistic="Minimum"),
                cloudwatch.Metric(namespace="H100SpotCapacityController", metric_name="NoEligibleRegion", statistic="Maximum"),
                cloudwatch.Metric(namespace="H100SpotCapacityController", metric_name="RegionSignalAgeSeconds", statistic="Maximum"),
                cloudwatch.Metric(namespace="H100SpotCapacityController", metric_name="RecommendationDiffersFromActive", statistic="Maximum"),
            ])],
            [cloudwatch.GraphWidget(title="Failover, retries and interruptions", left=[
                cloudwatch.Metric(namespace="H100SpotCapacityController", metric_name="FailoverState", statistic="Maximum"),
                cloudwatch.Metric(namespace="H100SpotCapacityController", metric_name="FailoverTrigger", statistic="Maximum"),
                cloudwatch.Metric(namespace="H100SpotCapacityController", metric_name="RetryCount", statistic="Sum", dimensions_map=operational_dimensions),
                cloudwatch.Metric(namespace="H100SpotCapacityController", metric_name="InterruptionCount", statistic="Sum", dimensions_map=operational_dimensions),
            ])],
            [cloudwatch.GraphWidget(title="Existing EKS node readiness", left=[
                cloudwatch.Metric(namespace="H100SpotCapacityController", metric_name="EksRegisteredNodeCount", statistic="Maximum"),
                cloudwatch.Metric(namespace="H100SpotCapacityController", metric_name="EksReadyNodeCount", statistic="Maximum"),
            ])],
            [cloudwatch.GraphWidget(title="Per-Zone expansion and capacity", left=[
                cloudwatch.Metric(namespace="H100SpotCapacityController", metric_name="ZoneActive", statistic="Maximum"),
                cloudwatch.Metric(namespace="H100SpotCapacityController", metric_name="ZoneMachineCapacity", statistic="Maximum"),
            ])],
        ])
        cloudwatch.Alarm(
            self, "NoEligibleRegionAlarm",
            alarm_name=f"{name_prefix}-No-Eligible-Region",
            metric=cloudwatch.Metric(namespace="H100SpotCapacityController", metric_name="NoEligibleRegion", statistic="Maximum", period=Duration.minutes(15)),
            threshold=1, evaluation_periods=3,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
        )
        cloudwatch.Alarm(
            self, "StaleRegionSignalAlarm",
            alarm_name=f"{name_prefix}-Stale-Region-Signal",
            metric=cloudwatch.Metric(namespace="H100SpotCapacityController", metric_name="RegionSignalAgeSeconds", statistic="Maximum", period=Duration.minutes(15)),
            threshold=1200, evaluation_periods=2,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
        )
        CfnOutput(self, "StateTableName", value=self.state_table.table_name)
        CfnOutput(self, "NotificationTopicArn", value=self.notifications.topic_arn)
        CfnOutput(self, "RegionSelectionModes", value="manual,recommend,auto_initial")
