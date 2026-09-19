"""Audit Core-tracked Live paths; repair only an explicitly selected exact plan.

The normal timer never repairs Live edits. Unknown remote-only paths are neither
listed nor deleted: they can be legitimate inputs to Vault -> Core publication.
CLI audit/repair share the ordinary promotion lock and never move its checkpoint.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import uuid
from typing import Any, Sequence
from urllib.parse import urlsplit, urlunsplit

from .core_promotion import CORE_REPOSITORY, _managed_by_projection, _resolve_commit, _tree_blob
from .core_promotion_transport import HTTPResponse, HTTPTransport, PromotionTransportError, _real_http_request
from .managed_promotion_transport import APPEARANCE_PATH, _appearance_projection, _decode_appearance, _merge_appearance
from .public_export import load_config
from .webdav_create import build_target_url

MAX_FILES = 4096
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_PLAN_BYTES = 8 * 1024 * 1024
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_ETAG = re.compile(r'"[\x21\x23-\x7e\x80-\xff]*"\Z')


class ReconciliationError(PromotionTransportError):
    """Fixed-code failure; never include response content or credentials."""


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def _path(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise ReconciliationError("invalid_managed_path")
    path = PurePosixPath(value)
    if (path.is_absolute() or path.as_posix() != value or ".." in path.parts
        or path.parts[0] == ".git" or "\\" in value or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ReconciliationError("invalid_managed_path")
    return value


def _etag(value: Any) -> str | None:
    if isinstance(value, str) and len(value) <= 1024 and _ETAG.fullmatch(value):
        return value
    return None


def _binding(base_url: str, username: str) -> str:
    # Bind the approval to an endpoint/account without persisting those values.
    validated = build_target_url(base_url, "98-System/__binding__")
    parsed = urlsplit(validated)
    canonical = urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, "", ""))
    return _digest(_json([canonical, username]))


def _safe_chain(path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise ReconciliationError("unsafe_local_path")
    for part in reversed((path, *path.parents)):
        if os.path.lexists(part) and part.is_symlink():
            raise ReconciliationError("symlink_local_path")


def _read_local(path: Path, limit: int) -> bytes:
    _safe_chain(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ReconciliationError("invalid_local_artifact")
        with os.fdopen(os.dup(fd), "rb") as handle:
            data = handle.read(limit + 1)
        after = os.fstat(fd)
        if len(data) > limit or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size, after.st_mtime_ns, after.st_ctime_ns
        ):
            raise ReconciliationError("local_artifact_changed")
        return data
    finally:
        os.close(fd)


def store_artifact(directory: Path, value: dict, suffix: str) -> tuple[str, Path]:
    if suffix not in {"live-drift-plan", "live-drift-receipt"}:
        raise ReconciliationError("invalid_artifact_kind")
    _safe_chain(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = directory.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022:
        raise ReconciliationError("unsafe_artifact_directory")
    data = _json(value)
    if len(data) > MAX_PLAN_BYTES:
        raise ReconciliationError("artifact_limit_exceeded")
    digest = _digest(data)
    path = directory / f"{digest}.{suffix}.json"
    _safe_chain(path)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        if _read_local(path, MAX_PLAN_BYTES) != data:
            raise ReconciliationError("existing_artifact_mismatch") from None
        return digest, path
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        dfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except BaseException:
        # A partial immutable file is not accepted as a successful artifact.
        # Keep it as evidence; a later digest check will fail closed.
        raise
    return digest, path


def _manifest(core_repository: Path, core_commit: str, config_path: Path) -> dict[str, bytes]:
    if not isinstance(core_commit, str) or not _COMMIT.fullmatch(core_commit):
        raise ReconciliationError("exact_core_commit_required")
    try:
        if _resolve_commit(core_repository, core_commit, label="Core") != core_commit:
            raise ReconciliationError("core_commit_mismatch")
        config = load_config(config_path)
        result = subprocess.run(
            ["git", "ls-tree", "-rz", "--full-tree", core_commit], cwd=core_repository,
            capture_output=True, check=False,
        )
        if result.returncode:
            raise ReconciliationError("cannot_read_core_tree")
        files: dict[str, bytes] = {}
        total = 0
        for row in result.stdout.split(b"\0"):
            if not row:
                continue
            metadata, raw_path = row.split(b"\t", 1)
            path = _path(raw_path.decode("utf-8"))
            if not _managed_by_projection(path, config):
                continue
            mode, kind, _oid = metadata.split()
            if mode not in {b"100644", b"100755"} or kind != b"blob":
                raise ReconciliationError("unsupported_managed_tree_entry")
            if len(files) >= MAX_FILES:
                raise ReconciliationError("managed_file_count_limit")
            data = _tree_blob(core_repository, core_commit, path)
            if data is None or len(data) > MAX_FILE_BYTES:
                raise ReconciliationError("managed_file_size_limit")
            total += len(data)
            if total > MAX_TOTAL_BYTES:
                raise ReconciliationError("managed_total_size_limit")
            if path == APPEARANCE_PATH:
                _decode_appearance(data, label="Core appearance", core_owned=True)
            files[path] = data
        if not files:
            raise ReconciliationError("empty_managed_manifest")
        return dict(sorted(files.items()))
    except ReconciliationError:
        raise
    except Exception:
        raise ReconciliationError("invalid_core_manifest") from None


def _request(*, method: str, path: str, base_url: str, username: str, password: str,
             timeout: float, transport: HTTPTransport | None,
             headers: dict[str, str] | None = None, body: bytes | None = None) -> HTTPResponse:
    if not math.isfinite(timeout) or timeout <= 0 or timeout > 300:
        raise ReconciliationError("invalid_request_timeout")
    try:
        response = (transport or _real_http_request)(
            method=method, target_url=build_target_url(base_url, path), username=username,
            password=password, headers=headers or {"Accept": "application/octet-stream"},
            body=body, timeout=timeout, response_limit=MAX_FILE_BYTES,
        )
    except Exception:
        raise ReconciliationError("webdav_request_failed") from None
    if not isinstance(response.body, bytes) or len(response.body) > MAX_FILE_BYTES:
        raise ReconciliationError("remote_response_limit")
    return response


def _observe(path: str, connection: dict) -> HTTPResponse:
    response = _request(method="GET", path=path, **connection)
    if response.status not in {200, 404}:
        raise ReconciliationError("webdav_observation_not_200_or_404")
    return response


def _desired(path: str, desired: bytes, response: HTTPResponse) -> bool:
    if response.status == 404:
        return False
    if path != APPEARANCE_PATH:
        return response.body == desired
    try:
        core = _decode_appearance(desired, label="Core appearance", core_owned=True)
        remote = _decode_appearance(response.body, label="Live appearance")
        return _appearance_projection(core) == _appearance_projection(remote)
    except Exception:
        raise ReconciliationError("invalid_appearance_requires_manual_review") from None


def audit_live(*, core_repository: Path, core_commit: str, config_path: Path,
               base_url: str, username: str, password: str, timeout: float = 30.0,
               transport: HTTPTransport | None = None) -> dict:
    """Read-only remote audit of every Core-tracked allowlisted path."""
    files = _manifest(core_repository, core_commit, config_path)
    binding = _binding(base_url, username)
    policy_sha = _digest(_read_local(config_path.absolute(), MAX_PLAN_BYTES))
    connection = dict(base_url=base_url, username=username, password=password,
                      timeout=timeout, transport=transport)
    observations = []
    total = 0
    for path, desired in files.items():
        response = _observe(path, connection)
        total += len(response.body)
        if total > MAX_TOTAL_BYTES:
            raise ReconciliationError("remote_total_size_limit")
        if _desired(path, desired, response):
            continue
        observations.append({
            "path": path, "desired_sha256": _digest(desired),
            "observed_sha256": _digest(response.body) if response.status == 200 else None,
            "observed_etag": _etag(response.etag) if response.status == 200 else None,
        })
    return {
        "record_version": 1, "stage": "core_live_drift_plan",
        "source_repository": CORE_REPOSITORY, "core_commit": core_commit,
        "policy_sha256": policy_sha, "target_binding": binding,
        "audit_scope": "core_tracked_managed_paths", "audited_files": len(files),
        "remote_only_paths": "not_enumerated_or_deleted", "observations": observations,
    }


def parse_plan(data: bytes, expected_sha256: str) -> dict:
    if len(data) > MAX_PLAN_BYTES or not _SHA.fullmatch(expected_sha256) or _digest(data) != expected_sha256:
        raise ReconciliationError("plan_digest_mismatch")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result
    try:
        plan = json.loads(data, object_pairs_hook=unique)
        expected = {"record_version", "stage", "source_repository", "core_commit", "policy_sha256",
                    "target_binding", "audit_scope", "audited_files", "remote_only_paths", "observations"}
        if not isinstance(plan, dict) or set(plan) != expected or _json(plan) != data:
            raise ValueError("noncanonical plan")
        if type(plan["record_version"]) is not int or plan["record_version"] != 1:
            raise ValueError("version")
        if (plan["stage"] != "core_live_drift_plan" or plan["source_repository"] != CORE_REPOSITORY
            or plan["audit_scope"] != "core_tracked_managed_paths"
            or plan["remote_only_paths"] != "not_enumerated_or_deleted"
            or not _COMMIT.fullmatch(plan["core_commit"])
            or not _SHA.fullmatch(plan["policy_sha256"]) or not _SHA.fullmatch(plan["target_binding"])):
            raise ValueError("binding")
        if type(plan["audited_files"]) is not int or not 0 < plan["audited_files"] <= MAX_FILES:
            raise ValueError("count")
        entries = plan["observations"]
        if not isinstance(entries, list) or len(entries) > plan["audited_files"]:
            raise ValueError("entries")
        paths = []
        for item in entries:
            if not isinstance(item, dict) or set(item) != {"path", "desired_sha256", "observed_sha256", "observed_etag"}:
                raise ValueError("entry")
            paths.append(_path(item["path"]))
            if not _SHA.fullmatch(item["desired_sha256"]):
                raise ValueError("desired hash")
            if item["observed_sha256"] is not None and not _SHA.fullmatch(item["observed_sha256"]):
                raise ValueError("observed hash")
            if item["observed_etag"] is not None and _etag(item["observed_etag"]) is None:
                raise ValueError("etag")
            if item["observed_sha256"] is None and item["observed_etag"] is not None:
                raise ValueError("missing etag")
        if paths != sorted(set(paths)):
            raise ValueError("duplicate or unordered path")
    except (ValueError, TypeError, KeyError, UnicodeError, ReconciliationError):
        raise ReconciliationError("invalid_reconciliation_plan") from None
    return plan


def repair_live(*, plan_data: bytes, expected_sha256: str, core_repository: Path,
                current_core_commit: str, config_path: Path, receipt_directory: Path,
                base_url: str, username: str, password: str, timeout: float = 30.0,
                transport: HTTPTransport | None = None) -> tuple[dict, Path]:
    """Caller holds promotion.lock; all writes use the exact reviewed observation.

    This is per-file CAS, not a multi-file transaction. Partial/ambiguous effects
    are receipted, never rolled back. A same-plan retry rechecks remote state.
    """
    plan = parse_plan(plan_data, expected_sha256)
    if current_core_commit != plan["core_commit"]:
        raise ReconciliationError("core_head_changed_reaudit_required")
    if _digest(_read_local(config_path.absolute(), MAX_PLAN_BYTES)) != plan["policy_sha256"]:
        raise ReconciliationError("policy_changed_reaudit_required")
    if _binding(base_url, username) != plan["target_binding"]:
        raise ReconciliationError("remote_binding_mismatch")
    files = _manifest(core_repository, current_core_commit, config_path)
    if len(files) != plan["audited_files"]:
        raise ReconciliationError("manifest_scope_changed")
    connection = dict(base_url=base_url, username=username, password=password,
                      timeout=timeout, transport=transport)
    prepared = []
    total = 0
    # Full preflight before the first PUT. Never infer an approved desired body
    # from the plan; obtain it from the immutable Core commit and policy again.
    for entry in plan["observations"]:
        path = entry["path"]
        if path not in files or _digest(files[path]) != entry["desired_sha256"]:
            raise ReconciliationError("plan_path_not_bound_to_core")
        response = _observe(path, connection)
        total += len(response.body)
        if total > MAX_TOTAL_BYTES:
            raise ReconciliationError("remote_total_size_limit")
        if _desired(path, files[path], response):
            prepared.append((entry, None, None))
            continue
        digest = _digest(response.body) if response.status == 200 else None
        if digest != entry["observed_sha256"]:
            raise ReconciliationError("live_changed_reaudit_required")
        headers = {"Content-Type": "application/octet-stream", "X-NC-WebDAV-Auto-Mkcol": "1"}
        if response.status == 404:
            headers["If-None-Match"] = "*"
        else:
            strong = _etag(response.etag)
            if strong is None or strong != entry["observed_etag"]:
                raise ReconciliationError("strong_etag_changed_or_unavailable")
            headers["If-Match"] = strong
        desired = files[path]
        if path == APPEARANCE_PATH and response.status == 200:
            try:
                desired = _merge_appearance(response.body, _decode_appearance(files[path], label="Core appearance", core_owned=True))
            except Exception:
                raise ReconciliationError("appearance_merge_failed") from None
        prepared.append((entry, desired, headers))
    receipt = {
        "record_version": 1, "stage": "core_live_drift_receipt", "run_id": uuid.uuid4().hex,
        "plan_sha256": expected_sha256, "core_commit": current_core_commit,
        "status": "started", "outcomes": [], "checkpoint_advanced": False,
    }
    store_artifact(receipt_directory, receipt, "live-drift-receipt")
    try:
        for entry, desired, headers in prepared:
            path = entry["path"]
            if desired is None:
                receipt["outcomes"].append({"path": path, "result": "already_desired"})
                continue
            receipt["outcomes"].append({"path": path, "result": "attempting"})
            store_artifact(receipt_directory, receipt, "live-drift-receipt")
            result = _request(method="PUT", path=path, headers=headers, body=desired, **connection)
            if result.status in {409, 412}:
                receipt["outcomes"][-1]["result"] = "cas_conflict"
                raise ReconciliationError("repair_cas_conflict")
            if result.status not in {200, 201, 204}:
                raise ReconciliationError("repair_put_not_successful")
            verified = _observe(path, connection)
            if verified.status != 200 or verified.body != desired:
                raise ReconciliationError("repair_postwrite_mismatch")
            receipt["outcomes"][-1]["result"] = "applied"
            store_artifact(receipt_directory, receipt, "live-drift-receipt")
    except BaseException:
        if receipt["outcomes"] and receipt["outcomes"][-1]["result"] == "attempting":
            receipt["outcomes"][-1]["result"] = "ambiguous_requires_recheck"
        receipt["status"] = "failed_or_partial"
        store_artifact(receipt_directory, receipt, "live-drift-receipt")
        raise
    receipt["status"] = "completed"
    _sha, path = store_artifact(receipt_directory, receipt, "live-drift-receipt")
    return receipt, path


def main(argv: Sequence[str] | None = None) -> int:
    from . import promotion_deployment as deployment
    from .core_promotion_transport import load_checkpoint
    from .webdav_create import _read_password

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("audit", "repair"))
    parser.add_argument("--state-root", type=Path, default=deployment.DEFAULT_STATE_ROOT)
    parser.add_argument("--config", type=Path, default=deployment.DEFAULT_POLICY_PATH)
    parser.add_argument("--base-url", default=os.environ.get("OBSIDIAN_PROMOTION_BASE_URL"))
    parser.add_argument("--username", default=os.environ.get("OBSIDIAN_PROMOTION_USERNAME"))
    parser.add_argument("--password-file", type=Path, default=deployment.DEFAULT_PASSWORD_FILE)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--plan-sha256")
    args = parser.parse_args(argv)
    try:
        if not args.base_url or not args.username:
            raise ReconciliationError("missing_remote_configuration")
        if args.operation == "repair" and (args.plan is None or args.plan_sha256 is None):
            raise ReconciliationError("explicit_plan_and_digest_required")
        if args.operation == "audit" and (args.plan is not None or args.plan_sha256 is not None):
            raise ReconciliationError("audit_does_not_accept_approval")
        _safe_chain(args.state_root.absolute())
        with deployment._production_lock(args.state_root / "promotion.lock"):
            core = deployment._ensure_core_repository(args.state_root)
            head = deployment._head_commit(core, git_runner=deployment._run_git)
            checkpoint = load_checkpoint(args.state_root / "checkpoint.json")
            if (checkpoint.source_repository != CORE_REPOSITORY
                or checkpoint.last_observed_core_commit != head
                or checkpoint.policy_sha256 != _digest(_read_local(args.config.absolute(), MAX_PLAN_BYTES))):
                raise ReconciliationError("ordered_promotion_must_converge_first")
            connection = dict(base_url=args.base_url, username=args.username,
                              password=_read_password(args.password_file))
            if args.operation == "audit":
                plan = audit_live(core_repository=core, core_commit=head, config_path=args.config, **connection)
                count = len(plan["observations"])
                digest, _path_value = store_artifact(args.state_root / "drift/plans", plan, "live-drift-plan")
                print(json.dumps({"event": "core-live-audit", "result": "live_drift" if count else "converged",
                                  "plan_sha256": digest, "drift_count": count,
                                  "audited_files": plan["audited_files"], "canonical_mutations": 0}, sort_keys=True))
                return 3 if count else 0
            data = _read_local(args.plan.absolute(), MAX_PLAN_BYTES)
            receipt, _path_value = repair_live(
                plan_data=data, expected_sha256=args.plan_sha256, core_repository=core,
                current_core_commit=head, config_path=args.config,
                receipt_directory=args.state_root / "drift/receipts", **connection,
            )
            print(json.dumps({"event": "core-live-repair", "result": receipt["status"],
                              "plan_sha256": args.plan_sha256, "checkpoint_advanced": False}, sort_keys=True))
            return 0
    except Exception:
        print(json.dumps({"event": "core-live-reconciliation", "status": "failed",
                          "values_printed": False}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
