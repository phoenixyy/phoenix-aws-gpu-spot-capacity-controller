# Tasks

## 1. Isolated CPU configuration and eligibility

- [x] 1.1 Add explicit cpu-validation test identity, c6i.large allowlist, single manual Region/standard-AZ/standalone/one-machine limits, run/start/expiry inputs and disabled example; verify local config tests reject every profile escape, unsafe duration and HYD/GPU CPU substitution.
- [x] 1.2 Add CPU metadata verification and Standard Spot quota selection/reporting to dry-run and runtime without bypassing GPU checks; verify fake/stubber tests for Spot support, CPU identity, malformed/failed metadata, offerings, subnet/SG identity, pinned LT and insufficient quota.
- [x] 1.3 Keep CPU metrics at zero accelerators and prohibit CPU failover plans; verify handler/metric/CLI regression tests and document that CPU success is not B300, GPU readiness or real interruption evidence.

## 2. Bounded persistent acquisition

- [x] 2.1 Add UTC test expiry and termination-on-expiration to the frozen Fleet request with stable serialization; verify botocore shape checks, intent round trips and retry tokens stay identical after response/state failures.
- [x] 2.2 Reject new capacity writes outside the approved start/expiry window, preserve uncertainty and prevent expired Fleet recreation or deadline extension; verify expiry, concurrent configuration change, ambiguous result, old-generation and residual-instance regressions.
- [x] 2.3 Document the 30-minute demand window, USD 0.10/hour effective Spot cap and USD 1.00 review budget separately from an exact billing cap; verify disabled example and preview expose these boundaries and production request defaults are unchanged.

## 3. Minimal disposable deployment and lifecycle tools

- [x] 3.1 Add a test-only template using the existing runtime handler, one-writer Lambda, DynamoDB, logs and schedule without collectors/dashboard/SNS or production template changes; verify synthesized resource inventory, unique naming, destruction policy and legacy customer-template regressions.
- [x] 3.2 Package runtime modules and necessary dependencies in an isolated directory; verify archive manifest and import/handler smoke tests with fake credentials and no AWS networking, removing all temporary package/synth artifacts after tests.
- [x] 3.3 Define exact-LT/PassRole and ownership-scoped IAM for the test, dedicated no-ingress SG/profile/LT, encrypted delete-on-termination root and no public IP/network dependency; verify template/policy tests and prepare a readable permission/resource inventory for operator review.
- [x] 3.4 Add a resumable resource receipt and bounded test/cleanup runbook, recording resource IDs before mutations and distinguishing uncertain writes; verify offline failure/recovery tests cannot touch foreign resources or replay creation blindly.

## 4. Offline integration acceptance and live approval

- [x] 4.1 Run the entire existing 213-test baseline plus new CPU suites with AWS HTTP blocked, validate this change strictly and inspect source diff; retain exact counts, results and proof production GPU/HYD contracts and the existing delivery zip were not weakened or overwritten.
- [x] 4.2 Perform scoped read-only AMI/offering/subnet/routing, pending-quota and Fleet service-linked-role/effective-permission review in Seoul; record classified prerequisites without claiming current inventory or quota headroom.
- [x] 4.3 Present the final template/IAM/resource scope, account ending 4508, Seoul, c6i.large, one-machine 30-minute window, USD 0.10/hour cap, USD 1.00 review budget and cleanup scope; verify direct user authorization before any cloud creation, permission change or instance termination.

## 5. Authorized real CPU control-path validation

- [x] 5.1 Deploy only run-owned fixtures and the minimal controller through AWS MCP, persist a disabled target and confirm packaged Lambda/DynamoDB identity; verify actual stack/function state and read back the target before explicit enablement.
- [ ] 5.2 Enable with a fresh approved UTC expiry and observe one initial running Spot instance; record FleetId, request identity, DynamoDB state and inventory, treating pending/shortfall as waiting rather than success.
- [ ] 5.3 Confirm ownership/attachment and deliberately terminate only the initial test instance once; verify that it reaches terminal state and a distinct Spot instance appears in the same maintain Fleet with stable persisted identity before expiry.
- [x] 5.4 Capture Lambda responses, logs and relevant capacity/state evidence; report real Spot interruption, B300 support/fulfillment, GPU/workload readiness and checkpoint recovery as NOT RUN.

## 6. Cleanup and final evidence

- [x] 6.1 Disable persisted test configuration and settle writers, cancel demand, terminate remaining run-owned instances and verify terminal Fleet plus zero nonterminal owned instances and no test EBS volumes; retain concrete recovery evidence instead of guessing if a write outcome is unknown.
- [x] 6.2 Remove this run's schedule/controller/table/logs/roles/profile/LT/SG and staging objects/bucket, never production or pre-existing shared resources; verify deletions and list any retained or unresolved cloud resources.
- [x] 6.3 Remove task-owned local build/test directories after settling jobs, retain concise local receipts/report and intended source/planning files, and publish the final CPU result with exact pass/incomplete/NOT RUN boundaries and verified cleanup status.

## Workflow follow-up

- Planning is complete only after strict validation. Stop and obtain a new user implementation request before applying this change, as required by the project-local propose workflow.
- If cloud prerequisites or permissions require wider scope, pause affected actions and ask the user to review rather than auto-expanding authorization.
- Do not commit/push, replace the existing 0.2.0 zip or archive either OpenSpec change without a separate user request.
