from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from .canonical_mutation import ApprovalRecord, ExecutionReceipt, ValidatedMutation


class ArtifactLifecycleError(RuntimeError):
    """Raised when an AI lifecycle artifact cannot be stored or trusted safely."""


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ArtifactLayout:
    root: Path
    untrusted: Path
    validation: Path
    review: Path
    receipts: Path


@dataclass(frozen=True)
class ValidationRecord:
    proposal_sha256: str
    result: str
    validated_at: str
    mutation_sha256: str | None
    reason: str | None


@dataclass(frozen=True)
class ReviewRecord:
    mutation_sha256: str
    decision: str
    decided_at: str
    approver: str

    @property
    def approved(self) -> bool:
        return self.decision == "approve"

    def to_approval(self) -> ApprovalRecord:
        return ApprovalRecord(
            approved=self.approved,
            mutation_sha256=self.mutation_sha256,
        )


def _canonical_json_bytes(payload: Mapping[str, object]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ArtifactLifecycleError(f"duplicate JSON property: {key}")
        result[key] = value
    return result


def _decode_json_object(data: bytes, *, label: str) -> dict[str, object]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArtifactLifecycleError(f"{label} must be valid UTF-8") from exc
    try:
        value = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except ArtifactLifecycleError:
        raise
    except json.JSONDecodeError as exc:
        raise ArtifactLifecycleError(f"{label} is not valid JSON: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ArtifactLifecycleError(f"{label} must be a JSON object")
    return value


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _require_sha256(value: str, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ArtifactLifecycleError(f"{label} must be a lowercase SHA-256 hex digest")
    return value


def _require_safe_directory(path: Path, *, create: bool) -> None:
    if create:
        path.mkdir(mode=0o755, exist_ok=True)
    try:
        path.lstat()
    except FileNotFoundError as exc:
        raise ArtifactLifecycleError(f"required directory does not exist: {path}") from exc
    if path.is_symlink() or not path.is_dir():
        raise ArtifactLifecycleError(f"artifact path is not a safe directory: {path}")


def ensure_artifact_layout(ai_root: Path) -> ArtifactLayout:
    ai_root = ai_root.absolute()
    _require_safe_directory(ai_root, create=False)
    layout = ArtifactLayout(
        root=ai_root,
        untrusted=ai_root / "00-Untrusted",
        validation=ai_root / "10-Validation",
        review=ai_root / "20-Review",
        receipts=ai_root / "30-Receipts",
    )
    for path in (layout.untrusted, layout.validation, layout.review, layout.receipts):
        _require_safe_directory(path, create=True)
    return layout


def _open_readonly_nofollow(path: Path) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        return os.open(path, flags)
    except OSError as exc:
        raise ArtifactLifecycleError(f"cannot safely open artifact: {path}") from exc


def _read_exact_file(path: Path) -> bytes:
    fd = _open_readonly_nofollow(path)
    try:
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(fd)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise ArtifactLifecycleError("short write while persisting artifact")
        view = view[written:]


def _store_immutable(path: Path, data: bytes) -> Path:
    _require_safe_directory(path.parent, create=False)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = os.open(path, flags, 0o644)
    except FileExistsError:
        existing = _read_exact_file(path)
        if existing == data:
            return path
        raise ArtifactLifecycleError(
            f"immutable artifact already exists with different bytes: {path}"
        )
    except OSError as exc:
        raise ArtifactLifecycleError(f"cannot create artifact: {path}") from exc

    try:
        _write_all(fd, data)
        os.fsync(fd)
    except Exception:
        try:
            os.close(fd)
        finally:
            try:
                path.unlink()
            except OSError:
                pass
        raise
    else:
        os.close(fd)

    dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return path


def store_untrusted_proposal(ai_root: Path, proposal_bytes: bytes) -> tuple[str, Path]:
    layout = ensure_artifact_layout(ai_root)
    digest = sha256_bytes(proposal_bytes)
    path = layout.untrusted / f"{digest}.proposal.json"
    return digest, _store_immutable(path, proposal_bytes)


def store_validated_mutation(ai_root: Path, validated: ValidatedMutation) -> Path:
    layout = ensure_artifact_layout(ai_root)
    digest = _require_sha256(validated.mutation_sha256, label="validated mutation SHA-256")
    actual = sha256_bytes(validated.artifact_bytes)
    if actual != digest:
        raise ArtifactLifecycleError("validated mutation bytes do not match mutation_sha256")
    return _store_immutable(
        layout.validation / f"{digest}.mutation.json",
        validated.artifact_bytes,
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def validation_record_bytes(
    *,
    proposal_sha256: str,
    result: str,
    mutation_sha256: str | None = None,
    reason: str | None = None,
    validated_at: str | None = None,
) -> bytes:
    proposal_digest = _require_sha256(proposal_sha256, label="proposal_sha256")
    timestamp = validated_at or _utc_now()
    if not isinstance(timestamp, str) or not timestamp.endswith("Z"):
        raise ArtifactLifecycleError("validated_at must be a UTC timestamp ending in Z")

    if result == "accepted":
        if mutation_sha256 is None:
            raise ArtifactLifecycleError("accepted validation requires mutation_sha256")
        mutation_digest: str | None = _require_sha256(
            mutation_sha256, label="mutation_sha256"
        )
        if reason is not None:
            raise ArtifactLifecycleError(
                "accepted validation must not contain a rejection reason"
            )
    elif result == "rejected":
        if mutation_sha256 is not None:
            raise ArtifactLifecycleError(
                "rejected validation must not contain mutation_sha256"
            )
        if not isinstance(reason, str) or not reason or len(reason) > 1024:
            raise ArtifactLifecycleError(
                "rejected validation requires a non-empty reason up to 1024 characters"
            )
        mutation_digest = None
    else:
        raise ArtifactLifecycleError("validation result must be accepted or rejected")

    return _canonical_json_bytes(
        {
            "record_version": 1,
            "proposal_sha256": proposal_digest,
            "result": result,
            "validated_at": timestamp,
            "mutation_sha256": mutation_digest,
            "reason": reason,
        }
    )


def store_validation_record(
    ai_root: Path,
    *,
    proposal_sha256: str,
    result: str,
    mutation_sha256: str | None = None,
    reason: str | None = None,
    validated_at: str | None = None,
) -> Path:
    layout = ensure_artifact_layout(ai_root)
    proposal_digest = _require_sha256(proposal_sha256, label="proposal_sha256")
    proposal_path = layout.untrusted / f"{proposal_digest}.proposal.json"
    proposal_bytes = _read_exact_file(proposal_path)
    if sha256_bytes(proposal_bytes) != proposal_digest:
        raise ArtifactLifecycleError("untrusted proposal artifact hash mismatch")

    if result == "accepted":
        if mutation_sha256 is None:
            raise ArtifactLifecycleError("accepted validation requires mutation_sha256")
        mutation_digest = _require_sha256(mutation_sha256, label="mutation_sha256")
        mutation_path = layout.validation / f"{mutation_digest}.mutation.json"
        mutation_bytes = _read_exact_file(mutation_path)
        if sha256_bytes(mutation_bytes) != mutation_digest:
            raise ArtifactLifecycleError("validated mutation artifact hash mismatch")

    data = validation_record_bytes(
        proposal_sha256=proposal_digest,
        result=result,
        mutation_sha256=mutation_sha256,
        reason=reason,
        validated_at=validated_at,
    )
    return _store_immutable(
        layout.validation / f"{proposal_digest}.validation.json",
        data,
    )


def parse_validation_record(data: bytes) -> ValidationRecord:
    value = _decode_json_object(data, label="validation record")
    required = {
        "record_version",
        "proposal_sha256",
        "result",
        "validated_at",
        "mutation_sha256",
        "reason",
    }
    if set(value) != required:
        raise ArtifactLifecycleError("validation record properties do not match contract")
    if type(value["record_version"]) is not int or value["record_version"] != 1:
        raise ArtifactLifecycleError("record_version must be integer 1")

    proposal_value = value["proposal_sha256"]
    if not isinstance(proposal_value, str):
        raise ArtifactLifecycleError("proposal_sha256 must be a string")
    proposal_digest = _require_sha256(proposal_value, label="proposal_sha256")
    result = value["result"]
    validated_at = value["validated_at"]
    mutation_value = value["mutation_sha256"]
    reason = value["reason"]
    if not isinstance(validated_at, str) or not validated_at.endswith("Z"):
        raise ArtifactLifecycleError("validated_at must be a UTC timestamp ending in Z")

    if result == "accepted":
        if not isinstance(mutation_value, str):
            raise ArtifactLifecycleError("accepted validation requires mutation_sha256")
        mutation_digest = _require_sha256(mutation_value, label="mutation_sha256")
        if reason is not None:
            raise ArtifactLifecycleError(
                "accepted validation must not contain a rejection reason"
            )
    elif result == "rejected":
        if mutation_value is not None:
            raise ArtifactLifecycleError(
                "rejected validation must not contain mutation_sha256"
            )
        if not isinstance(reason, str) or not reason or len(reason) > 1024:
            raise ArtifactLifecycleError(
                "rejected validation requires a non-empty reason up to 1024 characters"
            )
        mutation_digest = None
    else:
        raise ArtifactLifecycleError("validation result must be accepted or rejected")

    return ValidationRecord(
        proposal_sha256=proposal_digest,
        result=result,
        validated_at=validated_at,
        mutation_sha256=mutation_digest,
        reason=reason,
    )


def review_record_bytes(
    *,
    mutation_sha256: str,
    decision: str,
    approver: str,
    decided_at: str | None = None,
) -> bytes:
    digest = _require_sha256(mutation_sha256, label="mutation_sha256")
    if decision not in {"approve", "reject"}:
        raise ArtifactLifecycleError("review decision must be approve or reject")
    if not isinstance(approver, str) or not approver or len(approver) > 256:
        raise ArtifactLifecycleError(
            "approver must be a non-empty string up to 256 characters"
        )
    timestamp = decided_at or _utc_now()
    if not isinstance(timestamp, str) or not timestamp.endswith("Z"):
        raise ArtifactLifecycleError("decided_at must be a UTC timestamp ending in Z")
    return _canonical_json_bytes(
        {
            "record_version": 1,
            "mutation_sha256": digest,
            "decision": decision,
            "decided_at": timestamp,
            "approver": approver,
        }
    )


def store_review_record(
    ai_root: Path,
    *,
    mutation_sha256: str,
    decision: str,
    approver: str,
    decided_at: str | None = None,
) -> Path:
    layout = ensure_artifact_layout(ai_root)
    digest = _require_sha256(mutation_sha256, label="mutation_sha256")
    mutation_path = layout.validation / f"{digest}.mutation.json"
    mutation_bytes = _read_exact_file(mutation_path)
    if sha256_bytes(mutation_bytes) != digest:
        raise ArtifactLifecycleError("validated mutation artifact hash mismatch")
    data = review_record_bytes(
        mutation_sha256=digest,
        decision=decision,
        approver=approver,
        decided_at=decided_at,
    )
    return _store_immutable(layout.review / f"{digest}.approval.json", data)


def parse_review_record(data: bytes) -> ReviewRecord:
    value = _decode_json_object(data, label="review record")
    required = {
        "record_version",
        "mutation_sha256",
        "decision",
        "decided_at",
        "approver",
    }
    if set(value) != required:
        raise ArtifactLifecycleError("review record properties do not match contract")
    if type(value["record_version"]) is not int or value["record_version"] != 1:
        raise ArtifactLifecycleError("record_version must be integer 1")

    digest_value = value["mutation_sha256"]
    if not isinstance(digest_value, str):
        raise ArtifactLifecycleError("mutation_sha256 must be a string")
    digest = _require_sha256(digest_value, label="mutation_sha256")
    decision = value["decision"]
    decided_at = value["decided_at"]
    approver = value["approver"]
    if decision not in {"approve", "reject"}:
        raise ArtifactLifecycleError("review decision must be approve or reject")
    if not isinstance(decided_at, str) or not decided_at.endswith("Z"):
        raise ArtifactLifecycleError("decided_at must be a UTC timestamp ending in Z")
    if not isinstance(approver, str) or not approver or len(approver) > 256:
        raise ArtifactLifecycleError(
            "approver must be a non-empty string up to 256 characters"
        )
    return ReviewRecord(
        mutation_sha256=digest,
        decision=decision,
        decided_at=decided_at,
        approver=approver,
    )


def load_review_record(ai_root: Path, mutation_sha256: str) -> ReviewRecord:
    layout = ensure_artifact_layout(ai_root)
    digest = _require_sha256(mutation_sha256, label="mutation_sha256")
    data = _read_exact_file(layout.review / f"{digest}.approval.json")
    record = parse_review_record(data)
    if record.mutation_sha256 != digest:
        raise ArtifactLifecycleError("review record is bound to a different mutation")
    return record


def store_execution_receipt(ai_root: Path, receipt: ExecutionReceipt) -> Path:
    layout = ensure_artifact_layout(ai_root)
    digest = _require_sha256(receipt.mutation_sha256, label="receipt mutation SHA-256")
    mutation_path = layout.validation / f"{digest}.mutation.json"
    mutation_bytes = _read_exact_file(mutation_path)
    if sha256_bytes(mutation_bytes) != digest:
        raise ArtifactLifecycleError("validated mutation artifact hash mismatch")
    return _store_immutable(
        layout.receipts / f"{digest}.receipt.json",
        receipt.to_json_bytes(),
    )
