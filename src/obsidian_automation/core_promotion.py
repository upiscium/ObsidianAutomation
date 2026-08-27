from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping, Sequence

from .artifact_lifecycle import (
    ArtifactLifecycleError,
    _canonical_json_bytes,
    _decode_json_object,
    sha256_bytes,
)
from .public_export import ExportConfig, ExportError, _matches_any, load_config


PROMOTION_PLAN_VERSION = 1
PROMOTION_POLICY_VERSION = "public-projection-roundtrip-v0"
CORE_REPOSITORY = "upiscium/ObsidianCore"
MAX_PROMOTION_FILE_BYTES = 2 * 1024 * 1024
MAX_PROMOTION_CHANGES = 4096
MAX_PATH_CHARS = 1024
_GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40,64}$")
_ALLOWED_ACTIONS = {"create", "update", "delete"}
_ALLOWED_DISPOSITIONS = {"apply", "already_applied", "conflict"}


class PromotionError(RuntimeError):
    """Raised when a Core-to-Vault promotion cannot be proven safe."""


@dataclass(frozen=True)
class PromotionChange:
    action: str
    path: str
    before_sha256: str | None
    after_sha256: str | None


@dataclass(frozen=True)
class PromotionPlan:
    source_repository: str
    base_commit: str
    head_commit: str
    policy_version: str
    policy_sha256: str
    changes: tuple[PromotionChange, ...]

    def to_json_bytes(self) -> bytes:
        return _canonical_json_bytes(
            {
                "record_version": PROMOTION_PLAN_VERSION,
                "source_repository": self.source_repository,
                "base_commit": self.base_commit,
                "head_commit": self.head_commit,
                "policy": {
                    "version": self.policy_version,
                    "sha256": self.policy_sha256,
                },
                "changes": [
                    {
                        "action": change.action,
                        "path": change.path,
                        "before_sha256": change.before_sha256,
                        "after_sha256": change.after_sha256,
                    }
                    for change in self.changes
                ],
            }
        )


@dataclass(frozen=True)
class PromotionObservation:
    action: str
    path: str
    before_sha256: str | None
    after_sha256: str | None
    current_sha256: str | None
    disposition: str


@dataclass(frozen=True)
class PromotionReconciliation:
    plan_sha256: str
    observations: tuple[PromotionObservation, ...]

    def to_json_bytes(self) -> bytes:
        return _canonical_json_bytes(
            {
                "record_version": 1,
                "plan_sha256": self.plan_sha256,
                "observations": [
                    {
                        "action": item.action,
                        "path": item.path,
                        "before_sha256": item.before_sha256,
                        "after_sha256": item.after_sha256,
                        "current_sha256": item.current_sha256,
                        "disposition": item.disposition,
                    }
                    for item in self.observations
                ],
            }
        )

    @property
    def has_conflict(self) -> bool:
        return any(item.disposition == "conflict" for item in self.observations)

    @property
    def pending_count(self) -> int:
        return sum(item.disposition == "apply" for item in self.observations)


def _run_git(
    repository: Path,
    args: Sequence[str],
    *,
    text: bool = False,
) -> bytes | str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=repository,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=text,
        )
    except FileNotFoundError as exc:
        raise PromotionError("git is required for Core promotion") from exc
    if completed.returncode != 0:
        stderr = completed.stderr
        if isinstance(stderr, bytes):
            detail = stderr.decode("utf-8", "replace").strip()
        else:
            detail = stderr.strip()
        raise PromotionError(
            f"git command failed ({completed.returncode}): git {' '.join(args)}: {detail}"
        )
    return completed.stdout


def _repository_root(repository: Path) -> Path:
    repository = repository.resolve()
    if not repository.is_dir():
        raise PromotionError(f"Core repository does not exist: {repository}")
    top = str(_run_git(repository, ["rev-parse", "--show-toplevel"], text=True)).strip()
    if Path(top).resolve() != repository:
        raise PromotionError("Core repository path must be the Git repository root")
    return repository


def _resolve_commit(repository: Path, ref: str, *, label: str) -> str:
    if not isinstance(ref, str) or not ref or ref != ref.strip():
        raise PromotionError(f"{label} must be a non-empty trimmed Git ref")
    resolved = str(_run_git(repository, ["rev-parse", "--verify", f"{ref}^{{commit}}"], text=True)).strip()
    if _GIT_COMMIT_RE.fullmatch(resolved) is None:
        raise PromotionError(f"{label} did not resolve to a supported commit digest")
    return resolved


def _require_ancestor(repository: Path, base: str, head: str) -> None:
    completed = subprocess.run(
        ["git", "merge-base", "--is-ancestor", base, head],
        cwd=repository,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode == 0:
        return
    if completed.returncode == 1:
        raise PromotionError("promotion base commit is not an ancestor of head commit")
    raise PromotionError(
        f"cannot verify promotion ancestry: {completed.stderr.strip()}"
    )


def _safe_path(path: object) -> str:
    if not isinstance(path, str) or not path or len(path) > MAX_PATH_CHARS:
        raise PromotionError("promotion path is invalid")
    if path.startswith("/") or "\\" in path or "\x00" in path:
        raise PromotionError(f"unsafe promotion path: {path!r}")
    parts = PurePosixPath(path).parts
    if any(part in {"", ".", ".."} for part in parts):
        raise PromotionError(f"unsafe promotion path: {path!r}")
    if parts and parts[0] == ".git":
        raise PromotionError("Git metadata cannot be promoted")
    return path


def _managed_by_projection(path: str, config: ExportConfig) -> bool:
    relative = _safe_path(path)
    if not _matches_any(relative, config.include):
        return False
    if _matches_any(relative, config.exclude):
        return False
    repository_owned = (".git", ".git/**", "**/.git", "**/.git/**", *config.repository_owned)
    if _matches_any(relative, repository_owned):
        return False
    return True


def _policy_sha256(config_path: Path) -> str:
    try:
        data = config_path.read_bytes()
    except OSError as exc:
        raise PromotionError(f"cannot read promotion policy: {config_path}") from exc
    return sha256_bytes(data)


def _diff_paths(repository: Path, base: str, head: str) -> tuple[tuple[str, str], ...]:
    raw = _run_git(
        repository,
        ["diff", "--name-status", "-z", "--no-renames", base, head, "--"],
    )
    assert isinstance(raw, bytes)
    fields = raw.split(b"\x00")
    if fields and fields[-1] == b"":
        fields.pop()
    if len(fields) % 2 != 0:
        raise PromotionError("Git returned malformed name-status output")
    changes: list[tuple[str, str]] = []
    for index in range(0, len(fields), 2):
        try:
            status = fields[index].decode("ascii")
            path = fields[index + 1].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PromotionError("Core contains a promotion path that is not valid UTF-8") from exc
        path = _safe_path(path)
        if status not in {"A", "M", "D"}:
            raise PromotionError(
                f"unsupported Git change type {status!r} for promotion path {path}"
            )
        changes.append((status, path))
    return tuple(changes)


def _tree_blob(repository: Path, commit: str, path: str) -> bytes | None:
    relative = _safe_path(path)
    raw = _run_git(repository, ["ls-tree", "-z", commit, "--", relative])
    assert isinstance(raw, bytes)
    if not raw:
        return None
    entries = [item for item in raw.split(b"\x00") if item]
    if len(entries) != 1:
        raise PromotionError(f"Git tree lookup is ambiguous for {relative}")
    try:
        meta, entry_path = entries[0].split(b"\t", 1)
        mode, object_type, _object_id = meta.decode("ascii").split(" ", 2)
        decoded_path = entry_path.decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise PromotionError(f"Git tree entry is malformed for {relative}") from exc
    if decoded_path != relative:
        raise PromotionError(f"Git tree lookup returned another path for {relative}")
    if object_type != "blob" or mode not in {"100644", "100755"}:
        raise PromotionError(
            f"promotion supports regular files only; unsafe Git mode {mode} for {relative}"
        )
    content = _run_git(repository, ["show", f"{commit}:{relative}"])
    assert isinstance(content, bytes)
    if len(content) > MAX_PROMOTION_FILE_BYTES:
        raise PromotionError(
            f"promotion file exceeds {MAX_PROMOTION_FILE_BYTES} bytes: {relative}"
        )
    return content


def _digest(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


def _validate_change(change: PromotionChange) -> PromotionChange:
    action = change.action
    path = _safe_path(change.path)
    if action not in _ALLOWED_ACTIONS:
        raise PromotionError(f"unsupported promotion action: {action}")
    before = change.before_sha256
    after = change.after_sha256
    for label, value in (("before_sha256", before), ("after_sha256", after)):
        if value is not None and not isinstance(value, str):
            raise PromotionError(f"{label} must be a SHA-256 string or null")
        if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise PromotionError(f"{label} is not a lowercase SHA-256 digest")
    if action == "create" and (before is not None or after is None):
        raise PromotionError("create promotion must have null before and non-null after")
    if action == "update" and (before is None or after is None or before == after):
        raise PromotionError("update promotion must have distinct before and after digests")
    if action == "delete" and (before is None or after is not None):
        raise PromotionError("delete promotion must have non-null before and null after")
    return PromotionChange(action, path, before, after)


def build_promotion_plan(
    core_repository: Path,
    *,
    base_ref: str,
    head_ref: str,
    config_path: Path,
) -> PromotionPlan:
    repository = _repository_root(core_repository)
    try:
        config = load_config(config_path)
    except ExportError as exc:
        raise PromotionError(str(exc)) from exc
    base = _resolve_commit(repository, base_ref, label="base_ref")
    head = _resolve_commit(repository, head_ref, label="head_ref")
    _require_ancestor(repository, base, head)

    changes: list[PromotionChange] = []
    for status, path in _diff_paths(repository, base, head):
        if not _managed_by_projection(path, config):
            continue
        before_bytes = None if status == "A" else _tree_blob(repository, base, path)
        after_bytes = None if status == "D" else _tree_blob(repository, head, path)
        if status != "A" and before_bytes is None:
            raise PromotionError(f"base commit is missing changed managed path: {path}")
        if status != "D" and after_bytes is None:
            raise PromotionError(f"head commit is missing changed managed path: {path}")
        before = _digest(before_bytes)
        after = _digest(after_bytes)
        if before == after:
            continue
        action = {"A": "create", "M": "update", "D": "delete"}[status]
        changes.append(_validate_change(PromotionChange(action, path, before, after)))

    if len(changes) > MAX_PROMOTION_CHANGES:
        raise PromotionError(
            f"promotion contains more than {MAX_PROMOTION_CHANGES} managed changes"
        )
    changes.sort(key=lambda item: item.path.casefold())
    return PromotionPlan(
        source_repository=CORE_REPOSITORY,
        base_commit=base,
        head_commit=head,
        policy_version=PROMOTION_POLICY_VERSION,
        policy_sha256=_policy_sha256(config_path),
        changes=tuple(changes),
    )


def parse_promotion_plan(data: bytes) -> PromotionPlan:
    try:
        value = _decode_json_object(data, label="Core promotion plan")
    except ArtifactLifecycleError as exc:
        raise PromotionError(str(exc)) from exc
    required = {
        "record_version",
        "source_repository",
        "base_commit",
        "head_commit",
        "policy",
        "changes",
    }
    if set(value) != required:
        raise PromotionError("Core promotion plan properties do not match contract")
    if type(value["record_version"]) is not int or value["record_version"] != PROMOTION_PLAN_VERSION:
        raise PromotionError("Core promotion plan record_version must be integer 1")
    if value["source_repository"] != CORE_REPOSITORY:
        raise PromotionError("Core promotion plan source repository is invalid")
    base = value["base_commit"]
    head = value["head_commit"]
    if not isinstance(base, str) or _GIT_COMMIT_RE.fullmatch(base) is None:
        raise PromotionError("Core promotion base_commit is invalid")
    if not isinstance(head, str) or _GIT_COMMIT_RE.fullmatch(head) is None:
        raise PromotionError("Core promotion head_commit is invalid")
    policy = value["policy"]
    if not isinstance(policy, dict) or set(policy) != {"version", "sha256"}:
        raise PromotionError("Core promotion policy properties do not match contract")
    if policy["version"] != PROMOTION_POLICY_VERSION:
        raise PromotionError("Core promotion policy version is unsupported")
    policy_sha = policy["sha256"]
    if not isinstance(policy_sha, str) or re.fullmatch(r"[0-9a-f]{64}", policy_sha) is None:
        raise PromotionError("Core promotion policy SHA-256 is invalid")
    raw_changes = value["changes"]
    if not isinstance(raw_changes, list) or len(raw_changes) > MAX_PROMOTION_CHANGES:
        raise PromotionError("Core promotion changes are invalid")
    changes: list[PromotionChange] = []
    seen: set[str] = set()
    for raw in raw_changes:
        if not isinstance(raw, dict) or set(raw) != {
            "action",
            "path",
            "before_sha256",
            "after_sha256",
        }:
            raise PromotionError("Core promotion change properties do not match contract")
        change = _validate_change(
            PromotionChange(
                raw["action"],
                raw["path"],
                raw["before_sha256"],
                raw["after_sha256"],
            )
        )
        folded = change.path.casefold()
        if folded in seen:
            raise PromotionError("Core promotion plan contains duplicate paths")
        seen.add(folded)
        changes.append(change)
    if [item.path for item in changes] != sorted(
        (item.path for item in changes), key=str.casefold
    ):
        raise PromotionError("Core promotion changes must be sorted by path")
    return PromotionPlan(
        source_repository=CORE_REPOSITORY,
        base_commit=base,
        head_commit=head,
        policy_version=PROMOTION_POLICY_VERSION,
        policy_sha256=policy_sha,
        changes=tuple(changes),
    )


def promotion_plan_sha256(plan: PromotionPlan) -> str:
    normalized = parse_promotion_plan(plan.to_json_bytes())
    return sha256_bytes(normalized.to_json_bytes())


def verify_plan_against_core(
    plan: PromotionPlan,
    core_repository: Path,
    *,
    config_path: Path,
) -> None:
    expected = build_promotion_plan(
        core_repository,
        base_ref=plan.base_commit,
        head_ref=plan.head_commit,
        config_path=config_path,
    )
    if expected != plan:
        raise PromotionError("Core promotion plan does not match exact Core commits and policy")


def _local_file_bytes(root: Path, relative: str) -> bytes | None:
    root = root.resolve()
    if not root.is_dir():
        raise PromotionError(f"Vault snapshot root does not exist: {root}")
    parts = PurePosixPath(_safe_path(relative)).parts
    current = root
    for part in parts:
        candidate = current / part
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode):
            raise PromotionError(f"symlink is not allowed in promotion path: {relative}")
        current = candidate
    info = current.stat()
    if not stat.S_ISREG(info.st_mode):
        raise PromotionError(f"promotion target is not a regular file: {relative}")
    if info.st_size > MAX_PROMOTION_FILE_BYTES:
        raise PromotionError(
            f"Vault promotion target exceeds {MAX_PROMOTION_FILE_BYTES} bytes: {relative}"
        )
    data = current.read_bytes()
    if len(data) > MAX_PROMOTION_FILE_BYTES:
        raise PromotionError(
            f"Vault promotion target exceeds {MAX_PROMOTION_FILE_BYTES} bytes: {relative}"
        )
    return data


def _disposition(change: PromotionChange, current_sha256: str | None) -> str:
    if change.action == "create":
        if current_sha256 is None:
            return "apply"
        if current_sha256 == change.after_sha256:
            return "already_applied"
        return "conflict"
    if change.action == "update":
        if current_sha256 == change.before_sha256:
            return "apply"
        if current_sha256 == change.after_sha256:
            return "already_applied"
        return "conflict"
    if change.action == "delete":
        if current_sha256 == change.before_sha256:
            return "apply"
        if current_sha256 is None:
            return "already_applied"
        return "conflict"
    raise AssertionError(f"unsupported action: {change.action}")


def reconcile_promotion_plan(plan: PromotionPlan, vault_snapshot_root: Path) -> PromotionReconciliation:
    normalized = parse_promotion_plan(plan.to_json_bytes())
    plan_sha = promotion_plan_sha256(normalized)
    observations: list[PromotionObservation] = []
    for change in normalized.changes:
        current = _digest(_local_file_bytes(vault_snapshot_root, change.path))
        disposition = _disposition(change, current)
        if disposition not in _ALLOWED_DISPOSITIONS:
            raise AssertionError(disposition)
        observations.append(
            PromotionObservation(
                action=change.action,
                path=change.path,
                before_sha256=change.before_sha256,
                after_sha256=change.after_sha256,
                current_sha256=current,
                disposition=disposition,
            )
        )
    return PromotionReconciliation(plan_sha, tuple(observations))


def _write_exact(path: Path, data: bytes) -> None:
    path = path.absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(fd)


def _plan_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="obsidian-core-promotion-plan",
        description=(
            "Build an immutable Core-to-Vault promotion plan from two ObsidianCore commits. "
            "Only paths managed by the public projection policy are eligible."
        ),
    )
    parser.add_argument("--core-repo", type=Path, required=True)
    parser.add_argument("--base-ref", required=True)
    parser.add_argument("--head-ref", required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def plan_main(argv: Sequence[str] | None = None) -> int:
    args = _plan_parser().parse_args(argv)
    try:
        plan = build_promotion_plan(
            args.core_repo,
            base_ref=args.base_ref,
            head_ref=args.head_ref,
            config_path=args.config,
        )
        data = plan.to_json_bytes()
        parsed = parse_promotion_plan(data)
        if parsed != plan:
            raise PromotionError("promotion plan canonical round-trip mismatch")
        _write_exact(args.output, data)
    except (PromotionError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "plan_sha256": sha256_bytes(data),
                "output": str(args.output),
                "base_commit": plan.base_commit,
                "head_commit": plan.head_commit,
                "change_count": len(plan.changes),
                "changes": [
                    {"action": item.action, "path": item.path} for item in plan.changes
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def _reconcile_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="obsidian-core-promotion-reconcile",
        description="Reconcile a Core promotion plan against an exact local Live Vault snapshot.",
    )
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--vault-snapshot-root", type=Path, required=True)
    return parser


def reconcile_main(argv: Sequence[str] | None = None) -> int:
    args = _reconcile_parser().parse_args(argv)
    try:
        data = args.plan.read_bytes()
        plan = parse_promotion_plan(data)
        if sha256_bytes(plan.to_json_bytes()) != sha256_bytes(data):
            raise PromotionError("promotion plan is not canonical")
        reconciliation = reconcile_promotion_plan(plan, args.vault_snapshot_root)
    except (PromotionError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "plan_sha256": reconciliation.plan_sha256,
                "has_conflict": reconciliation.has_conflict,
                "pending_count": reconciliation.pending_count,
                "observations": [
                    {
                        "action": item.action,
                        "path": item.path,
                        "current_sha256": item.current_sha256,
                        "disposition": item.disposition,
                    }
                    for item in reconciliation.observations
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 3 if reconciliation.has_conflict else 0
