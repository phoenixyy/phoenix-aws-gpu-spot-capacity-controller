## ADDED Requirements

### Requirement: Independent bounded NTH integration
The repository SHALL provide offline NTH Helm configuration generation only for generic existing-eks targets; it SHALL NOT alter capacity reconciliation, install NTH automatically or contact a cluster while generating configuration.

#### Scenario: Ineligible target
- **WHEN** a standalone, HYD or CPU target is supplied
- **THEN** generation fails without cloud or cluster operations

### Requirement: Explicit target scheduling and permissions acknowledgement
Generated IMDS values SHALL pin chart/app versions, Linux nodes, capacity-target-id, capacity-source=spot and an explicit opt-in label. GPU toleration SHALL match the target contract rather than tolerate all taints. Default SHALL be dryRun=true. Real draining SHALL require an explicit acknowledgement. Documentation SHALL state that ClusterRole permissions remain cluster-wide; selectors are not an RBAC boundary.

#### Scenario: Safe initial review
- **WHEN** no activation acknowledgement is given
- **THEN** NTH values keep dryRun=true, queue/ASG/scheduled/rebalance features off and emptyDir deletion off

#### Scenario: Activate after review
- **WHEN** the operator explicitly requests draining and acknowledges RBAC
- **THEN** values set dryRun=false without adding AWS IAM or changing Fleet desired capacity

### Requirement: Verified render and operational boundaries
Offline tests SHALL verify generated values and pinned upstream chart rendering. Guides SHALL describe compatibility/IMDS prerequisites, real interruption vs manual termination limits, PDB and short-notice failure, release-specific removal and preservation of capacity state.

#### Scenario: No live acceptance claimed
- **WHEN** offline tests pass without a cluster
- **THEN** real cordon/drain and interruption acceptance remain NOT RUN
