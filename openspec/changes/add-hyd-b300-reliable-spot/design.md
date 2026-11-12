# Design

## Context

The motivation and scope are defined in [proposal](<proposal.md>). The repository already has a generic GPU profile, maintain Fleet requests, tag-based discovery and DynamoDB conditional state writes. The existing creation flow calls EC2 before persisting its request epoch, discards the returned FleetId and can create despite residual instances. Runtime preflight is not a HYD/B300 acquisition contract; the dry-run quota path hardcodes G/VT. CDK physical names currently collide for two same-account/same-Region stacks. No customer AWS identity, subnet, AMI, budget or Spot support evidence has been supplied.

Existing H100-oriented OpenSpec context predates generic GPU support; this change deliberately adds a separate opt-in acquisition policy, not a new GPU-family limitation on generic production.

## Goals / Non-Goals

**Goals:** Make the initial HYD/B300 capacity request and later maintenance recoverable without duplicate full-target Fleets, preserve explicit money/destruction boundaries, and provide independently deployable customer inputs with reproducible offline tests.

**Non-Goals:** No claim of guaranteed Spot allocation, HA training continuity, checkpoint restoration, EKS integration or distributed-training topology. No live AWS reads/writes, credential handling, global Profile changes, Git push or cloud release in the implementation phase. Cross-Region defects found during review remain documented; the HYD profile cannot exercise them.

## Decisions

### 1. Opt-in policy rather than a fork or hardcoded generic behavior

Add `acquisition_profile` with default `generic` and opt-in `hyd-b300`. Retain `accelerator_profile: gpu-production`. HYD validation requires only ap-south-2, exactly p6-b300.48xlarge, manual selection, standalone, no Local Zones and no surge. Ownership includes a customer identity and a unique target ID. A profile escape is a configuration error, not a silent substitution. Generic and functional-validation configurations retain their existing defaults.

Alternative rejected: globally replace p5/H100 defaults with B300. That would break existing users and would not supply a verifiable acquisition boundary.

### 2. Persist immutable creation evidence before submitting it

Extend runtime state with a creation generation, immutable request, creation timestamp, status and returned FleetId. The request contains the resolved price, approved pool set, launch inputs and stable ClientToken. Store native DynamoDB-safe numbers (integers/Decimal rather than floats); restore request shape deterministically. Keep state serialization backward compatible.

Initial flow:

1. Read state consistently and discover owned Fleets plus all non-terminated owned instances. Multiple Fleets or orphan instances stop capacity writes.
2. Build the approved request and conditionally persist the immutable intent before the EC2 call. A competing state claim loses before submitting a different token.
3. Call CreateFleet using the stored request; inspect FleetId and embedded errors.
4. Persist returned FleetId/result using the expected state generation. If this commit fails, leave the intent recoverable rather than generate a new request.
5. While creation is unresolved, re-read/recover the same request identity. Do not incorporate changed price, pools or operator config into that request. Expose a creation-contract conflict instead.

A known Fleet temporarily absent from tag discovery is queried by saved identity; absence alone does not authorize replacement. Replacements need authoritative terminal Fleet evidence and zero residual instances. Unknown/timed-out outcomes preserve the intent. Retry age is bounded conservatively (initially one hour, below the service's supported idempotency horizon); after that, ambiguous writes stop for operator resolution, not a new token. Operator recovery must inspect evidence and never simply delete the intent.

Alternative rejected: a new timestamp ClientToken for every reconciliation, or deleting/recreating the Fleet when short. Both permit competing requests and unexpected cost.

### 3. Serialize capacity writers and separate configuration versions from metadata

Use reserved concurrency one on the customer reconciler and a per-target conditional writer claim for runtime mutation entry, with acquisition before capacity actions and release in finally. The claim expiry must exceed the invocation timeout plus bounded SDK I/O. All creation retries still use the same frozen intent; serialization alone is insufficient after crashes. No other runtime Lambda or CLI command acquires competing capacity ownership. Externally bypassing the reconciler/approval path is unsupported.

Canonical operator configuration fingerprints exclude hydrated GPU model/count and persisted runtime fields. GPU metadata has a separate evidence fingerprint. A state claim does not authorize a different config version. CLI target updates cannot make an unresolved creation silently request a new contract.

Expired writer claims permit recovery only of the same creation intent. They do not prove an uncertain EC2 mutation was rejected. Tests must pause an in-flight fake write and show that another writer cannot issue a competing mutation.

### 4. One Fleet, explicit placement strategy, no HYD Region failover

`placement_strategy` defaults to existing sequential behavior; `all-approved` initially includes all validated standard placements in one Fleet. The HYD example chooses all-approved for independent instances. Active pool changes never multiply desired capacity. Do not add Local Zones in this strategy.

Add a destination guard before all automatic Region plan generation. HYD additionally forbids manual migration. When no destination exists, keep normal reconciliation/shortfall notifications; do not call the plan builder. Interrupted machines are replenished by the existing maintain Fleet. The event Lambda reports the actual event Region after allowlist/ownership validation; it does not create another Fleet.

### 5. Unified read-only preflight evidence, not stock prediction

Share structured HYD checks between CLI dry-run and initial/runtime capacity mutation prerequisites. Mandatory checks: B300/8 GPU identity, SupportedUsageClasses containing spot, approved standard-AZ offerings, subnet AZ/VPC/state/address evidence, launch-template contract, pricing and P Spot vCPU quota. Return machine-readable per-check status and actionable codes. Failures from these reads block affected new writes but do not cancel a previously active maintain request.

The quota helper selects the family-appropriate quota (P for B300, G/VT for the existing validation fixture), computes required vCPUs from AWS metadata and reports total quota separately from known consumption. Usage evidence is conservative: do not double count a fulfilled request and its running instance, and do not label incomplete observations as free headroom. A clearly insufficient total quota blocks launch; unresolved consumption is labelled unknown, and service-side quota errors remain authoritative, not automatically interpreted as capacity shortage.

SPS and Spot-price snapshots are advisory. Single-type low SPS is expected and cannot hard-reject HYD acquisition. Type offering, metadata and quota cannot reserve capacity. Paginate relevant instance, offering and request inventories.

Use botocore timeouts and standard/adaptive bounded retries; controller schedules are not a high-frequency capacity race. CreateFleet pool errors and necessary Fleet status/history evidence must distinguish launch/configuration rejection from waiting. Add only the IAM read operations the implementation actually uses.

### 6. Honor explicit customer budget without weakening default cap safety

Resolve the Linux On-Demand price with the existing fail-closed resolver. Effective cap is min(resolved On-Demand, explicit per-type Spot cap when present). Use it consistently in preflight, creation intent and Fleet updates. Invalid or ambiguous pricing still blocks new mutations. Intent freezing prevents price refresh from changing an unresolved ClientToken.

This is an intentional correction to the old test that expects static caps to be overridden. Document the behavior change and do not promise Spot discount percentages or bidding priority.

### 7. Customer namespaces are opt-in deployment inputs

Add a validated CDK/app deployment namespace (short lowercase letters, digits and hyphens, with length bounded by the shortest named AWS resource limit). Prefix resource physical names with Phoenix-Codex-Local-Spot plus the namespace. Keep the legacy no-namespace naming path identical. Include namespace in stack identity/environment as needed, and require the HYD customer configuration identity to match its deployment scope. Targets and launch-template instance/volume tags remain explicit.

Two same-account stacks must synthesize distinct tables, roles, functions, rules, log groups, topics and dashboards. Lambda assets exclude local tools, generated plans/test outputs and private customer input files. IAM capacity mutation conditions bind the customer target/scope and reviewed launch templates where supported; destructive actions cannot operate on another customer's tagged resources. Merely changing names is not full IAM isolation.

Alternative rejected: sharing the existing fixed-name table and one Fleet between customers. The first release uses a control-plane stack per customer rather than introducing a multi-tenant scheduler.

### 8. Offline release first, real fulfillment later

Keep disabled example placeholders for both customer namespaces. The quickstart separates local validation, authorized account read-only preflight, deployment, capacity enablement, ongoing observation and cancellation. Do not package filled account inputs or credentials. Version the implementation once complete, with an explicit not-live-validated status.

Run the full existing unit/CDK/deployment tests plus new HYD and transaction tests in an isolated Python environment. Use fake credentials/stubbers, disable metadata credential discovery, and block accidental AWS network calls in regression tests. Synth outputs and temporary build fixtures must use task-owned temporary directories with teardown. Keep only source, intended deliverables and compact test evidence; remove task-owned tool/test intermediates without touching approved global tools or shared caches.

## Risks / Trade-offs

- [HYD B300 might not expose Spot in the customer account] → Spot eligibility is an explicit gate; do not deliver a false success or switch purchase mode.
- [Strict no-surge policy produces downtime] → Document replenishment versus guaranteed availability; rebalance is out of scope.
- [Ambiguous AWS writes outlive normal retry windows] → Retain evidence, stop automatic new generations and require operator review; safety takes precedence over unbounded retry.
- [All-approved AZs are unsuitable for tightly coupled training] → Label examples independent-instance capacity only; no gang/cluster guarantee.
- [Quota usage reads can be incomplete or overlap] → Separate total, required and unknown headroom evidence; service quota rejection stays distinct.
- [Customer IAM scope and resource names differ] → Verify both names and policy conditions in synthesis tests; no source default renames.
- [New optional fields alter existing fingerprint compatibility] → Canonicalize defaults and preserve old state reads; require fresh explicit enablement/review for HYD adoption.

## Migration Plan

1. Implement and run offline tests against the existing baseline; keep the OpenSpec change unarchived until verification.
2. New HYD customer stacks use distinct namespaces and disabled targets; reviewed Launch Templates are customer prerequisites, not auto-created networks.
3. Read-only account preflight and live one-machine validation require separate operator authorization. Capture Spot support, quota, FleetId, actual GPU readiness and repeated replenishment evidence before production claims.
4. Existing generic deployments retain default names. Existing active owned Fleets are discovered and retained; new creation protocol is applied only when safe. Old unresolved/missing state with residual resources is an incident, not permission to reconstruct full capacity.
5. Roll back controller code/config only after checking pending creation intent. Do not delete durable intent records or cancel existing Fleets implicitly. Pausing Lambda or enabled=false does not stop AWS demand; Fleet cancellation and instance termination remain separate authorized actions.

## Open Questions

- The two customer namespace values, AWS accounts, budget ceilings and approved AMI/subnet/Launch Template IDs are deployment inputs; placeholders are sufficient for implementation.
- Actual regional/account Spot usage-class support and current fulfillable pools remain unverified until separately authorized read-only discovery and live validation.
