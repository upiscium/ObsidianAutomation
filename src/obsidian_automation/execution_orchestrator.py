from __future__ import annotations

import fcntl
import hashlib
import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

from .artifact_lifecycle import (
    ArtifactLifecycleError,
    ReviewRecord,
    _canonical_json_bytes,
    _decode_json_object,
    _read_exact_file,
    _require_sha256,
    _store_immutable,
    _utc_now,
    ensure_artifact_layout,
    parse_review_record,
    sha256_bytes,
    store_execution_receipt,
)
from .canonical_mutation import (
    CreateNoteMutation,
    ExecutionReceipt,
    MutationValidationError,
    NotePolicy,
    _open_parent_dirfd,
    _parse_create_note,
    _safe_components,
    execute_create_note,
    validate_create_note,
)


class ExecutionOrchestrationError(RuntimeError):
    """Raised when durable execution state is unsafe or inconsistent."""


@dataclass(frozen=True)
class ExecutionIntent:
    mutation_sha256: str
    approval_sha256: str
    target_path: str
    content_sha256: str
    prepared_at: str

    def to_json_bytes(self) -> bytes:
        return _canonical_json_bytes(
            {
                "record_version": 1,
                "mutation_sha256": self.mutation_sha256,
                "approval_sha256": self.approval_sha256,
                "target_path": self.target_path,
                "content_sha256": self.content_sha256,
                "prepared_at": self.prepared_at,
            }
        )


@dataclass(frozen=True)
class ReconciliationResult:
    status: str
    mutation_sha256: str
    target_path: str | None
    reason: str | None
    receipt: ExecutionReceipt | None = None


def _execution_directory(ai_root: Path) -> Path:
    layout = ensure_artifact_layout(ai_root)
    execution = layout.root / "25-Execution"
    execution.mkdir(mode=0o755, exist_ok=True)
    try:
        info = execution.lstat()
    except FileNotFoundError as exc:
        raise ExecutionOrchestrationError("execution directory disappeared") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ExecutionOrchestrationError(
            f"execution path is not a safe directory: {execution}"
        )
    parent_flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        parent_flags |= os.O_CLOEXEC
    parent_fd = os.open(layout.root, parent_flags)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    return execution


@contextmanager
def _mutation_lock(execution_dir: Path, mutation_sha256: str) -> Iterator[None]:
    digest = _require_sha256(mutation_sha256, label="mutation_sha256")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    lock_path = execution_dir / f"{digest}.lock"
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise ExecutionOrchestrationError("cannot open execution lock") from exc
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def parse_execution_intent(data: bytes) -> ExecutionIntent:
    try:
        value = _decode_json_object(data, label="execution intent")
    except ArtifactLifecycleError as exc:
        raise ExecutionOrchestrationError(str(exc)) from exc
    required = {
        "record_version",
        "mutation_sha256",
        "approval_sha256",
        "target_path",
        "content_sha256",
        "prepared_at",
    }
    if set(value) != required:
        raise ExecutionOrchestrationError(
            "execution intent properties do not match contract"
        )
    if type(value["record_version"]) is not int or value["record_version"] != 1:
        raise ExecutionOrchestrationError("execution intent record_version must be 1")

    mutation_sha = value["mutation_sha256"]
    approval_sha = value["approval_sha256"]
    content_sha = value["content_sha256"]
    target_path = value["target_path"]
    prepared_at = value["prepared_at"]
    if not isinstance(mutation_sha, str):
        raise ExecutionOrchestrationError("execution intent mutation_sha256 is invalid")
    if not isinstance(approval_sha, str):
        raise ExecutionOrchestrationError("execution intent approval_sha256 is invalid")
    if not isinstance(content_sha, str):
        raise ExecutionOrchestrationError("execution intent content_sha256 is invalid")
    try:
        mutation_digest = _require_sha256(mutation_sha, label="mutation_sha256")
        approval_digest = _require_sha256(approval_sha, label="approval_sha256")
        content_digest = _require_sha256(content_sha, label="content_sha256")
    except ArtifactLifecycleError as exc:
        raise ExecutionOrchestrationError(str(exc)) from exc
    if not isinstance(target_path, str) or not target_path:
        raise ExecutionOrchestrationError("execution intent target_path is invalid")
    if not isinstance(prepared_at, str) or not prepared_at.endswith("Z"):
        raise ExecutionOrchestrationError("execution intent prepared_at must end in Z")
    return ExecutionIntent(
        mutation_sha256=mutation_digest,
        approval_sha256=approval_digest,
        target_path=target_path,
        content_sha256=content_digest,
        prepared_at=prepared_at,
    )


def _intent_path(execution_dir: Path, digest: str) -> Path:
    return execution_dir / f"{digest}.intent.json"


def _load_intent(execution_dir: Path, digest: str) -> ExecutionIntent | None:
    path = _intent_path(execution_dir, digest)
    try:
        data = _read_exact_file(path)
    except ArtifactLifecycleError as exc:
        if not os.path.lexists(path):
            return None
        raise ExecutionOrchestrationError(str(exc)) from exc
    intent = parse_execution_intent(data)
    if intent.mutation_sha256 != digest:
        raise ExecutionOrchestrationError("execution intent is bound to another mutation")
    return intent


def _load_context(
    ai_root: Path,
    digest: str,
    *,
    allowed_roots: Sequence[str],
) -> tuple[bytes, CreateNoteMutation, bytes, ReviewRecord]:
    layout = ensure_artifact_layout(ai_root)
    mutation_path = layout.validation / f"{digest}.mutation.json"
    try:
        mutation_bytes = _read_exact_file(mutation_path)
    except ArtifactLifecycleError as exc:
        raise ExecutionOrchestrationError(str(exc)) from exc
    if sha256_bytes(mutation_bytes) != digest:
        raise ExecutionOrchestrationError("validated mutation artifact hash mismatch")
    try:
        mutation = _parse_create_note(mutation_bytes)
        parts = _safe_components(mutation.target_path, label="target.path")
        authorized = False
        for root in allowed_roots:
            root_parts = _safe_components(root, label="allowed root")
            if len(parts) > len(root_parts) and parts[: len(root_parts)] == root_parts:
                authorized = True
                break
    except MutationValidationError as exc:
        raise ExecutionOrchestrationError(str(exc)) from exc
    if not authorized:
        raise ExecutionOrchestrationError(
            "validated target is outside current deployment-policy-approved roots"
        )

    review_path = layout.review / f"{digest}.approval.json"
    try:
        review_bytes = _read_exact_file(review_path)
        review = parse_review_record(review_bytes)
    except ArtifactLifecycleError as exc:
        raise ExecutionOrchestrationError("review record is missing or invalid") from exc
    if review.mutation_sha256 != digest:
        raise ExecutionOrchestrationError("review record is bound to another mutation")
    if not review.approved:
        raise ExecutionOrchestrationError("mutation does not have affirmative human approval")
    return mutation_bytes, mutation, review_bytes, review


def _verify_intent(
    intent: ExecutionIntent,
    *,
    mutation: CreateNoteMutation,
    review_bytes: bytes,
) -> None:
    if intent.target_path != mutation.target_path:
        raise ExecutionOrchestrationError("execution intent target does not match mutation")
    expected_content = hashlib.sha256(mutation.content.encode("utf-8")).hexdigest()
    if intent.content_sha256 != expected_content:
        raise ExecutionOrchestrationError("execution intent content hash does not match mutation")
    if intent.approval_sha256 != sha256_bytes(review_bytes):
        raise ExecutionOrchestrationError("approval artifact changed after intent preparation")


def _parse_receipt(data: bytes) -> ExecutionReceipt:
    try:
        value = _decode_json_object(data, label="execution receipt")
    except ArtifactLifecycleError as exc:
        raise ExecutionOrchestrationError(str(exc)) from exc
    required = {
        "mutation_id",
        "mutation_sha256",
        "target_path",
        "content_sha256",
        "executed_at",
        "result",
    }
    if set(value) != required or value["result"] != "success":
        raise ExecutionOrchestrationError("execution receipt does not match contract")
    fields = ("mutation_id", "mutation_sha256", "target_path", "content_sha256", "executed_at")
    if any(not isinstance(value[field], str) or not value[field] for field in fields):
        raise ExecutionOrchestrationError("execution receipt contains invalid fields")
    try:
        mutation_sha = _require_sha256(value["mutation_sha256"], label="mutation_sha256")
        content_sha = _require_sha256(value["content_sha256"], label="content_sha256")
    except ArtifactLifecycleError as exc:
        raise ExecutionOrchestrationError(str(exc)) from exc
    if not value["executed_at"].endswith("Z"):
        raise ExecutionOrchestrationError("receipt executed_at must end in Z")
    return ExecutionReceipt(
        mutation_id=value["mutation_id"],
        mutation_sha256=mutation_sha,
        target_path=value["target_path"],
        content_sha256=content_sha,
        executed_at=value["executed_at"],
    )


def _observe_target(
    vault_root: Path,
    target_path: str,
    expected_content_sha256: str,
) -> tuple[str, str | None]:
    try:
        parts = _safe_components(target_path, label="target.path")
        with _open_parent_dirfd(vault_root, parts) as (parent_fd, target_name):
            names = os.listdir(parent_fd)
            folded = target_name.casefold()
            matches = [name for name in names if name.casefold() == folded]
            if not matches:
                return "absent", None
            if matches != [target_name]:
                return "conflict", f"target has case-fold collision: {matches!r}"

            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            try:
                fd = os.open(target_name, flags, dir_fd=parent_fd)
            except OSError:
                return "conflict", "target is not a safely readable regular file"
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    return "conflict", "target exists but is not a regular file"
                digest = hashlib.sha256()
                while True:
                    chunk = os.read(fd, 1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
            finally:
                os.close(fd)
    except MutationValidationError as exc:
        return "conflict", str(exc)

    if digest.hexdigest() == expected_content_sha256:
        return "matching", None
    return "conflict", "target exists with content different from execution intent"


def _reconcile_unlocked(
    ai_root: Path,
    vault_root: Path,
    digest: str,
    *,
    allowed_roots: Sequence[str],
) -> ReconciliationResult:
    layout = ensure_artifact_layout(ai_root)
    execution_dir = _execution_directory(ai_root)
    intent = _load_intent(execution_dir, digest)
    if intent is None:
        return ReconciliationResult("not_started", digest, None, None)

    mutation_bytes, mutation, review_bytes, _ = _load_context(
        ai_root,
        digest,
        allowed_roots=allowed_roots,
    )
    if sha256_bytes(mutation_bytes) != intent.mutation_sha256:
        raise ExecutionOrchestrationError("intent mutation hash no longer matches artifact")
    _verify_intent(intent, mutation=mutation, review_bytes=review_bytes)

    target_status, target_reason = _observe_target(
        vault_root,
        intent.target_path,
        intent.content_sha256,
    )
    receipt_path = layout.receipts / f"{digest}.receipt.json"
    try:
        receipt_bytes = _read_exact_file(receipt_path)
    except ArtifactLifecycleError as exc:
        if not os.path.lexists(receipt_path):
            receipt_bytes = None
        else:
            raise ExecutionOrchestrationError(
                "receipt exists but cannot be safely read"
            ) from exc

    if receipt_bytes is not None:
        receipt = _parse_receipt(receipt_bytes)
        expected = (
            receipt.mutation_sha256 == digest
            and receipt.mutation_id == mutation.mutation_id
            and receipt.target_path == intent.target_path
            and receipt.content_sha256 == intent.content_sha256
        )
        if not expected:
            return ReconciliationResult(
                "conflict",
                digest,
                intent.target_path,
                "receipt does not match durable execution intent",
                receipt,
            )
        if target_status != "matching":
            return ReconciliationResult(
                "conflict",
                digest,
                intent.target_path,
                "receipt claims success but canonical target no longer matches intent",
                receipt,
            )
        return ReconciliationResult(
            "completed", digest, intent.target_path, None, receipt
        )

    if target_status == "absent":
        return ReconciliationResult("pending_retry", digest, intent.target_path, None)
    if target_status == "matching":
        return ReconciliationResult(
            "effect_observed_without_receipt",
            digest,
            intent.target_path,
            (
                "target matches intended content but no receipt proves which actor "
                "created it; refusing automatic success claim"
            ),
        )
    return ReconciliationResult(
        "conflict",
        digest,
        intent.target_path,
        target_reason or "canonical target conflicts with execution intent",
    )


def reconcile_execution(
    ai_root: Path,
    vault_root: Path,
    mutation_sha256: str,
    *,
    allowed_roots: Sequence[str],
) -> ReconciliationResult:
    try:
        digest = _require_sha256(mutation_sha256, label="mutation_sha256")
    except ArtifactLifecycleError as exc:
        raise ExecutionOrchestrationError(str(exc)) from exc
    execution_dir = _execution_directory(ai_root)
    with _mutation_lock(execution_dir, digest):
        return _reconcile_unlocked(
            ai_root,
            vault_root,
            digest,
            allowed_roots=allowed_roots,
        )


def _prepare_unlocked(
    ai_root: Path,
    vault_root: Path,
    digest: str,
    *,
    allowed_roots: Sequence[str],
    note_policy: NotePolicy | None,
    prepared_at: str | None,
) -> ExecutionIntent:
    execution_dir = _execution_directory(ai_root)
    existing = _load_intent(execution_dir, digest)
    mutation_bytes, mutation, review_bytes, _ = _load_context(
        ai_root,
        digest,
        allowed_roots=allowed_roots,
    )
    if existing is not None:
        _verify_intent(existing, mutation=mutation, review_bytes=review_bytes)
        return existing

    try:
        validated = validate_create_note(
            mutation_bytes,
            vault_root=vault_root,
            allowed_roots=allowed_roots,
            note_policy=note_policy,
        )
    except MutationValidationError as exc:
        raise ExecutionOrchestrationError(
            f"mutation is no longer admissible before intent preparation: {exc}"
        ) from exc
    if validated.mutation_sha256 != digest or validated.artifact_bytes != mutation_bytes:
        raise ExecutionOrchestrationError(
            "stored validated mutation is not the exact canonical artifact"
        )

    timestamp = prepared_at or _utc_now()
    if not isinstance(timestamp, str) or not timestamp.endswith("Z"):
        raise ExecutionOrchestrationError("prepared_at must end in Z")
    intent = ExecutionIntent(
        mutation_sha256=digest,
        approval_sha256=sha256_bytes(review_bytes),
        target_path=mutation.target_path,
        content_sha256=hashlib.sha256(mutation.content.encode("utf-8")).hexdigest(),
        prepared_at=timestamp,
    )
    try:
        _store_immutable(_intent_path(execution_dir, digest), intent.to_json_bytes())
    except ArtifactLifecycleError as exc:
        raise ExecutionOrchestrationError(str(exc)) from exc
    return intent


def prepare_execution_intent(
    ai_root: Path,
    vault_root: Path,
    mutation_sha256: str,
    *,
    allowed_roots: Sequence[str],
    note_policy: NotePolicy | None = None,
    prepared_at: str | None = None,
) -> ExecutionIntent:
    try:
        digest = _require_sha256(mutation_sha256, label="mutation_sha256")
    except ArtifactLifecycleError as exc:
        raise ExecutionOrchestrationError(str(exc)) from exc
    execution_dir = _execution_directory(ai_root)
    with _mutation_lock(execution_dir, digest):
        return _prepare_unlocked(
            ai_root,
            vault_root,
            digest,
            allowed_roots=allowed_roots,
            note_policy=note_policy,
            prepared_at=prepared_at,
        )


def run_approved_create_note(
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
        raise ExecutionOrchestrationError(str(exc)) from exc
    execution_dir = _execution_directory(ai_root)
    with _mutation_lock(execution_dir, digest):
        state = _reconcile_unlocked(
            ai_root,
            vault_root,
            digest,
            allowed_roots=allowed_roots,
        )
        if state.status == "not_started":
            _prepare_unlocked(
                ai_root,
                vault_root,
                digest,
                allowed_roots=allowed_roots,
                note_policy=note_policy,
                prepared_at=None,
            )
            state = _reconcile_unlocked(
                ai_root,
                vault_root,
                digest,
                allowed_roots=allowed_roots,
            )

        if state.status in {
            "completed",
            "effect_observed_without_receipt",
            "conflict",
        }:
            return state
        if state.status != "pending_retry":
            raise ExecutionOrchestrationError(
                f"unexpected reconciliation state: {state.status}"
            )

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

        final = _reconcile_unlocked(
            ai_root,
            vault_root,
            digest,
            allowed_roots=allowed_roots,
        )
        if final.status != "completed":
            raise ExecutionOrchestrationError(
                f"receipt persisted but reconciliation is {final.status}"
            )
        return final
