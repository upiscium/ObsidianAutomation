"""Existing pre-review Status/Dashboard compatibility for typed External Source.

No real ChatGPT Export, live LLM or production Job DB. Synthetic-only
Source/Review fixtures exercise admission, pending, disposition, terminal.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_chatgpt_external_review_adapter import completed_source, decide
from test_chatgpt_external_worker_e2e import setup
from test_pre_review_status import _root, _submit

from obsidian_automation.admin_ai_status import load_ai_health
from obsidian_automation.pre_review_reconcile import reconcile_post_review
from obsidian_automation.pre_review_status import (
    ALL_STATES,
    EXTERNAL_STATES,
    LEGACY_STATES,
    PreReviewStatusError,
    build_status,
    parse_status,
)


def _now():
    return datetime.now(UTC) + timedelta(seconds=10)


def test_legacy_only_status_keeps_version2_exact_state_shape(tmp_path):
    root = _root(tmp_path)
    _submit(root, "Only original Vault Context", "2026-10-21T00:00:00Z")
    status = build_status(root, now=_now())
    assert status.record_version == 2
    assert status.external_jobs == 0
    assert status.external_review_waiting == 0
    data = json.loads(status.to_json_bytes())
    assert data["record_version"] == 2
    assert set(data["states"]) == set(LEGACY_STATES)
    assert "external_source" not in data
    assert parse_status(status.to_json_bytes()) == status


def test_external_admitted_status_switches_to_typed_v3(tmp_path):
    ai, _, _, staged, _ = setup(tmp_path)
    status = build_status(ai, now=_now())
    data = json.loads(status.to_json_bytes())
    assert data["record_version"] == 3
    assert set(data["states"]) == set(ALL_STATES)
    assert data["states"]["external_admitted"] == 2
    assert data["states"]["awaiting_human_review"] == 0
    assert data["external_source"] == {"jobs": 2, "review_waiting": 0}
    assert status.current_jobs == 2
    assert status.external_jobs == 2
    assert parse_status(status.to_json_bytes()) == status
    assert staged["state"] == "external_admitted"
    assert all(name in data["states"] for name in EXTERNAL_STATES)
    assert str(staged["job_id"]) not in status.to_json_bytes().decode()


def test_existing_admin_dashboard_accepts_v3_without_leaking_source(tmp_path):
    ai, _, preview, staged, _ = setup(tmp_path)
    status = build_status(ai, now=_now())
    path = tmp_path / "pre-review-status.json"
    path.write_bytes(status.to_json_bytes())
    path.chmod(0o600)
    value = load_ai_health(path=path, now=_now())
    assert value["freshness"] == "fresh"
    assert value["health"] == "ok"
    assert value["review_count"] == 0
    assert value["backpressure"] is False
    serialized = json.dumps(value)
    assert "source_hmac" not in serialized
    assert "source_revision" not in serialized
    assert staged["job_id"] not in serialized
    assert preview.source_hmac not in serialized


def test_actual_external_pending_and_shared_review_backpressure_count(tmp_path):
    ai, _, _, _, _, _, _, _ = completed_source(
        tmp_path
    )
    status = build_status(ai, now=_now(), backpressure_threshold=1)
    data = json.loads(status.to_json_bytes())
    assert data["record_version"] == 3
    assert data["states"]["external_admitted"] == 1
    assert data["states"]["awaiting_human_review"] == 1
    assert data["external_source"] == {"jobs": 2, "review_waiting": 1}
    assert data["review_wait"]["count"] == 1
    assert data["backpressure"]["active"] is True
    assert data["planner_cadence"]["awaiting_human_review"] == 1
    assert status.external_review_waiting == 1
    assert parse_status(status.to_json_bytes()) == status


def test_shared_review_disposition_stays_typed_across_reconciliation(tmp_path):
    ai, private, preview, staged, _, receipt, packet, _ = completed_source(
        tmp_path
    )
    acted = decide(ai, private, preview, staged, receipt, packet, decision="reject")
    assert acted.state == "external_review_recorded"
    intermediate = build_status(ai, now=_now())
    assert intermediate.record_version == 3
    assert intermediate.states["external_review_recorded"] == 1
    assert intermediate.states["awaiting_human_review"] == 0
    assert intermediate.external_jobs == 2
    assert intermediate.external_review_waiting == 0
    assert parse_status(intermediate.to_json_bytes()) == intermediate
    reconcile_post_review(ai)
    terminal = build_status(ai, now=_now())
    assert terminal.record_version == 3
    assert terminal.states["external_review_recorded"] == 0
    assert terminal.states["human_rejected"] == 1
    assert terminal.external_jobs == 2
    assert terminal.external_review_waiting == 0
    assert parse_status(terminal.to_json_bytes()) == terminal


@pytest.mark.parametrize("alteration", [
    lambda d: d["states"].update({"rogue_external": 1}),
    lambda d: d["states"].pop("external_admitted"),
    lambda d: d["states"].update({"external_admitted": True}),
    lambda d: d["external_source"].update({"jobs": True}),
    lambda d: d["external_source"].update({"jobs": 99}),
    lambda d: d["external_source"].update({"review_waiting": 99}),
    lambda d: d["external_source"].update({"secret": "private"}),
    lambda d: d.update({"record_version": 2}),
])
def test_v3_untrusted_shape_or_counts_fail_closed(tmp_path, alteration):
    ai, _, _, _, _ = setup(tmp_path)
    data = json.loads(build_status(ai, now=_now()).to_json_bytes())
    alteration(data)
    with pytest.raises(PreReviewStatusError):
        parse_status((json.dumps(data, separators=(",", ":")) + "\n").encode())


def test_v3_requires_nonzero_external_jobs_and_no_private_ids(tmp_path):
    ai, _, preview, staged, _ = setup(tmp_path)
    data = json.loads(build_status(ai, now=_now()).to_json_bytes())
    data["external_source"]["jobs"] = 0
    with pytest.raises(PreReviewStatusError):
        parse_status(json.dumps(data).encode())
    raw = json.dumps(data)
    assert preview.source_hmac not in raw
    assert preview.revision_sha256 not in raw
    assert staged["job_id"] not in raw


def test_versioned_schema_keeps_old_contract_immutable():
    legacy = json.loads(
        Path("schemas/pre-review-operational-status-v1.schema.json").read_text()
    )
    current = json.loads(
        Path("schemas/pre-review-operational-status-external-v3.schema.json").read_text()
    )
    assert legacy["properties"]["record_version"]["const"] == 2
    assert current["properties"]["record_version"]["const"] == 3
    assert set(legacy["properties"]["states"]["required"]) == set(LEGACY_STATES)
    assert set(current["properties"]["states"]["required"]) == set(ALL_STATES)
    assert current["properties"]["external_source"]["additionalProperties"] is False
    assert "source_hmac" not in json.dumps(current)


def test_blocked_external_worker_remains_visible_in_typed_status(tmp_path):
    """External Blocked shares the legacy state name but has an External attempt."""
    from obsidian_automation.chatgpt_external_worker import (
        run_external_synthetic_worker_once,
    )

    ai, private, preview, staged, vault = setup(tmp_path)
    result = run_external_synthetic_worker_once(
        ai, private_root=private, vault_root=vault, preview=preview,
        job_id=staged["job_id"],
        fake_generator_wire=b'{"untrusted": "wrong generator schema"}',
    )
    assert result.state == "blocked"
    status = build_status(ai, now=_now())
    assert status.record_version == 3
    assert status.external_jobs == 2
    assert status.external_review_waiting == 0
    assert status.states["blocked"] == 1
    assert status.states["external_admitted"] == 1
    assert status.pipeline_health == "CRITICAL"
    assert parse_status(status.to_json_bytes()) == status


def test_mixed_legacy_and_external_jobs_share_one_status_projection(tmp_path):
    """Do not create another status database or conceal native queued work."""
    from test_pre_review_status import _recipe

    from obsidian_automation.context_bundle import ContextBundle, store_context_bundle
    from obsidian_automation.pre_review_job import submit_job

    ai, _, _, staged, _ = setup(tmp_path)
    legacy_sha, _ = store_context_bundle(
        ai,
        ContextBundle(
            query="Synthetic legacy job stays visible",
            created_at="2026-10-21T00:00:00Z",
            sources=(),
        ),
    )
    legacy = submit_job(ai, context_sha256=legacy_sha, recipe=_recipe())
    assert legacy["job_id"] != staged["job_id"]
    status = build_status(ai, now=_now())
    raw = json.loads(status.to_json_bytes())
    assert raw["record_version"] == 3
    assert raw["states"]["queued"] == 1
    assert raw["states"]["external_admitted"] == 2
    assert raw["external_source"]["jobs"] == 2
    assert raw["current_jobs"] == 3
    assert raw["review_wait"]["count"] == 0
    assert parse_status(status.to_json_bytes()) == status
    assert legacy["job_id"] not in status.to_json_bytes().decode()
    assert staged["job_id"] not in status.to_json_bytes().decode()
