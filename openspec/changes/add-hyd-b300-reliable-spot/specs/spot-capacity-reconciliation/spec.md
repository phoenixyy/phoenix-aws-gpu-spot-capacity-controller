# Spot Capacity Reconciliation Delta

## MODIFIED Requirements

### Requirement: Idempotent reconciliation
The system SHALL identify fleets and launched instances through ownership tags and
stable target identifiers. Repeated reconciliation runs for an unchanged target
MUST NOT create duplicate fleets, repeatedly reactivate the same fallback
transition, or modify resources not owned by the controller. An initial or replacement
creation SHALL have a durable immutable intent before capacity writes and SHALL
reuse that intent until the prior result is resolved.

#### Scenario: Reconciliation is repeated
- **WHEN** two reconciliation runs process the same unchanged enabled target
- **THEN** both runs SHALL resolve to the same controller-owned fleet and only one
  fleet SHALL exist for that target

#### Scenario: EC2 creation succeeds before state completion
- **WHEN** EC2 accepts a creation but the process fails before saving its result
- **THEN** retries SHALL retain the same creation contract and idempotency identity
  rather than submitting a new competing request

#### Scenario: Configuration changes while creation is unresolved
- **WHEN** an operator changes capacity, pools, price or launch inputs while the
  prior creation intent has an uncertain outcome
- **THEN** reconciliation SHALL refuse the changed creation until the prior outcome
  is resolved without rotating the existing request identity

### Requirement: Preferred-Zone sequential expansion
The system SHALL by default initially request capacity from one approved standard Availability
Zone in the active Region. A Zone that fulfills any target capacity SHALL remain
preferred for subsequent machines. If the target remains short for the configured
per-Zone threshold, the system SHALL add the next approved standard Availability
Zone; only after all approved standard Zones are active SHALL it add eligible Local
Zones one at a time. The initial threshold SHALL be fifteen minutes and MAY be
overridden. Zone expansion MUST NOT change desired or maximum capacity. An explicit
all-approved strategy SHALL expose all approved standard AZs initially within one
Fleet and SHALL NOT expose Local Zones or increase the target count.

#### Scenario: Preferred Zone fulfills the target
- **WHEN** the default sequential strategy fulfills the desired machine count before its expansion threshold
- **THEN** the system SHALL keep later standard and Local Zones inactive

#### Scenario: Preferred Zone partially fulfills the target
- **WHEN** the default sequential strategy holds target instances but remains below desired capacity beyond
  the expansion threshold
- **THEN** the system SHALL retain that Zone as preferred and add the next approved
  Zone for only the fleet's remaining capacity

#### Scenario: Standard Zones are exhausted
- **WHEN** the default sequential strategy has activated all approved standard Availability Zones and remains short
  beyond the expansion threshold
- **THEN** the system SHALL add the next eligible approved Local Zone without
  changing desired or maximum capacity

#### Scenario: Local Zone obtains target capacity
- **WHEN** an approved Local Zone launches one or more target instances
- **THEN** the system SHALL retain that Local Zone as an active preferred pool and
  SHALL NOT remove it merely because it is a Local Zone

#### Scenario: Non-capacity error does not advance expansion
- **WHEN** reconciliation cannot establish a valid shortfall because configuration,
  authorization, or a required dependency is invalid
- **THEN** the system SHALL classify the error and SHALL NOT advance the Zone timer

#### Scenario: Local Zone prerequisites are not ready
- **WHEN** a configured Local Zone is not opted in, lacks an approved subnet, or
  does not offer an approved H100 instance type
- **THEN** the system SHALL mark it ineligible, report the reason, and continue
  without changing Local Zone resources

#### Scenario: All-approved standard pools are exposed
- **WHEN** the explicit all-approved strategy is selected for approved standard AZs
- **THEN** one maintain Fleet SHALL expose those pools from its initial request
  while retaining the unchanged total desired machine count

## ADDED Requirements

### Requirement: Residual owned capacity blocks replacement
When no active owned Fleet is found, the controller SHALL discover all non-terminated owned instances even if runtime state is absent. Any residual capacity SHALL block a new full-target Fleet and SHALL NOT be terminated implicitly. A known Fleet's temporary invisibility or an uncertain creation SHALL NOT be treated as verified deletion.

#### Scenario: Deleted Fleet retains instances
- **WHEN** a Fleet is no longer active but its owned instances remain running,
  pending, stopping, stopped or shutting down
- **THEN** the controller SHALL report residual capacity and SHALL NOT create a
  replacement full-target Fleet

#### Scenario: Runtime state is missing
- **WHEN** no state or active Fleet is visible but owned non-terminated instances exist
- **THEN** the controller SHALL detect them and block creation without adopting or terminating them

#### Scenario: Prior creation is temporarily invisible
- **WHEN** a saved Fleet result or creation intent is not yet visible in ownership discovery
- **THEN** reconciliation SHALL preserve that identity and report pending visibility rather than minting another generation

### Requirement: Single Region shortfall remains reconcilable
If no other approved destination Region exists, prolonged shortfall SHALL continue the current Region's reconciliation and notification path without generating a cross-Region plan or failing the invocation. The existing maintain request SHALL remain active.

#### Scenario: HYD-only request waits longer than the failover threshold
- **WHEN** all approved HYD AZs are active and the target remains short beyond the configured failover interval
- **THEN** reconciliation SHALL continue for the same HYD Fleet and SHALL NOT attempt another Region

### Requirement: Fleet response failures are observable
Capacity-write results SHALL be checked for returned identity and embedded errors. Unfulfilled capacity SHALL be distinguished from rejected launch prerequisites, quota failures and transient API failures. An ambiguous accepted request SHALL retain its creation identity for safe recovery.

#### Scenario: Creation response contains a Fleet and pool errors
- **WHEN** the service returns a Fleet identity with unfulfilled-pool errors
- **THEN** the controller SHALL retain the Fleet identity and report the errors without creating another request

#### Scenario: Creation response is ambiguous
- **WHEN** a request times out or returns insufficient evidence to resolve creation
- **THEN** the controller SHALL report an unresolved creation and preserve the same intent for recovery
