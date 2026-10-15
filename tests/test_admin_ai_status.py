"""Regression tests for the bounded AI management read model."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from obsidian_automation.admin_ai_status import load_ai_health
from obsidian_automation.pre_review_status import ALL_STATES


UTC_NOW = datetime(2026, 10, 11, 11, 0, tzinfo=timezone.utc)


def _projection(
    generated_at: str = "2026-10-11T10:59:30Z",
    *, health: str | None = None, current_version: bool = False,
    state_overrides: dict[str, int] | None = None,
) -> bytes:
    states = {name: 0 for name in ALL_STATES}
    states.update({
        "awaiting_human_review": 2,
        "retryable_failure": 1,
        "retry_exhausted": 3,
        "blocked": 2,
        "deterministic_reject": 4,
    })
    if state_overrides:
        if not set(state_overrides) <= set(states):
            raise ValueError("unsupported test fixture state")
        states.update(state_overrides)
    if health is None:
        health = (
            "CRITICAL" if states["blocked"] or states["retry_exhausted"]
            else "WARNING" if states["retryable_failure"] else "OK"
        )
    payload: dict[str, object] = {
        "record_version": 2 if current_version else 1,
        "authority": "orchestration_status_projection_only",
        "generated_at": generated_at,
        "pipeline_health": health,
        "reasons": ["UNTRUSTED_REASON_CANARY"],
        "current_jobs": sum(states.values()),
        "states": states,
        "review_wait": {
            "count": 2, "oldest_age_seconds": 61,
            "reminder_after_seconds": 86400, "reminder_due": False,
        },
        "backpressure": {"threshold": 8, "active": False},
    }
    if current_version:
        payload["planner_cadence"] = {
            "eligible": False, "interval_seconds": 10800,
            "reason": "active_wait", "awaiting_human_review": 2,
            "last_submission_at": None,
            "next_eligible_at": generated_at,
            "last_selection_policy": None,
            "last_objective_policy": None,
            "last_novelty_skip_at": None,
            "last_novelty_skip_reason": None,
        }
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode()


def _write(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o600)
    return path


@pytest.mark.parametrize("current_version", [False, True])
def test_valid_status_provides_only_sanitized_fresh_counts(
    tmp_path: Path, current_version: bool,
) -> None:
    src = _write(tmp_path / "status" / "projection.json", _projection(
        current_version=current_version
    ))
    value = load_ai_health(path=src, now=UTC_NOW)
    assert value == {
        "freshness": "fresh",
        "health": "critical",
        "generated_at": "2026-10-11T10:59:30Z",
        "age_seconds": 30,
        "review_count": 2,
        "operational_failures": 5,
        "pending_retry_failures": 1,
        "incidents": [
            {"code": "blocked_generations", "severity": "critical", "affected": 2},
            {"code": "retry_exhausted_generations", "severity": "critical", "affected": 3},
            {"code": "retryable_failure_pending", "severity": "warning", "affected": 1},
        ],
        "deterministic_rejections": 4,
        "backpressure": False,
        "review_reminder_due": False,
    }
    encoded = json.dumps(value)
    assert "UNTRUSTED_REASON_CANARY" not in encoded
    assert "planner_cadence" not in encoded
    assert "states" not in encoded


def test_critical_projection_is_not_collapsed_to_healthy(tmp_path: Path) -> None:
    src = _write(tmp_path / "projection.json", _projection(health="CRITICAL"))
    assert load_ai_health(path=src, now=UTC_NOW)["health"] == "critical"


def test_stale_projection_cannot_show_old_review_count_as_current(tmp_path: Path) -> None:
    src = _write(tmp_path / "projection.json", _projection(
        "2026-10-11T10:44:59Z"
    ))
    val = load_ai_health(path=src, now=UTC_NOW)
    assert val["freshness"] == "stale"
    assert val["health"] == "unknown"
    assert val["review_count"] is None
    assert val["operational_failures"] is None
    assert val["age_seconds"] == 901


def test_near_boundary_and_small_clock_skew(tmp_path: Path) -> None:
    src = _write(tmp_path / "projection.json", _projection(
        "2026-10-11T10:45:01Z"
    ))
    assert load_ai_health(path=src, now=UTC_NOW)["freshness"] == "fresh"
    src.write_bytes(_projection("2026-10-11T11:00:30Z"))
    val = load_ai_health(path=src, now=UTC_NOW)
    assert val["freshness"] == "fresh"
    assert val["age_seconds"] == 0


def test_future_projection_is_invalid(tmp_path: Path) -> None:
    src = _write(tmp_path / "projection.json", _projection(
        "2026-10-11T11:01:10Z"
    ))
    val = load_ai_health(path=src, now=UTC_NOW)
    assert val["freshness"] == "invalid"
    assert val["generated_at"] is None
    assert val["review_count"] is None


def test_missing_and_malformed_inputs_do_not_leak_source(tmp_path: Path) -> None:
    path = tmp_path / "no-status.json"
    assert load_ai_health(path=path, now=UTC_NOW)["freshness"] == "unavailable"
    src = _write(path, b'{"token":"private_secret"}')
    val = load_ai_health(path=src, now=UTC_NOW)
    assert val["freshness"] == "unavailable"
    assert "private_secret" not in str(val)
    src.write_bytes(b"x" * (64 * 1024 + 1))
    assert load_ai_health(path=src, now=UTC_NOW)["freshness"] == "unavailable"


def test_symlink_leaf_and_parent_are_rejected(tmp_path: Path) -> None:
    safe = _write(tmp_path / "real" / "projection.json", _projection())
    alias = tmp_path / "status.json"
    alias.symlink_to(safe)
    assert load_ai_health(path=alias, now=UTC_NOW)["freshness"] == "unavailable"
    alias_dir = tmp_path / "alias"
    alias_dir.symlink_to(safe.parent, target_is_directory=True)
    assert load_ai_health(
        path=alias_dir / "projection.json", now=UTC_NOW
    )["freshness"] == "unavailable"


def test_hardlink_and_writable_file_are_rejected(tmp_path: Path) -> None:
    src = _write(tmp_path / "projection.json", _projection())
    alias = tmp_path / "linked.json"
    os.link(src, alias)
    assert load_ai_health(path=src, now=UTC_NOW)["freshness"] == "unavailable"
    alias.unlink()
    src.chmod(0o666)
    assert load_ai_health(path=src, now=UTC_NOW)["freshness"] == "unavailable"


def test_directory_and_relative_path_are_rejected(tmp_path: Path) -> None:
    assert load_ai_health(path=tmp_path, now=UTC_NOW)["freshness"] == "unavailable"
    assert load_ai_health(path=Path("relative.json"), now=UTC_NOW)["freshness"] == "unavailable"
    assert load_ai_health(path=tmp_path / ".." / "fake.json", now=UTC_NOW)[
        "freshness"
    ] == "unavailable"


def test_atomic_replace_reports_consistent_new_version(tmp_path: Path) -> None:
    src = _write(tmp_path / "projection.json", _projection())
    replace = _write(tmp_path / "new.json", _projection(
        health="WARNING",
        state_overrides={"blocked": 0, "retry_exhausted": 0},
    ))
    os.replace(replace, src)
    val = load_ai_health(path=src, now=UTC_NOW)
    assert val["health"] == "warning"


def test_untrusted_timestamp_and_invalid_configuration(tmp_path: Path) -> None:
    src = _write(tmp_path / "projection.json", _projection())
    with pytest.raises(ValueError):
        load_ai_health(path=src, now=datetime(2026, 10, 11, 11))
    with pytest.raises(ValueError):
        load_ai_health(path=src, now=UTC_NOW, stale_after_seconds=0)
    with pytest.raises(ValueError):
        load_ai_health(path=src, now=UTC_NOW, stale_after_seconds=True)

def test_inconsistent_pipeline_health_is_untrusted_not_green(tmp_path: Path) -> None:
    src = _write(tmp_path / "projection.json", _projection(health="OK"))
    value = load_ai_health(path=src, now=UTC_NOW)
    assert value["freshness"] == "invalid"
    assert value["health"] == "unknown"
    assert value["review_count"] is None
    assert value["operational_failures"] is None
    assert value["incidents"] == []


def test_expected_deterministic_reject_is_not_an_error(tmp_path: Path) -> None:
    src = _write(tmp_path / "projection.json", _projection(
        state_overrides={
            "blocked": 0, "retry_exhausted": 0, "retryable_failure": 0,
            "deterministic_reject": 13,
        },
    ))
    value = load_ai_health(path=src, now=UTC_NOW)
    assert value["freshness"] == "fresh"
    assert value["health"] == "ok"
    assert value["operational_failures"] == 0
    assert value["pending_retry_failures"] == 0
    assert value["deterministic_rejections"] == 13
    assert value["incidents"] == []


def test_retryable_failure_is_visible_without_proven_retry_budget(tmp_path: Path) -> None:
    src = _write(tmp_path / "projection.json", _projection(
        state_overrides={"blocked": 0, "retry_exhausted": 0},
    ))
    value = load_ai_health(path=src, now=UTC_NOW)
    assert value["health"] == "warning"
    assert value["operational_failures"] == 0
    assert value["pending_retry_failures"] == 1
    # This state may represent a final failed attempt before the next claim:
    # the projection does not prove that another retry is available.
    assert value["incidents"] == [
        {"code": "retryable_failure_pending", "severity": "warning", "affected": 1},
    ]


def test_stale_source_does_not_expose_actionable_or_retry_counts(tmp_path: Path) -> None:
    src = _write(tmp_path / "projection.json", _projection(
        "2026-10-11T10:44:58Z",
    ))
    value = load_ai_health(path=src, now=UTC_NOW)
    assert value["freshness"] == "stale"
    assert value["health"] == "unknown"
    assert value["operational_failures"] is None
    assert value["pending_retry_failures"] is None
    assert value["incidents"] == []

@pytest.mark.parametrize("alter", [
    lambda p: p["backpressure"].update(active=True),
    lambda p: p["review_wait"].update(reminder_due=True),
    lambda p: p["review_wait"].update(oldest_age_seconds=None),
])
def test_inconsistent_derived_review_fields_are_unknown(
    tmp_path: Path, alter,
) -> None:
    record = json.loads(_projection())
    alter(record)
    src = _write(
        tmp_path / "projection.json",
        (json.dumps(record) + "\n").encode(),
    )
    value = load_ai_health(path=src, now=UTC_NOW)
    assert value["freshness"] == "invalid"
    assert value["health"] == "unknown"
    assert value["review_count"] is None
    assert value["backpressure"] is None
    assert value["incidents"] == []


def test_unhashable_health_type_does_not_raise_or_export_private_data(
    tmp_path: Path,
) -> None:
    record = json.loads(_projection())
    record["pipeline_health"] = []
    record["reasons"] = ["UNTRUSTED_CANARY"]
    src = _write(tmp_path / "projection.json", json.dumps(record).encode())
    value = load_ai_health(path=src, now=UTC_NOW)
    assert value["freshness"] == "unavailable"
    assert value["health"] == "unknown"
    assert value["review_count"] is None
    assert "UNTRUSTED_CANARY" not in str(value)

@pytest.mark.parametrize("invalid", [
    bytes([0xFF, 0xFE, 0xF0]),
    b'{"duplicate_field":1,"duplicate_field":2}',
])
def test_underlying_decoder_errors_are_unknown_and_keep_other_services_visible(
    tmp_path: Path, invalid: bytes,
) -> None:
    import http.client
    import threading
    from obsidian_automation.admin_dashboard import make_server

    src = _write(tmp_path / "projection.json", invalid)
    assert load_ai_health(path=src, now=UTC_NOW)["freshness"] == "unavailable"

    def systemd_observer():
        return [{
            "id": "test-service", "label": "Independent Systemd Observation",
            "health": "observed_success", "schedule": "scheduled",
            "latest_execution": "succeeded",
        }]

    server = make_server(
        port=0, observer=systemd_observer,
        ai_observer=lambda: load_ai_health(path=src, now=UTC_NOW),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            conn.request("GET", "/api/v1/status", headers={"Host": f"127.0.0.1:{port}"})
            response = conn.getresponse()
            data = response.read()
            assert response.status == 200
        finally:
            conn.close()
        payload = json.loads(data)
        assert payload["ai_pipeline"]["health"] == "unknown"
        assert payload["ai_pipeline"]["freshness"] == "unavailable"
        assert payload["ai_pipeline"]["review_count"] is None
        assert payload["services"][0]["id"] == "test-service"
        assert b"duplicate_field" not in data
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()
