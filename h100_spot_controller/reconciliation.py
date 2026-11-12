"""Recoverable single-writer transactions around one persistent Spot Fleet."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
import json
from typing import Any, Callable
from uuid import uuid4
from time import monotonic

from .preflight import collect_cpu_preflight

from .config import CapacityTarget
from .failover import target_configuration_version
from .fleet import (
    INACTIVE_FLEET_STATES, FleetOwnershipError, find_fleet_by_id,
    find_owned_fleet, find_owned_instances, fleet_request, fulfilled_by_zone,
    is_owned_fleet, observe_fleet_capacity, reconcile_existing_fleet,
)
from .outcomes import ReconciliationOutcome, classify_fleet_errors
from .placement import PlacementState, initial_placement, next_placement_to_activate
from .state import StateStore, VersionedState

CREATION_REPLAY_WINDOW = timedelta(hours=1)


class _CapacityWriteBlocked(RuntimeError):
    def __init__(self, kind: str, code: str):
        self.kind, self.code = kind, code


class _GuardedCapacityClient:
    """Recheck elapsed time and current config immediately before each write."""
    def __init__(self, client: Any, guard: Callable[[], datetime]):
        self.client, self.guard = client, guard

    def __getattr__(self, name: str) -> Any:
        method = getattr(self.client, name)
        if name not in {"create_fleet", "modify_fleet"}:
            return method
        def invoke(**kwargs: Any) -> Any:
            self.guard()
            return method(**kwargs)
        return invoke


def initial_zone_ids(target: CapacityTarget) -> tuple[str, ...]:
    if target.placement_strategy == "all-approved" or target.accelerator_profile == "cpu-validation":
        return tuple(item.zone_id for item in target.region_inputs(target.active_region).standard_placements)
    return (initial_placement(target).zone_id,)


def _outcome(target: CapacityTarget, kind: str, fulfilled: int = 0, code: str | None = None,
             fleet_id: str | None = None, errors: tuple[str, ...] = ()) -> ReconciliationOutcome:
    return ReconciliationOutcome(kind, target.target_id, target.active_region,
                                 target.desired_instance_count, fulfilled, code, fleet_id, errors)


def reconcile_target(ec2: Any, target: CapacityTarget, store: StateStore, now: datetime,
                     price_caps: dict[str, Any] | None = None, *,
                     configuration_guard: Callable[[], bool] | None = None,
                     pricing: Any = None, quota_client: Any = None) -> ReconciliationOutcome:
    """Claim before capacity writes; crashes retain an immutable creation intent.

    The deployed Lambda has a one-minute timeout and reserved concurrency one.
    The three-minute claim prevents concurrent runtime writers, while EC2 tokens
    make recovery of an interrupted creation safe. An expired claim alone never
    authorizes another creation generation.
    """
    target.validate()
    started_clock = monotonic()
    owner = uuid4().hex
    if not store.acquire_writer(target.target_id, owner, now):
        return _outcome(target, "writer_busy", code="TARGET_WRITER_BUSY")
    try:
        if target.enabled and configuration_guard is not None and not configuration_guard():
            return _outcome(target, "creation_conflict", code="TARGET_CONFIGURATION_CHANGED")
        def write_guard() -> datetime:
            current_now = now + timedelta(seconds=max(0, monotonic() - started_clock))
            window = target.validation_window_status(current_now)
            if window:
                raise _CapacityWriteBlocked(window, "CPU_VALIDATION_WINDOW_CLOSED")
            if target.enabled and configuration_guard is not None and not configuration_guard():
                raise _CapacityWriteBlocked("creation_conflict", "TARGET_CONFIGURATION_CHANGED")
            return current_now

        guarded = _GuardedCapacityClient(ec2, write_guard) if target.accelerator_profile == "cpu-validation" else ec2
        return _reconcile_locked(guarded, target, store, now, price_caps, pricing=pricing, quota_client=quota_client)
    except _CapacityWriteBlocked as error:
        return _outcome(target, error.kind, code=error.code)
    finally:
        store.release_writer(target.target_id, owner)


def _reconcile_locked(ec2: Any, target: CapacityTarget, store: StateStore, now: datetime,
                      price_caps: dict[str, Any] | None, *, pricing: Any = None, quota_client: Any = None) -> ReconciliationOutcome:
    previous = store.get(target.target_id)
    state = previous or VersionedState(target.target_id, 0, target.active_region, initial_zone_ids(target))
    if target.accelerator_profile == "cpu-validation":
        identity = (target.validation_run_id, target.validation_started_at, target.validation_expires_at)
        pinned = (state.validation_run_id, state.validation_started_at, state.validation_expires_at)
        if state.validation_run_id is not None and pinned != identity:
            return _outcome(target, "creation_conflict", code="CPU_VALIDATION_IDENTITY_IMMUTABLE")
        if target.enabled:
            if state.validation_run_id is None:
                if state.creation_request_json is not None or state.owned_fleet_id is not None or state.fleet_request_epoch is not None:
                    return _outcome(target, "creation_conflict", code="CPU_LEGACY_INTENT_REQUIRES_REVIEW")
                state = replace(state, version=state.version + 1, validation_run_id=identity[0],
                                validation_started_at=identity[1], validation_expires_at=identity[2])
                store.put_if_version(state, None if previous is None else previous.version)
                previous = state
            window = target.validation_window_status(now)
            if window:
                return _outcome(target, window, code="CPU_VALIDATION_WINDOW_CLOSED", fleet_id=state.owned_fleet_id)
            if state.creation_configuration_version is not None and state.creation_configuration_version != target_configuration_version(target):
                return _outcome(target, "creation_conflict", code="CPU_VALIDATION_CONTRACT_IMMUTABLE")
            evidence = collect_cpu_preflight(target, ec2, quota_client, pricing)
            if not evidence["ready"]:
                return _outcome(target, "preflight_blocked", code=evidence["error_codes"][0] if evidence["error_codes"] else "CPU_PREFLIGHT_UNVERIFIED",
                                errors=tuple(evidence["error_codes"]))
            price_caps = evidence["on_demand_caps_usd"]
    if state.active_region != target.active_region:
        return _outcome(target, "configuration_error", code="STATE_REGION_MISMATCH")
    try:
        fleet = find_owned_fleet(ec2, target)
    except FleetOwnershipError:
        return _outcome(target, "ownership_mismatch", code="MULTIPLE_OWNED_FLEETS")

    if not target.enabled:
        fulfilled = sum(fulfilled_by_zone(ec2, fleet, target).values()) if fleet else 0
        if previous is None:
            store.put_if_version(replace(state, version=1), None)
        return _outcome(target, "disabled", fulfilled)

    # Discovery absence is not proof of deletion. A returned or previously
    # observed identity must be resolved explicitly before a new generation.
    prior_terminal = False
    if state.owned_fleet_id is not None:
        if fleet is not None and fleet["FleetId"] != state.owned_fleet_id:
            return _outcome(target, "ownership_mismatch", code="RECORDED_FLEET_MISMATCH")
        if fleet is None:
            known = find_fleet_by_id(ec2, state.owned_fleet_id)
            if known is None:
                return _outcome(target, "creation_pending", code="KNOWN_FLEET_NOT_VISIBLE",
                                fleet_id=state.owned_fleet_id)
            if not is_owned_fleet(known, target):
                return _outcome(target, "ownership_mismatch", code="RECORDED_FLEET_NOT_OWNED")
            prior_terminal = known.get("FleetState", known.get("State")) in INACTIVE_FLEET_STATES
            if not prior_terminal:
                fleet = known

    if fleet is None:
        residual = find_owned_instances(ec2, target)
        if residual:
            return _outcome(target, "orphaned_capacity", len(residual), "OWNED_INSTANCES_WITHOUT_ACTIVE_FLEET")
        if prior_terminal and target.accelerator_profile == "cpu-validation":
            return _outcome(target, "creation_conflict", code="CPU_TERMINAL_FLEET_REQUIRES_CLEANUP", fleet_id=state.owned_fleet_id)
        if prior_terminal:
            state = replace(state, owned_fleet_id=None, creation_request_json=None,
                            creation_started_at=None, creation_configuration_version=None,
                            creation_status=None, creation_error_codes=(), fleet_request_epoch=None)
        if state.creation_request_json is not None:
            # No pool expansion or price/config refresh until the original
            # request's result is resolved. Never mutate the frozen JSON.
            if state.creation_configuration_version != target_configuration_version(target):
                return _outcome(target, "creation_conflict", code="UNRESOLVED_CREATION_CONTRACT_CHANGED")
            request = json.loads(state.creation_request_json)
            desired_request = fleet_request(target, state.active_zone_ids, price_caps, state.fleet_request_epoch)
            if desired_request != request:
                return _outcome(target, "creation_conflict", code="UNRESOLVED_CREATION_REQUEST_CHANGED")
            if state.creation_started_at is None or now - state.creation_started_at >= CREATION_REPLAY_WINDOW:
                return _outcome(target, "creation_pending", code="CREATION_REPLAY_WINDOW_EXPIRED")
        else:
            if state.fleet_request_epoch is not None:
                return _outcome(target, "creation_pending", code="LEGACY_CREATION_REQUIRES_REVIEW")
            epoch = uuid4().hex
            request = fleet_request(target, state.active_zone_ids, price_caps, epoch)
            intent = replace(
                state, version=state.version + 1, fleet_request_epoch=epoch,
                creation_request_json=json.dumps(request, sort_keys=True, separators=(",", ":")),
                creation_started_at=now, creation_configuration_version=target_configuration_version(target),
                creation_status="pending", creation_error_codes=(),
            )
            store.put_if_version(intent, None if previous is None else previous.version)
            state = intent
        # Timeouts/exceptions leave the precommitted identity intact. Even a
        # response without FleetId may have accepted work, so never rotate it.
        response = ec2.create_fleet(**request) or {}
        kind, codes = classify_fleet_errors(response.get("Errors", []))
        fleet_id = response.get("FleetId")
        completed = replace(
            state, version=state.version + 1, owned_fleet_id=fleet_id,
            creation_status="accepted" if fleet_id else "pending", creation_error_codes=codes,
        )
        store.put_if_version(completed, state.version)
        if not fleet_id:
            return _outcome(target, "creation_pending", code="CREATE_RESULT_UNRESOLVED", errors=codes)
        return _outcome(target, kind, code=codes[0] if codes else None, fleet_id=fleet_id, errors=codes)

    if state.creation_status == "pending":
        # Recover an accepted creation whose final DynamoDB write failed.
        recorded_token = json.loads(state.creation_request_json or "{}").get("ClientToken")
        if recorded_token is not None and fleet.get("ClientToken") != recorded_token:
            return _outcome(target, "ownership_mismatch", code="PENDING_FLEET_TOKEN_MISMATCH")
        state = replace(state, creation_status="accepted", owned_fleet_id=fleet["FleetId"])
    capacity = observe_fleet_capacity(ec2, fleet, target)
    zone_capacity = capacity.by_zone
    fulfilled = sum(zone_capacity.values())
    owned_instances = find_owned_instances(ec2, target)
    foreign = {item["InstanceId"] for item in owned_instances} - set(capacity.owned_instance_ids)
    if foreign:
        return _outcome(target, "orphaned_capacity", len(owned_instances), "OWNED_INSTANCES_OUTSIDE_FLEET", fleet["FleetId"])
    if target.acquisition_profile == "hyd-b300" or target.accelerator_profile == "cpu-validation":
        expected_type = "c6i.large" if target.accelerator_profile == "cpu-validation" else "p6-b300.48xlarge"
        if any(item.get("InstanceType") != expected_type for item in owned_instances):
            return _outcome(target, "configuration_error", fulfilled, "OWNED_INSTANCE_TYPE_MISMATCH", fleet["FleetId"])
        if any(item.get("InstanceLifecycle") != "spot" for item in owned_instances):
            return _outcome(target, "configuration_error", fulfilled, "OWNED_INSTANCE_NOT_SPOT", fleet["FleetId"])
        approved_zones = {item.zone_id for item in target.region_inputs(target.active_region).standard_placements}
        if not set(zone_capacity).issubset(approved_zones):
            return _outcome(target, "configuration_error", fulfilled, "OWNED_PLACEMENT_UNAPPROVED", fleet["FleetId"])
    approved_shrink = (target.excess_instance_termination and
                       fleet.get("TargetCapacitySpecification", {}).get("TotalTargetCapacity", 0) > target.desired_instance_count)
    if len(owned_instances) > target.maximum_instance_count and not approved_shrink:
        return _outcome(target, "orphaned_capacity", len(owned_instances), "OWNED_CAPACITY_EXCEEDS_MAXIMUM", fleet["FleetId"])
    request_zones = tuple(zone for zone in state.active_zone_ids if zone_capacity.get(zone, 0) > 0) + tuple(
        zone for zone in state.active_zone_ids if zone_capacity.get(zone, 0) <= 0
    )
    if target.accelerator_profile == "cpu-validation":
        # Fulfillment in another approved AZ must not reorder the frozen priorities.
        request_zones = state.active_zone_ids
    outcome = reconcile_existing_fleet(ec2, target, fleet, request_zones, fulfilled, price_caps, now=now)
    outcome = replace(outcome, fleet_id=fleet["FleetId"])
    if outcome.kind == "shortfall":
        since = state.shortfall_since or now
        placement = PlacementState(state.active_zone_ids, zone_capacity, since)
        added = next_placement_to_activate(target, placement, now)
        zones = state.active_zone_ids if added is None else (*state.active_zone_ids, added.zone_id)
        configured = target.region_inputs(target.active_region)
        all_zones = {item.zone_id for item in (*configured.standard_placements, *configured.local_zone_placements)}
        all_since = None if added is not None or not all_zones.issubset(set(zones)) else (state.all_zones_shortfall_since or now)
        next_state = replace(state, version=state.version + 1, active_zone_ids=zones,
                             shortfall_since=now if added is not None else since,
                             all_zones_shortfall_since=all_since, owned_fleet_id=fleet["FleetId"])
    elif outcome.kind == "healthy":
        next_state = replace(state, version=state.version + 1, shortfall_since=None,
                             all_zones_shortfall_since=None, owned_fleet_id=fleet["FleetId"])
    else:
        # Invalid prerequisites are not evidence that capacity has been short.
        next_state = replace(state, version=state.version + 1, owned_fleet_id=fleet["FleetId"])
    store.put_if_version(next_state, None if previous is None else previous.version)
    return outcome
