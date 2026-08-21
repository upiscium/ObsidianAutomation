from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from obsidian_automation.artifact_lifecycle import (
    ensure_artifact_layout,
    load_review_record,
    store_review_record,
    store_untrusted_proposal,
    store_validated_mutation,
    store_validation_record,
)
from obsidian_automation.canonical_mutation import execute_create_note, validate_create_note
from obsidian_automation.execution_orchestrator import (
    ExecutionOrchestrationError,
    parse_execution_intent,
    prepare_execution_intent,
    reconcile_execution,
    run_approved_create_note,
)


def _setup(tmp_path: Path):
    vault = tmp_path / "vault"
    (vault / "11-Knowledge").mkdir(parents=True)
    ai_root = vault / "20-AI"
    ai_root.mkdir()

    proposal = (
        b'{"contract_version":1,"operation":"create_note",'
        b'"mutation_id":"durable-create-note-test",'
        b'"target":{"path":"11-Knowledge/reconciled.md"},'
        b'"content":"# Durable\\n"}\n'
    )
    validated = validate_create_note(
        proposal,
        vault_root=vault,
        allowed_roots=["11-Knowledge"],
    )
    proposal_sha, _ = store_untrusted_proposal(ai_root, proposal)
    store_validated_mutation(ai_root, validated)
    store_validation_record(
        ai_root,
        proposal_sha256=proposal_sha,
        result="accepted",
        mutation_sha256=validated.mutation_sha256,
        validated_at="2026-08-21T04:30:00Z",
    )
    store_review_record(
        ai_root,
        mutation_sha256=validated.mutation_sha256,
        decision="approve",
        approver="human",
        decided_at="2026-08-21T04:31:00Z",
    )
    return vault, ai_root, validated


def test_prepare_intent_is_durable_and_bound_to_approval(tmp_path: Path) -> None:
    vault, ai_root, validated = _setup(tmp_path)
    intent = prepare_execution_intent(
        ai_root,
        vault,
        validated.mutation_sha256,
        allowed_roots=["11-Knowledge"],
        prepared_at="2026-08-21T04:32:00Z",
    )
    path = ai_root / "25-Execution" / f"{validated.mutation_sha256}.intent.json"
    assert path.exists()
    parsed = parse_execution_intent(path.read_bytes())
    assert parsed == intent
    assert parsed.target_path == validated.mutation.target_path
    assert parsed.content_sha256 == hashlib.sha256(b"# Durable\n").hexdigest()
    assert not (vault / validated.mutation.target_path).exists()


def test_normal_run_creates_note_receipt_and_completed_state(tmp_path: Path) -> None:
    vault, ai_root, validated = _setup(tmp_path)
    result = run_approved_create_note(
        ai_root,
        vault,
        validated.mutation_sha256,
        allowed_roots=["11-Knowledge"],
    )
    assert result.status == "completed"
    assert result.receipt is not None
    assert (vault / validated.mutation.target_path).read_text() == "# Durable\n"
    receipt_path = (
        ensure_artifact_layout(ai_root).receipts
        / f"{validated.mutation_sha256}.receipt.json"
    )
    assert receipt_path.exists()

    second = run_approved_create_note(
        ai_root,
        vault,
        validated.mutation_sha256,
        allowed_roots=["11-Knowledge"],
    )
    assert second.status == "completed"
    assert (vault / validated.mutation.target_path).read_text() == "# Durable\n"


def test_crash_before_effect_reconciles_to_pending_and_can_retry(tmp_path: Path) -> None:
    vault, ai_root, validated = _setup(tmp_path)
    prepare_execution_intent(
        ai_root,
        vault,
        validated.mutation_sha256,
        allowed_roots=["11-Knowledge"],
    )
    pending = reconcile_execution(
        ai_root,
        vault,
        validated.mutation_sha256,
        allowed_roots=["11-Knowledge"],
    )
    assert pending.status == "pending_retry"

    completed = run_approved_create_note(
        ai_root,
        vault,
        validated.mutation_sha256,
        allowed_roots=["11-Knowledge"],
    )
    assert completed.status == "completed"


def test_crash_after_effect_before_receipt_is_not_claimed_as_success(tmp_path: Path) -> None:
    vault, ai_root, validated = _setup(tmp_path)
    prepare_execution_intent(
        ai_root,
        vault,
        validated.mutation_sha256,
        allowed_roots=["11-Knowledge"],
    )
    review = load_review_record(ai_root, validated.mutation_sha256)
    execute_create_note(
        validated.artifact_bytes,
        approval=review.to_approval(),
        vault_root=vault,
        allowed_roots=["11-Knowledge"],
    )

    state = reconcile_execution(
        ai_root,
        vault,
        validated.mutation_sha256,
        allowed_roots=["11-Knowledge"],
    )
    assert state.status == "effect_observed_without_receipt"
    assert state.receipt is None

    rerun = run_approved_create_note(
        ai_root,
        vault,
        validated.mutation_sha256,
        allowed_roots=["11-Knowledge"],
    )
    assert rerun.status == "effect_observed_without_receipt"
    assert (vault / validated.mutation.target_path).read_text() == "# Durable\n"
    receipt_path = (
        ensure_artifact_layout(ai_root).receipts
        / f"{validated.mutation_sha256}.receipt.json"
    )
    assert not receipt_path.exists()


def test_conflicting_target_is_never_overwritten(tmp_path: Path) -> None:
    vault, ai_root, validated = _setup(tmp_path)
    prepare_execution_intent(
        ai_root,
        vault,
        validated.mutation_sha256,
        allowed_roots=["11-Knowledge"],
    )
    target = vault / validated.mutation.target_path
    target.write_text("# Human content\n")

    state = run_approved_create_note(
        ai_root,
        vault,
        validated.mutation_sha256,
        allowed_roots=["11-Knowledge"],
    )
    assert state.status == "conflict"
    assert target.read_text() == "# Human content\n"


def test_receipt_target_divergence_is_reported_as_conflict(tmp_path: Path) -> None:
    vault, ai_root, validated = _setup(tmp_path)
    completed = run_approved_create_note(
        ai_root,
        vault,
        validated.mutation_sha256,
        allowed_roots=["11-Knowledge"],
    )
    assert completed.status == "completed"

    target = vault / validated.mutation.target_path
    target.write_text("# Changed later\n")
    state = reconcile_execution(
        ai_root,
        vault,
        validated.mutation_sha256,
        allowed_roots=["11-Knowledge"],
    )
    assert state.status == "conflict"
    assert "receipt claims success" in (state.reason or "")


def test_approval_tamper_after_intent_is_rejected(tmp_path: Path) -> None:
    vault, ai_root, validated = _setup(tmp_path)
    prepare_execution_intent(
        ai_root,
        vault,
        validated.mutation_sha256,
        allowed_roots=["11-Knowledge"],
    )
    review_path = (
        ensure_artifact_layout(ai_root).review
        / f"{validated.mutation_sha256}.approval.json"
    )
    review_path.write_text(
        "{"
        '"record_version":1,'
        f'"mutation_sha256":"{validated.mutation_sha256}",'
        '"decision":"approve",'
        '"decided_at":"2026-08-21T04:31:00Z",'
        '"approver":"different-human"'
        "}\n"
    )
    with pytest.raises(
        ExecutionOrchestrationError,
        match="approval artifact changed after intent preparation",
    ):
        reconcile_execution(
            ai_root,
            vault,
            validated.mutation_sha256,
            allowed_roots=["11-Knowledge"],
        )


def test_symlink_parent_swap_after_intent_is_conflict(tmp_path: Path) -> None:
    vault, ai_root, validated = _setup(tmp_path)
    prepare_execution_intent(
        ai_root,
        vault,
        validated.mutation_sha256,
        allowed_roots=["11-Knowledge"],
    )

    original = vault / "11-Knowledge"
    moved = vault / "11-Knowledge-real"
    original.rename(moved)
    outside = tmp_path / "outside"
    outside.mkdir()
    original.symlink_to(outside, target_is_directory=True)

    state = run_approved_create_note(
        ai_root,
        vault,
        validated.mutation_sha256,
        allowed_roots=["11-Knowledge"],
    )
    assert state.status == "conflict"
    assert not (outside / "reconciled.md").exists()


def test_reconcile_without_intent_reports_not_started(tmp_path: Path) -> None:
    vault, ai_root, validated = _setup(tmp_path)
    state = reconcile_execution(
        ai_root,
        vault,
        validated.mutation_sha256,
        allowed_roots=["11-Knowledge"],
    )
    assert state.status == "not_started"


def test_current_policy_is_rechecked_during_reconciliation(tmp_path: Path) -> None:
    vault, ai_root, validated = _setup(tmp_path)
    prepare_execution_intent(
        ai_root,
        vault,
        validated.mutation_sha256,
        allowed_roots=["11-Knowledge"],
    )
    with pytest.raises(ExecutionOrchestrationError, match="outside current"):
        reconcile_execution(
            ai_root,
            vault,
            validated.mutation_sha256,
            allowed_roots=["Different-Root"],
        )
