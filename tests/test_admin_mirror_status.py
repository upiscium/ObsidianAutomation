"""Bounded read-only mirror diagnostic contract for the admin Dashboard."""
from __future__ import annotations

import pytest

from obsidian_automation.admin_mirror_status import load_mirror_observation


def observation(**updates):
    value = {
        "record_version": 1,
        "source": "systemd_latest_attempt",
        "health": "OK",
        "latest_attempt": "succeeded",
        "last_success_age_seconds": 12,
        "running_seconds": None,
        "reasons": [],
        "durable_success_history": False,
        "atomic_snapshot": False,
    }
    value.update(updates)
    return value


def test_recent_success_is_only_systemd_evidence():
    result = load_mirror_observation(observer=observation)
    assert result == {
        "source": "systemd_latest_attempt",
        "health": "ok",
        "latest_attempt": "succeeded",
        "last_success_age_seconds": 12,
        "running_seconds": None,
        "reasons": [],
        "durable_success_history": False,
        "remote_verified": False,
    }


def test_aging_and_stale_success_remain_visible():
    warning = load_mirror_observation(observer=lambda: observation(
        health="WARNING", reasons=["refresh_success_aging"],
        last_success_age_seconds=950,
    ))
    critical = load_mirror_observation(observer=lambda: observation(
        health="CRITICAL", reasons=["refresh_success_stale"],
        last_success_age_seconds=1950,
    ))
    assert warning["health"] == "warning"
    assert critical["health"] == "critical"
    assert critical["last_success_age_seconds"] == 1950
    assert not critical["remote_verified"]


def test_real_failure_and_in_progress_are_not_recast_as_success():
    failure = load_mirror_observation(observer=lambda: observation(
        health="CRITICAL", latest_attempt="failed",
        last_success_age_seconds=None, reasons=["latest_attempt_failed"],
    ))
    running = load_mirror_observation(observer=lambda: observation(
        health="UNKNOWN", latest_attempt="in_progress",
        last_success_age_seconds=None, running_seconds=4,
        reasons=["run_in_progress_success_history_unavailable"],
    ))
    assert failure["latest_attempt"] == "failed"
    assert failure["last_success_age_seconds"] is None
    assert running["running_seconds"] == 4
    assert running["health"] == "unknown"


@pytest.mark.parametrize("changes", [
    {"record_version": True},
    {"source": "private_vault_file"},
    {"durable_success_history": True},
    {"atomic_snapshot": True},
    {"health": "HEALTHY"},
    {"health": "OK", "latest_attempt": "failed", "last_success_age_seconds": None},
    {"health": "OK", "reasons": ["refresh_success_aging"]},
    {"latest_attempt": "succeeded", "last_success_age_seconds": -1},
    {"latest_attempt": "succeeded", "last_success_age_seconds": True},
    {"latest_attempt": "failed", "last_success_age_seconds": 9},
    {"latest_attempt": "succeeded", "running_seconds": 3},
    {"reasons": "latest_attempt_failed"},
    {"reasons": ["unexpected_token=VERY_SENSITIVE_VALUE"]},
    {"reasons": ["timer_not_loaded", "timer_not_loaded"]},
    {"reasons": ["timer_not_loaded"] * 17},
])
def test_untrusted_or_contradictory_classification_fails_closed(changes):
    result = load_mirror_observation(observer=lambda: observation(**changes))
    assert result["health"] == "unknown"
    assert result["last_success_age_seconds"] is None
    assert result["reasons"] == []
    assert "VERY_SENSITIVE_VALUE" not in str(result)


def test_observer_error_never_returns_raw_exception():
    def raises():
        raise OSError("secret=REAL_AUTH_CREDENTIAL")

    result = load_mirror_observation(observer=raises)
    assert result["health"] == "unknown"
    assert "REAL_AUTH_CREDENTIAL" not in str(result)
    assert result["remote_verified"] is False


def test_fixed_reason_codes_and_no_free_form_diagnostics():
    result = load_mirror_observation(observer=lambda: observation(
        health="CRITICAL", latest_attempt="unknown",
        last_success_age_seconds=None,
        reasons=["timer_not_active", "timer_not_enabled"],
    ))
    assert result["reasons"] == ["timer_not_active", "timer_not_enabled"]
    assert result["durable_success_history"] is False
