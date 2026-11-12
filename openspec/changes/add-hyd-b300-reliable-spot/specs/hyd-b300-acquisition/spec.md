# HYD B300 Acquisition Delta

## Purpose

Define a customer-deliverable contract for persistent acquisition of NVIDIA B300 Spot instances in Hyderabad, separating eligibility evidence from actual fulfillment and isolating customer capacity ownership.

## ADDED Requirements

### Requirement: Fixed Hyderabad B300 acquisition boundary
The opt-in HYD B300 profile SHALL accept only ap-south-2 and p6-b300.48xlarge, manual Region selection, standalone integration and approved standard-AZ subnets. It SHALL reject Local Zones, other Regions or types, cross-Region plans, rebalance surge and On-Demand fallback. Examples SHALL be disabled with desired and maximum machine counts of one.

#### Scenario: Disabled example is validated
- **WHEN** an operator validates the HYD B300 example before replacing placeholders
- **THEN** local configuration validation SHALL show the fixed Region/type and disabled one-machine target without any AWS write

#### Scenario: Configuration escapes the profile
- **WHEN** the HYD B300 profile includes another Region/type, Local Zones, dynamic Region selection, EKS integration or rebalance surge
- **THEN** configuration validation SHALL reject it before capacity writes

### Requirement: Verified B300 Spot eligibility
Before initial capacity creation the HYD B300 profile SHALL require AWS metadata identifying NVIDIA B300 with eight GPUs and Spot usage-class support. Unsupported or unverified evidence SHALL block creation with a classified result. Metadata acceptance SHALL NOT assert that available Spot capacity exists.

#### Scenario: B300 supports Spot
- **WHEN** metadata verifies the approved type, B300 identity, eight GPUs and Spot usage class
- **THEN** the type SHALL pass eligibility while actual fulfillment remains unknown

#### Scenario: Spot is not supported or metadata is incomplete
- **WHEN** Spot support is absent, metadata is unavailable or GPU identity/count cannot be verified
- **THEN** no new Fleet SHALL be created and preflight SHALL distinguish unsupported evidence from unavailable evidence

### Requirement: Approved HYD placements and quota evidence
Preflight SHALL verify approved subnets belong to the configured HYD standard AZs, are available and offer B300. It SHALL report P Spot vCPU quota, required vCPUs and known usage evidence separately. A quota below required capacity or unverified mandatory prerequisite SHALL block initial creation; a total quota value alone SHALL NOT be described as available headroom.

#### Scenario: AZ and quota prerequisites pass
- **WHEN** approved placements verify successfully and P Spot quota is sufficient for the target size
- **THEN** preflight SHALL expose the evidence and required vCPUs without claiming a reservation or guaranteed fulfillment

#### Scenario: Prerequisites cannot be verified
- **WHEN** an approved placement is invalid, quota is below required capacity or a mandatory read fails
- **THEN** initial creation SHALL be blocked with the failed prerequisite identified

### Requirement: Persistent waiting is not an eligibility failure
An approved HYD B300 maintain request SHALL remain active during shortfall. Placement scores SHALL be advisory and SHALL NOT reject an otherwise eligible fixed-type request. Control-plane errors SHALL be classified separately from capacity unavailability; preflight SHALL make no capacity writes.

#### Scenario: Single-type placement score is low
- **WHEN** an eligible HYD B300 request has a low, absent or rate-limited placement score
- **THEN** the controller SHALL continue its approved fixed-Region capacity behavior without adding unapproved types

#### Scenario: No Spot capacity is available
- **WHEN** the existing maintain Fleet has less fulfilled capacity than desired
- **THEN** the controller SHALL report waiting/shortfall and retain the request rather than deleting and recreating competing Fleets

### Requirement: Customer handoff states limitations
Customer instructions SHALL provide independent target/deployment examples, prerequisite discovery, read-only review, explicit enablement and separate cancellation/termination steps. They SHALL state that automatic machine replenishment does not restore workloads, guarantee B300 availability, or support tightly coupled multi-AZ distributed training.

#### Scenario: Two customers use the version
- **WHEN** two customers prepare deployments using the documented examples
- **THEN** each SHALL have independent namespaces, state tables and ownership targets rather than sharing a Fleet or credentials

#### Scenario: Operator pauses the controller
- **WHEN** enabled is changed to false without cancelling the Fleet
- **THEN** documentation SHALL warn that the AWS maintain request continues and SHALL show a separate approved cancellation procedure
