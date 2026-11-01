"""Synthetic and producer-schema-aligned fixtures for #316 source-only v0."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from obsidian_automation.github_pipeline_evidence import (
    InvalidEvidence, parse_producer_receipt, summarize_candidate_receipts,
)
from obsidian_automation.github_project_status_mutation import (
    ProjectStatusTransportResult,
)
from obsidian_automation.github_project_overview import (
    ProjectOverviewTransportResult,
)

NOW = datetime(2026, 11, 1, 3, 0, tzinfo=timezone.utc)
S = "a" * 64
T = "b" * 64
U = "c" * 64


def _status(outcome="applied"):
    return ProjectStatusTransportResult(
        proposal_sha256=S, project_path="10-Project/example.md",
        repository="example/project", expected_status="planning",
        desired_status="running", outcome=outcome,
        before_content_sha256=S,
        after_content_sha256=S if outcome == "already_desired" else T,
        completed_at="2026-11-01T02:59:00Z",
    ).to_json_bytes()


def _overview(outcome="updated"):
    return ProjectOverviewTransportResult(
        proposal_sha256=T, project_path="10-Project/example.md",
        status_path="10-Project/Status.md",
        repository="example/project", outcome=outcome,
        before_content_sha256=(
            None if outcome == "created" else
            T if outcome == "already_desired" else S
        ), after_content_sha256=T,
        completed_at="2026-11-01T02:58:00Z",
    ).to_json_bytes()


@pytest.mark.parametrize("outcome", ("applied", "recovered", "already_desired"))
def test_real_status_producer_schema(outcome):
    result = parse_producer_receipt(_status(outcome), kind="status", now=NOW)
    assert result.kind == "status"
    assert result.changed == (outcome != "already_desired")


@pytest.mark.parametrize("outcome", ("created", "updated", "recovered", "already_desired"))
def test_real_overview_producer_schema(outcome):
    result = parse_producer_receipt(_overview(outcome), kind="overview", now=NOW)
    assert result.kind == "overview"
    assert result.changed == (outcome != "already_desired")


def _mutate(raw, **updates):
    obj = json.loads(raw)
    obj.update(updates)
    return json.dumps(obj).encode()


@pytest.mark.parametrize("kind,raw", (
    ("status", _status()), ("overview", _overview()),
))
def test_no_trust_from_presence_alone(kind, raw):
    records = {"status": [], "overview": []}
    records[kind] = [raw]
    model = summarize_candidate_receipts(records, now=NOW)
    assert model[kind]["evidence_state"] == "untrusted"
    assert model[kind]["last_outcome"] is None
    assert model["last_verified_pipeline_success"] is None
    assert model["overall_health"] == "unknown"
    assert model["active_incident"] == "unknown"


def test_attested_producer_readback_remains_distinct_from_current_health():
    result = summarize_candidate_receipts(
        {"status": [_status()], "overview": [_overview()]},
        now=NOW, source_attested=True,
    )
    assert result["status"] == {
        "evidence_state": "producer_readback",
        "last_producer_readback_at": "2026-11-01T02:59:00Z",
        "last_outcome": "applied", "freshness": "fresh",
    }
    assert result["overview"]["last_outcome"] == "updated"
    assert result["last_verified_pipeline_success"] is None
    assert result["overall_health"] == "unknown"
    output = json.dumps(result)
    for private in ("example/project", "example.md", S, T, "https://", "token"):
        assert private not in output


@pytest.mark.parametrize("bad", (
    _mutate(_status(), unknown="new-field"),
    _mutate(_status(), stage="github_project_overview_transport"),
    _mutate(_status(), record_version=True),
    _mutate(_status(), before_content_sha256=T, after_content_sha256=T),
    _mutate(_status(), completed_at="2026-11-01T02:59:00"),
    _mutate(_status(), completed_at="2099-01-01T00:00:00Z"),
    _mutate(_status(), completed_at="0001-01-01T00:00:00+23:59"),
    _mutate(_status(), outcome="queued"),
    _mutate(_status(), outcome=[]),
    _mutate(_status(), expected_status={}),
    _mutate(_status(), desired_status=[]),
    _mutate(_status(), expected_status="running"),
    _mutate(_status(), project_path="10-Project/\ud800.md"),
    _mutate(_status(), project_path="../private/token"),
    _mutate(_status(), repository="https://example.com/secret"),
    _mutate(_status(), proposal_sha256="NOT_SHA"),
    _mutate(_status(), desired_status="cancelled"),
    _status() + b" " * 8192,
    b'{"record_version":1,"record_version":1}',
    b'{"record_version":NaN}',
    b"\xff",
))
def test_invalid_status_receipt_is_rejected(bad):
    with pytest.raises(InvalidEvidence):
        parse_producer_receipt(bad, kind="status", now=NOW)


@pytest.mark.parametrize("bad", (
    _mutate(_overview(), status_path="/etc/passwd"),
    _mutate(_overview(), status_path="10-Project/other/Status.md"),
    _mutate(_overview(), project_path="10-Project/\ud800.md"),
    _mutate(_overview(), completed_at="0001-01-01T00:00:00+23:59"),
    _mutate(_overview(), before_content_sha256=None, outcome="updated"),
    _mutate(_overview("created"), before_content_sha256=S),
    _mutate(_overview("already_desired"), after_content_sha256=U),
    _mutate(_overview(), outcome="applied"),
))
def test_invalid_overview_receipt_is_rejected(bad):
    with pytest.raises(InvalidEvidence):
        parse_producer_receipt(bad, kind="overview", now=NOW)


def test_invalid_receipt_makes_only_affected_stream_unknown():
    result = summarize_candidate_receipts(
        {"status": [_status(), b'{}'], "overview": [_overview()]},
        now=NOW, source_attested=True,
    )
    assert result["status"]["evidence_state"] == "invalid"
    assert result["status"]["last_outcome"] is None
    assert result["overview"]["evidence_state"] == "producer_readback"


def test_no_evidence_never_proves_success():
    result = summarize_candidate_receipts(
        {"status": [], "overview": []}, now=NOW, source_attested=True,
    )
    assert result["status"]["evidence_state"] == "missing"
    assert result["overview"]["evidence_state"] == "missing"
    assert result["last_verified_pipeline_success"] is None


def test_stale_and_future_clock_are_distinct_from_success():
    old = _mutate(
        _status(), completed_at=(NOW - timedelta(days=3)).isoformat()
    )
    future = _mutate(
        _overview(), completed_at=(NOW + timedelta(seconds=20)).isoformat()
    )
    result = summarize_candidate_receipts(
        {"status": [old], "overview": [future]},
        now=NOW, source_attested=True,
    )
    assert result["status"]["freshness"] == "stale"
    assert result["overview"]["freshness"] == "future_skew"
    assert result["overall_health"] == "unknown"


def test_duplicate_conflicting_results_are_invalid():
    records = {"status": [_status(), _mutate(_status(), outcome="recovered")], "overview": []}
    result = summarize_candidate_receipts(records, now=NOW, source_attested=True)
    assert result["status"]["evidence_state"] == "invalid"


def test_bound_count_and_invalid_arguments_fail_closed():
    result = summarize_candidate_receipts(
        {"status": [_status()] * 2049, "overview": []},
        now=NOW, source_attested=True,
    )
    assert result["status"]["evidence_state"] == "invalid"
    with pytest.raises(ValueError):
        summarize_candidate_receipts({"status": []}, now=NOW)
    with pytest.raises(ValueError):
        summarize_candidate_receipts({"status": [], "overview": []}, now=NOW, source_attested=1)


@pytest.mark.parametrize("kind,raw", (
    ("status", _mutate(_status(), expected_status="running")),
    ("status", _mutate(_status(), project_path="10-Project/\ud800.md")),
    ("overview", _mutate(_overview(), project_path="10-Project/\ud800.md")),
))
def test_invalid_producer_invariants_fail_closed_in_attested_projection(kind, raw):
    inputs = {"status": [_status()], "overview": [_overview()]}
    inputs[kind] = [raw]
    result = summarize_candidate_receipts(
        inputs, now=NOW, source_attested=True,
    )
    assert result[kind]["evidence_state"] == "invalid"
    assert result[kind]["last_outcome"] is None
    assert result["overall_health"] == "unknown"
    assert result["last_verified_pipeline_success"] is None
