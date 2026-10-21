"""Synthetic External Source disposition via SHARED Human Review v3 artifact.

This is a non-approval admission slice: original Human Review Record,
existing Reconcile and terminal states are reused, while all explicitly
synthetic external Source candidates remain unverified/non-executable.
"""

from __future__ import annotations

import json
import os
import zipfile

import pytest
from test_chatgpt_export_review_preview import input_export
from test_chatgpt_external_worker_e2e import run, setup

from obsidian_automation.artifact_lifecycle import (
    ArtifactLifecycleError,
    load_review_record,
)
from obsidian_automation.chatgpt_export_intake import publish_archive
from obsidian_automation.chatgpt_external_review_adapter import (
    ExternalDispositionError,
    record_external_nonapproval_disposition,
)
from obsidian_automation.chatgpt_external_review_evidence import (
    read_private_review_evidence,
)
from obsidian_automation.knowledge_review import create_evaluation_bound_review
from obsidian_automation.pre_review_job import (
    awaiting_human_review_count,
    job_status,
    stage_output,
)
from obsidian_automation.pre_review_reconcile import (
    PreReviewReconcileError,
    reconcile_post_review,
)


def completed_source(tmp_path):
    ai, private, preview, staged, vault = setup(tmp_path)
    before = {p.name: p.read_bytes() for p in (vault / "11-Knowledge").iterdir()}
    result = run(ai, private, preview, staged, vault)
    assert result.awaiting_human_review
    packet = read_private_review_evidence(
        ai,
        private_root=private,
        preview=preview,
        job_id=staged["job_id"],
    )
    return ai, private, preview, staged, vault, result, packet, before


def decide(
    ai,
    private,
    preview,
    staged,
    receipt,
    packet,
    *,
    decision="reject",
    approver="synthetic-reviewer",
):
    return record_external_nonapproval_disposition(
        ai,
        private_root=private,
        preview=preview,
        job_id=staged["job_id"],
        evaluation_sha256=receipt.evaluation_sha256,
        expected_source_revision_sha256=preview.revision_sha256,
        expected_evidence_packet_sha256=packet.packet_sha256,
        decision=decision,
        approver=approver,
        decided_at="2026-10-21T09:00:00Z",
    )


@pytest.mark.parametrize(
    "decision,target,count_key",
    [
        ("reject", "human_rejected", "human_rejected"),
        ("keep_as_idea", "human_kept_as_idea", "human_kept_as_idea"),
    ],
)
def test_shared_v3_human_review_record_and_existing_reconcile(
    tmp_path,
    decision,
    target,
    count_key,
):
    ai, private, preview, staged, vault, receipt, packet, original = completed_source(
        tmp_path
    )
    action = decide(ai, private, preview, staged, receipt, packet, decision=decision)
    assert action.decision == decision
    assert action.state == "external_review_recorded"
    assert action.record_version == 3
    assert action.human_approval_granted is False
    assert action.canonical_mutation_authorized is False
    assert action.source_revision_sha256 == preview.revision_sha256
    assert action.evaluation_sha256 == receipt.evaluation_sha256
    assert action.evidence_packet_sha256 == packet.packet_sha256
    assert len(action.review_sha256) == 64

    recorded = load_review_record(ai, action.mutation_sha256)
    assert recorded.record_version == 3
    assert recorded.decision == decision
    assert recorded.evaluation_sha256 == receipt.evaluation_sha256
    assert recorded.approver == "synthetic-reviewer"
    assert awaiting_human_review_count(ai) == 0
    assert stage_output(ai, staged["generation_id"], "external_evaluation") is not None
    assert stage_output(ai, staged["generation_id"], "evaluation") is None

    finish = reconcile_post_review(ai, max_items=1)
    assert finish["changed"] == 1
    assert finish[count_key] == 1
    assert finish["approved_pending_execution"] == 0
    assert job_status(ai, staged["job_id"])["current_generation"]["state"] == target
    assert reconcile_post_review(ai)["changed"] == 0
    assert {
        p.name: p.read_bytes() for p in (vault / "11-Knowledge").iterdir()
    } == original
    assert not list((ai / "30-Receipts").glob("*.json"))


def test_approve_and_unrecognized_decisions_are_refused_even_for_valid_source(tmp_path):
    ai, private, preview, staged, _, receipt, packet, _ = completed_source(tmp_path)
    for decision in ("approve", "proceed", "", None, "APPROVE", "keep-as-idea"):
        with pytest.raises(
            ExternalDispositionError,
            match="external_approval_not_enabled",
        ):
            decide(ai, private, preview, staged, receipt, packet, decision=decision)
    assert awaiting_human_review_count(ai) == 1
    assert not list((ai / "20-Review").glob("*.approval.json"))
    with pytest.raises(
        ArtifactLifecycleError, match="external Source requires separate Human Review"
    ):
        create_evaluation_bound_review(
            ai,
            evaluation_sha256=receipt.evaluation_sha256,
            decision="approve",
            approver="synthetic-reviewer",
        )


def test_stale_source_disallows_disposition_and_keeps_queue_waiting(tmp_path):
    ai, private, preview, staged, _, receipt, packet, _ = completed_source(tmp_path)
    second = tmp_path / "changed-before-review.zip"
    with zipfile.ZipFile(second, "w") as output:
        output.writestr(
            "conversations.json",
            json.dumps(input_export(assistant_text="Later corrected claim")),
        )
    os.chmod(second, 0o600)
    publish_archive(private, second)
    with pytest.raises(
        ExternalDispositionError,
        match="external_review_source_invalid",
    ):
        decide(ai, private, preview, staged, receipt, packet)
    assert awaiting_human_review_count(ai) == 1
    assert not list((ai / "20-Review").glob("*.approval.json"))


@pytest.mark.parametrize(
    "field",
    [
        "evaluation",
        "revision",
        "packet",
        "job",
    ],
)
def test_forged_identity_or_review_evidence_never_writes_review(tmp_path, field):
    ai, private, preview, staged, _, receipt, packet, _ = completed_source(tmp_path)
    args = {
        "ai_root": ai,
        "private_root": private,
        "preview": preview,
        "job_id": staged["job_id"],
        "evaluation_sha256": receipt.evaluation_sha256,
        "expected_source_revision_sha256": preview.revision_sha256,
        "expected_evidence_packet_sha256": packet.packet_sha256,
        "decision": "reject",
        "approver": "synthetic-reviewer",
        "decided_at": "2026-10-21T09:00:00Z",
    }
    fields = {
        "evaluation": "evaluation_sha256",
        "revision": "expected_source_revision_sha256",
        "packet": "expected_evidence_packet_sha256",
        "job": "job_id",
    }
    args[fields[field]] = "b" * 64
    with pytest.raises(ExternalDispositionError):
        record_external_nonapproval_disposition(**args)
    assert not list((ai / "20-Review").glob("*.approval.json"))
    assert awaiting_human_review_count(ai) == 1


def test_crash_between_review_record_and_job_state_is_idempotently_recoverable(
    tmp_path,
    monkeypatch,
):
    import obsidian_automation.chatgpt_external_review_adapter as adapter

    ai, private, preview, staged, _, receipt, packet, _ = completed_source(tmp_path)
    original = adapter._connect_rw
    calls = 0

    def fail_once(root):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("synthetic.db.failure.DO_NOT_LEAK")
        return original(root)

    with monkeypatch.context() as patch:
        patch.setattr(adapter, "_connect_rw", fail_once)
        with pytest.raises(ExternalDispositionError) as captured:
            decide(ai, private, preview, staged, receipt, packet)
        assert str(captured.value) == "external_review_recording_failed", (
            str(captured.value),
            calls,
        )
        assert calls == 1
    existing = load_review_record(
        ai,
        stage_output(
            ai,
            staged["generation_id"],
            "external_evaluation",
        )["mutation_sha256"],
    )
    assert existing.decision == "reject"
    assert awaiting_human_review_count(ai) == 1
    recovered = decide(ai, private, preview, staged, receipt, packet)
    assert recovered.decision == "reject"
    assert recovered.state == "external_review_recorded"
    assert reconcile_post_review(ai)["human_rejected"] == 1


def test_reconcile_never_promotes_forged_external_approve(tmp_path):
    ai, private, preview, staged, _, receipt, packet, _ = completed_source(tmp_path)
    recorded = decide(ai, private, preview, staged, receipt, packet)
    path = ai / "20-Review" / f"{recorded.mutation_sha256}.approval.json"
    data = json.loads(path.read_bytes())
    data["decision"] = "approve"
    path.write_bytes(
        (json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n").encode()
    )
    with pytest.raises(
        PreReviewReconcileError,
        match="external Source approval not authorized",
    ):
        reconcile_post_review(ai)
    assert job_status(ai, staged["job_id"])["current_generation"]["state"] == (
        "external_review_recorded"
    )


def test_invalid_approver_and_real_scope_refused(tmp_path):
    from dataclasses import replace

    ai, private, preview, staged, _, receipt, packet, _ = completed_source(tmp_path)
    with pytest.raises(ExternalDispositionError):
        decide(ai, private, preview, staged, receipt, packet, approver="")
    fake_real = replace(preview, scope="real-user-import")
    with pytest.raises(
        ExternalDispositionError, match="external_review_source_invalid"
    ):
        record_external_nonapproval_disposition(
            ai,
            private_root=private,
            preview=fake_real,
            job_id=staged["job_id"],
            evaluation_sha256=receipt.evaluation_sha256,
            expected_source_revision_sha256=preview.revision_sha256,
            expected_evidence_packet_sha256=packet.packet_sha256,
            decision="reject",
            approver="synthetic-reviewer",
        )
    assert not list((ai / "20-Review").glob("*.approval.json"))
