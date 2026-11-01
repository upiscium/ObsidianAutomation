"""Historical Status Writer terminal evidence: real producer schemas, no live effects."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from obsidian_automation.github_pipeline_status_history import (
    InvalidEvidence,
    parse_status_rejection,
    parse_status_success,
    summarize_status_history,
)
from obsidian_automation.github_project_status_mutation import ProjectStatusTransportResult
from obsidian_automation.github_project_status_worker import _persist_rejection

NOW = datetime(2026, 11, 1, 3, 0, tzinfo=timezone.utc)
A, B, C = "a" * 64, "b" * 64, "c" * 64


def _success(*, sha=A, when="2026-11-01T02:05:00Z", outcome="applied"):
    return ProjectStatusTransportResult(
        proposal_sha256=sha,
        project_path="10-Project/Status.md",
        repository="org/private-repository",
        expected_status="planning", desired_status="running",
        outcome=outcome,
        before_content_sha256=A,
        after_content_sha256=A if outcome == "already_desired" else B,
        completed_at=when,
    ).to_json_bytes()


def _rejection(
    *, sha=B, outcome="rejected_conflict", reason="etag_cas_conflict",
    http_status=412, when="2026-11-01T02:10:00Z",
):
    row = {
        "record_version": 1,
        "stage": "github_project_status_transport",
        "proposal_sha256": sha,
        "project": "10-Project/team/Status.md",
        "outcome": outcome,
        "reason": reason,
        "completed_at": when,
    }
    if http_status is not None:
        row["http_status"] = http_status
    return (json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def _mutate(raw, **fields):
    row = json.loads(raw)
    row.update(fields)
    return json.dumps(row).encode()


def _view(results=(), rejections=(), **kwargs):
    return summarize_status_history(
        {"results": list(results), "rejections": list(rejections)}, now=NOW, **kwargs,
    )


def test_real_status_worker_rejection_schema_is_supported(tmp_path):
    path = _persist_rejection(
        tmp_path, B, project="10-Project/team/Status.md",
        outcome="rejected_conflict", reason="etag_cas_conflict", http_status=412,
    )
    result = parse_status_rejection(path.read_bytes(), now=datetime.now(timezone.utc))
    assert result.outcome == "rejected_conflict"
    assert result.category == "cas_conflict"
    assert result.http_status == 412
    assert result.reason_code == "etag_cas_conflict"


def test_real_status_worker_canonical_conflict_has_no_http_status(tmp_path):
    path = _persist_rejection(
        tmp_path, C, project="10-Project/team/Status.md",
        outcome="rejected_conflict", reason="canonical_conflict",
    )
    result = parse_status_rejection(path.read_bytes(), now=datetime.now(timezone.utc))
    assert result.http_status is None and result.category == "cas_conflict"


def test_actual_status_transport_result_exact_schema():
    item = parse_status_success(_success(), now=NOW)
    assert item.outcome == "applied" and item.category == "verified_producer_readback"


@pytest.mark.parametrize("http_status", (401, 403))
def test_authority_rejected_is_not_transport_success(http_status):
    raw = _rejection(outcome="rejected_transport", reason="authority_rejection",
                     http_status=http_status)
    record = parse_status_rejection(raw, now=NOW)
    assert record.category == "authority_rejected"
    output = _view(rejections=[raw], source_attested=True)
    assert output["last_status_rejection"]["category"] == "authority_rejected"
    assert output["last_verified_status_readback"] is None
    assert output["overall_health"] == "unknown"


@pytest.mark.parametrize("status", (400, 404, 409, 429))
def test_deterministic_client_rejections(status):
    raw = _rejection(outcome="rejected_transport",
                     reason="http_client_rejection", http_status=status)
    assert parse_status_rejection(raw, now=NOW).category == "client_rejected"


@pytest.mark.parametrize("status", (301, 507))
def test_http_response_rejections(status):
    raw = _rejection(outcome="rejected_transport",
                     reason="http_response_rejection", http_status=status)
    assert parse_status_rejection(raw, now=NOW).category == "http_rejected"


def test_later_failure_preserves_earlier_verified_producer_readback():
    outcome = _view(results=[_success()], rejections=[_rejection()], source_attested=True)
    assert outcome["last_terminal_observation"]["category"] == "cas_conflict"
    assert outcome["last_status_rejection"]["outcome"] == "rejected_conflict"
    assert outcome["last_verified_status_readback"]["outcome"] == "applied"
    assert outcome["last_verified_status_readback"]["completed_at"] == "2026-11-01T02:05:00Z"
    assert outcome["last_attempt"] is None
    assert outcome["active_incident"] == "unknown"
    assert outcome["retrying"] == "unknown"
    assert outcome["last_verified_pipeline_success"] is None


def test_later_success_does_not_erase_or_claim_recovery_of_incident():
    output = _view(
        results=[_success(sha=A, when="2026-11-01T02:20:00Z")],
        rejections=[_rejection(sha=B, when="2026-11-01T02:10:00Z")],
        source_attested=True,
    )
    assert output["last_terminal_observation"]["outcome"] == "applied"
    assert output["last_status_rejection"]["outcome"] == "rejected_conflict"
    assert output["active_incident"] == "unknown"
    assert output["last_attempt"] is None


def test_recovered_is_historical_readback_not_current_convergence():
    output = _view(results=[_success(outcome="recovered")], source_attested=True)
    assert output["last_verified_status_readback"]["outcome"] == "recovered"
    assert output["last_verified_pipeline_success"] is None
    assert output["overall_health"] == "unknown"


def test_idempotent_noop_and_duplicate_same_raw_count_only_once():
    raw = _success(outcome="already_desired")
    output = _view(results=[raw, raw], source_attested=True)
    assert output["last_verified_status_readback"]["outcome"] == "already_desired"
    assert output["last_status_rejection"] is None
    assert output["history_completeness"] == "terminal_only"


def test_tied_terminal_times_do_not_invent_a_latest_order():
    output = _view(
        results=[_success(sha=A, when="2026-11-01T02:10:00Z")],
        rejections=[_rejection(sha=B, when="2026-11-01T02:10:00Z")],
        source_attested=True,
    )
    assert output["last_terminal_observation"]["category"] == "ambiguous_order"
    assert output["last_terminal_observation"]["outcome"] is None
    assert output["last_verified_status_readback"]["outcome"] == "applied"
    assert output["active_incident"] == "unknown"


def test_bad_source_is_untrusted_not_completed_success():
    output = _view(results=[_success()])
    assert output["evidence_state"] == "untrusted"
    assert output["last_terminal_observation"] is None
    assert output["last_verified_status_readback"] is None
    assert output["last_status_rejection"] is None
    assert output["overall_health"] == "unknown"


def test_no_records_do_not_imply_idle():
    output = _view(source_attested=True)
    assert output["evidence_state"] == "missing"
    assert output["last_attempt"] is None
    assert output["active_incident"] == "unknown"


def test_staleness_is_about_terminal_evidence_not_current_vault():
    output = _view(results=[_success(when="2026-10-29T02:00:00Z")],
                   source_attested=True)
    assert output["freshness"] == "stale"
    assert output["overall_health"] == "unknown"


def test_small_clock_skew_is_not_green():
    output = _view(results=[_success(when="2026-11-01T03:01:00Z")],
                   source_attested=True)
    assert output["freshness"] == "future_skew"
    assert output["last_verified_pipeline_success"] is None


def test_no_secret_or_path_or_sha_exposed_in_model():
    output = _view(results=[_success()], rejections=[_rejection()],
                   source_attested=True)
    serialized = json.dumps(output, sort_keys=True)
    for token in ("10-Project", "org/private-repository", A, B, "team/Status.md", "project_path"):
        assert token not in serialized


@pytest.mark.parametrize("bad", (
    _mutate(_rejection(), stage="other"),
    _mutate(_rejection(), record_version=True),
    _mutate(_rejection(), project="../other/path"),
    _mutate(_rejection(), project="10-Project/\\ud800.md"),
    _mutate(_rejection(), outcome="queued"),
    _mutate(_rejection(), outcome=[]),
    _mutate(_rejection(), reason="raw exception stack trace"),
    _mutate(_rejection(), reason={}),
    _mutate(_rejection(), proposal_sha256="uppercase"),
    _mutate(_rejection(), completed_at="2099-01-01T00:00:00Z"),
    _mutate(_rejection(), completed_at="0001-01-01T00:00:00+23:59"),
    _mutate(_rejection(), http_status=True),
    _mutate(_rejection(), http_status=200),
    _mutate(_rejection(), http_status=502),
    _mutate(_rejection(), outcome="rejected_transport"),
    _mutate(_rejection(), outcome="rejected_transport", reason="authority_rejection", http_status=412),
    _mutate(_rejection(), outcome="rejected_transport", reason="http_client_rejection", http_status=503),
    _mutate(_rejection(), outcome="rejected_transport", reason="http_client_rejection", http_status=412),
    _mutate(_rejection(), outcome="rejected_transport", reason="http_response_rejection", http_status=500),
    _mutate(_rejection(), outcome="rejected_transport", reason="http_response_rejection", http_status=503),
    _mutate(_rejection(), reason="canonical_conflict", http_status=412),
    _mutate(_rejection(), reason="etag_cas_conflict", http_status=401),
    b"{}",
    b"{",
    b"\xff",
    _rejection() + b" " * 8192,
    b'{"record_version":1,"record_version":1}',
    b'{"record_version":NaN}',
))
def test_malformed_rejection_fails_closed(bad):
    with pytest.raises(InvalidEvidence):
        parse_status_rejection(bad, now=NOW)
    output = _view(rejections=[bad], source_attested=True)
    assert output["evidence_state"] == "invalid"
    assert output["last_terminal_observation"] is None


def test_conflicting_terminal_for_same_proposal_is_invalid():
    output = _view(results=[_success(sha=A)],
                   rejections=[_rejection(sha=A)], source_attested=True)
    assert output["evidence_state"] == "invalid"
    assert output["last_verified_status_readback"] is None


def test_same_proposal_with_conflicting_success_bytes_is_invalid():
    output = _view(results=[_success(sha=A), _success(sha=A, outcome="recovered")],
                   source_attested=True)
    assert output["evidence_state"] == "invalid"


def test_capacity_bound_is_fail_closed():
    output = _view(results=[_success()] * 2049, source_attested=True)
    assert output["evidence_state"] == "capacity_exceeded"
    assert output["last_verified_status_readback"] is None


@pytest.mark.parametrize("bad_kwargs", (
    {"source_attested":1}, {"fresh_seconds":True}, {"fresh_seconds":0},
))
def test_invalid_arguments_are_explicit(bad_kwargs):
    with pytest.raises(ValueError):
        _view(**bad_kwargs)


def test_non_sequence_and_unexpected_kind_is_not_authoritative():
    output = summarize_status_history({"results": "bad", "rejections": []}, now=NOW,
                                      source_attested=True)
    assert output["evidence_state"] == "invalid"
    with pytest.raises(ValueError):
        summarize_status_history({"results": [], "rejections": [], "extra": []},
                                 now=NOW, source_attested=True)
