"""Sanitized read-only AI mirror latest-attempt diagnostic for the Dashboard.

This is *not* durable pipeline success or remote Nextcloud verification.
It only projects the existing, fixed-unit systemd mirror diagnostic.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping

from .mirror_status import StatusError, collect_status

_REASONS = frozenset({
    "timer_not_loaded", "service_not_loaded", "timer_not_active",
    "timer_not_enabled", "unexpected_service_type",
    "run_in_progress_success_history_unavailable",
    "latest_attempt_failed", "unexpected_service_state",
    "no_completed_attempt_observed", "inconsistent_monotonic_timestamps",
    "refresh_success_stale", "refresh_success_aging",
    "completion_not_proven",
})
_HEALTH = frozenset({"OK", "WARNING", "CRITICAL", "UNKNOWN"})
_ATTEMPTS = frozenset({"unknown", "in_progress", "failed", "not_observed", "succeeded"})
_MAX_SECONDS = 10 * 366 * 24 * 3600


def _unknown() -> dict[str, object]:
    return {
        "source": "systemd_latest_attempt",
        "health": "unknown",
        "latest_attempt": "unknown",
        "last_success_age_seconds": None,
        "running_seconds": None,
        "reasons": [],
        "durable_success_history": False,
        "remote_verified": False,
    }


def _duration(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 0 <= value <= _MAX_SECONDS:
        raise ValueError("invalid duration")
    return value


def load_mirror_observation(
    *, observer: Callable[[], Mapping[str, object]] = collect_status
) -> dict[str, object]:
    """Expose fixed classifications only, with fail-closed unknown fallback.

    No paths, raw properties, credentials, arbitrary text or commands enter
    this projection. The underlying mirror_status observer has a fixed
    timer/service registry and performs no writes.
    """
    try:
        report = observer()
        if not isinstance(report, Mapping):
            raise ValueError("invalid diagnostic")
        if (
            type(report.get("record_version")) is not int
            or report["record_version"] != 1
            or report.get("source") != "systemd_latest_attempt"
            or report.get("durable_success_history") is not False
            or report.get("atomic_snapshot") is not False
        ):
            raise ValueError("untrusted diagnostic")
        health = report.get("health")
        attempt = report.get("latest_attempt")
        reasons = report.get("reasons")
        if (
            type(health) is not str or health not in _HEALTH
            or type(attempt) is not str or attempt not in _ATTEMPTS
            or not isinstance(reasons, list)
            or len(reasons) > 16
            or any(type(r) is not str or r not in _REASONS for r in reasons)
            or len(set(reasons)) != len(reasons)
        ):
            raise ValueError("invalid classifications")
        age = _duration(report.get("last_success_age_seconds"))
        runtime = _duration(report.get("running_seconds"))
        if (
            (age is not None and attempt != "succeeded")
            or (runtime is not None and attempt != "in_progress")
            or (health == "OK" and (attempt != "succeeded" or reasons))
        ):
            raise ValueError("contradictory diagnostic")
        return {
            "source": "systemd_latest_attempt",
            "health": health.lower(),
            "latest_attempt": attempt,
            "last_success_age_seconds": age,
            "running_seconds": runtime,
            "reasons": list(reasons),
            "durable_success_history": False,
            "remote_verified": False,
        }
    except (StatusError, OSError, ValueError, TypeError, KeyError):
        return _unknown()
