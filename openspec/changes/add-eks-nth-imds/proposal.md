# Add independent NTH IMDS integration for existing-eks

## Why
The EC2 maintain controller restores machine capacity but its Spot event Lambda only emits notifications. Existing self-managed EKS nodes have no repository-provided interruption cordon/drain integration.

## Scope approved by operator
Repository implementation, offline validation and GitHub publication only. No live cluster access, installation, AWS writes, IAM/SQS changes or Kubernetes mutations in this task.

## Changes
- Pin upstream NTH app v1.25.6 / Helm chart 0.27.6 (verified upstream Chart.yaml), not an assertion of latest release.
- Provide offline target-to-Helm-values generator for generic existing-eks only, with Linux/target/Spot/explicit opt-in selectors and configured GPU-taint toleration.
- Default dryRun=true; enabling real cordon/drain requires explicit acknowledgement of cluster-scoped RBAC and disruption risk.
- Use IMDS hostNetwork with IMDSv2 retained; no AWS credentials, IRSA, queue or ASG hooks.
- Interruptions cordon/drain; rebalance monitoring disabled by default because cordon without replacement in a fixed-count Fleet can strand capacity. Scheduled/ASG handling disabled in this bounded integration.
- Document pinned compatibility (upstream tested Kubernetes 1.29–1.32), IMDS access, image/network prerequisites, RBAC capabilities vs selector limitations, PDB/local-data/termination limits, activation, validation and rollback.

## Non-goals
Automatic checkpointing, early replacement/surge, automatic manual-termination drain, guarantees of graceful shutdown, managed-node-group/Karpenter installation, changes to HYD/CPU contracts or automatic cluster deployment.
