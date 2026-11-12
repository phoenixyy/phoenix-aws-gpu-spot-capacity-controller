# Design

## Context

See the proposal for motivation. Existing configuration accepts only `gpu-production` or bounded G6e `functional-validation`; production metadata hydration requires a positive GPU count. `spot_quota_code` recognizes only P and G/VT families. The reconciler already persists immutable CreateFleet intent, records FleetId, uses per-target writer claims and rejects residual capacity. Fleet requests are maintain/Spot-only but have no expiry. The existing customer stack uses RETAIN for table/logs/topic and provisions collectors/dashboard not needed for this one-off test.

Read-only Seoul discovery succeeded for the global account ending 4508, Standard Spot quota 1152 vCPU and c6i.large Spot support; no described Spot instances or Fleets were present. Default-VPC subnets exist, alongside production-tagged subnets belonging to another project. Do not use the latter. Pending demand and all launch/IAM prerequisites remain unverified.

## Goals / Non-Goals

**Goals:** Exercise the actual runtime handler, DynamoDB stores, recoverable acquisition logic and maintain Fleet behavior with minimal disposable infrastructure. Keep every production default and the existing 0.2.0 deliverable unchanged. Make test identity, deadline, spending assumptions and cleanup evidence explicit.

**Non-Goals:** General CPU production support, a new scheduler, production stack refactoring, CloudFormation/CDK bootstrap changes, GPU or workload recovery and actual AWS Spot interruption. Do not deploy the full collectors/dashboard/SNS suite merely to test one machine.

## Decisions

1. **Separate test profile rather than bypass GPU checks.** Add an explicit CPU branch to config, hydration, dry-run and handlers; inspect AWS metadata for exact approved CPU type, supported architecture, no GPU accelerators and Spot usage class. Preserve strict GPU branches and existing G6e fixtures. CPU metrics always realize zero GPUs. Reject failover plans for the CPU profile, following HYD isolation rather than broadening generic migration.
2. **Reuse acquisition/state, not a standalone raw API demo.** The test reconciler invokes the existing runtime handler and DynamoDB stores. A separate minimal test template packages these modules with runtime-required dependencies and Python 3.12. Only a test table, reconciler, scoped roles/policies, logs and one schedule are deployed. Manual Lambda invocation also validates the response. A script that merely calls CreateFleet would not establish controller integration.
3. **Use one fixed small non-burstable CPU.** c6i.large is 2 vCPU/4 GiB, supports Spot in Seoul and avoids T3 credit charges. All approved default-VPC AZ placements share one target=1 Fleet. Fix exact approved AMI and numeric LT version; no unapproved type fallback. Review price through the existing unique Linux On-Demand resolver and cap Spot at the lower of that price and USD 0.10/hour. If pricing makes that cap ineligible, stop rather than raise it silently.
4. **Two independent expiry barriers.** The test configuration stores approved start and UTC expiry, at most 30 minutes apart. The runtime rejects capacity writes after expiry; the immutable request carries `ValidUntil` and `TerminateInstancesWithExpiration=true`. Store timestamps in a replay-safe representation and serialize correctly for boto3. Neither controller restart nor request recovery extends the deadline. Reject changed expiry while intent is unresolved. Server expiration is an independent backstop, not an exact-minute termination or total-dollar billing guarantee.
5. **Isolated launch/security scope.** Read-only review AMI, subnet AZ/VPC/offering and routing, existing Fleet service-linked role, permissions and pending quota usage. Use default VPC only after approval; create a run-owned security group with no inbound rules, encrypted gp3 root of 8 GiB if supported by AMI minimum, delete-on-termination, IMDSv2 and dedicated instance role/profile. Prefer no public IPv4 and no NAT/endpoints for this pure EC2 capacity test: bootstrap must not need network downloads or SSM connection. If any extra network/resource or IAM scope is necessary, pause and obtain user review rather than expand it implicitly. No production subnet, SG or policy changes.
6. **Minimal least-privilege deployment and no bootstrap.** Use a unique `Phoenix-Codex-Local-Spot-` namespace and target/run tags; scope PassRole to exact instance role and EC2 mutations to test ownership and reviewed LT. Stage only task-owned artifacts through AWS MCP/presigned S3 as needed; do not execute local AWS CLI or SDK as a tool-channel substitute. Inspect the complete test template and effective privilege boundary before deployment. Create no service-linked role without specific approval. Use DESTROY only for isolated test infrastructure, never change production RETAIN settings.
7. **Single replacement and bounded observation.** On initial allocation, save DescribeFleets/DescribeFleetInstances/DescribeInstances responses and DynamoDB state including FleetId and frozen intent. Terminate only the initial owned test InstanceId after confirming attachment. Wait for it to become terminal, then establish a different Spot InstanceId in the same Fleet. Distinguish pending/running from application readiness; do not resize to compensate for pending state. No repeated turnover or indefinite waits.
8. **Cleanup is part of acceptance.** Disable persisted target under writer claim, settle writes and cancel demand before final instance termination. Unknown intent cannot be discarded. Verify all owned instances terminal and test volumes gone, then remove isolated schedule/Lambda/table/logs/roles/profile/LT/SG and staging objects/bucket created by this test. Maintain a local receipt of resource IDs before writes and results after cleanup. Use finally-based cleanup for test-owned local artifacts; if interrupted, reconcile receipt and actual cloud state rather than replay creation.

## Risks / Trade-offs

- [IAM/service-linked-role blockers] → Pause affected writes for user review; do not enlarge or work around permissions.
- [Default VPC is shared] → Read-only subnet use, separate no-ingress SG, no route/subnet modifications, no production-tagged placement.
- [USD 1.00 is a review budget, not a billing hard stop] → Confirm USD 0.10/hour cap, 30-minute demand expiry, small EBS and minimal control plane; inspect pricing before launch and report estimates separately from actual billed cost.
- [Capacity unavailable] → Keep one request identity within deadline and report waiting/incomplete; no On-Demand fallback.
- [Package dependency/real IAM issues missed by fakes] → Test actual packaged handler and deployed Lambda responses; retain categorized failures without declaring success.
- [Expiration/API uncertainty or interrupted control session] → Frozen receipt, server-side expiry and scoped recovery; never delete evidence merely to finish.

## Migration Plan

No existing stack migration. After a new user request approves this plan, implement profile and test-only assets, run full offline regression with AWS network blocked and retain a compact result. Then review account/Region, template/IAM, launch inputs and budget with the operator before cloud writes. Deploy the isolated stack, store a disabled target, explicitly enable with a fresh 30-minute window, run first allocation/replacement and cleanup. Rollback removes only this test infrastructure after demand is cancelled. Keep a local evidence report; do not modify or republish the existing 0.2.0 zip, commit/push or archive the change automatically.

## Open Questions

Resolved at cloud preflight, without changing the design: exact approved AL2023 AMI/root-size minimum, default-VPC placement IDs, price snapshot, effective IAM and existence of Fleet service-linked role. Failed prerequisites stop the run; no scope-changing fallback is authorized by this plan.
