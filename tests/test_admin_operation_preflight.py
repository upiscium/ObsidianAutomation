from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from obsidian_automation.admin_operation_preflight import (
    OperationCandidate, PreflightRejected, preflight_operation,
)

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
RID = "12345678-1234-4234-8234-123456789abc"


def params(action="pause", sid="github-sync", timestamp=None):
    timestamp = timestamp or NOW.isoformat(timespec="seconds")
    request = {
        "version": 1, "request_id": RID, "service_id": sid,
        "action": action, "observed_at": timestamp,
    }
    source = {
        "generated_at": timestamp,
        "services": [{
            "id": sid, "schedule": "scheduled", "latest_execution": "succeeded",
            "health": "observed_success",
        }],
    }
    return request, source


def candidate(r=None, s=None, **kwargs):
    base_r, base_s = params()
    return preflight_operation(
        base_r if r is None else r, base_s if s is None else s,
        now=NOW, deployment_quiescent=True, **kwargs,
    )


def test_candidate_never_claims_authority_or_contains_command():
    result = candidate()
    assert isinstance(result, OperationCandidate)
    assert result.authority == "requires_broker_live_recheck"
    assert result.as_public_dict() == {
        "request_id": RID, "service_id": "github-sync", "action": "pause",
        "observed_at": NOW.isoformat(timespec="seconds"),
        "authority": "requires_broker_live_recheck",
    }
    assert not any("systemctl" in value for value in result.as_public_dict().values())


def test_fails_closed_without_deployment_quiescence():
    r, s = params()
    with pytest.raises(PreflightRejected, match="deployment_authority_unverified"):
        preflight_operation(r, s, now=NOW)


@pytest.mark.parametrize("patch,reason", [
    ({"service_id": "sshd"}, "unknown_service"),
    ({"service_id": "core-promotion; reboot"}, "unknown_service"),
    ({"action": "stop"}, "unknown_action"),
    ({"action": "activate"}, "unknown_action"),
    ({"action": "run_once; rm"}, "unknown_action"),
    ({"request_id": "not-uuid"}, "invalid_request_id"),
    ({"version": True}, "invalid_version"),
    ({"version": 2}, "invalid_version"),
    ({"observed_at": "2026-10-15T15:00:00+03:00"}, "stale_observation"),
])
def test_malformed_requests_rejected(patch, reason):
    r, s = params()
    r.update(patch)
    with pytest.raises(PreflightRejected) as e:
        candidate(r, s)
    assert e.value.reason == reason


def test_extra_secret_field_rejected_with_no_echo():
    r, s = params()
    r["password"] = "canary-sensitive-value"
    with pytest.raises(PreflightRejected) as e:
        candidate(r, s)
    assert str(e.value) == "invalid_request"
    assert "canary" not in str(e.value)


@pytest.mark.parametrize("age_seconds", [31, 3600, -1])
def test_stale_or_future_state_rejected(age_seconds):
    r, s = params(timestamp=(NOW-timedelta(seconds=age_seconds)).isoformat(timespec="seconds"))
    with pytest.raises(PreflightRejected, match="stale_observation"):
        candidate(r, s)


def test_allows_exact_thirty_second_boundary():
    r, s = params(timestamp=(NOW-timedelta(seconds=30)).isoformat(timespec="seconds"))
    assert candidate(r, s).service_id == "github-sync"


@pytest.mark.parametrize("schedule,action,allowed", [
    ("scheduled", "pause", True), ("paused", "pause", False),
    ("paused", "resume", True), ("scheduled", "resume", False),
    ("scheduled", "run_once", False), ("paused", "run_once", True),
    ("unknown", "pause", False), ("triggering", "run_once", False),
])
def test_action_requires_expected_state(schedule, action, allowed):
    r, s = params(action=action)
    s["services"][0]["schedule"] = schedule
    if schedule == "paused":
        s["services"][0]["health"] = "paused"
    if allowed:
        assert candidate(r, s).action == action
    else:
        with pytest.raises(PreflightRejected):
            candidate(r, s)


@pytest.mark.parametrize("execution", ["running", "unknown", "not_installed"])
def test_no_action_when_execution_is_unsafe(execution):
    r, s = params()
    s["services"][0]["latest_execution"] = execution
    with pytest.raises(PreflightRejected, match="unverifiable_service"):
        candidate(r, s)


def test_duplicate_or_missing_observation_fails_closed():
    r, s = params()
    s["services"].append(dict(s["services"][0]))
    with pytest.raises(PreflightRejected, match="unverifiable_service"):
        candidate(r, s)
    s["services"] = []
    with pytest.raises(PreflightRejected, match="unverifiable_service"):
        candidate(r, s)


def test_invalid_time_or_naive_clock_fails_closed():
    r, s = params(timestamp="2026-13-15T12:00:00Z")
    with pytest.raises(PreflightRejected, match="invalid_observation_time"):
        candidate(r, s)
    r, s = params()
    with pytest.raises(PreflightRejected, match="invalid_clock"):
        preflight_operation(r, s, now=datetime(2026, 10, 15, 12), deployment_quiescent=True)


def test_preflight_is_not_connected_to_http_mutations():
    from obsidian_automation.admin_dashboard import make_server
    import http.client
    server = make_server(port=0, observer=lambda: [], ai_observer=lambda: {})
    assert server.server_address[0] == "127.0.0.1"
    try:
        import threading
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        c = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
        c.request("POST", "/api/v1/operations", headers={"Host": f"127.0.0.1:{server.server_address[1]}"})
        res = c.getresponse()
        assert res.status == 405
        res.read()
        c.close()
        server.shutdown()
        thread.join(timeout=3)
    finally:
        server.server_close()


@pytest.mark.parametrize("deployment_quiescent", [False, None, "true", 1, [True], {}])
def test_quiescence_requires_actual_true_boolean(deployment_quiescent):
    r, s = params()
    with pytest.raises(PreflightRejected, match="deployment_authority_unverified"):
        preflight_operation(
            r, s, now=NOW, deployment_quiescent=deployment_quiescent,
        )


@pytest.mark.parametrize("schedule,execution,health", [
    ("scheduled", "succeeded", "paused"),
    ("scheduled", "succeeded", "unverified"),
    ("scheduled", "failed", "observed_success"),
    ("scheduled", "not_observed", "observed_success"),
    ("paused", "succeeded", "observed_success"),
    ("paused", "not_observed", "unverified"),
])
def test_contradictory_known_states_rejected(schedule, execution, health):
    r, s = params()
    s["services"][0].update(
        schedule=schedule, latest_execution=execution, health=health,
    )
    with pytest.raises(PreflightRejected, match="inconsistent_service_state"):
        candidate(r, s)


def test_failed_execution_can_only_have_failed_health():
    r, s = params()
    s["services"][0].update(latest_execution="failed", health="failed")
    assert candidate(r, s).action == "pause"
