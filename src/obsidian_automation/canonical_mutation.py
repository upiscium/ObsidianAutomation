from __future__ import annotations

import hashlib
import json
import os
import secrets
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Mapping, Sequence


class MutationValidationError(RuntimeError):
    """Raised when a canonical mutation is not admissible."""


class MutationExecutionError(RuntimeError):
    """Raised when an approved mutation cannot be executed safely."""


@dataclass(frozen=True)
class CreateNoteMutation:
    contract_version: int
    operation: str
    mutation_id: str
    target_path: str
    content: str


@dataclass(frozen=True)
class ValidatedMutation:
    mutation: CreateNoteMutation
    artifact_bytes: bytes
    mutation_sha256: str


@dataclass(frozen=True)
class ApprovalRecord:
    approved: bool
    mutation_sha256: str


@dataclass(frozen=True)
class ExecutionReceipt:
    mutation_id: str
    mutation_sha256: str
    target_path: str
    content_sha256: str
    executed_at: str
    result: str = "success"

    def to_json_bytes(self) -> bytes:
        payload = {
            "mutation_id": self.mutation_id,
            "mutation_sha256": self.mutation_sha256,
            "target_path": self.target_path,
            "content_sha256": self.content_sha256,
            "executed_at": self.executed_at,
            "result": self.result,
        }
        return _canonical_json_bytes(payload)


NotePolicy = Callable[[CreateNoteMutation], None]


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise MutationValidationError(f"duplicate JSON property: {key}")
        result[key] = value
    return result


def _canonical_json_bytes(payload: Mapping[str, object]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _decode_artifact(artifact_bytes: bytes) -> dict[str, object]:
    try:
        text = artifact_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MutationValidationError("mutation artifact must be valid UTF-8") from exc

    try:
        value = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except MutationValidationError:
        raise
    except json.JSONDecodeError as exc:
        raise MutationValidationError(f"mutation artifact is not valid JSON: {exc.msg}") from exc

    if not isinstance(value, dict):
        raise MutationValidationError("mutation artifact must be a JSON object")
    return value


def _parse_create_note(artifact_bytes: bytes) -> CreateNoteMutation:
    value = _decode_artifact(artifact_bytes)
    required = {"contract_version", "operation", "mutation_id", "target", "content"}
    if set(value) != required:
        missing = sorted(required - set(value))
        unknown = sorted(set(value) - required)
        raise MutationValidationError(
            f"mutation properties do not match contract; missing={missing}, unknown={unknown}"
        )

    if type(value["contract_version"]) is not int or value["contract_version"] != 1:
        raise MutationValidationError("contract_version must be integer 1")
    if value["operation"] != "create_note":
        raise MutationValidationError("operation must be create_note")

    mutation_id = value["mutation_id"]
    if not isinstance(mutation_id, str) or not 1 <= len(mutation_id) <= 128:
        raise MutationValidationError("mutation_id must be a string of length 1..128")

    target = value["target"]
    if not isinstance(target, dict) or set(target) != {"path"}:
        raise MutationValidationError("target must contain exactly one path property")
    target_path = target["path"]
    if (
        not isinstance(target_path, str)
        or not 4 <= len(target_path) <= 1024
        or not target_path.endswith(".md")
    ):
        raise MutationValidationError("target.path must be a 4..1024 character .md path")

    content = value["content"]
    if not isinstance(content, str) or not content:
        raise MutationValidationError("content must be a non-empty string")
    try:
        mutation_id.encode("utf-8")
        target_path.encode("utf-8")
        content.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise MutationValidationError("mutation text must be valid Unicode encodable as UTF-8") from exc

    return CreateNoteMutation(
        contract_version=1,
        operation="create_note",
        mutation_id=mutation_id,
        target_path=target_path,
        content=content,
    )


def _safe_components(path: str, *, label: str) -> tuple[str, ...]:
    if not path or path.startswith("/") or "\\" in path or "\x00" in path:
        raise MutationValidationError(f"{label} must be a relative POSIX path")
    parts = tuple(path.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        raise MutationValidationError(f"{label} contains an unsafe path component")
    return parts


def _authorized(path_parts: Sequence[str], allowed_roots: Sequence[str]) -> bool:
    for root in allowed_roots:
        root_parts = _safe_components(root, label="allowed root")
        if len(path_parts) > len(root_parts) and tuple(path_parts[: len(root_parts)]) == root_parts:
            return True
    return False


@contextmanager
def _open_parent_dirfd(vault_root: Path, path_parts: Sequence[str]) -> Iterator[tuple[int, str]]:
    if len(path_parts) < 2:
        raise MutationValidationError("target path must have a parent directory")

    flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC

    fds: list[int] = []
    try:
        try:
            current = os.open(vault_root, flags)
        except OSError as exc:
            raise MutationValidationError(
                "Vault root must be an existing non-symlink directory"
            ) from exc
        fds.append(current)

        for component in path_parts[:-1]:
            try:
                names = os.listdir(current)
            except OSError as exc:
                raise MutationValidationError("cannot inspect destination directory") from exc

            folded = component.casefold()
            aliases = [name for name in names if name.casefold() == folded and name != component]
            if aliases:
                raise MutationValidationError(
                    f"case-fold collision in destination path: {component!r} aliases {aliases!r}"
                )
            try:
                child = os.open(component, flags, dir_fd=current)
            except FileNotFoundError as exc:
                raise MutationValidationError(
                    f"destination parent directory does not exist: {component}"
                ) from exc
            except OSError as exc:
                raise MutationValidationError(
                    f"destination path component is not a safe directory: {component}"
                ) from exc
            fds.append(child)
            current = child

        yield current, path_parts[-1]
    finally:
        for fd in reversed(fds):
            try:
                os.close(fd)
            except OSError:
                pass


def _check_target_absent(parent_fd: int, target_name: str) -> None:
    try:
        names = os.listdir(parent_fd)
    except OSError as exc:
        raise MutationValidationError("cannot inspect target directory") from exc

    folded = target_name.casefold()
    aliases = [name for name in names if name.casefold() == folded]
    if aliases:
        raise MutationValidationError(
            f"target already exists or has a case-fold collision: {aliases!r}"
        )

    try:
        os.stat(target_name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise MutationValidationError("cannot safely inspect target") from exc
    raise MutationValidationError("target already exists")


def _validate_semantics(
    mutation: CreateNoteMutation,
    *,
    vault_root: Path,
    allowed_roots: Sequence[str],
    note_policy: NotePolicy | None,
) -> tuple[str, ...]:
    path_parts = _safe_components(mutation.target_path, label="target.path")
    if not allowed_roots:
        raise MutationValidationError("at least one allowed canonical root is required")
    if not _authorized(path_parts, allowed_roots):
        raise MutationValidationError("target path is outside deployment-policy-approved roots")

    with _open_parent_dirfd(vault_root, path_parts) as (parent_fd, target_name):
        _check_target_absent(parent_fd, target_name)

    if note_policy is not None:
        try:
            note_policy(mutation)
        except MutationValidationError:
            raise
        except Exception as exc:
            raise MutationValidationError(f"note policy rejected mutation: {exc}") from exc

    return path_parts


def validate_create_note(
    proposal_bytes: bytes,
    *,
    vault_root: Path,
    allowed_roots: Sequence[str],
    note_policy: NotePolicy | None = None,
) -> ValidatedMutation:
    mutation = _parse_create_note(proposal_bytes)
    _validate_semantics(
        mutation,
        vault_root=vault_root,
        allowed_roots=allowed_roots,
        note_policy=note_policy,
    )
    payload = {
        "contract_version": mutation.contract_version,
        "operation": mutation.operation,
        "mutation_id": mutation.mutation_id,
        "target": {"path": mutation.target_path},
        "content": mutation.content,
    }
    artifact_bytes = _canonical_json_bytes(payload)
    return ValidatedMutation(
        mutation=mutation,
        artifact_bytes=artifact_bytes,
        mutation_sha256=hashlib.sha256(artifact_bytes).hexdigest(),
    )


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise MutationExecutionError("short write while creating note")
        view = view[written:]


def _create_exclusive_note(parent_fd: int, target_name: str, content: bytes) -> None:
    temp_name = f".obsidian-create-note-{secrets.token_hex(16)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC

    temp_created = False
    fd: int | None = None
    try:
        fd = os.open(temp_name, flags, 0o644, dir_fd=parent_fd)
        temp_created = True
        _write_all(fd, content)
        os.fsync(fd)
        os.close(fd)
        fd = None

        try:
            os.link(
                temp_name,
                target_name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise MutationValidationError(
                "target appeared before effect; refusing overwrite"
            ) from exc
        except OSError as exc:
            raise MutationExecutionError("atomic create failed") from exc

        os.unlink(temp_name, dir_fd=parent_fd)
        temp_created = False
        os.fsync(parent_fd)
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if temp_created:
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
            except OSError:
                pass


def execute_create_note(
    validated_artifact_bytes: bytes,
    *,
    approval: ApprovalRecord | None,
    vault_root: Path,
    allowed_roots: Sequence[str],
    note_policy: NotePolicy | None = None,
) -> ExecutionReceipt:
    if approval is None or not approval.approved:
        raise MutationValidationError("affirmative human approval is required")

    actual_sha = hashlib.sha256(validated_artifact_bytes).hexdigest()
    if approval.mutation_sha256 != actual_sha:
        raise MutationValidationError("approval hash does not match validated mutation artifact")

    mutation = _parse_create_note(validated_artifact_bytes)
    path_parts = _validate_semantics(
        mutation,
        vault_root=vault_root,
        allowed_roots=allowed_roots,
        note_policy=note_policy,
    )
    content_bytes = mutation.content.encode("utf-8")

    # Re-open the destination through dirfds at the effect boundary rather than
    # trusting pathname resolution performed during earlier validation.
    with _open_parent_dirfd(vault_root, path_parts) as (parent_fd, target_name):
        _check_target_absent(parent_fd, target_name)
        _create_exclusive_note(parent_fd, target_name, content_bytes)

    return ExecutionReceipt(
        mutation_id=mutation.mutation_id,
        mutation_sha256=actual_sha,
        target_path=mutation.target_path,
        content_sha256=hashlib.sha256(content_bytes).hexdigest(),
        executed_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    )
