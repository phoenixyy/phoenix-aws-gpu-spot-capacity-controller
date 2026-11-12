# Spec Delta

## Purpose

Define a bounded CPU-only Spot integration test of the controller's persistent capacity lifecycle without weakening GPU production requirements or claiming HYD/B300 validation.

## ADDED Requirements

### Requirement: Explicit isolated CPU test identity
The system SHALL accept CPU capacity only under an explicit `cpu-validation` profile with purpose `cpu-spot-validation`, a unique run identity, exactly one manual Region, standalone integration, standard AZs, and desired/maximum counts of one. Only `c6i.large` SHALL be permitted initially. Local Zones, failover, rebalancing surge and On-Demand fallback SHALL be prohibited.

#### Scenario: Bounded CPU configuration is accepted
- **WHEN** a disabled CPU validation target meets every test constraint
- **THEN** local validation SHALL accept it without AWS writes and report zero accelerators

#### Scenario: CPU target escapes its test contract
- **WHEN** the target enables multiple Regions, more than one machine, another type, Local Zones, dynamic selection or GPU production identity
- **THEN** the system SHALL reject it before capacity writes

### Requirement: Production GPU verification remains unchanged
The system SHALL continue rejecting CPU-only metadata in GPU production and HYD/B300 profiles. Selecting the CPU test profile SHALL NOT weaken other profiles' Region, type, launch, price, ownership or enablement checks. CPU observations SHALL NOT be reported as GPU realization or B300 readiness.

#### Scenario: CPU is submitted as GPU production
- **WHEN** a CPU-only instance type is configured under `gpu-production` or `hyd-b300`
- **THEN** the existing GPU validation SHALL reject the target without a capacity write

### Requirement: Evidence-based CPU eligibility
Before CPU capacity writes, the system SHALL verify CPU metadata and Spot support, approved standard AZ offerings and subnet/VPC identity, the pinned launch contract and a valid effective price cap. It SHALL report Standard Spot quota, required vCPUs and incomplete usage separately. Mandatory unknown or failed evidence SHALL prevent writes; passing checks SHALL NOT claim stock or reserved quota headroom.

#### Scenario: Metadata or mandatory evidence fails
- **WHEN** a required read fails, CPU metadata is malformed, Spot is unsupported or the launch contract differs
- **THEN** the system SHALL return a classified blocked result without creating or modifying capacity

#### Scenario: Standard quota is insufficient
- **WHEN** verified Standard Spot total quota is below the test machine's required vCPUs
- **THEN** the system SHALL block capacity creation and distinguish quota failure from capacity waiting

### Requirement: Explicit bounded live authorization
Cloud creation SHALL require reviewed account/Region, test resource scope, per-hour price cap, total review budget and maximum duration. The system SHALL default the test to disabled and SHALL require explicit enablement. Mutations SHALL target only test-owned resources; existing production resources and policies SHALL remain untouched.

#### Scenario: Cloud review is not approved
- **WHEN** planning or offline testing finishes but cloud scope is not approved
- **THEN** the system SHALL keep the test disabled and SHALL NOT create cloud resources

### Requirement: Immutable test expiry and request recovery
An enabled CPU test SHALL have a UTC expiry at most 30 minutes after its approved start. The request SHALL include server-side Fleet expiration with instance termination. After expiry the controller SHALL issue no new capacity request or increase and SHALL NOT reset expiry or generate another request identity. Existing intent, concurrency and residual-capacity protections SHALL apply.

#### Scenario: Controller stops before cleanup
- **WHEN** the controller is unavailable while the request reaches its approved expiry
- **THEN** the accepted Fleet's server-side expiration SHALL bound continuing demand and request instance termination independently of that controller

#### Scenario: An expired test is reconciled
- **WHEN** a target is reconciled after expiry or its saved request has expired
- **THEN** the system SHALL report expiry without creating a replacement Fleet or extending the run

#### Scenario: Creation response is lost
- **WHEN** an initial CPU creation outcome is ambiguous or final state persistence fails
- **THEN** recovery SHALL retain the same frozen request identity or block, never issue a competing full-target request

### Requirement: Real control-path replacement evidence
The test SHALL use the existing controller reconciliation and persisted state. Acceptance SHALL record an initial Spot instance, saved Fleet identity, one deliberate termination and a distinct replacement instance attached to the same maintain Fleet. Deliberate termination SHALL be labeled separately from AWS Spot interruption. Capacity waiting or deadline expiry without replacement SHALL be reported as incomplete, not passing.

#### Scenario: Same Fleet replenishes a deliberately terminated instance
- **WHEN** the initial test instance reaches terminal state and a distinct owned Spot instance appears in the same Fleet before expiry
- **THEN** evidence SHALL establish CPU replacement and persisted identity without claiming B300 or real interruption validation

### Requirement: Scoped cleanup and retained evidence
Cleanup SHALL prevent further test demand before terminating remaining test instances, verify Fleet terminal state and zero nonterminal owned instances, and remove only this run's control-plane, launch and network fixtures. Unknown creation outcomes SHALL preserve receipts and block unsafe deletion. Final evidence SHALL distinguish remaining resources, cleanup failures and intentionally retained records.

#### Scenario: Successful run cleanup
- **WHEN** the test completes or reaches its time limit
- **THEN** the system SHALL cancel test demand, terminate remaining test instances, verify their volumes are removed and delete test-only infrastructure without changing pre-existing resources

#### Scenario: Ownership or creation outcome is uncertain
- **WHEN** cleanup cannot establish this run's ownership or authoritative request identity
- **THEN** it SHALL stop affected destructive actions and report the concrete unresolved resources for operator review
