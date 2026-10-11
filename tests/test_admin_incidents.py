from __future__ import annotations

from dataclasses import replace

import pytest

from obsidian_automation.admin_incidents import (
    collect_pipeline,
    summarize_pipeline,
    unavailable_pipeline,
)
from obsidian_automation.planner_cadence import PlannerCadenceSnapshot
from obsidian_automation.pre_review_status import ALL_STATES, PreReviewStatus

STAMP = "2026-10-11T12:00:00Z"


def scheduler_status(**states: int) -> PreReviewStatus:
    counts = {name: 0 for name in ALL_STATES}
    counts.update(states)
    review = counts["awaiting_human_review"]
    critical = bool(counts["blocked"] or counts["retry_exhausted"])
    warning = bool(counts["retryable_failure"])
    cadence = PlannerCadenceSnapshot(
        observed_at=STAMP,
        eligible=True,
        interval_seconds=3600,
        reason="eligible",
        awaiting_human_review=review,
        last_submission_at=None,
        next_eligible_at=STAMP,
        last_selection_policy=None,
        last_objective_policy=None,
        last_novelty_skip_at=None,
        last_novelty_skip_reason=None,
    )
    return PreReviewStatus(
        generated_at=STAMP,
        pipeline_health="CRITICAL" if critical else ("WARNING" if warning else "OK"),
        reasons=(
            tuple(code for state, code in (
                ("blocked", "blocked_generations"),
                ("retry_exhausted", "retry_exhausted_generations"),
            ) if counts[state])
            if critical else (("retryable_failures",) if warning else ())
        ),
        current_jobs=sum(counts.values()),
        states=counts,
        oldest_review_wait_seconds=0 if review else None,
        review_reminder_due=False,
        review_reminder_seconds=86400,
        backpressure_active=review >= 8,
        backpressure_threshold=8,
        planner_cadence=cadence,
    )


def test_normal_deterministic_rejection_never_sets_incident() -> None:
    value = summarize_pipeline(scheduler_status(
        deterministic_reject=17, awaiting_human_review=2, completed=4,
    ))
    assert value["health"] == "no_known_fault"
    assert value["incidents"] == []
    assert value["reason_codes"] == []
    assert value["normal_rejections"] == 17
    assert value["review_pending"] == 2
    assert value["observed_at"] == STAMP


def test_blocked_and_retry_exhausted_are_distinct_actionable_incidents() -> None:
    value = summarize_pipeline(scheduler_status(
        blocked=2, retry_exhausted=3, deterministic_reject=10,
    ))
    assert value["health"] == "failed"
    assert value["incidents"] == [
        {"code": "blocked_generations", "severity": "critical", "affected": 2, "active": True},
        {"code": "retry_exhausted_generations", "severity": "critical", "affected": 3, "active": True},
    ]
    assert value["normal_rejections"] == 10


def test_retryable_is_transient_not_critical() -> None:
    value = summarize_pipeline(scheduler_status(
        retryable_failure=4, deterministic_reject=5,
    ))
    assert value["health"] == "retrying"
    assert value["transient_failures"] == 4
    assert value["incidents"] == []
    assert value["reason_codes"] == ["retryable_failures"]


def test_inconsistent_health_and_malformed_counts_fail_closed() -> None:
    valid = scheduler_status(blocked=1)
    with pytest.raises(ValueError, match="inconsistent"):
        summarize_pipeline(replace(valid, pipeline_health="OK"))
    with pytest.raises(Exception):
        summarize_pipeline(replace(valid, current_jobs=7))
    with pytest.raises(ValueError, match="invalid"):
        summarize_pipeline(object())


def test_unknown_must_not_be_interpreted_as_zero_incidents() -> None:
    value = unavailable_pipeline()
    assert value["health"] == "unknown"
    assert value["review_pending"] is None
    assert value["normal_rejections"] is None
    assert value["reason_codes"] == ["pipeline_not_configured"]
    assert unavailable_pipeline("UNTRUSTED CREDENTIAL DATA")["reason_codes"] == [
        "pipeline_observation_unavailable"
    ]


def test_missing_storage_does_not_create_authoritative_state(tmp_path) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(Exception):
        collect_pipeline(missing)
    assert not missing.exists()

def test_status_endpoint_surfaces_verified_pipeline_without_nonessential_fields() -> None:
    import http.client
    import json
    import threading
    from obsidian_automation.admin_dashboard import make_server

    server = make_server(
        port=0, observer=lambda: [],
        pipeline_observer=lambda: scheduler_status(
            blocked=2, retry_exhausted=1, deterministic_reject=9,
        ),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        conn.request("GET", "/api/v1/status", headers={"Host": f"127.0.0.1:{port}"})
        res = conn.getresponse()
        payload = json.loads(res.read())
        conn.close()
        assert res.status == 200
        assert payload["authority"] == "observation_only"
        assert payload["actions"] == {"start": False, "stop": False, "trigger": False}
        assert payload["ai_pipeline"]["health"] == "failed"
        assert payload["ai_pipeline"]["normal_rejections"] == 9
        assert {i["code"] for i in payload["ai_pipeline"]["incidents"]} == {
            "blocked_generations", "retry_exhausted_generations",
        }
        assert set(payload["ai_pipeline"]) == {
            "source", "health", "reason_codes", "observed_at",
            "review_pending", "normal_rejections", "transient_failures", "incidents",
        }
        text = json.dumps(payload)
        assert "password" not in text.lower()
        assert "absolute" not in text.lower()
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


@pytest.mark.parametrize("pipeline_observer", [
    lambda: (_ for _ in ()).throw(RuntimeError("private_password=DO_NOT_LEAK")),
    lambda: {"unsafe": "private_password=DO_NOT_LEAK"},
    lambda: replace(scheduler_status(blocked=2), pipeline_health="OK"),
])
def test_pipeline_observer_failure_is_unknown_and_never_leaks(
    pipeline_observer,
) -> None:
    import http.client
    import json
    import threading
    from obsidian_automation.admin_dashboard import make_server

    server = make_server(
        port=0, observer=lambda: [], pipeline_observer=pipeline_observer,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        conn.request("GET", "/api/v1/status", headers={"Host": f"localhost:{port}"})
        res = conn.getresponse()
        data = res.read()
        conn.close()
        assert res.status == 200
        payload = json.loads(data)
        assert payload["ai_pipeline"]["health"] == "unknown"
        assert payload["ai_pipeline"]["review_pending"] is None
        assert payload["ai_pipeline"]["reason_codes"] == ["pipeline_observation_unavailable"]
        assert b"DO_NOT_LEAK" not in data
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()
