from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .artifact_lifecycle import (
    ArtifactLifecycleError,
    _canonical_json_bytes,
    _decode_json_object,
    _read_exact_file,
    _require_sha256,
    _store_immutable,
    _utc_now,
    ensure_artifact_layout,
    sha256_bytes,
    store_execution_receipt,
)
from .canonical_mutation import MutationValidationError, NotePolicy, execute_create_note
from .execution_orchestrator import (
    ExecutionOrchestrationError,
    ReconciliationResult,
    _execution_directory,
    _intent_path,
    _load_context,
    _load_intent,
    _mutation_lock,
    _prepare_unlocked,
    _reconcile_unlocked,
)


class HumanRecoveryError(RuntimeError):
    """Raised when a Human recovery decision is invalid or unsafe."""


@dataclass(frozen=True)
class RecoveryRecord:
    mutation_sha256: str
    intent_sha256: str
    decision: str
    observed_status: str
    target_path: str
    expected_content_sha256: str
    decided_at: str
    resolver: str
    reason: str

    def to_json_bytes(self) -> bytes:
        return _canonical_json_bytes(
            {
                "record_version": 1,
                "mutation_sha256": self.mutation_sha256,
                "intent_sha256": self.intent_sha256,
                "decision": self.decision,
                "observed_status": self.observed_status,
                "target_path": self.target_path,
                "expected_content_sha256": self.expected_content_sha256,
                "decided_at": self.decided_at,
                "resolver": self.resolver,
                "reason": self.reason,
            }
        )


def _recovery_path(ai_root: Path, digest: str) -> Path:
    layout = ensure_artifact_layout(ai_root)
    return layout.review / f"{digest}.recovery.json"


def parse_recovery_record(data: bytes) -> RecoveryRecord:
    try:
        value = _decode_json_object(data, label="recovery record")
    except ArtifactLifecycleError as exc:
        raise HumanRecoveryError(str(exc)) from exc

    required = {
        "record_version",
        "mutation_sha256",
        "intent_sha256",
        "decision",
        "observed_status",
        "target_path",
        "expected_content_sha256",
        "decided_at",
        "resolver",
        "reason",
    }
    if set(value) != required:
        raise HumanRecoveryError("recovery record properties do not match contract")
    if type(value["record_version"]) is not int or value["record_version"] != 1:
        raise HumanRecoveryError("recovery record_version must be integer 1")

    mutation_sha = value["mutation_sha256"]
    intent_sha = value["intent_sha256"]
    content_sha = value["expected_content_sha256"]
    if not isinstance(mutation_sha, str) or not isinstance(intent_sha, str) or not isinstance(content_sha, str):
        raise HumanRecoveryError("recovery SHA-256 fields must be strings")
    try:
        mutation_digest = _require_sha256(mutation_sha, label="mutation_sha256")
        intent_digest = _require_sha256(intent_sha, label="intent_sha256")
        content_digest = _require_sha256(content_sha, label="expected_content_sha256")
    except ArtifactLifecycleError as exc:
        raise HumanRecoveryError(str(exc)) from exc

    decision = value["decision"]
    if decision not in {"adopt_observed_effect", "abandon"}:
        raise HumanRecoveryError("recovery decision must be adopt_observed_effect or abandon")

    observed_status = value["observed_status"]
    if observed_status not in {"effect_observed_without_receipt", "conflict"}:
        raise HumanRecoveryError("recovery observed_status is not recoverable")
    if decision == "adopt_observed_effect" and observed_status != "effect_observed_without_receipt":
        raise HumanRecoveryError("adopt_observed_effect requires an observed matching effect")

    target_path = value["target_path"]
    decided_at = value["decided_at"]
    resolver = value["resolver"]
    reason = value["reason"]
    if not isinstance(target_path, str) or not target_path:
        raise HumanRecoveryError("recovery target_path must be a non-empty string")
    if not isinstance(decided_at, str) or not decided_at.endswith("Z"):
        raise HumanRecoveryError("recovery decided_at must be a UTC timestamp ending in Z")
    if not isinstance(resolver, str) or not resolver or len(resolver) > 256:
        raise HumanRecoveryError("resolver must be a non-empty string up to 256 characters")
    if not isinstance(reason, str) or not reason or len(reason) > 2048:
        raise HumanRecoveryError("reason must be a non-empty string up to 2048 characters")

    return RecoveryRecord(
        mutation_sha256=mutation_digest,
        intent_sha256=intent_digest,
        decision=decision,
        observed_status=observed_status,
        target_path=target_path,
        expected_content_sha256=content_digest,
        decided_at=decided_at,
        resolver=resolver,
        reason=reason,
    )


def load_recovery_record(ai_root: Path, mutation_sha256: str) -> RecoveryRecord | None:
    try:
        digest = _require_sha256(mutation_sha256, label="mutation_sha256")
    except ArtifactLifecycleError as exc:
        raise HumanRecoveryError(str(exc)) from exc
    path = _recovery_path(ai_root, digest)
    try:
        data = _read_exact_file(path)
    except ArtifactLifecycleError as exc:
        if not path.exists() and not path.is_symlink():
            return None
        raise HumanRecoveryError("recovery record exists but cannot be safely read") from exc
    record = parse_recovery_record(data)
    if record.mutation_sha256 != digest:
        raise HumanRecoveryError("recovery record is bound to another mutation")
    return record


def _intent_binding(ai_root: Path, digest: str):
    execution_dir = _execution_directory(ai_root)
    intent = _load_intent(execution_dir, digest)
    if intent is None:
        raise HumanRecoveryError("durable execution intent is required before recovery")
    intent_path = _intent_path(execution_dir, digest)
    try:
        intent_bytes = _read_exact_file(intent_path)
    except ArtifactLifecycleError as exc:
        raise HumanRecoveryError("execution intent cannot be safely read") from exc
    return execution_dir, intent, sha256_bytes(intent_bytes)


def _apply_recovery_unlocked(
    ai_root: Path,
    digest: str,
    raw: ReconciliationResult,
) -> ReconciliationResult:
    record = load_recovery_record(ai_root, digest)
    if record is None:
        return raw

    _, intent, intent_sha = _intent_binding(ai_root, digest)
    if record.intent_sha256 != intent_sha:
        return ReconciliationResult(
            "conflict",
            digest,
            intent.target_path,
            "recovery record no longer matches the durable execution intent",
            raw.receipt,
        )
    if record.target_path != intent.target_path or record.expected_content_sha256 != intent.content_sha256:
        return ReconciliationResult(
            "conflict",
            digest,
            intent.target_path,
            "recovery record target binding does not match execution intent",
            raw.receipt,
        )

    if raw.status == "completed":
        return ReconciliationResult(
            "conflict",
            digest,
            intent.target_path,
            "success receipt appeared after a Human recovery decision",
            raw.receipt,
        )

    if record.decision == "abandon":
        return ReconciliationResult(
            "resolved_abandoned",
            digest,
            intent.target_path,
            record.reason,
            None,
        )

    if raw.status != "effect_observed_without_receipt":
        return ReconciliationResult(
            "conflict",
            digest,
            intent.target_path,
            "adopted observed effect no longer matches the canonical target",
            raw.receipt,
        )
    return ReconciliationResult(
        "resolved_effect_adopted",
        digest,
        intent.target_path,
        (
            "Human accepted the observed canonical effect without claiming executor provenance; "
            + record.reason
        ),
        None,
    )


def record_human_recovery(
    ai_root: Path,
    vault_root: Path,
    mutation_sha256: str,
    *,
    decision: str,
    resolver: str,
    reason: str,
    allowed_roots: Sequence[str],
    decided_at: str | None = None,
) -> RecoveryRecord:
    try:
        digest = _require_sha256(mutation_sha256, label="mutation_sha256")
    except ArtifactLifecycleError as exc:
        raise HumanRecoveryError(str(exc)) from exc
    if decision not in {"adopt_observed_effect", "abandon"}:
        raise HumanRecoveryError("recovery decision must be adopt_observed_effect or abandon")
    if not isinstance(resolver, str) or not resolver or len(resolver) > 256:
        raise HumanRecoveryError("resolver must be a non-empty string up to 256 characters")
    if not isinstance(reason, str) or not reason or len(reason) > 2048:
        raise HumanRecoveryError("reason must be a non-empty string up to 2048 characters")

    execution_dir = _execution_directory(ai_root)
    with _mutation_lock(execution_dir, digest):
        existing = load_recovery_record(ai_root, digest)
        if existing is not None:
            if (
                existing.decision == decision
                and existing.resolver == resolver
                and existing.reason == reason
            ):
                return existing
            raise HumanRecoveryError("immutable recovery decision already exists")

        raw = _reconcile_unlocked(
            ai_root,
            vault_root,
            digest,
            allowed_roots=allowed_roots,
        )
        if raw.status not in {"effect_observed_without_receipt", "conflict"}:
            raise HumanRecoveryError(f"reconciliation state is not recoverable: {raw.status}")
        if decision == "adopt_observed_effect" and raw.status != "effect_observed_without_receipt":
            raise HumanRecoveryError("adopt_observed_effect is only valid for an observed matching effect")

        _, intent, intent_sha = _intent_binding(ai_root, digest)
        timestamp = decided_at or _utc_now()
        if not isinstance(timestamp, str) or not timestamp.endswith("Z"):
            raise HumanRecoveryError("decided_at must be a UTC timestamp ending in Z")
        record = RecoveryRecord(
            mutation_sha256=digest,
            intent_sha256=intent_sha,
            decision=decision,
            observed_status=raw.status,
            target_path=intent.target_path,
            expected_content_sha256=intent.content_sha256,
            decided_at=timestamp,
            resolver=resolver,
            reason=reason,
        )
        try:
            _store_immutable(_recovery_path(ai_root, digest), record.to_json_bytes())
        except ArtifactLifecycleError as exc:
            raise HumanRecoveryError(str(exc)) from exc
        return record


def reconcile_recovery_aware_execution(
    ai_root: Path,
    vault_root: Path,
    mutation_sha256: str,
    *,
    allowed_roots: Sequence[str],
) -> ReconciliationResult:
    try:
        digest = _require_sha256(mutation_sha256, label="mutation_sha256")
    except ArtifactLifecycleError as exc:
        raise HumanRecoveryError(str(exc)) from exc
    execution_dir = _execution_directory(ai_root)
    with _mutation_lock(execution_dir, digest):
        raw = _reconcile_unlocked(
            ai_root,
            vault_root,
            digest,
            allowed_roots=allowed_roots,
        )
        return _apply_recovery_unlocked(ai_root, digest, raw)


def run_recovery_aware_create_note(
    ai_root: Path,
    vault_root: Path,
    mutation_sha256: str,
    *,
    allowed_roots: Sequence[str],
    note_policy: NotePolicy | None = None,
) -> ReconciliationResult:
    try:
        digest = _require_sha256(mutation_sha256, label="mutation_sha256")
    except ArtifactLifecycleError as exc:
        raise HumanRecoveryError(str(exc)) from exc
    execution_dir = _execution_directory(ai_root)
    with _mutation_lock(execution_dir, digest):
        raw = _reconcile_unlocked(
            ai_root,
            vault_root,
            digest,
            allowed_roots=allowed_roots,
        )
        state = _apply_recovery_unlocked(ai_root, digest, raw)
        if state.status in {
            "completed",
            "effect_observed_without_receipt",
            "conflict",
            "resolved_effect_adopted",
            "resolved_abandoned",
        }:
            return state

        if state.status == "not_started":
            _prepare_unlocked(
                ai_root,
                vault_root,
                digest,
                allowed_roots=allowed_roots,
                note_policy=note_policy,
                prepared_at=None,
            )
            raw = _reconcile_unlocked(
                ai_root,
                vault_root,
                digest,
                allowed_roots=allowed_roots,
            )
            state = _apply_recovery_unlocked(ai_root, digest, raw)

        if state.status != "pending_retry":
            return state

        mutation_bytes, _, _, review = _load_context(
            ai_root,
            digest,
            allowed_roots=allowed_roots,
        )
        try:
            receipt = execute_create_note(
                mutation_bytes,
                approval=review.to_approval(),
                vault_root=vault_root,
                allowed_roots=allowed_roots,
                note_policy=note_policy,
            )
        except MutationValidationError as exc:
            raise ExecutionOrchestrationError(
                f"effect-boundary validation rejected mutation: {exc}"
            ) from exc
        try:
            store_execution_receipt(ai_root, receipt)
        except ArtifactLifecycleError as exc:
            raise ExecutionOrchestrationError(
                "canonical effect completed but receipt persistence failed"
            ) from exc

        raw = _reconcile_unlocked(
            ai_root,
            vault_root,
            digest,
            allowed_roots=allowed_roots,
        )
        final = _apply_recovery_unlocked(ai_root, digest, raw)
        if final.status != "completed":
            raise ExecutionOrchestrationError(
                f"receipt persisted but recovery-aware reconciliation is {final.status}"
            )
        return final
