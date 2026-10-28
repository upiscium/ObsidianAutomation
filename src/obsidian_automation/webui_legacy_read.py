"""Side-effect-free B2/B4 legacy status -> conservative v1 read-only views.

Only accepts already-materialized plain built-in dictionaries. It never owns,
creates, probes, schedules, or invokes a job, and never copies private run
metadata into a response. A legacy status string is not verified effect
evidence; all exposed attempts have unknown effects and no verified history.
"""
from __future__ import annotations

import math
import re

from .webui_control_read import SCHEMA_VERSION, compose_read_catalog

_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_SNAPSHOT_RUN = frozenset(("run_id", "request_id", "trigger", "status",
                           "created", "completed", "stage", "remote_ref"))
_DAILY_RUN = frozenset(("run_id", "request_id", "trigger", "status",
                        "created", "completed", "date_jst", "capture_sha256",
                        "digest_sha256", "original_event_count",
                        "rendered_entity_count", "publication_outcome"))
_SNAPSHOT_STATUSES = frozenset(("queued", "running", "saved", "no_changes",
                                "failed", "unconfirmed"))
_DAILY_STATUSES = frozenset(("queued", "running", "preview_ready", "published",
                             "publish_failed", "failed", "unconfirmed"))
_STAGES = frozenset(("queued", "running", "sync", "fetch", "snapshot", "push",
                     "verify", "interrupted"))
_LABEL = {"b2": "Vault Snapshot to private Gitea",
          "b4": "GitHub progress into Daily Note"}


def _plain(value: object, keys: frozenset[str]) -> bool:
    return (type(value) is dict and
            all(type(k) is str for k in value) and value.keys() == keys)


def _time(value: object) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def _hex(value: object, pattern: re.Pattern[str]) -> bool:
    return type(value) is str and pattern.fullmatch(value) is not None


def _valid_opaque(value: object, pattern: re.Pattern[str]) -> bool:
    return value is None or _hex(value, pattern)


def _settings(raw: object, identifier: str) -> tuple[str, dict]:
    if identifier == "b2":
        keys = frozenset(("version", "configured", "enabled", "interval_seconds",
                          "next_due", "source_name", "destination_name",
                          "gitea_write"))
    else:
        keys = frozenset(("configured", "enabled", "interval_seconds", "next_due"))
    if not _plain(raw, keys):
        raise ValueError("invalid_legacy_settings")
    if identifier == "b2" and not (
        type(raw["version"]) is int and raw["version"] == 1
        and type(raw["source_name"]) is str and raw["source_name"] == "nextcloud"
        and type(raw["destination_name"]) is str and raw["destination_name"] == "gitea"
        and type(raw["gitea_write"]) is str
        and raw["gitea_write"] in ("verified", "unverified")
    ):
        raise ValueError("invalid_snapshot_binding")
    configured, enabled = raw["configured"], raw["enabled"]
    interval, due = raw["interval_seconds"], raw["next_due"]
    low = 60 if identifier == "b2" else 3600
    if (type(configured) is not bool or type(enabled) is not bool
            or type(interval) is not int or not low <= interval <= 86400
            or (enabled and not configured)
            or (due is not None and not _time(due))
            or (not enabled and due is not None)):
        raise ValueError("invalid_legacy_schedule")
    schedule = {"state": "enabled" if enabled else "disabled",
                "intervalSeconds": interval,
                "nextDueAt": due if enabled else None,
                "reason": "due_unavailable" if enabled and due is None else None}
    return ("configured" if configured else "missing"), schedule


def _run(raw: object, identifier: str, observed_at: float) -> tuple[dict, dict | None, float | None]:
    neutral = {"state": "unknown", "requestId": None, "startedAt": None}
    if raw is None:
        return neutral, None, None
    keys = _SNAPSHOT_RUN if identifier == "b2" else _DAILY_RUN
    if not _plain(raw, keys):
        raise ValueError("invalid_legacy_run")
    run_id, request_id, trigger = raw["run_id"], raw["request_id"], raw["trigger"]
    status, created, completed = raw["status"], raw["created"], raw["completed"]
    allowed = _SNAPSHOT_STATUSES if identifier == "b2" else _DAILY_STATUSES
    if (not _hex(run_id, _HEX32) or not _hex(request_id, _HEX32)
            or type(trigger) is not str or trigger not in ("manual", "scheduled")
            or type(status) is not str or status not in allowed
            or not _time(created) or created > observed_at):
        raise ValueError("invalid_legacy_attempt")
    active = status in ("queued", "running")
    if (active and completed is not None
            or not active and (not _time(completed)
                               or not created <= completed <= observed_at)):
        raise ValueError("invalid_legacy_completion")
    if identifier == "b2":
        if (type(raw["stage"]) is not str or raw["stage"] not in _STAGES
                or not _valid_opaque(raw["remote_ref"], _HEX40)):
            raise ValueError("invalid_snapshot_metadata")
    else:
        if (not _valid_opaque(raw["date_jst"], _DATE)
                or not _valid_opaque(raw["capture_sha256"], _HEX64)
                or not _valid_opaque(raw["digest_sha256"], _HEX64)
                or any(value is not None and
                       (type(value) is not int or not 0 <= value <= 100000)
                       for value in (raw["original_event_count"], raw["rendered_entity_count"]))
                or (raw["publication_outcome"] is not None and
                    (type(raw["publication_outcome"]) is not str or
                     raw["publication_outcome"] not in
                     ("applied", "recovered", "already_desired")))):
            raise ValueError("invalid_daily_metadata")
        if status == "published" and raw["publication_outcome"] is None:
            raise ValueError("unbound_daily_publication")
    effect_kind = ("snapshot_ref" if identifier == "b2" else
                   "daily_vault" if status in ("published", "publish_failed") else
                   "daily_preview")
    execution = ({"state": status, "requestId": request_id, "startedAt": None}
                 if active else neutral)
    outcome = (None if active else
               "failure" if status in ("failed", "publish_failed") else "unknown")
    last = {"requestId": request_id,
            "state": status if active else "completed",
            "outcome": outcome,
            "completedAt": None if active else completed,
            "effect": {"kind": effect_kind, "state": "unknown", "changed": None}}
    return execution, last, created


def _candidate(raw: object, identifier: str, observed_at: float) -> object:
    if raw is None:
        return None
    keys = (frozenset(("version", "job", "settings", "last_run"))
            if identifier == "b2" else
            frozenset(("version", "job", "publication_enabled", "settings", "last_run")))
    if not _plain(raw, keys):
        return False
    if (type(raw["version"]) is not int or raw["version"] != 1 or
            type(raw["job"]) is not str or
            raw["job"] != ("vault_snapshot" if identifier == "b2" else
                            "github_daily_preview")):
        return False
    if identifier == "b4" and type(raw["publication_enabled"]) is not bool:
        return False
    try:
        config, schedule = _settings(raw["settings"], identifier)
        execution, last, created = _run(raw["last_run"], identifier, observed_at)
    except (ValueError, TypeError, OverflowError):
        return False
    return {
        "schemaVersion": SCHEMA_VERSION, "id": identifier, "label": _LABEL[identifier],
        "adapterMode": "read_only", "revision": "0" * 32,
        "observedAt": observed_at, "integration": "pending",
        "configuration": {"state": config,
                          "reason": None if config == "configured" else "configuration_missing"},
        "schedule": schedule, "execution": execution,
        "lastAttempt": last, "lastAttemptAt": created,
        "lastVerifiedOutcome": None, "availableActions": [],
        "actionReasons": {"run_once": "read_only", "set_schedule": "read_only"},
    }


def project_legacy_read(snapshot_status: object, daily_status: object,
                        observed_at: float) -> dict[str, dict]:
    """Project two supplied snapshots without opening or starting a job.

    None represents an absent attachment; invalid data is unavailable.
    This intentionally never grants admission or reports verified effects.
    """
    if not _time(observed_at):
        raise ValueError("invalid_observed_at")
    candidates = {}
    if snapshot_status is not None:
        candidates["b2"] = _candidate(snapshot_status, "b2", observed_at)
    if daily_status is not None:
        candidates["b4"] = _candidate(daily_status, "b4", observed_at)
    # Omitting a missing owner creates not_attached; explicitly malformed
    # attached snapshots create unavailable. Never conflate those conditions.
    # The already-merged exact v1 composer is the final fail-closed boundary.
    views = compose_read_catalog(candidates, observed_at)["workflows"]
    return {"b2": views[1], "b4": views[3]}
