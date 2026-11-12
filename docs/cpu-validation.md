# Isolated CPU Spot validation

This is a test-only controller profile, not general CPU production support. It
exercises the same persisted creation intent, per-target writer claim, EC2 maintain
Fleet and controller reconciliation used by the GPU path. A CPU pass is **not**
B300 fulfillment, GPU/driver/workload readiness, checkpoint recovery or an actual
AWS Spot interruption. One deliberately terminated owned instance and a distinct
replacement in the same Fleet are separate evidence from AWS interruption.

## Identity and bounds

Start from the disabled [CPU example](../config/cpu-validation.example.yaml).
All fixture identifiers must be replaced with reviewed run-owned launch inputs.
Each live run requires its own approved account and Region. The historical test used Seoul **ap-northeast-2**,
one **c6i.large** (2 vCPU, 4 GiB), standalone integration, one manual Region and
approved standard AZs only. No Local Zones, dynamic Region selection, failover,
capacity-rebalancing surge or On-Demand fallback are supported. Other profiles
retain their existing strict GPU/HYD contracts.

Required configuration:

- `accelerator_profile: cpu-validation` and `acquisition_profile: generic`.
- Desired and maximum instance counts both `1`; only `c6i.large`.
- `validation_run_id`: a unique 1–64 character alphanumeric/hyphen/underscore ID.
- Ownership tags `purpose: cpu-spot-validation` and `validation-run-id` equal to
  that run ID, in addition to controller-generated target ownership tags.
- Explicit positive `spot_price_cap_usd` no greater than `0.10`.
- `validation_review_budget_usd: "1.00"` (or a smaller reviewed positive amount).
- `validation_started_at` and `validation_expires_at`: quoted UTC ISO8601 strings,
  for example `"2026-01-01T00:00:00+00:00"`. `Z` input is normalized to `+00:00`.
  No naive or non-UTC timestamps. Both may be absent only while disabled.
- Expiry must be strictly later than start and **no more than 30 minutes** later.
  Capacity writes are allowed at start but never at or after expiry.

The disabled example intentionally has no approved timestamps. Enabling requires
fresh, explicitly approved start and expiry, an exact AMI, a dedicated instance
profile/security group, numeric pinned Launch Template version, encrypted root
volume, UserData digest and `cpu-validation-v1` bootstrap contract. Cleanup must
preserve identity and approved timestamps when persisting `enabled: false`.
A deployed CPU runtime (`STATE_TABLE_NAME` present) additionally requires exact
`TARGET_ID` and `VALIDATION_RUN_ID` environment bindings; absent or different run
identity fails closed. Offline injected fixtures without a deployment environment
do not need these variables.

## Eligibility before demand

The CPU dry-run/preflight and runtime use the same mandatory read-only checks:

1. Exact `c6i.large`, 2 integer vCPUs, 4096 MiB, x86_64, supported Spot usage class
   and no GPU/FPGA/inference/Neuron accelerator metadata. Failed or malformed reads
   block; there is no broad GPU-error fallback to CPU.
2. Every approved AZ is available and standard, with the exact type offered.
3. Every subnet matches its configured AZ ID, is available, has an IP and shares
   a VPC with the exact approved no-ingress security group(s).
4. Pinned Launch Template identity/version and approved AMI/profile/SG/UserData,
   IMDSv2, encryption, delete-on-termination and instance/volume ownership tags.
   No conflicting type, requirements, purchase option or fixed subnet/AZ.
   One primary interface must explicitly disable public IPv4. The exact AMI must
   be available, x86_64 and EBS-backed. Bootstrap requires no network downloads.
5. A unique verified Linux On-Demand hourly price. The effective Spot cap is the
   lower of this price and the configured cap; neither is raised implicitly.
6. **Standard Spot quota `L-34B43A08`**, with required count **2 vCPU**. Unknown
   quota or a total below 2 blocks. Running usage is a lower bound; pending/Fleet
   demand and available quota headroom remain separately unknown. The controller
   reports neither stock nor reserved headroom from eligibility.

CPU observations and published accelerator metrics remain zero even if a caller
supplies nonzero accelerator metric arguments. CPU migration planning/execution
is prohibited. CPU previews never contain GPU/P-family quota evidence.

## Frozen demand and independent expiration

At the first enabled reconciliation, the runtime state pins run/start/expiry
under the writer claim before a capacity write. These fields survive intent
recovery, terminal Fleet observations and expired runs. A changed run, start or
expiry is a conflict, not renewal. Once an intent exists, the CPU launch/config
contract is immutable. The CLI also checks persisted configuration and runtime
identity before updating CPU configuration. Disabling is allowed, but resetting
or deleting recovery state is not a renewal procedure. A later independently
approved test must have a new isolated target/run and infrastructure.

The frozen CreateFleet request is `Type=maintain`, Spot-only, machine weight one,
with all approved standard placements and one desired machine. It includes:

- `ValidUntil`: the same canonical UTC expiry string (accepted by botocore's
  timestamp serializer and safe to persist in immutable JSON).
- `TerminateInstancesWithExpiration: true`.
- A stable ClientToken derived from the entire request and persisted request epoch.

Timeout, ambiguous response or final-state persistence failure retains exactly the
same request/token. Recovery either resolves/replays that identity before expiry
or blocks; it never creates a competing generation. Terminal CPU Fleets require
cleanup rather than automatic recreation. Pool/target changes do not trigger CPU
ModifyFleet. Comparing an AWS-described CPU Fleet tolerates numerically identical
positive finite price strings with different trailing zeros (for example
`0.096` versus `0.0960000000`); this comparison never rewrites frozen request JSON,
pricing strings or ClientToken. Missing, invalid, nonfinite/nonpositive prices and
genuine price changes remain blocked. Generic/GPU comparison behavior is unchanged.
Both the handler and direct reconciler reject the closed window;
the direct path rechecks elapsed time and current configuration immediately before
CreateFleet. In-flight uncertainty remains recorded even if expiry arrives before
result recovery. AWS server expiration provides a controller-independent demand
backstop and requests instance termination, but does not prove cleanup completed.

## Price cap, window and review budget are different

The **USD 0.10/hour** limit is a per-instance Spot price ceiling, not a cap on the
whole account bill. The **30-minute** window bounds permitted demand; it is not an
exact-minute termination guarantee. **USD 1.00** is an operator review budget,
not an enforced aggregate billing stop. EBS, control plane, logs, requests,
rounding and delayed termination/cleanup can add costs. An effective hourly cap
below USD 0.10 is expected when On-Demand pricing is lower. Record reviewed price
and estimated costs separately from actual AWS billing. Never silently extend
expiry, raise caps, add fallback types/Regions or add networking to obtain success.

## Evidence and cleanup boundary

These implementation/offline tests authorize no cloud actions. The root operator
alone owns live account review, resource creation, explicit enablement, one owned
instance termination, same-Fleet replacement observation and full cleanup. Save
FleetId, frozen request/token, DynamoDB state and instance identities. Pending or
shortfall is waiting, not success; expiry without replacement is incomplete.

Cleanup first persists disabled demand using the same run identity, settles
writers and cancels verified owned Fleet demand. Terminate only remaining owned
instances, verify terminal Fleet, zero nonterminal owned instances and no retained
run-owned EBS volumes, then remove the run-owned control-plane, launch/security
and staging resources. Unknown write outcomes or ownership stop affected deletion;
retain concise recovery receipts, never infer deletion from discovery absence.
Production stacks, pre-existing shared resources and the existing GPU delivery
artifact must not be touched. Local tests use fake credentials, blocked AWS HTTP,
no bytecode/cache generation and context-managed temporary fixtures.
