from __future__ import annotations

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
from obsidian_automation.execution_orchestrator import prepare_execution_intent
from obsidian_automation.human_recovery import (
    HumanRecoveryError,
    load_recovery_record,
    parse_recovery_record,
    reconcile_recovery_aware_execution,
    record_human_recovery,
    run_recovery_aware_create_note,
)


def _setup(tmp_path: Path):
    vault = tmp_path / "vault"
    (vault / "11-Knowledge").mkdir(parents=True)
    ai_root = vault / "20-AI"
    ai_root.mkdir()
    proposal = (
        b'{"contract_version":1,"operation":"create_note",'
        b'"mutation_id":"human-recovery-test",'
        b'"target":{"path":"11-Knowledge/recovery.md"},'
        b'"content":"# Recovery\\n"}\n'
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
        validated_at="2026-08-21T05:00:00Z",
    )
    store_review_record(
        ai_root,
        mutation_sha256=validated.mutation_sha256,
        decision="approve",
        approver="human",
        decided_at="2026-08-21T05:01:00Z",
    )
    return vault, ai_root, validated


def _effect_without_receipt(tmp_path: Path):
    vault, ai_root, validated = _setup(tmp_path)
    prepare_execution_intent(
        ai_root,
        vault,
        validated.mutation_sha256,
        allowed_roots=["11-Knowledge"],
        prepared_at="2026-08-21T05:02:00Z",
    )
    review = load_review_record(ai_root, validated.mutation_sha256)
    execute_create_note(
        validated.artifact_bytes,
        approval=review.to_approval(),
        vault_root=vault,
        allowed_roots=["11-Knowledge"],
    )
    return vault, ai_root, validated


def test_human_can_adopt_matching_effect_without_executor_provenance(tmp_path: Path) -> None:
    vault, ai_root, validated = _effect_without_receipt(tmp_path)
    record = record_human_recovery(
        ai_root,
        vault,
        validated.mutation_sha256,
        decision="adopt_observed_effect",
        resolver="human",
        reason="Reviewed canonical note and accept current state",
        allowed_roots=["11-Knowledge"],
        decided_at="2026-08-21T05:03:00Z",
    )
    assert record.observed_status == "effect_observed_without_receipt"
    assert record.decision == "adopt_observed_effect"

    state = reconcile_recovery_aware_execution(
        ai_root,
        vault,
        validated.mutation_sha256,
        allowed_roots=["11-Knowledge"],
    )
    assert state.status == "resolved_effect_adopted"
    assert state.receipt is None
    assert "without claiming executor provenance" in (state.reason or "")

    receipt = ensure_artifact_layout(ai_root).receipts / f"{validated.mutation_sha256}.receipt.json"
    assert not receipt.exists()


def test_adopted_effect_blocks_automatic_rerun_and_receipt_fabrication(tmp_path: Path) -> None:
    vault, ai_root, validated = _effect_without_receipt(tmp_path)
    record_human_recovery(
        ai_root,
        vault,
        validated.mutation_sha256,
        decision="adopt_observed_effect",
        resolver="human",
        reason="Accept observed effect",
        allowed_roots=["11-Knowledge"],
    )
    state = run_recovery_aware_create_note(
        ai_root,
        vault,
        validated.mutation_sha256,
        allowed_roots=["11-Knowledge"],
    )
    assert state.status == "resolved_effect_adopted"
    assert state.receipt is None
    assert (vault / validated.mutation.target_path).read_text() == "# Recovery\n"


def test_adopted_effect_divergence_becomes_conflict(tmp_path: Path) -> None:
    vault, ai_root, validated = _effect_without_receipt(tmp_path)
    record_human_recovery(
        ai_root,
        vault,
        validated.mutation_sha256,
        decision="adopt_observed_effect",
        resolver="human",
        reason="Accept observed effect",
        allowed_roots=["11-Knowledge"],
    )
    (vault / validated.mutation.target_path).write_text("# Changed later\n")
    state = reconcile_recovery_aware_execution(
        ai_root,
        vault,
        validated.mutation_sha256,
        allowed_roots=["11-Knowledge"],
    )
    assert state.status == "conflict"


def test_human_can_abandon_conflict_and_future_retry_stays_blocked(tmp_path: Path) -> None:
    vault, ai_root, validated = _setup(tmp_path)
    prepare_execution_intent(
        ai_root,
        vault,
        validated.mutation_sha256,
        allowed_roots=["11-Knowledge"],
    )
    target = vault / validated.mutation.target_path
    target.write_text("# Human content\n")
    record_human_recovery(
        ai_root,
        vault,
        validated.mutation_sha256,
        decision="abandon",
        resolver="human",
        reason="Keep independently authored note and retire this mutation",
        allowed_roots=["11-Knowledge"],
    )
    state = reconcile_recovery_aware_execution(
        ai_root,
        vault,
        validated.mutation_sha256,
        allowed_roots=["11-Knowledge"],
    )
    assert state.status == "resolved_abandoned"

    target.unlink()
    rerun = run_recovery_aware_create_note(
        ai_root,
        vault,
        validated.mutation_sha256,
        allowed_roots=["11-Knowledge"],
    )
    assert rerun.status == "resolved_abandoned"
    assert not target.exists()


def test_adopt_is_rejected_for_conflicting_content(tmp_path: Path) -> None:
    vault, ai_root, validated = _setup(tmp_path)
    prepare_execution_intent(
        ai_root,
        vault,
        validated.mutation_sha256,
        allowed_roots=["11-Knowledge"],
    )
    (vault / validated.mutation.target_path).write_text("# Different\n")
    with pytest.raises(HumanRecoveryError, match="only valid"):
        record_human_recovery(
            ai_root,
            vault,
            validated.mutation_sha256,
            decision="adopt_observed_effect",
            resolver="human",
            reason="Should not be accepted",
            allowed_roots=["11-Knowledge"],
        )


def test_recovery_is_not_allowed_for_pending_or_completed_execution(tmp_path: Path) -> None:
    vault, ai_root, validated = _setup(tmp_path)
    prepare_execution_intent(
        ai_root,
        vault,
        validated.mutation_sha256,
        allowed_roots=["11-Knowledge"],
    )
    with pytest.raises(HumanRecoveryError, match="not recoverable"):
        record_human_recovery(
            ai_root,
            vault,
            validated.mutation_sha256,
            decision="abandon",
            resolver="human",
            reason="No ambiguous state exists",
            allowed_roots=["11-Knowledge"],
        )

    completed = run_recovery_aware_create_note(
        ai_root,
        vault,
        validated.mutation_sha256,
        allowed_roots=["11-Knowledge"],
    )
    assert completed.status == "completed"
    with pytest.raises(HumanRecoveryError, match="not recoverable"):
        record_human_recovery(
            ai_root,
            vault,
            validated.mutation_sha256,
            decision="abandon",
            resolver="human",
            reason="Cannot rewrite completed history",
            allowed_roots=["11-Knowledge"],
        )


def test_recovery_decision_is_immutable_and_idempotent(tmp_path: Path) -> None:
    vault, ai_root, validated = _effect_without_receipt(tmp_path)
    first = record_human_recovery(
        ai_root,
        vault,
        validated.mutation_sha256,
        decision="adopt_observed_effect",
        resolver="human",
        reason="Reviewed",
        allowed_roots=["11-Knowledge"],
        decided_at="2026-08-21T05:03:00Z",
    )
    second = record_human_recovery(
        ai_root,
        vault,
        validated.mutation_sha256,
        decision="adopt_observed_effect",
        resolver="human",
        reason="Reviewed",
        allowed_roots=["11-Knowledge"],
        decided_at="2026-08-21T06:00:00Z",
    )
    assert second == first

    with pytest.raises(HumanRecoveryError, match="immutable"):
        record_human_recovery(
            ai_root,
            vault,
            validated.mutation_sha256,
            decision="abandon",
            resolver="human",
            reason="Changed mind",
            allowed_roots=["11-Knowledge"],
        )


def test_recovery_record_parser_rejects_unknown_and_duplicate_fields() -> None:
    digest = "a" * 64
    unknown = (
        "{"
        '"record_version":1,'
        f'"mutation_sha256":"{digest}",'
        f'"intent_sha256":"{digest}",'
        '"decision":"abandon",'
        '"observed_status":"conflict",'
        '"target_path":"11-Knowledge/x.md",'
        f'"expected_content_sha256":"{digest}",'
        '"decided_at":"2026-08-21T05:00:00Z",'
        '"resolver":"human",'
        '"reason":"reason",'
        '"extra":true'
        "}\n"
    ).encode()
    with pytest.raises(HumanRecoveryError, match="properties"):
        parse_recovery_record(unknown)

    duplicate = unknown.replace(b'"extra":true', b'"decision":"abandon"')
    with pytest.raises(HumanRecoveryError, match="duplicate"):
        parse_recovery_record(duplicate)


def test_recovery_record_is_bound_to_exact_intent_bytes(tmp_path: Path) -> None:
    vault, ai_root, validated = _effect_without_receipt(tmp_path)
    record_human_recovery(
        ai_root,
        vault,
        validated.mutation_sha256,
        decision="adopt_observed_effect",
        resolver="human",
        reason="Reviewed",
        allowed_roots=["11-Knowledge"],
    )
    intent_path = ai_root / "25-Execution" / f"{validated.mutation_sha256}.intent.json"
    original = intent_path.read_bytes()
    intent_path.write_bytes(original.replace(b'"prepared_at":', b'"prepared_at" :'))

    state = reconcile_recovery_aware_execution(
        ai_root,
        vault,
        validated.mutation_sha256,
        allowed_roots=["11-Knowledge"],
    )
    assert state.status == "conflict"
    assert "durable execution intent" in (state.reason or "")


def test_load_recovery_record_returns_persisted_decision(tmp_path: Path) -> None:
    vault, ai_root, validated = _effect_without_receipt(tmp_path)
    created = record_human_recovery(
        ai_root,
        vault,
        validated.mutation_sha256,
        decision="adopt_observed_effect",
        resolver="human",
        reason="Reviewed",
        allowed_roots=["11-Knowledge"],
    )
    loaded = load_recovery_record(ai_root, validated.mutation_sha256)
    assert loaded == created
