"""Offline, opt-in NTH values for self-managed existing-eks Spot nodes.

Never creates AWS/Kubernetes clients or deploys anything. Scheduling selectors
limit normal DaemonSet placement, not its cluster-wide Kubernetes RBAC authority.
"""
import re

from .config import CapacityTarget, ConfigurationError

CHART = "oci://public.ecr.aws/aws-ec2/helm/aws-node-termination-handler"
CHART_VERSION = "0.27.6"
APP_VERSION = "v1.25.6"
TESTED_KUBERNETES = ("1.29", "1.30", "1.31", "1.32")
OPT_IN_LABEL = "spot-controller.phoenix.dev/nth-enabled"


def nth_values(target: CapacityTarget, region: str, kubernetes_version: str, *,
               enable_draining: bool = False, acknowledge_cluster_rbac: bool = False) -> dict:
    target.validate()
    if target.integration_mode != "existing-eks" or target.acquisition_profile != "generic" or target.accelerator_profile != "gpu-production":
        raise ConfigurationError("NTH integration requires generic gpu-production existing-eks")
    if kubernetes_version not in TESTED_KUBERNETES:
        raise ConfigurationError("pinned NTH release is verified here only for Kubernetes 1.29–1.32; review/update the pin for other versions")
    if enable_draining and not acknowledge_cluster_rbac:
        raise ConfigurationError("real draining requires explicit cluster-wide RBAC/disruption acknowledgement")
    inputs = target.region_inputs(region)
    eks = inputs.eks
    if eks is None:
        raise ConfigurationError("region has no EKS contract")
    if eks.required_labels.get("capacity-target-id") != target.target_id or eks.required_labels.get("capacity-source") != "spot":
        raise ConfigurationError("EKS labels must match target_id and capacity-source=spot")
    # Kubernetes label value syntax, unlike target ids, is bounded to 63 chars.
    if len(target.target_id) > 63 or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?", target.target_id):
        raise ConfigurationError("target_id must be a valid Kubernetes label value")
    taint = re.fullmatch(r"([^:=\s]+)(?:=([^:\s]*))?:(NoSchedule|NoExecute)", eks.gpu_taint)
    if not taint:
        raise ConfigurationError("GPU taint must be key[=value]:NoSchedule or NoExecute")
    key, value, effect = taint.groups()
    toleration = {"key": key, "operator": "Equal", "value": value or "", "effect": effect}
    return {
        "image": {"repository": "public.ecr.aws/aws-ec2/aws-node-termination-handler", "tag": APP_VERSION},
        "targetNodeOs": "linux", "useHostNetwork": True,
        "rbac": {"create": True, "pspEnabled": False},
        "serviceAccount": {"create": True, "annotations": {}},
        "dryRun": not enable_draining, "cordonOnly": False,
        "enableSqsTerminationDraining": False,
        "enableSpotInterruptionDraining": True,
        "enableScheduledEventDraining": False,
        "enableASGLifecycleDraining": False,
        "enableRebalanceMonitoring": False, "enableRebalanceDraining": False,
        "deleteLocalData": False, "ignoreDaemonSets": True,
        "podTerminationGracePeriod": -1, "nodeTerminationGracePeriod": 120,
        "enableOutOfServiceTaint": False, "emitKubernetesEvents": False,
        "linuxNodeSelector": {"capacity-target-id": target.target_id, "capacity-source": "spot", OPT_IN_LABEL: "true"},
        "daemonsetTolerations": [], "tolerations": [], "linuxTolerations": [toleration],
        "enableProbesServer": True, "jsonLogging": True,
        "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"memory": "256Mi"}},
    }
