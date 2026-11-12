# Proposal

## Why

HYD/B300 0.2.0 has passed 213 offline tests but has not exercised the deployed controller, DynamoDB state and real EC2 maintain Fleet replenishment. The operator cannot use HYD or GPU quota; a bounded CPU-only integration test in an already enabled Region can validate the common control path without claiming B300 readiness.

## What Changes

- Add an explicit test-only `cpu-validation` profile, initially allowlisting only `c6i.large`, one machine, one manual Region and approved standard AZs, with zero accelerator counts.
- Keep `gpu-production`, `hyd-b300` and existing G6e validation contracts unchanged. Do not bypass GPU verification with generic exception handling.
- Reuse existing immutable creation intent, writer claim, configuration guard, owned-inventory and cleanup paths; add Standard Spot quota and CPU metadata/Spot support evidence to previews and runtime validation.
- Bound the test using an explicit UTC expiry no more than 30 minutes from approved enablement, a per-hour price cap and a review budget of USD 1.00. Prevent new demand after expiry; use AWS Fleet expiration/termination settings as an independent capacity backstop, not as a billing guarantee.
- Provide a minimal isolated test deployment reusing the real reconciler Lambda and DynamoDB implementation, with reviewed IAM, unique ownership, no inbound ports and no production-network changes.
- Record first allocation, persisted FleetId/intent, one deliberately terminated test instance, same-Fleet replacement, demand cancellation and verified resource cleanup. Mark true Spot interruption and all B300 checks NOT RUN.

## Capabilities

### New Capabilities

- `cpu-spot-validation`: Isolated, time-bounded single-machine CPU Spot validation of the existing controller and recoverable Fleet lifecycle, including authorization and scoped cleanup.

### Modified Capabilities

None. CPU acceptance applies only to the explicit new test identity; existing GPU validation, safety and HYD profile requirements remain unchanged.

## Impact

Configuration, metadata hydration, preflight/quota reporting, runtime handlers, Fleet request generation, test-only deployment packaging, documentation and offline tests are affected. Do not upgrade, destroy or mutate existing stacks or publish to GitHub. Planning performs no AWS writes.

Read-only discovery found global account ending 4508, enabled Seoul (`ap-northeast-2`), Standard Spot quota 1152 vCPU, `c6i.large` with 2 vCPU/4 GiB and Spot support, no described Spot instances and no EC2 Fleets in Seoul. Pending requests, AMI, routing, service-linked roles and effective IAM still require scoped review; quota total is not reserved headroom or stock.

## Non-goals

GPU workload/driver validation, real AWS Spot interruption, checkpoint/resume, multi-node training, multi-Region migration, quota/Region enablement, production rollout, load testing and automatic On-Demand fallback. A CPU pass is not B300 fulfillment evidence.
