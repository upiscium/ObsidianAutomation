"""Opt-in guarded WebDAV writer for the immutable Phase 1 Vault format plan.

No CLI, scheduler, automatic credential lookup or implicit write authority.
The official read-only plan remains the sole source of eligible transformations.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit

from .artifact_lifecycle import _store_immutable
from .core_promotion_transport import (
    HTTPResponse, HTTPTransport, _real_http_request,
)
from .vault_formatter import DEFAULT_MAX_BYTES, PLAN_VERSION, POLICY_VERSION, SCOPE, format_markdown
from .webdav_create import WebDAVCreateError, build_target_url

MAX_TARGETS = 32
MAX_PLAN_BYTES = 8 * 1024 * 1024
MAX_SELECTED_BYTES = 8 * 1024 * 1024
MAX_RECEIPT_BYTES = 64 * 1024
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_OPERATION = re.compile(r"[a-f0-9]{32}\Z")
_ETAG = re.compile(r'"[\x21\x23-\x7e\x80-\xff]*"\Z')


class FormatWriteError(RuntimeError):
    """No user content, tokens, or provider response bodies in errors."""


@dataclass(frozen=True)
class _Pending:
    path: str
    before_sha256: str
    after_sha256: str
    etag: str | None
    desired: bytes | None
    disposition: str


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(data: Any) -> bytes:
    return (json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _target(path: object) -> str:
    if not isinstance(path, str):
        raise FormatWriteError("invalid_target")
    try:
        size = len(path.encode("utf-8"))
    except UnicodeError:
        raise FormatWriteError("invalid_target") from None
    if size > 1024:
        raise FormatWriteError("invalid_target")
    try:
        parsed = PurePosixPath(path)
    except (ValueError, TypeError):
        raise FormatWriteError("invalid_target") from None
    if (
        parsed.as_posix() != path
        or parsed.is_absolute()
        or len(parsed.parts) < 2
        or parsed.parts[0] != "98-System"
        or any(part in ("", ".", "..") or part.startswith(".") for part in parsed.parts[1:])
        or parsed.suffix.lower() not in (".md", ".markdown")
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in path)
        or "\\" in path
    ):
        raise FormatWriteError("invalid_target")
    return path


def _strong_etag(value: object) -> str | None:
    if isinstance(value, str) and len(value) <= 1024 and _ETAG.fullmatch(value):
        return value
    return None


def _validated_plan(plan: object) -> tuple[str, dict[str, dict]]:
    """Re-admit a versioned plan as metadata; never trust it as desired bytes."""
    if type(plan) is not dict or "diffs" in plan and type(plan["diffs"]) is not dict:
        raise FormatWriteError("invalid_plan")
    fixed = {key: value for key, value in plan.items() if key not in ("plan_sha256", "diffs")}
    try:
        encoded = json.dumps(fixed, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise FormatWriteError("invalid_plan") from None
    if len(encoded) > MAX_PLAN_BYTES:
        raise FormatWriteError("plan_too_large")
    digest = _digest(encoded)
    if (
        type(plan.get("plan_sha256")) is not str
        or plan["plan_sha256"] != digest
        or type(fixed.get("schema_version")) is not int
        or fixed["schema_version"] != PLAN_VERSION
        or fixed.get("policy_version") != POLICY_VERSION
        or fixed.get("scope") != SCOPE
        or fixed.get("mode") != "dry-run"
        or type(fixed.get("results")) is not list
        or type(fixed.get("counts")) is not dict
        or type(fixed.get("scanned_bytes")) is not int
        or fixed["scanned_bytes"] < 0
    ):
        raise FormatWriteError("invalid_plan")
    entries: dict[str, dict] = {}
    for item in fixed["results"]:
        if type(item) is not dict or type(item.get("path")) is not str:
            raise FormatWriteError("invalid_plan_entry")
        if item["path"] in entries:
            raise FormatWriteError("duplicate_plan_entry")
        if item.get("status") == "error":
            raise FormatWriteError("plan_contains_error")
        if item.get("status") not in {"would_change", "unchanged", "skipped"}:
            raise FormatWriteError("invalid_plan_status")
        entries[item["path"]] = item
    return digest, entries


def _root(directory: Path) -> Path:
    """Require existing, caller-owned private state; never follow local symlinks."""
    path = Path(directory)
    if not path.is_absolute() or ".." in path.parts:
        raise FormatWriteError("unsafe_receipt_root")
    try:
        for part in reversed((path, *path.parents)):
            if part.is_symlink():
                raise FormatWriteError("unsafe_receipt_symlink")
        info = path.lstat()
    except OSError:
        raise FormatWriteError("unavailable_receipt_root") from None
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
    ):
        raise FormatWriteError("unsafe_receipt_permissions")
    return path


def _record(
    directory: Path,
    *,
    operation: str,
    stage: str,
    plan_sha: str,
    binding_sha: str,
    outcomes: list[dict],
) -> str:
    payload = {
        "version": 1,
        "stage": stage,
        "operation_id": operation,
        "plan_sha256": plan_sha,
        "binding_sha256": binding_sha,
        "recorded_at": _utc_now(),
        "outcomes": outcomes,
    }
    data = _canonical(payload)
    if len(data) > MAX_RECEIPT_BYTES:
        raise FormatWriteError("receipt_too_large")
    sha = _digest(data)
    try:
        _store_immutable(directory / f"{sha}.vault-formatter-receipt.json", data)
    except Exception:
        raise FormatWriteError("receipt_not_durable") from None
    return sha


def _marker(directory: Path, operation: str) -> None:
    """Reserve operation identity durably *before* any remote effect."""
    target = directory / f"operation-{operation}.marker"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW
    try:
        fd = os.open(target, flags, 0o600)
    except FileExistsError:
        raise FormatWriteError("operation_replay_blocked") from None
    except OSError:
        raise FormatWriteError("operation_reservation_failed") from None
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(b"vault-format-writer-v1\n")
            handle.flush()
            os.fsync(handle.fileno())
        parent_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except OSError:
        raise FormatWriteError("operation_reservation_failed") from None


def _request(
    transport: HTTPTransport | None,
    *,
    method: str,
    url: str,
    username: str,
    password: str,
    headers: Mapping[str, str],
    body: bytes | None,
    timeout: float,
    limit: int,
) -> HTTPResponse:
    if not math.isfinite(timeout) or not 0 < timeout <= 60:
        raise FormatWriteError("invalid_timeout")
    request = transport if transport is not None else _real_http_request
    response = request(
        method=method, target_url=url, username=username,
        password=password, headers=headers, body=body,
        timeout=timeout, response_limit=limit,
    )
    if (
        not isinstance(response, HTTPResponse)
        or type(response.status) is not int
        or type(response.body) is not bytes
        or len(response.body) > limit
    ):
        raise FormatWriteError("invalid_webdav_response")
    return response


def _observe(
    *,
    transport: HTTPTransport | None,
    url: str,
    username: str,
    password: str,
    timeout: float,
) -> HTTPResponse | None:
    try:
        return _request(
            transport, method="GET", url=url, username=username,
            password=password, headers={"Accept": "application/octet-stream"},
            body=None, timeout=timeout, limit=DEFAULT_MAX_BYTES,
        )
    except Exception:
        return None


def _result(path: str, before: str, after: str, status: str) -> dict:
    return {
        "path": path,
        "before_sha256": before,
        "after_sha256": after,
        "result": status,
    }


def _binding(base_url: str, username: str) -> str:
    target = build_target_url(base_url, "98-System/__binding__.md")
    parsed = urlsplit(target)
    # Never store the URL/username/credential; only its digest.
    return _digest(_canonical([parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, username]))


def apply_plan(
    plan: dict,
    *,
    selected_paths: Sequence[str] = (),
    enabled: bool = False,
    cas_server_verified: bool = False,
    receipt_dir: Path | None = None,
    base_url: str = "",
    username: str = "",
    password: str = "",
    timeout: float = 30.0,
    transport: HTTPTransport | None = None,
    operation_id: str | None = None,
) -> dict[str, Any]:
    """Apply selected Phase1 transformations only after separate operator enablement.

    With defaults this is *always* no-network/no-write. Setting enabled alone
    never writes; a separate pre-accepted server If-Match capability is needed.
    This internal API is not exposed as a production CLI or scheduler.
    """
    if type(enabled) is not bool or type(cas_server_verified) is not bool:
        raise FormatWriteError("invalid_authorization_mode")
    plan_sha, entries = _validated_plan(plan)
    if type(selected_paths) not in (list, tuple):
        raise FormatWriteError("invalid_selection")
    selected = sorted(set(_target(path) for path in selected_paths))
    if len(selected) != len(selected_paths) or len(selected) > MAX_TARGETS:
        raise FormatWriteError("invalid_selection")
    for path in selected:
        if path not in entries or entries[path]["status"] != "would_change":
            raise FormatWriteError("non_changing_or_unknown_target")
        item = entries[path]
        if (
            type(item.get("before_sha256")) is not str or not _SHA.fullmatch(item["before_sha256"])
            or type(item.get("after_sha256")) is not str or not _SHA.fullmatch(item["after_sha256"])
            or item["before_sha256"] == item["after_sha256"]
            or type(item.get("before_size")) is not int or not 0 <= item["before_size"] <= DEFAULT_MAX_BYTES
            or type(item.get("after_size")) is not int or not 0 <= item["after_size"] <= DEFAULT_MAX_BYTES
            or type(item.get("changed_lines")) is not list or not item["changed_lines"]
            or not all(type(n) is int and n > 0 for n in item["changed_lines"])
        ):
            raise FormatWriteError("invalid_target_plan")
    if sum(entries[path]["before_size"] for path in selected) > MAX_SELECTED_BYTES:
        raise FormatWriteError("selected_byte_budget")
    if not enabled or not selected:
        return {"status": "disabled" if not enabled else "no_changes", "plan_sha256": plan_sha,
                "outcomes": [], "vault_modified": False}
    if not cas_server_verified:
        raise FormatWriteError("server_cas_not_verified")
    if not isinstance(username, str) or not username or not isinstance(password, str) or not password:
        raise FormatWriteError("missing_private_connection")
    if receipt_dir is None:
        raise FormatWriteError("receipt_root_required")
    try:
        binding_sha = _binding(base_url, username)
    except (WebDAVCreateError, ValueError):
        raise FormatWriteError("invalid_private_connection") from None
    directory = _root(receipt_dir)
    operation = uuid.uuid4().hex if operation_id is None else operation_id
    if type(operation) is not str or _OPERATION.fullmatch(operation) is None:
        raise FormatWriteError("invalid_operation_id")
    _marker(directory, operation)
    _record(directory, operation=operation, stage="accepted", plan_sha=plan_sha,
            binding_sha=binding_sha, outcomes=[])

    pending: list[_Pending] = []
    preflight: list[dict] = []
    for path in selected:
        item = entries[path]
        url = build_target_url(base_url, path)
        response = _observe(transport=transport, url=url, username=username,
                            password=password, timeout=timeout)
        before, after = item["before_sha256"], item["after_sha256"]
        if response is None or response.status != 200:
            reason = "remote_unavailable" if response is None else "remote_missing_or_rejected"
            preflight.append(_result(path, before, after, reason))
            continue
        observed_sha = _digest(response.body)
        if observed_sha == after:
            pending.append(_Pending(path, before, after, None, None, "already_desired"))
            preflight.append(_result(path, before, after, "already_desired"))
            continue
        if observed_sha != before or len(response.body) != item["before_size"]:
            preflight.append(_result(path, before, after, "source_changed"))
            continue
        expected, lines, _ = format_markdown(response.body)
        if (
            expected is None
            or expected == response.body
            or len(expected) != item["after_size"]
            or _digest(expected) != after
            or list(lines) != item["changed_lines"]
        ):
            preflight.append(_result(path, before, after, "formatter_policy_mismatch"))
            continue
        etag = _strong_etag(response.etag)
        if etag is None:
            preflight.append(_result(path, before, after, "weak_or_missing_etag"))
            continue
        pending.append(_Pending(path, before, after, etag, expected, "ready"))
        preflight.append(_result(path, before, after, "ready"))

    if any(item["result"] not in {"already_desired", "ready"} for item in preflight):
        sha = _record(directory, operation=operation, stage="preflight_blocked",
                      plan_sha=plan_sha, binding_sha=binding_sha, outcomes=preflight)
        return {"status": "blocked", "plan_sha256": plan_sha,
                "operation_id": operation, "receipt_sha256": sha,
                "outcomes": preflight, "vault_modified": False}

    final: list[dict] = []
    observed_writes = False
    for item in pending:
        if item.disposition == "already_desired":
            final.append(_result(item.path, item.before_sha256, item.after_sha256, "already_desired"))
            continue
        url = build_target_url(base_url, item.path)
        # This durable record exists before any conditional PUT.
        _record(directory, operation=operation, stage="attempting",
                plan_sha=plan_sha, binding_sha=binding_sha,
                outcomes=final + [_result(item.path, item.before_sha256, item.after_sha256, "attempting")])
        try:
            put = _request(transport, method="PUT", url=url, username=username, password=password,
                           headers={"If-Match": item.etag, "Content-Type": "text/markdown; charset=utf-8"},
                           body=item.desired, timeout=timeout, limit=64 * 1024)
        except Exception:
            put = None
        if put is not None and put.status in {401, 403, 404, 409, 412, 423}:
            status = "cas_conflict" if put.status in {409, 412, 423} else "write_rejected"
            final.append(_result(item.path, item.before_sha256, item.after_sha256, status))
            break  # stop applying further files after a CAS refusal
        # Any accepted/uncertain PUT requires a fresh canonical GET readback.
        remote = _observe(transport=transport, url=url, username=username,
                          password=password, timeout=timeout)
        if remote is not None and remote.status == 200 and _digest(remote.body) == item.after_sha256:
            if put is None or put.status in {500, 502, 503, 504}:
                status = "recovered"
                observed_writes = True
            elif put.status in {200, 204}:
                status = "applied"
                observed_writes = True
            else:
                status = "indeterminate"
        else:
            status = "indeterminate"
        final.append(_result(item.path, item.before_sha256, item.after_sha256, status))
        if status == "indeterminate":
            break  # never issue further remote PUTs after an ambiguous effect

    statuses = {x["result"] for x in final}
    outcome = ("indeterminate" if "indeterminate" in statuses else
               "complete" if statuses <= {"applied", "recovered", "already_desired"} else
               "partial" if observed_writes else "blocked")
    sha = _record(directory, operation=operation, stage=outcome, plan_sha=plan_sha,
                  binding_sha=binding_sha, outcomes=final)
    modified: bool | None = None if outcome == "indeterminate" else observed_writes
    return {"status": outcome, "plan_sha256": plan_sha, "operation_id": operation,
            "receipt_sha256": sha, "outcomes": final, "vault_modified": modified}
