# Tasks

## 1. HYD contract and preflight

- [x] 1.1 Add optional acquisition/placement policy fields and HYD-only/B300-only/standalone/no-surge validation; verify default generic fixtures remain valid and profile escape tests reject other Regions, types, Local Zones and dynamic selection.
- [x] 1.2 Add structured HYD metadata, Spot support, approved AZ/subnet and P Spot quota checks shared by dry-run and runtime; verify offline fake/stubber tests for valid B300, unsupported Spot, wrong model/count, failed reads, invalid subnet, missing offering and insufficient quota.
- [x] 1.3 Correct family-specific dry-run quota reporting and expose total quota, required vCPUs and known/unknown usage separately without claiming headroom; verify B300 uses the P quota and the existing G6e validation retains G/VT reporting.
- [x] 1.4 Add a disabled HYD target example with customer identity, all-approved placements and placeholder launch inputs; document eligibility versus stock and verify local validation plus example contract tests without AWS calls.

## 2. Durable Fleet transactions and residual safety

- [x] 2.1 Add backward-compatible state serialization for immutable creation intent, generation, request identity, timestamps, result FleetId and writer claim; verify old state reads, DynamoDB-safe numeric round trips, conditional ownership and release tests.
- [x] 2.2 Persist the creation intent before CreateFleet, freeze its complete request and save returned identity/errors; verify concurrent initial requests, process failure after EC2 success, failed state completion and unresolved config/price/pool changes all reuse one intent or stop before a competing write.
- [x] 2.3 Always discover residual owned instances when no active Fleet is found, including missing state; verify pending/running/stopping/stopped/shutting-down residuals and duplicate Fleets block new full-target creation without implicit termination.
- [x] 2.4 Resolve saved Fleet identity directly during discovery gaps and rotate generations only on authoritative terminal evidence plus zero residuals; verify delayed visibility, timeouts, expired replay windows and uncertain prior outcomes retain evidence and do not mint new requests.
- [x] 2.5 Canonicalize operator config fingerprints separately from hydrated metadata and add per-target writer serialization; verify raw/hydrated config identity remains stable, stale or concurrent writers cannot issue competing mutations, and document safe unresolved-intent recovery.
- [x] 2.6 Paginate relevant Fleet instance/owned-instance/offering inventories used by the transaction checks; verify multi-page offline fixtures do not hide ownership or capacity.

## 3. Persistent HYD operation and budget

- [x] 3.1 Implement explicit all-approved standard-AZ initialization within one Fleet while preserving generic sequential behavior; verify multiple AZ overrides retain one unchanged total target and do not activate Local Zones.
- [x] 3.2 Guard automatic failover with a valid destination and reject HYD manual migration; verify a HYD-only target beyond every shortfall timer continues reconciling the original Fleet and never calls another Region's capacity API.
- [x] 3.3 Classify CreateFleet identity/embedded errors and capacity/control-plane outcomes, with bounded SDK retries/timeouts; verify capacity waiting, quota, IAM, invalid launch inputs, throttling and ambiguous accepted requests are distinguishable and never trigger competing Fleets.
- [x] 3.4 Apply the lower explicit customer cap and On-Demand ceiling consistently to previews and writes; replace the old override-policy test, verify both min directions and invalid pricing cases, and document the policy correction.
- [x] 3.5 Validate interruption-event Region against the allowlist and verify actual instance ownership before notification; test HYD-owned, foreign instance and foreign Region events and confirm the event Lambda performs no capacity writes.
- [x] 3.6 Document continuous replenish semantics, prolonged-shortfall observation and separate pause/cancel/terminate actions; verify the documented CLI arguments against parser tests and keep checkpoint/workload restoration explicitly out of scope.

## 4. Isolated customer deployment and delivery

- [x] 4.1 Add validated opt-in deployment namespace routing in app/CDK and keep no-namespace physical names unchanged; synthesize two customer namespaces and verify non-colliding names plus legacy-name regression tests.
- [x] 4.2 Bind customer/target runtime ownership and IAM capacity mutation scope to reviewed target/launch inputs, add only necessary preflight read permissions and reserve one reconciler writer; verify policy/template tests reject broad cross-customer destructive scope and preserve least-privilege boundaries.
- [x] 4.3 Exclude generated plans, tools/test outputs and private customer configuration from Lambda assets; verify asset-manifest tests and ensure filled customer IDs/credentials are not included in deliverables.
- [x] 4.4 Write the HYD quickstart with two independent customer examples, read-only evidence requirements, explicit deployment/enable approvals, AMI prerequisites and safe cancellation; verify local example validation and documented command help without creating AWS resources.
- [x] 4.5 Update release metadata and add a local release-check entrypoint after implementation; verify version consistency and mark actual HYD/B300 fulfillment and interruption validation NOT RUN.

## 5. Integration acceptance and cleanup

- [x] 5.1 Run the full existing domain, region selection, CDK and deployment-template suites plus new HYD transaction tests in an isolated environment with fake credentials and accidental AWS network access blocked; retain a compact result with exact test count and failures resolved.
- [x] 5.2 Validate this OpenSpec change strictly, inspect the source diff and run package/CLI/example smoke checks; verify no unresolved plan tasks are silently marked complete and no GitHub push or AWS write occurred.
- [x] 5.3 Settle test processes and remove task-owned synth/fixture/build intermediates through safe verified paths; verify disposable artifacts are gone and report the intended retained source, environment and delivery evidence separately.

## Workflow follow-up

- Present these planning artifacts and obtain a new implementation request before starting the official apply workflow.
- After code and offline acceptance, separately authorize customer account read-only discovery and one-machine live B300 Spot validation; do not treat offline passing tests as regional fulfillment evidence.
- Archive the OpenSpec change only after implementation/review is complete and the user requests that workflow; archival is not part of the tracked implementation tasks.
