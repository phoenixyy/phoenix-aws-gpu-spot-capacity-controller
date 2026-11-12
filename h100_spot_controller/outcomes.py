"""Structured reconciliation outcomes used for logs, metrics and decisions."""

from dataclasses import dataclass
from typing import Literal

OutcomeKind = Literal[
    "disabled", "healthy", "shortfall", "rate_limited", "configuration_error", "authorization_error", "dependency_error",
    "ownership_mismatch", "invalid_fleet", "awaiting_approval", "source_terminating", "failover_complete",
    "writer_busy", "orphaned_capacity", "creation_pending", "creation_conflict", "quota_error",
    "validation_not_started", "validation_expired", "preflight_blocked",
]


@dataclass(frozen=True)
class ReconciliationOutcome:
    kind: OutcomeKind
    target_id: str
    active_region: str
    desired_machine_count: int
    fulfilled_machine_count: int
    error_code: str | None = None
    fleet_id: str | None = None
    pool_error_codes: tuple[str, ...] = ()

    @property
    def advances_zone_timer(self) -> bool:
        return self.kind == "shortfall"


def classify_api_error(error: Exception) -> tuple[OutcomeKind, str]:
    """Map AWS-style errors without allowing them to advance capacity timers."""
    code = str(getattr(error, "response", {}).get("Error", {}).get("Code") or error.__class__.__name__)
    return classify_error_code(code)


def classify_error_code(code: str) -> tuple[OutcomeKind, str]:
    lowered = code.lower()
    if any(value in lowered for value in ("accessdenied", "unauthorized", "authfailure", "operationnotpermitted")):
        return "authorization_error", code
    if any(value in lowered for value in ("throttl", "requestlimit", "toomanyrequests", "maxconfiglimit")):
        return "rate_limited", code
    if any(value in lowered for value in ("spotinstancelimit", "maxspotinstancecount", "vcpu", "instancelimit", "limitexceeded")):
        return "quota_error", code
    if any(value in lowered for value in ("insufficientinstancecapacity", "insufficientcapacity", "unfulfillablecapacity")):
        return "shortfall", code
    if any(value in lowered for value in ("invalidami", "invalidsubnet", "invalidlaunchtemplate", "invalidparameter", "unsupported")):
        return "configuration_error", code
    return "dependency_error", code


def classify_fleet_errors(errors: list[dict]) -> tuple[OutcomeKind, tuple[str, ...]]:
    """CreateFleet can accept a request and still return launch-pool errors."""
    codes = tuple(sorted({str(item.get("ErrorCode", "UNKNOWN_FLEET_ERROR")) for item in errors}))
    priority = {"shortfall": 0, "dependency_error": 1, "rate_limited": 2,
                "quota_error": 3, "configuration_error": 4, "authorization_error": 5}
    kind: OutcomeKind = "shortfall"
    for code in codes:
        candidate, _ = classify_error_code(code)
        if priority.get(candidate, 1) > priority.get(kind, 0):
            kind = candidate
    return kind, codes
