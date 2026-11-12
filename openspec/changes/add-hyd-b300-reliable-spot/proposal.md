# Proposal

## Why

Two customers need a narrowly scoped Hyderabad B300 Spot controller now. The existing maintain Fleet architecture already retries interrupted capacity, but creation intent is persisted after the EC2 write, residual instances do not block replacement, and HYD-only shortfall can enter an invalid cross-Region path; these gaps must be closed before customer use.

## What Changes

- Add an opt-in `acquisition_profile: hyd-b300` contract: only `ap-south-2`, only `p6-b300.48xlarge`, manual Region selection, standalone integration, approved standard AZs, no Local Zones, no Region failover, no On-Demand fallback or rebalance surge. Default disabled, desired/maximum `1/1`.
- Validate B300 identity, eight GPUs, Spot usage-class support, approved offerings/subnets and P Spot vCPU quota evidence before initial capacity writes. Distinguish unsupported, unknown prerequisites, quota errors and waiting for capacity; never claim that metadata or a quote proves available Spot stock.
- Introduce a durable, immutable Fleet creation intent before EC2 writes, stable retries and returned FleetId persistence. Reject request changes during unresolved creation and block replacement when owned residual instances or uncertain prior creation exist.
- Keep a single maintain Fleet requesting the target after interruptions; HYD-only prolonged shortfall reports and continues reconciling instead of trying a cross-Region plan.
- Offer `placement_strategy: all-approved` for independent HYD instances while preserving the existing sequential strategy as the generic default. Never create one target-sized Fleet per AZ.
- Make a configured per-type customer Spot ceiling effective by taking the minimum of it and the resolved Linux On-Demand ceiling, and expose the resolved ceiling in preflight.
- Add customer-scoped deployment naming, ownership and isolated target examples without renaming default legacy deployments. Clarify pause-controller versus cancel-demand versus terminate semantics.
- Add offline failure/concurrency regression tests, a HYD customer quickstart and a local release check. No cloud deployment is included.

## Capabilities

### New Capabilities
- `hyd-b300-acquisition`: Fixed-Region/fixed-type acquisition contract, read-only eligibility/preflight evidence and independent customer handoff.

### Modified Capabilities
- `spot-capacity-reconciliation`: Durable creation intent, orphan safeguards, unresolved visibility handling, all-approved standard-AZ placement and non-failing single-Region shortfall.
- `spot-capacity-safety`: Effective customer budget ceiling and opt-in customer deployment isolation while retaining explicit enable/cleanup boundaries.

## Impact

- Runtime: configuration, reconciliation, Fleet request/observation, DynamoDB state, Lambda handlers, preflight/dry-run, CLI and quota/metadata reads.
- Deployment: app/CDK naming and IAM read permissions; default physical resource names remain unchanged. New customer deployments must have unique namespaces and independent state tables.
- Delivery: disabled HYD example, two-customer setup instructions, tests and version metadata.
- Compatibility: existing generic GPU and bounded validation profiles remain supported. **BREAKING policy correction:** an explicit static Spot cap will no longer be silently overridden by a higher On-Demand-derived cap.
- Non-goals: checkpoint/resume or task scheduling, EKS/Slurm integration, distributed-training gang placement, proactive replacement/Capacity Rebalancing, cross-Region migration fixes, real customer AWS discovery/deployment, credentials, GitHub commit/push and claims of actual B300 Spot availability.
