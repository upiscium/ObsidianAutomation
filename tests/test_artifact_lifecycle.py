from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from obsidian_automation.artifact_lifecycle import (
    ArtifactLifecycleError,
    ensure_artifact_layout,
    load_review_record,
    parse_review_record,
    parse_validation_record,
    store_execution_receipt,
    store_review_record,
    store_untrusted_proposal,
    store_validated_mutation,
    store_validation_record,
)
from obsidian_automation.canonical_mutation import ExecutionReceipt, validate_create_note


def _proposal(target: str = "11-Knowledge/example.md") -> bytes:
    return (
        "{"
        '"contract_version":1,'
        '"operation":"create_note",'
        '"mutation_id":"../opaque-id-is-not-a-path",'
        f'"target":{{"path":"{target}"}},'
        '"content":"# Example\\n"'
        "}\n"
    ).encode("utf-8")


def _validated(tmp_path: Path):
    vault = tmp_path / "vault"
    (vault / "11-Knowledge").mkdir(parents=True)
    proposal = _proposal()
    validated = validate_create_note(
        proposal,
        vault_root=vault,
        allowed_roots=["11-Knowledge"],
    )
    return vault, proposal, validated


def test_layout_and_untrusted_proposals_are_content_addressed(tmp_path: Path) -> None:
    ai_root = tmp_path / "20-AI"
    ai_root.mkdir()
    layout = ensure_artifact_layout(ai_root)
    assert layout.untrusted.name == "00-Untrusted"
    assert layout.validation.name == "10-Validation"
    assert layout.review.name == "20-Review"
    assert layout.receipts.name == "30-Receipts"

    proposal = _proposal()
    digest, path = store_untrusted_proposal(ai_root, proposal)
    assert digest == hashlib.sha256(proposal).hexdigest()
    assert path.name == f"{digest}.proposal.json"
    assert path.read_bytes() == proposal
    assert ".." not in path.name

    second_digest, second_path = store_untrusted_proposal(ai_root, proposal)
    assert second_digest == digest
    assert second_path == path


def test_layout_rejects_symlinked_stage_directory(tmp_path: Path) -> None:
    ai_root = tmp_path / "20-AI"
    ai_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (ai_root / "10-Validation").symlink_to(outside)
    with pytest.raises(ArtifactLifecycleError, match="safe directory"):
        ensure_artifact_layout(ai_root)


def test_validated_artifact_and_accept_record_bind_hash_chain(tmp_path: Path) -> None:
    _, proposal, validated = _validated(tmp_path)
    ai_root = tmp_path / "20-AI"
    ai_root.mkdir()
    proposal_sha, _ = store_untrusted_proposal(ai_root, proposal)
    mutation_path = store_validated_mutation(ai_root, validated)
    assert mutation_path.name == f"{validated.mutation_sha256}.mutation.json"
    assert mutation_path.read_bytes() == validated.artifact_bytes

    record_path = store_validation_record(
        ai_root,
        proposal_sha256=proposal_sha,
        result="accepted",
        mutation_sha256=validated.mutation_sha256,
        validated_at="2026-08-21T00:00:00Z",
    )
    record = parse_validation_record(record_path.read_bytes())
    assert record.result == "accepted"
    assert record.proposal_sha256 == proposal_sha
    assert record.mutation_sha256 == validated.mutation_sha256
    assert record.reason is None


def test_rejected_validation_record_has_no_mutation_hash(tmp_path: Path) -> None:
    ai_root = tmp_path / "20-AI"
    ai_root.mkdir()
    proposal = b'{"not":"a valid mutation"}\n'
    proposal_sha, _ = store_untrusted_proposal(ai_root, proposal)
    record_path = store_validation_record(
        ai_root,
        proposal_sha256=proposal_sha,
        result="rejected",
        reason="invalid schema",
        validated_at="2026-08-21T00:00:00Z",
    )
    record = parse_validation_record(record_path.read_bytes())
    assert record.result == "rejected"
    assert record.mutation_sha256 is None
    assert record.reason == "invalid schema"


def test_review_is_single_immutable_decision_bound_to_validated_bytes(tmp_path: Path) -> None:
    _, _, validated = _validated(tmp_path)
    ai_root = tmp_path / "20-AI"
    ai_root.mkdir()
    store_validated_mutation(ai_root, validated)
    approval_path = store_review_record(
        ai_root,
        mutation_sha256=validated.mutation_sha256,
        decision="approve",
        approver="human",
        decided_at="2026-08-21T00:01:00Z",
    )
    review = load_review_record(ai_root, validated.mutation_sha256)
    approval = review.to_approval()
    assert approval.approved is True
    assert approval.mutation_sha256 == validated.mutation_sha256

    same_path = store_review_record(
        ai_root,
        mutation_sha256=validated.mutation_sha256,
        decision="approve",
        approver="human",
        decided_at="2026-08-21T00:01:00Z",
    )
    assert same_path == approval_path

    with pytest.raises(ArtifactLifecycleError, match="different bytes"):
        store_review_record(
            ai_root,
            mutation_sha256=validated.mutation_sha256,
            decision="reject",
            approver="human",
            decided_at="2026-08-21T00:02:00Z",
        )


def test_review_parser_rejects_unknown_and_duplicate_properties() -> None:
    digest = "a" * 64
    unknown = (
        "{"
        '"record_version":1,'
        f'"mutation_sha256":"{digest}",'
        '"decision":"approve",'
        '"decided_at":"2026-08-21T00:00:00Z",'
        '"approver":"human",'
        '"extra":true'
        "}\n"
    ).encode()
    with pytest.raises(ArtifactLifecycleError, match="properties"):
        parse_review_record(unknown)

    duplicate = (
        "{"
        '"record_version":1,'
        f'"mutation_sha256":"{digest}",'
        '"decision":"approve",'
        '"decision":"reject",'
        '"decided_at":"2026-08-21T00:00:00Z",'
        '"approver":"human"'
        "}\n"
    ).encode()
    with pytest.raises(ArtifactLifecycleError, match="duplicate"):
        parse_review_record(duplicate)


def test_review_requires_existing_exact_validated_artifact(tmp_path: Path) -> None:
    ai_root = tmp_path / "20-AI"
    ai_root.mkdir()
    with pytest.raises(ArtifactLifecycleError, match="cannot safely open artifact"):
        store_review_record(
            ai_root,
            mutation_sha256="b" * 64,
            decision="approve",
            approver="human",
            decided_at="2026-08-21T00:00:00Z",
        )


def test_receipt_is_immutable_and_requires_validated_artifact(tmp_path: Path) -> None:
    _, _, validated = _validated(tmp_path)
    ai_root = tmp_path / "20-AI"
    ai_root.mkdir()
    store_validated_mutation(ai_root, validated)
    receipt = ExecutionReceipt(
        mutation_id=validated.mutation.mutation_id,
        mutation_sha256=validated.mutation_sha256,
        target_path=validated.mutation.target_path,
        content_sha256=hashlib.sha256(validated.mutation.content.encode("utf-8")).hexdigest(),
        executed_at="2026-08-21T00:03:00Z",
    )
    path = store_execution_receipt(ai_root, receipt)
    assert path.name == f"{validated.mutation_sha256}.receipt.json"
    assert path.read_bytes() == receipt.to_json_bytes()
    assert store_execution_receipt(ai_root, receipt) == path


def test_validation_record_cannot_claim_nonexistent_mutation(tmp_path: Path) -> None:
    ai_root = tmp_path / "20-AI"
    ai_root.mkdir()
    proposal_sha, _ = store_untrusted_proposal(ai_root, _proposal())
    with pytest.raises(ArtifactLifecycleError, match="cannot safely open artifact"):
        store_validation_record(
            ai_root,
            proposal_sha256=proposal_sha,
            result="accepted",
            mutation_sha256="c" * 64,
            validated_at="2026-08-21T00:00:00Z",
        )
