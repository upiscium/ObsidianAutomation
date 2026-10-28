"""Pure, unattached v1 Catalog rehearsal; not an HTTP or adapter entry point.

Callers must independently obtain and authorize trusted read-only snapshots.
Pass already-materialized plain dictionaries, never ports, observers or lazy
Mappings. Exact built-in containers/scalars deliberately exclude user-defined
iteration, comparison, copying and conversion hooks. No source discovery occurs.
The explicit adapterMode is a caller assertion, NOT authenticated provenance.

This is a conservative subset, not a complete v1 runtime validator. Unknown outer
IDs/shapes invalidate the envelope; absent IDs are independent and invalid DTOs
invalidate only their own slot. Input ordering is irrelevant. JSON/pair sequences
are not accepted: duplicate JSON keys must be rejected BEFORE materialization;
duplicates already discarded by a decoder cannot be detected here.

Connected observations/actions and read_only verified results are rejected.
Structurally valid mock verified history is suppressed, including a completed
verified lastAttempt and its creation time: this module has no evidence with
which to claim real verification. It does not convert success to failure/unknown
or invent an effect. Fixed labels replace the limited accepted fixture labels.
Fallback revisions are inert zero tokens, not admission/concurrency authority.
No existing workflows/Snapshot/Daily response or route is replaced.
"""

from __future__ import annotations

import math
import re


SCHEMA_VERSION = "webui-workflow-control/v1"
_IDS = ("b1", "b2", "b3", "b4", "b5")
_LABELS = (
    "AI-derived notes", "Vault Snapshot to private Gitea", "Project data into Vault",
    "GitHub progress into Daily Note", "ChatGPT Export intake",
)
_ACTIONS = (
    ("generate", "set_schedule"), ("run_once", "set_schedule"),
    ("sync_project", "set_schedule"), ("run_once", "set_schedule"),
    ("stage_archive", "import_inventory"),
)
_KINDS = (
    ("review_artifact",), ("snapshot_ref",), ("project_vault",),
    ("daily_preview", "daily_vault"), ("private_inventory",),
)
_REASONS = frozenset((
    "pending_integration", "not_attached", "observation_unavailable", "read_only",
    "mock_only", "configuration_missing", "configuration_invalid", "busy",
    "scheduler_decision_required", "input_required", "reconciliation_required",
    "due_unavailable", "manual_only", "unsupported_action", "stale_revision",
    "authority_rejected",
))
_KEYS = frozenset((
    "schemaVersion", "id", "label", "adapterMode", "revision", "observedAt",
    "integration", "configuration", "schedule", "execution", "lastAttempt",
    "lastVerifiedOutcome", "lastAttemptAt", "availableActions", "actionReasons",
))
_HEX = re.compile(r"[0-9a-f]{32}\Z")


def _object(value: object, keys) -> bool:
    return (type(value) is dict and all(type(k) is str for k in value)
            and value.keys() == set(keys))


def _enum(value: object, choices) -> bool:
    return type(value) is str and value in choices


def _time(value: object) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def _handle(value: object) -> bool:
    return type(value) is str and _HEX.fullmatch(value) is not None


def _reason(value: object) -> bool:
    return _enum(value, _REASONS)


def _effect(value: object, index: int) -> bool:
    if not _object(value, ("kind", "state", "changed")):
        return False
    kind, state, changed = value["kind"], value["state"], value["changed"]
    if not _enum(kind, ("none",) + _KINDS[index]):
        return False
    if not _enum(state, ("none", "verified", "unknown")):
        return False
    if kind == "none":
        return state == "none" and changed is None
    return (_enum(state, ("verified", "unknown"))
            and (changed is None or (state == "verified" and type(changed) is bool)))


def _configuration(value: object) -> bool:
    if not _object(value, ("state", "reason")):
        return False
    state, reason = value["state"], value["reason"]
    if not _enum(state, ("configured", "missing", "invalid", "not_observed")):
        return False
    if state == "configured":
        return reason is None
    if state == "missing":
        return _enum(reason, ("configuration_missing",))
    if state == "invalid":
        return _enum(reason, ("configuration_invalid",))
    return _enum(reason, ("pending_integration", "not_attached",
                          "observation_unavailable", "read_only", "mock_only"))


def _schedule(value: object, index: int, configured: bool) -> bool:
    if not _object(value, ("state", "intervalSeconds", "nextDueAt", "reason")):
        return False
    state, interval, due, reason = (value[k] for k in (
        "state", "intervalSeconds", "nextDueAt", "reason"))
    if not _enum(state, ("enabled", "disabled", "unknown", "not_applicable")):
        return False
    if reason is not None and not _reason(reason):
        return False
    if index == 4:
        return (state == "not_applicable" and interval is None and due is None
                and reason == "manual_only")
    if index in (0, 2):
        return (state == "unknown" and interval is None and due is None
                and _enum(reason, ("scheduler_decision_required", "pending_integration",
                                   "not_attached", "observation_unavailable")))
    if state == "unknown":
        return interval is None and due is None and reason is not None
    if state == "not_applicable":
        return False
    lower = 60 if index == 1 else 3600
    if interval is not None and not (type(interval) is int and lower <= interval <= 86400):
        return False
    if state == "disabled":
        return due is None and reason is None
    return (configured and interval is not None
            and ((_time(due) and reason is None)
                 or (due is None and reason == "due_unavailable")))


def _history(value: dict, index: int) -> bool:
    at, last, created, verified = (value[k] for k in (
        "observedAt", "lastAttempt", "lastAttemptAt", "lastVerifiedOutcome"))
    execution = value["execution"]
    if not _object(execution, ("state", "requestId", "startedAt")):
        return False
    state, request, started = (execution[k] for k in ("state", "requestId", "startedAt"))
    if not _enum(state, ("idle", "queued", "running", "unknown")):
        return False
    if state in ("idle", "unknown"):
        if request is not None or started is not None:
            return False
    elif not _handle(request) or (state == "queued" and started is not None):
        return False
    if started is not None and not (_time(started) and started <= at):
        return False
    if last is None:
        return created is None and verified is None and state in ("idle", "unknown")
    if not (_object(last, ("requestId", "state", "outcome", "completedAt", "effect"))
            and _handle(last["requestId"]) and _time(created) and created <= at
            and _enum(last["state"], ("queued", "running", "completed"))
            and _effect(last["effect"], index)):
        return False
    if started is not None and started < created:
        return False
    effect = last["effect"]
    if last["state"] != "completed":
        if not (last["outcome"] is None and last["completedAt"] is None
                and effect["state"] == "unknown" and state == last["state"]
                and request == last["requestId"]):
            return False
    else:
        if not (_enum(last["outcome"], ("success", "failure", "unknown"))
                and _time(last["completedAt"]) and created <= last["completedAt"] <= at
                and state in ("idle", "unknown")):
            return False
        outcome = last["outcome"]
        if outcome == "success" and effect["state"] != "verified":
            return False
        if outcome == "unknown" and effect["state"] != "unknown":
            return False
        if effect["state"] == "unknown" and state != "unknown":
            return False
    if verified is not None:
        if not (_object(verified, ("status", "completedAt", "effect"))
                and _enum(verified["status"], ("success", "failure"))
                and _time(verified["completedAt"]) and verified["completedAt"] <= at
                and _effect(verified["effect"], index)
                and verified["effect"]["state"] in ("verified", "none")):
            return False
        if verified["status"] == "success" and verified["effect"]["state"] != "verified":
            return False
        # A completion timestamp is not an attempt ID. An older verified
        # attempt may complete after a newer admission or share its completion
        # time. Only a completed newest attempt with a known effect must be
        # reflected by an accompanying latest-verified result.
        newest_verified = (last["state"] == "completed"
                           and last["outcome"] in ("success", "failure")
                           and effect["state"] in ("verified", "none"))
        if newest_verified and (verified["status"] != last["outcome"]
                                or verified["completedAt"] != last["completedAt"]
                                or verified["effect"] != effect):
            return False
    if value["adapterMode"] == "read_only":
        return (effect["state"] != "verified" and last["outcome"] != "success"
                and verified is None)
    return True


def _valid(value: object, index: int, observed_at: float) -> bool:
    if not _object(value, _KEYS):
        return False
    if not (_enum(value["schemaVersion"], (SCHEMA_VERSION,))
            and _enum(value["id"], (_IDS[index],))
            and _enum(value["adapterMode"], ("read_only", "mock"))
            and _enum(value["integration"], ("pending", "not_attached", "unavailable"))
            and _handle(value["revision"]) and _time(value["observedAt"])
            and value["observedAt"] <= observed_at):
        return False
    labels = (_LABELS[index],)
    if value["adapterMode"] == "mock":
        labels += (_LABELS[index] + " (mock)",)
        if index == 4:
            labels += (_LABELS[index] + " (TEST-ONLY mock)",)
    if not _enum(value["label"], labels):
        return False
    if not _configuration(value["configuration"]):
        return False
    if not _schedule(value["schedule"], index, value["configuration"]["state"] == "configured"):
        return False
    if not (type(value["availableActions"]) is list and not value["availableActions"]
            and _object(value["actionReasons"], _ACTIONS[index])
            and all(_reason(r) for r in value["actionReasons"].values())):
        return False
    return _history(value, index)


def _neutral(index: int, observed_at: float, integration: str | None = None) -> dict:
    if integration is None:
        integration = "not_attached" if index in (1, 3) else "pending"
    reason = {"unavailable": "observation_unavailable", "not_attached": "not_attached",
              "pending": "pending_integration"}[integration]
    schedule_reason = "manual_only" if index == 4 else (
        "scheduler_decision_required" if index in (0, 2) else reason)
    return {
        "schemaVersion": SCHEMA_VERSION, "id": _IDS[index], "label": _LABELS[index],
        "adapterMode": "read_only", "revision": "0" * 32, "observedAt": observed_at,
        "integration": integration, "configuration": {"state": "not_observed", "reason": reason},
        "schedule": {"state": "not_applicable" if index == 4 else "unknown",
                     "intervalSeconds": None, "nextDueAt": None, "reason": schedule_reason},
        "execution": {"state": "unknown", "requestId": None, "startedAt": None},
        "lastAttempt": None, "lastVerifiedOutcome": None, "lastAttemptAt": None,
        "availableActions": [], "actionReasons": {
            action: "scheduler_decision_required" if action == "set_schedule" and index in (0, 2)
            else "pending_integration" if action == "stage_archive" else reason
            for action in _ACTIONS[index]},
    }


def _copy(value):
    # Called only after bounded exact-shape validation; no deepcopy/pickle hooks.
    if type(value) is dict:
        return {k: _copy(v) for k, v in value.items()}
    if type(value) is list:
        return [_copy(v) for v in value]
    return value


def compose_read_catalog(observations: dict[str, object], observed_at: float) -> dict:
    """Compose five canonical slots without invoking any supplied dependency.

    observed_at is supplied (no clock read); invalid time raises a fixed
    ValueError. Only exact dicts are accepted, despite the common Mapping idiom.
    Invalid outer envelopes yield five unavailable slots, not an empty catalog.
    Candidate timestamps are preserved and cannot be newer than observed_at.
    No maximum age/freshness/authorization policy is asserted by this prototype.
    """
    if not _time(observed_at):
        raise ValueError("invalid_observed_at")
    envelope_valid = (type(observations) is dict
                      and all(type(k) is str and k in _IDS for k in observations))
    workflows = []
    for index, identifier in enumerate(_IDS):
        if not envelope_valid:
            workflow = _neutral(index, observed_at, "unavailable")
        elif identifier not in observations:
            workflow = _neutral(index, observed_at)
        else:
            candidate = observations[identifier]
            if not _valid(candidate, index, observed_at):
                workflow = _neutral(index, observed_at, "unavailable")
            elif candidate["integration"] in ("unavailable", "not_attached"):
                workflow = _neutral(index, observed_at, candidate["integration"])
            else:
                workflow = _copy(candidate)
                workflow["label"] = _LABELS[index] + (" (mock)" if candidate["adapterMode"] == "mock" else "")
                workflow["lastVerifiedOutcome"] = None
                last = workflow["lastAttempt"]
                if last is not None and last["effect"]["state"] == "verified":
                    workflow["lastAttempt"] = None
                    workflow["lastAttemptAt"] = None
        workflows.append(workflow)
    return {"schemaVersion": SCHEMA_VERSION, "authority": "observation_only",
            "observedAt": observed_at, "workflows": workflows}
