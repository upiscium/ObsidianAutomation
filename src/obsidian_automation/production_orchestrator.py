from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import stat
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Sequence

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
from .canonical_mutation import ExecutionReceipt, NotePolicy
from .execution_orchestrator import (
    ExecutionOrchestrationError,
    ReconciliationResult,
    _execution_directory,
    _intent_path,
    _load_context,
    _load_intent,
    _parse_receipt,
    _prepare_unlocked,
    _verify_intent,
)
from .human_recovery import load_recovery_record
from .webdav_create import (
    WebDAVCreateError,
    WebDAVCreateResult,
    WebDAVObservation,
    WebDAVTargetExists,
    _read_password,
    conditional_create,
    observe_remote,
)


class ProductionOrchestrationError(RuntimeError):
    """Raised when remote production execution state is unsafe or inconsistent."""


@dataclass(frozen=True)
class TransportRequest:
    mutation_sha256: str
    intent_sha256: str
    target_path: str
    content_sha256: str
    requested_at: str

    def to_json_bytes(self) -> bytes:
        return _canonical_json_bytes(
            {
                "record_version": 1,
                "mutation_sha256": self.mutation_sha256,
                "intent_sha256": self.intent_sha256,
                "target_path": self.target_path,
                "content_sha256": self.content_sha256,
                "requested_at": self.requested_at,
            }
        )


@dataclass(frozen=True)
class TransportResult:
    mutation_sha256: str
    request_sha256: str
    result: str
    target_path: str
    expected_content_sha256: str
    observed_content_sha256: str | None
    observed_at: str
    http_status: int
    etag: str | None

    def to_json_bytes(self) -> bytes:
        return _canonical_json_bytes(
            {
                "record_version": 1,
                "mutation_sha256": self.mutation_sha256,
                "request_sha256": self.request_sha256,
                "result": self.result,
                "target_path": self.target_path,
                "expected_content_sha256": self.expected_content_sha256,
                "observed_content_sha256": self.observed_content_sha256,
                "observed_at": self.observed_at,
                "http_status": self.http_status,
                "etag": self.etag,
            }
        )


@dataclass(frozen=True)
class RemoteRecoveryRecord:
    mutation_sha256: str
    intent_sha256: str
    transport_result_sha256: str
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
                "transport_result_sha256": self.transport_result_sha256,
                "decision": self.decision,
                "observed_status": self.observed_status,
                "target_path": self.target_path,
                "expected_content_sha256": self.expected_content_sha256,
                "decided_at": self.decided_at,
                "resolver": self.resolver,
                "reason": self.reason,
            }
        )


CreateCallable = Callable[..., WebDAVCreateResult]
ObserveCallable = Callable[..., WebDAVObservation]


def _safe_existing_directory(ai_root: Path, name: str) -> Path:
    root = ensure_artifact_layout(ai_root).root
    path = root / name
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise ProductionOrchestrationError(f"required directory does not exist: {path}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ProductionOrchestrationError(f"path is not a safe directory: {path}")
    return path


def _lock_directory(ai_root: Path) -> Path:
    return _safe_existing_directory(ai_root, "24-Locks")


def _transport_directory(ai_root: Path) -> Path:
    return _safe_existing_directory(ai_root, "27-Transport")


@contextmanager
def _production_lock(lock_dir: Path, mutation_sha256: str) -> Iterator[None]:
    digest = _require_sha256(mutation_sha256, label="mutation_sha256")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = os.open(lock_dir / f"{digest}.lock", flags, 0o600)
    except OSError as exc:
        raise ProductionOrchestrationError("cannot open production mutation lock") from exc
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _request_path(execution_dir: Path, digest: str) -> Path:
    return execution_dir / f"{digest}.transport-request.json"


def _result_path(transport_dir: Path, digest: str) -> Path:
    return transport_dir / f"{digest}.transport-result.json"


def _remote_recovery_path(ai_root: Path, digest: str) -> Path:
    return ensure_artifact_layout(ai_root).review / f"{digest}.remote-recovery.json"


def _require_timestamp(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ProductionOrchestrationError(f"{label} must be a UTC timestamp ending in Z")
    return value


def parse_transport_request(data: bytes) -> TransportRequest:
    try:
        value = _decode_json_object(data, label="transport request")
    except ArtifactLifecycleError as exc:
        raise ProductionOrchestrationError(str(exc)) from exc
    required = {
        "record_version",
        "mutation_sha256",
        "intent_sha256",
        "target_path",
        "content_sha256",
        "requested_at",
    }
    if set(value) != required or value.get("record_version") != 1:
        raise ProductionOrchestrationError("transport request properties do not match contract")
    try:
        mutation_sha = _require_sha256(value["mutation_sha256"], label="mutation_sha256")
        intent_sha = _require_sha256(value["intent_sha256"], label="intent_sha256")
        content_sha = _require_sha256(value["content_sha256"], label="content_sha256")
    except ArtifactLifecycleError as exc:
        raise ProductionOrchestrationError(str(exc)) from exc
    target_path = value["target_path"]
    if not isinstance(target_path, str) or not target_path:
        raise ProductionOrchestrationError("transport request target_path is invalid")
    return TransportRequest(
        mutation_sha256=mutation_sha,
        intent_sha256=intent_sha,
        target_path=target_path,
        content_sha256=content_sha,
        requested_at=_require_timestamp(value["requested_at"], label="requested_at"),
    )


def parse_transport_result(data: bytes) -> TransportResult:
    try:
        value = _decode_json_object(data, label="transport result")
    except ArtifactLifecycleError as exc:
        raise ProductionOrchestrationError(str(exc)) from exc
    required = {
        "record_version",
        "mutation_sha256",
        "request_sha256",
        "result",
        "target_path",
        "expected_content_sha256",
        "observed_content_sha256",
        "observed_at",
        "http_status",
        "etag",
    }
    if set(value) != required or value.get("record_version") != 1:
        raise ProductionOrchestrationError("transport result properties do not match contract")
    try:
        mutation_sha = _require_sha256(value["mutation_sha256"], label="mutation_sha256")
        request_sha = _require_sha256(value["request_sha256"], label="request_sha256")
        expected_sha = _require_sha256(
            value["expected_content_sha256"], label="expected_content_sha256"
        )
    except ArtifactLifecycleError as exc:
        raise ProductionOrchestrationError(str(exc)) from exc
    result = value["result"]
    if result not in {"created_verified", "target_exists_matching", "target_exists_conflict"}:
        raise ProductionOrchestrationError("transport result is invalid")
    observed = value["observed_content_sha256"]
    if observed is None:
        observed_sha = None
    else:
        try:
            observed_sha = _require_sha256(observed, label="observed_content_sha256")
        except ArtifactLifecycleError as exc:
            raise ProductionOrchestrationError(str(exc)) from exc
    if result in {"created_verified", "target_exists_matching"} and observed_sha != expected_sha:
        raise ProductionOrchestrationError("matching transport result must bind expected remote bytes")
    if result == "target_exists_conflict" and observed_sha is None:
        raise ProductionOrchestrationError("conflicting transport result requires observed content hash")
    target_path = value["target_path"]
    if not isinstance(target_path, str) or not target_path:
        raise ProductionOrchestrationError("transport result target_path is invalid")
    http_status = value["http_status"]
    if type(http_status) is not int or not 100 <= http_status <= 599:
        raise ProductionOrchestrationError("transport result http_status is invalid")
    etag = value["etag"]
    if etag is not None and not isinstance(etag, str):
        raise ProductionOrchestrationError("transport result etag is invalid")
    return TransportResult(
        mutation_sha256=mutation_sha,
        request_sha256=request_sha,
        result=result,
        target_path=target_path,
        expected_content_sha256=expected_sha,
        observed_content_sha256=observed_sha,
        observed_at=_require_timestamp(value["observed_at"], label="observed_at"),
        http_status=http_status,
        etag=etag,
    )


def parse_remote_recovery(data: bytes) -> RemoteRecoveryRecord:
    try:
        value = _decode_json_object(data, label="remote recovery record")
    except ArtifactLifecycleError as exc:
        raise ProductionOrchestrationError(str(exc)) from exc
    required = {
        "record_version",
        "mutation_sha256",
        "intent_sha256",
        "transport_result_sha256",
        "decision",
        "observed_status",
        "target_path",
        "expected_content_sha256",
        "decided_at",
        "resolver",
        "reason",
    }
    if set(value) != required or value.get("record_version") != 1:
        raise ProductionOrchestrationError("remote recovery properties do not match contract")
    try:
        mutation_sha = _require_sha256(value["mutation_sha256"], label="mutation_sha256")
        intent_sha = _require_sha256(value["intent_sha256"], label="intent_sha256")
        result_sha = _require_sha256(
            value["transport_result_sha256"], label="transport_result_sha256"
        )
        expected_sha = _require_sha256(
            value["expected_content_sha256"], label="expected_content_sha256"
        )
    except ArtifactLifecycleError as exc:
        raise ProductionOrchestrationError(str(exc)) from exc
    decision = value["decision"]
    observed_status = value["observed_status"]
    if decision not in {"adopt_observed_effect", "abandon"}:
        raise ProductionOrchestrationError("remote recovery decision is invalid")
    if observed_status not in {"remote_effect_observed_without_receipt", "conflict"}:
        raise ProductionOrchestrationError("remote recovery observed_status is invalid")
    if decision == "adopt_observed_effect" and observed_status != "remote_effect_observed_without_receipt":
        raise ProductionOrchestrationError("adopt_observed_effect requires matching remote effect")
    target_path = value["target_path"]
    resolver = value["resolver"]
    reason = value["reason"]
    if not isinstance(target_path, str) or not target_path:
        raise ProductionOrchestrationError("remote recovery target_path is invalid")
    if not isinstance(resolver, str) or not resolver or len(resolver) > 256:
        raise ProductionOrchestrationError("remote recovery resolver is invalid")
    if not isinstance(reason, str) or not reason or len(reason) > 2048:
        raise ProductionOrchestrationError("remote recovery reason is invalid")
    return RemoteRecoveryRecord(
        mutation_sha256=mutation_sha,
        intent_sha256=intent_sha,
        transport_result_sha256=result_sha,
        decision=decision,
        observed_status=observed_status,
        target_path=target_path,
        expected_content_sha256=expected_sha,
        decided_at=_require_timestamp(value["decided_at"], label="decided_at"),
        resolver=resolver,
        reason=reason,
    )


def _load_optional(path: Path, parser):
    try:
        data = _read_exact_file(path)
    except ArtifactLifecycleError as exc:
        if not os.path.lexists(path):
            return None, None
        raise ProductionOrchestrationError(
            f"artifact exists but cannot be safely read: {path}"
        ) from exc
    return data, parser(data)


def _load_request(execution_dir: Path, digest: str):
    data, request = _load_optional(_request_path(execution_dir, digest), parse_transport_request)
    if request is not None and request.mutation_sha256 != digest:
        raise ProductionOrchestrationError("transport request is bound to another mutation")
    return data, request


def _load_result(transport_dir: Path, digest: str):
    data, result = _load_optional(_result_path(transport_dir, digest), parse_transport_result)
    if result is not None and result.mutation_sha256 != digest:
        raise ProductionOrchestrationError("transport result is bound to another mutation")
    return data, result


def _load_remote_recovery(ai_root: Path, digest: str):
    data, recovery = _load_optional(_remote_recovery_path(ai_root, digest), parse_remote_recovery)
    if recovery is not None and recovery.mutation_sha256 != digest:
        raise ProductionOrchestrationError("remote recovery is bound to another mutation")
    return data, recovery


def _load_intent_binding(ai_root: Path, digest: str):
    execution_dir = _execution_directory(ai_root)
    intent = _load_intent(execution_dir, digest)
    if intent is None:
        return execution_dir, None, None
    try:
        intent_bytes = _read_exact_file(_intent_path(execution_dir, digest))
    except ArtifactLifecycleError as exc:
        raise ProductionOrchestrationError("execution intent cannot be safely read") from exc
    return execution_dir, intent, sha256_bytes(intent_bytes)


def _verify_request_binding(
    ai_root: Path,
    digest: str,
    request_bytes: bytes,
    request: TransportRequest,
    *,
    allowed_roots: Sequence[str],
):
    execution_dir, intent, intent_sha = _load_intent_binding(ai_root, digest)
    if intent is None or intent_sha is None:
        raise ProductionOrchestrationError("transport request requires durable execution intent")
    mutation_bytes, mutation, review_bytes, review = _load_context(
        ai_root,
        digest,
        allowed_roots=allowed_roots,
    )
    _verify_intent(intent, mutation=mutation, review_bytes=review_bytes)
    if request.intent_sha256 != intent_sha:
        raise ProductionOrchestrationError("transport request no longer matches execution intent")
    if request.target_path != intent.target_path or request.content_sha256 != intent.content_sha256:
        raise ProductionOrchestrationError("transport request target binding does not match intent")
    return execution_dir, intent, mutation_bytes, mutation, review


def _verify_result_binding(request_bytes: bytes, request: TransportRequest, result: TransportResult) -> None:
    if result.request_sha256 != sha256_bytes(request_bytes):
        raise ProductionOrchestrationError("transport result does not match exact request bytes")
    if result.target_path != request.target_path:
        raise ProductionOrchestrationError("transport result target does not match request")
    if result.expected_content_sha256 != request.content_sha256:
        raise ProductionOrchestrationError("transport result content hash does not match request")


def _legacy_recovery_exists(ai_root: Path, digest: str) -> bool:
    try:
        return load_recovery_record(ai_root, digest) is not None
    except Exception as exc:
        raise ProductionOrchestrationError("local recovery artifact is invalid") from exc


def _store_request_unlocked(
    ai_root: Path,
    vault_root: Path,
    digest: str,
    *,
    allowed_roots: Sequence[str],
    note_policy: NotePolicy | None,
    requested_at: str | None,
) -> TransportRequest:
    execution_dir = _execution_directory(ai_root)
    request_bytes, existing = _load_request(execution_dir, digest)
    if existing is not None and request_bytes is not None:
        _verify_request_binding(
            ai_root,
            digest,
            request_bytes,
            existing,
            allowed_roots=allowed_roots,
        )
        return existing

    intent = _prepare_unlocked(
        ai_root,
        vault_root,
        digest,
        allowed_roots=allowed_roots,
        note_policy=note_policy,
        prepared_at=None,
    )
    try:
        intent_bytes = _read_exact_file(_intent_path(execution_dir, digest))
    except ArtifactLifecycleError as exc:
        raise ProductionOrchestrationError("execution intent cannot be safely read") from exc
    timestamp = requested_at or _utc_now()
    _require_timestamp(timestamp, label="requested_at")
    request = TransportRequest(
        mutation_sha256=digest,
        intent_sha256=sha256_bytes(intent_bytes),
        target_path=intent.target_path,
        content_sha256=intent.content_sha256,
        requested_at=timestamp,
    )
    try:
        _store_immutable(_request_path(execution_dir, digest), request.to_json_bytes())
    except ArtifactLifecycleError as exc:
        raise ProductionOrchestrationError(str(exc)) from exc
    return request


def prepare_transport_request(
    ai_root: Path,
    vault_root: Path,
    mutation_sha256: str,
    *,
    allowed_roots: Sequence[str],
    note_policy: NotePolicy | None = None,
    requested_at: str | None = None,
) -> TransportRequest:
    try:
        digest = _require_sha256(mutation_sha256, label="mutation_sha256")
    except ArtifactLifecycleError as exc:
        raise ProductionOrchestrationError(str(exc)) from exc
    execution_dir = _execution_directory(ai_root)
    lock_dir = _lock_directory(ai_root)
    _transport_directory(ai_root)
    with _production_lock(lock_dir, digest):
        if _legacy_recovery_exists(ai_root, digest) or _load_remote_recovery(ai_root, digest)[1] is not None:
            raise ProductionOrchestrationError("Human recovery decision already exists")
        return _store_request_unlocked(
            ai_root,
            vault_root,
            digest,
            allowed_roots=allowed_roots,
            note_policy=note_policy,
            requested_at=requested_at,
        )


def process_transport_request(
    ai_root: Path,
    mutation_sha256: str,
    *,
    allowed_roots: Sequence[str],
    base_url: str,
    username: str,
    password: str,
    timeout: float = 30.0,
    allow_http: bool = False,
    create_fn: CreateCallable = conditional_create,
    observe_fn: ObserveCallable = observe_remote,
    observed_at: str | None = None,
) -> TransportResult:
    try:
        digest = _require_sha256(mutation_sha256, label="mutation_sha256")
    except ArtifactLifecycleError as exc:
        raise ProductionOrchestrationError(str(exc)) from exc
    execution_dir = _execution_directory(ai_root)
    lock_dir = _lock_directory(ai_root)
    transport_dir = _transport_directory(ai_root)
    with _production_lock(lock_dir, digest):
        request_bytes, request = _load_request(execution_dir, digest)
        if request is None or request_bytes is None:
            raise ProductionOrchestrationError("transport request does not exist")
        _, _, _, mutation, _ = _verify_request_binding(
            ai_root,
            digest,
            request_bytes,
            request,
            allowed_roots=allowed_roots,
        )
        if _legacy_recovery_exists(ai_root, digest) or _load_remote_recovery(ai_root, digest)[1] is not None:
            raise ProductionOrchestrationError("Human recovery decision suppresses transport")

        _, existing = _load_result(transport_dir, digest)
        if existing is not None:
            _verify_result_binding(request_bytes, request, existing)
            return existing

        content = mutation.content.encode("utf-8")
        if hashlib.sha256(content).hexdigest() != request.content_sha256:
            raise ProductionOrchestrationError("mutation content no longer matches request")

        try:
            created = create_fn(
                base_url=base_url,
                target_path=request.target_path,
                content=content,
                username=username,
                password=password,
                timeout=timeout,
                allow_http=allow_http,
            )
        except WebDAVTargetExists:
            try:
                observation = observe_fn(
                    base_url=base_url,
                    target_path=request.target_path,
                    expected_content_sha256=request.content_sha256,
                    username=username,
                    password=password,
                    timeout=timeout,
                    allow_http=allow_http,
                )
            except WebDAVCreateError as exc:
                raise ProductionOrchestrationError(str(exc)) from exc
            if observation.result == "absent":
                raise ProductionOrchestrationError(
                    "remote target disappeared after create precondition conflict; retry later"
                )
            timestamp = observed_at or _utc_now()
            result = TransportResult(
                mutation_sha256=digest,
                request_sha256=sha256_bytes(request_bytes),
                result=(
                    "target_exists_matching"
                    if observation.result == "matching"
                    else "target_exists_conflict"
                ),
                target_path=request.target_path,
                expected_content_sha256=request.content_sha256,
                observed_content_sha256=observation.content_sha256,
                observed_at=_require_timestamp(timestamp, label="observed_at"),
                http_status=412,
                etag=observation.etag,
            )
        except WebDAVCreateError as exc:
            raise ProductionOrchestrationError(str(exc)) from exc
        else:
            timestamp = observed_at or _utc_now()
            result = TransportResult(
                mutation_sha256=digest,
                request_sha256=sha256_bytes(request_bytes),
                result="created_verified",
                target_path=request.target_path,
                expected_content_sha256=request.content_sha256,
                observed_content_sha256=created.content_sha256,
                observed_at=_require_timestamp(timestamp, label="observed_at"),
                http_status=created.status_code,
                etag=created.etag,
            )

        try:
            _store_immutable(_result_path(transport_dir, digest), result.to_json_bytes())
        except ArtifactLifecycleError as exc:
            raise ProductionOrchestrationError(
                "remote effect was observed but transport result persistence failed"
            ) from exc
        return result


def _load_receipt(ai_root: Path, digest: str):
    path = ensure_artifact_layout(ai_root).receipts / f"{digest}.receipt.json"
    try:
        data = _read_exact_file(path)
    except ArtifactLifecycleError as exc:
        if not os.path.lexists(path):
            return None
        raise ProductionOrchestrationError("receipt exists but cannot be safely read") from exc
    return _parse_receipt(data)


def _verify_remote_recovery_binding(
    ai_root: Path,
    digest: str,
    recovery: RemoteRecoveryRecord,
    *,
    result_bytes: bytes,
    target_path: str,
    content_sha256: str,
) -> bool:
    _, intent, intent_sha = _load_intent_binding(ai_root, digest)
    return bool(
        intent is not None
        and intent_sha is not None
        and recovery.mutation_sha256 == digest
        and recovery.intent_sha256 == intent_sha
        and recovery.transport_result_sha256 == sha256_bytes(result_bytes)
        and recovery.target_path == target_path
        and recovery.expected_content_sha256 == content_sha256
    )


def _reconcile_production_unlocked(
    ai_root: Path,
    digest: str,
    *,
    allowed_roots: Sequence[str],
) -> ReconciliationResult:
    execution_dir, intent, _ = _load_intent_binding(ai_root, digest)
    transport_dir = _transport_directory(ai_root)
    if intent is None:
        return ReconciliationResult("not_started", digest, None, None)

    _, mutation, review_bytes, _ = _load_context(
        ai_root,
        digest,
        allowed_roots=allowed_roots,
    )
    _verify_intent(intent, mutation=mutation, review_bytes=review_bytes)

    request_bytes, request = _load_request(execution_dir, digest)
    receipt = _load_receipt(ai_root, digest)
    legacy_recovery = _legacy_recovery_exists(ai_root, digest)
    _, remote_recovery = _load_remote_recovery(ai_root, digest)
    if legacy_recovery:
        return ReconciliationResult(
            "conflict",
            digest,
            intent.target_path,
            "local recovery artifact is incompatible with production execution",
            receipt,
        )
    if request is None or request_bytes is None:
        if receipt is not None or remote_recovery is not None:
            return ReconciliationResult(
                "conflict", digest, intent.target_path, "terminal artifact exists without transport request", receipt
            )
        return ReconciliationResult("request_pending", digest, intent.target_path, None)

    _verify_request_binding(
        ai_root,
        digest,
        request_bytes,
        request,
        allowed_roots=allowed_roots,
    )
    result_bytes, result = _load_result(transport_dir, digest)
    if result is None or result_bytes is None:
        if receipt is not None or remote_recovery is not None:
            return ReconciliationResult(
                "conflict", digest, request.target_path, "terminal artifact exists without transport result", receipt
            )
        return ReconciliationResult("transport_pending", digest, request.target_path, None)

    _verify_result_binding(request_bytes, request, result)

    if result.result == "created_verified":
        if remote_recovery is not None:
            return ReconciliationResult(
                "conflict", digest, request.target_path, "Human recovery conflicts with verified remote create", receipt
            )
        if receipt is None:
            return ReconciliationResult("remote_verified_pending_receipt", digest, request.target_path, None)
        if not (
            receipt.mutation_sha256 == digest
            and receipt.mutation_id == mutation.mutation_id
            and receipt.target_path == request.target_path
            and receipt.content_sha256 == request.content_sha256
            and receipt.executed_at == result.observed_at
        ):
            return ReconciliationResult(
                "conflict", digest, request.target_path, "receipt does not match verified remote result", receipt
            )
        return ReconciliationResult("completed", digest, request.target_path, None, receipt)

    if receipt is not None:
        return ReconciliationResult(
            "conflict", digest, request.target_path, "receipt exists without verified remote create", receipt
        )

    if remote_recovery is not None:
        if not _verify_remote_recovery_binding(
            ai_root,
            digest,
            remote_recovery,
            result_bytes=result_bytes,
            target_path=request.target_path,
            content_sha256=request.content_sha256,
        ):
            return ReconciliationResult(
                "conflict", digest, request.target_path, "remote recovery no longer matches transport result"
            )
        if remote_recovery.decision == "abandon":
            return ReconciliationResult(
                "resolved_abandoned", digest, request.target_path, remote_recovery.reason
            )
        if (
            remote_recovery.decision == "adopt_observed_effect"
            and result.result == "target_exists_matching"
            and remote_recovery.observed_status == "remote_effect_observed_without_receipt"
        ):
            return ReconciliationResult(
                "resolved_effect_adopted",
                digest,
                request.target_path,
                "Human accepted the observed remote effect without claiming executor provenance; "
                + remote_recovery.reason,
            )
        return ReconciliationResult(
            "conflict", digest, request.target_path, "remote recovery is incompatible with transport result"
        )

    if result.result == "target_exists_matching":
        return ReconciliationResult(
            "remote_effect_observed_without_receipt",
            digest,
            request.target_path,
            "remote target matches intended bytes but conditional create returned 412; provenance is ambiguous",
        )
    return ReconciliationResult(
        "conflict",
        digest,
        request.target_path,
        "remote target existed with content different from the approved mutation",
    )


def reconcile_production_execution(
    ai_root: Path,
    mutation_sha256: str,
    *,
    allowed_roots: Sequence[str],
) -> ReconciliationResult:
    try:
        digest = _require_sha256(mutation_sha256, label="mutation_sha256")
    except ArtifactLifecycleError as exc:
        raise ProductionOrchestrationError(str(exc)) from exc
    lock_dir = _lock_directory(ai_root)
    _transport_directory(ai_root)
    with _production_lock(lock_dir, digest):
        return _reconcile_production_unlocked(ai_root, digest, allowed_roots=allowed_roots)


def advance_production_executor(
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
        raise ProductionOrchestrationError(str(exc)) from exc
    lock_dir = _lock_directory(ai_root)
    _transport_directory(ai_root)
    with _production_lock(lock_dir, digest):
        state = _reconcile_production_unlocked(ai_root, digest, allowed_roots=allowed_roots)
        if state.status in {"not_started", "request_pending"}:
            _store_request_unlocked(
                ai_root,
                vault_root,
                digest,
                allowed_roots=allowed_roots,
                note_policy=note_policy,
                requested_at=None,
            )
            return _reconcile_production_unlocked(ai_root, digest, allowed_roots=allowed_roots)
        if state.status != "remote_verified_pending_receipt":
            return state

        transport_dir = _transport_directory(ai_root)
        _, result = _load_result(transport_dir, digest)
        if result is None or result.result != "created_verified":
            raise ProductionOrchestrationError("verified remote result disappeared before receipt")
        _, mutation, _, _ = _load_context(ai_root, digest, allowed_roots=allowed_roots)
        receipt = ExecutionReceipt(
            mutation_id=mutation.mutation_id,
            mutation_sha256=digest,
            target_path=result.target_path,
            content_sha256=result.expected_content_sha256,
            executed_at=result.observed_at,
        )
        try:
            store_execution_receipt(ai_root, receipt)
        except ArtifactLifecycleError as exc:
            raise ProductionOrchestrationError(
                "verified remote effect exists but receipt persistence failed"
            ) from exc
        final = _reconcile_production_unlocked(ai_root, digest, allowed_roots=allowed_roots)
        if final.status != "completed":
            raise ProductionOrchestrationError(
                f"receipt persisted but production reconciliation is {final.status}"
            )
        return final


def record_remote_human_recovery(
    ai_root: Path,
    mutation_sha256: str,
    *,
    decision: str,
    resolver: str,
    reason: str,
    allowed_roots: Sequence[str],
    decided_at: str | None = None,
) -> RemoteRecoveryRecord:
    try:
        digest = _require_sha256(mutation_sha256, label="mutation_sha256")
    except ArtifactLifecycleError as exc:
        raise ProductionOrchestrationError(str(exc)) from exc
    if decision not in {"adopt_observed_effect", "abandon"}:
        raise ProductionOrchestrationError("remote recovery decision is invalid")
    if not isinstance(resolver, str) or not resolver or len(resolver) > 256:
        raise ProductionOrchestrationError("resolver is invalid")
    if not isinstance(reason, str) or not reason or len(reason) > 2048:
        raise ProductionOrchestrationError("reason is invalid")

    lock_dir = _lock_directory(ai_root)
    execution_dir = _execution_directory(ai_root)
    transport_dir = _transport_directory(ai_root)
    with _production_lock(lock_dir, digest):
        if _legacy_recovery_exists(ai_root, digest):
            raise ProductionOrchestrationError("local recovery artifact already exists")
        _, existing = _load_remote_recovery(ai_root, digest)
        if existing is not None:
            if existing.decision == decision and existing.resolver == resolver and existing.reason == reason:
                return existing
            raise ProductionOrchestrationError("immutable remote recovery decision already exists")

        state = _reconcile_production_unlocked(ai_root, digest, allowed_roots=allowed_roots)
        if state.status not in {"remote_effect_observed_without_receipt", "conflict"}:
            raise ProductionOrchestrationError(
                f"production state is not recoverable: {state.status}"
            )
        request_bytes, request = _load_request(execution_dir, digest)
        result_bytes, result = _load_result(transport_dir, digest)
        if request is None or request_bytes is None or result is None or result_bytes is None:
            raise ProductionOrchestrationError("transport result is required for recovery")
        if result.result not in {"target_exists_matching", "target_exists_conflict"}:
            raise ProductionOrchestrationError("verified create result is not Human-recoverable")
        if decision == "adopt_observed_effect" and result.result != "target_exists_matching":
            raise ProductionOrchestrationError(
                "adopt_observed_effect requires a matching remote observation"
            )
        _, intent, intent_sha = _load_intent_binding(ai_root, digest)
        if intent is None or intent_sha is None:
            raise ProductionOrchestrationError("execution intent is required for recovery")
        timestamp = _require_timestamp(decided_at or _utc_now(), label="decided_at")
        record = RemoteRecoveryRecord(
            mutation_sha256=digest,
            intent_sha256=intent_sha,
            transport_result_sha256=sha256_bytes(result_bytes),
            decision=decision,
            observed_status=(
                "remote_effect_observed_without_receipt"
                if result.result == "target_exists_matching"
                else "conflict"
            ),
            target_path=request.target_path,
            expected_content_sha256=request.content_sha256,
            decided_at=timestamp,
            resolver=resolver,
            reason=reason,
        )
        try:
            _store_immutable(_remote_recovery_path(ai_root, digest), record.to_json_bytes())
        except ArtifactLifecycleError as exc:
            raise ProductionOrchestrationError(str(exc)) from exc
        return record


def _common_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--ai-root", type=Path, required=True)
    parser.add_argument("--mutation-sha256", required=True)
    parser.add_argument("--allowed-root", action="append", required=True)


def executor_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="obsidian-production-executor")
    _common_parser(parser)
    parser.add_argument("--vault-root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        state = advance_production_executor(
            args.ai_root,
            args.vault_root,
            args.mutation_sha256,
            allowed_roots=args.allowed_root,
        )
    except (ArtifactLifecycleError, ExecutionOrchestrationError, ProductionOrchestrationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"status": state.status, "reason": state.reason}, sort_keys=True))
    return 0


def worker_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="obsidian-production-webdav-worker")
    _common_parser(parser)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--password-file", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)
    try:
        password = _read_password(args.password_file)
        result = process_transport_request(
            args.ai_root,
            args.mutation_sha256,
            allowed_roots=args.allowed_root,
            base_url=args.base_url,
            username=args.username,
            password=password,
            timeout=args.timeout,
        )
    except (
        ArtifactLifecycleError,
        ExecutionOrchestrationError,
        ProductionOrchestrationError,
        WebDAVCreateError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "result": result.result,
                "target_path": result.target_path,
                "content_sha256": result.expected_content_sha256,
            },
            sort_keys=True,
        )
    )
    return 0
