from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from obsidian_automation.artifact_lifecycle import (
    ensure_artifact_layout,
    store_review_record,
    store_untrusted_proposal,
    store_validated_mutation,
    store_validation_record,
)
from obsidian_automation.canonical_mutation import validate_create_note
from obsidian_automation.production_orchestrator import (
    ProductionOrchestrationError,
    advance_production_executor,
    process_transport_request,
    reconcile_production_execution,
    record_remote_human_recovery,
)
from obsidian_automation.webdav_create import (
    WebDAVCreateError,
    WebDAVCreateResult,
    WebDAVObservation,
    WebDAVTargetExists,
)


def _setup(tmp_path: Path):
    vault = tmp_path / "vault"
    (vault / "11-Knowledge").mkdir(parents=True)
    state = tmp_path / "state"
    state.mkdir()
    ensure_artifact_layout(state)
    (state / "27-Transport").mkdir()

    proposal = (
        b'{"contract_version":1,"operation":"create_note",'
        b'"mutation_id":"production-remote-test",'
        b'"target":{"path":"11-Knowledge/remote.md"},'
        b'"content":"# Remote\\n"}\n'
    )
    validated = validate_create_note(
        proposal,
        vault_root=vault,
        allowed_roots=["11-Knowledge"],
    )
    proposal_sha, _ = store_untrusted_proposal(state, proposal)
    store_validated_mutation(state, validated)
    store_validation_record(
        state,
        proposal_sha256=proposal_sha,
        result="accepted",
        mutation_sha256=validated.mutation_sha256,
        validated_at="2026-08-22T03:20:00Z",
    )
    store_review_record(
        state,
        mutation_sha256=validated.mutation_sha256,
        decision="approve",
        approver="human",
        decided_at="2026-08-22T03:21:00Z",
    )
    return vault, state, validated


def _created(content_sha256: str) -> WebDAVCreateResult:
    return WebDAVCreateResult(
        target_url="https://nextcloud.example/remote.md",
        content_sha256=content_sha256,
        status_code=201,
        etag='"created"',
    )


def test_remote_verified_result_precedes_receipt_and_local_mirror_stays_read_only(
    tmp_path: Path,
) -> None:
    vault, state, validated = _setup(tmp_path)
    first = advance_production_executor(
        state,
        vault,
        validated.mutation_sha256,
        allowed_roots=["11-Knowledge"],
    )
    assert first.status == "transport_pending"
    assert not (vault / validated.mutation.target_path).exists()

    request_path = state / "25-Execution" / f"{validated.mutation_sha256}.transport-request.json"
    result_path = state / "27-Transport" / f"{validated.mutation_sha256}.transport-result.json"
    receipt_path = state / "30-Receipts" / f"{validated.mutation_sha256}.receipt.json"
    assert request_path.exists()
    assert not result_path.exists()
    assert not receipt_path.exists()

    expected_sha = hashlib.sha256(validated.mutation.content.encode()).hexdigest()
    result = process_transport_request(
        state,
        validated.mutation_sha256,
        allowed_roots=["11-Knowledge"],
        base_url="https://unused.example/",
        username="sync",
        password="secret",
        create_fn=lambda **_: _created(expected_sha),
        observed_at="2026-08-22T03:22:00Z",
    )
    assert result.result == "created_verified"
    assert result_path.exists()
    assert not receipt_path.exists()
    assert not (vault / validated.mutation.target_path).exists()

    pending = reconcile_production_execution(
        state,
        validated.mutation_sha256,
        allowed_roots=["11-Knowledge"],
    )
    assert pending.status == "remote_verified_pending_receipt"

    completed = advance_production_executor(
        state,
        vault,
        validated.mutation_sha256,
        allowed_roots=["11-Knowledge"],
    )
    assert completed.status == "completed"
    assert completed.receipt is not None
    assert completed.receipt.executed_at == "2026-08-22T03:22:00Z"
    assert receipt_path.exists()
    assert not (vault / validated.mutation.target_path).exists()


def test_remote_create_crash_before_result_becomes_ambiguous_not_success(tmp_path: Path) -> None:
    vault, state, validated = _setup(tmp_path)
    advance_production_executor(
        state,
        vault,
        validated.mutation_sha256,
        allowed_roots=["11-Knowledge"],
    )
    remote: dict[str, bytes] = {}

    def create_then_fail(**kwargs):
        remote["content"] = kwargs["content"]
        raise WebDAVCreateError("verification connection lost after PUT")

    with pytest.raises(ProductionOrchestrationError, match="verification connection lost"):
        process_transport_request(
            state,
            validated.mutation_sha256,
            allowed_roots=["11-Knowledge"],
            base_url="https://unused.example/",
            username="sync",
            password="secret",
            create_fn=create_then_fail,
        )

    result_path = state / "27-Transport" / f"{validated.mutation_sha256}.transport-result.json"
    assert not result_path.exists()

    def exists(**_):
        raise WebDAVTargetExists("already exists")

    expected_sha = hashlib.sha256(remote["content"]).hexdigest()

    def observe(**_):
        return WebDAVObservation(
            target_url="https://nextcloud.example/remote.md",
            result="matching",
            status_code=200,
            content_sha256=expected_sha,
            etag='"matching"',
        )

    result = process_transport_request(
        state,
        validated.mutation_sha256,
        allowed_roots=["11-Knowledge"],
        base_url="https://unused.example/",
        username="sync",
        password="secret",
        create_fn=exists,
        observe_fn=observe,
        observed_at="2026-08-22T03:23:00Z",
    )
    assert result.result == "target_exists_matching"

    state_before_recovery = advance_production_executor(
        state,
        vault,
        validated.mutation_sha256,
        allowed_roots=["11-Knowledge"],
    )
    assert state_before_recovery.status == "remote_effect_observed_without_receipt"
    assert not (state / "30-Receipts" / f"{validated.mutation_sha256}.receipt.json").exists()

    recovery = record_remote_human_recovery(
        state,
        validated.mutation_sha256,
        decision="adopt_observed_effect",
        resolver="human",
        reason="Verified the remote note after transport crash",
        allowed_roots=["11-Knowledge"],
        decided_at="2026-08-22T03:24:00Z",
    )
    assert recovery.transport_result_sha256 == hashlib.sha256(result_path.read_bytes()).hexdigest()

    resolved = reconcile_production_execution(
        state,
        validated.mutation_sha256,
        allowed_roots=["11-Knowledge"],
    )
    assert resolved.status == "resolved_effect_adopted"
    assert not (state / "30-Receipts" / f"{validated.mutation_sha256}.receipt.json").exists()


def test_conflicting_remote_target_can_only_be_abandoned(tmp_path: Path) -> None:
    vault, state, validated = _setup(tmp_path)
    advance_production_executor(
        state,
        vault,
        validated.mutation_sha256,
        allowed_roots=["11-Knowledge"],
    )

    def exists(**_):
        raise WebDAVTargetExists("already exists")

    conflict_sha = hashlib.sha256(b"# Human\n").hexdigest()

    def observe(**_):
        return WebDAVObservation(
            target_url="https://nextcloud.example/remote.md",
            result="conflict",
            status_code=200,
            content_sha256=conflict_sha,
            etag='"human"',
        )

    result = process_transport_request(
        state,
        validated.mutation_sha256,
        allowed_roots=["11-Knowledge"],
        base_url="https://unused.example/",
        username="sync",
        password="secret",
        create_fn=exists,
        observe_fn=observe,
    )
    assert result.result == "target_exists_conflict"
    assert reconcile_production_execution(
        state,
        validated.mutation_sha256,
        allowed_roots=["11-Knowledge"],
    ).status == "conflict"

    with pytest.raises(ProductionOrchestrationError, match="requires a matching"):
        record_remote_human_recovery(
            state,
            validated.mutation_sha256,
            decision="adopt_observed_effect",
            resolver="human",
            reason="Do not allow this",
            allowed_roots=["11-Knowledge"],
        )

    record_remote_human_recovery(
        state,
        validated.mutation_sha256,
        decision="abandon",
        resolver="human",
        reason="Keep the existing Human note",
        allowed_roots=["11-Knowledge"],
    )
    assert reconcile_production_execution(
        state,
        validated.mutation_sha256,
        allowed_roots=["11-Knowledge"],
    ).status == "resolved_abandoned"


def test_remote_recovery_detects_transport_result_tamper(tmp_path: Path) -> None:
    vault, state, validated = _setup(tmp_path)
    advance_production_executor(
        state,
        vault,
        validated.mutation_sha256,
        allowed_roots=["11-Knowledge"],
    )

    def exists(**_):
        raise WebDAVTargetExists("already exists")

    expected_sha = hashlib.sha256(validated.mutation.content.encode()).hexdigest()

    def observe(**_):
        return WebDAVObservation(
            target_url="https://nextcloud.example/remote.md",
            result="matching",
            status_code=200,
            content_sha256=expected_sha,
            etag='"before"',
        )

    process_transport_request(
        state,
        validated.mutation_sha256,
        allowed_roots=["11-Knowledge"],
        base_url="https://unused.example/",
        username="sync",
        password="secret",
        create_fn=exists,
        observe_fn=observe,
        observed_at="2026-08-22T03:25:00Z",
    )
    record_remote_human_recovery(
        state,
        validated.mutation_sha256,
        decision="adopt_observed_effect",
        resolver="human",
        reason="Accept observed state",
        allowed_roots=["11-Knowledge"],
    )

    result_path = state / "27-Transport" / f"{validated.mutation_sha256}.transport-result.json"
    payload = json.loads(result_path.read_text())
    payload["etag"] = '"tampered"'
    result_path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")

    reconciled = reconcile_production_execution(
        state,
        validated.mutation_sha256,
        allowed_roots=["11-Knowledge"],
    )
    assert reconciled.status == "conflict"
    assert "no longer matches" in (reconciled.reason or "")


def test_transport_worker_requires_executor_request(tmp_path: Path) -> None:
    _, state, validated = _setup(tmp_path)
    with pytest.raises(ProductionOrchestrationError, match="does not exist"):
        process_transport_request(
            state,
            validated.mutation_sha256,
            allowed_roots=["11-Knowledge"],
            base_url="https://unused.example/",
            username="sync",
            password="secret",
            create_fn=lambda **_: _created("a" * 64),
        )
