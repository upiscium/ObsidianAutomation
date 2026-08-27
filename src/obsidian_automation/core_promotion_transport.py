from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence
from urllib.parse import urlsplit

from .artifact_lifecycle import _canonical_json_bytes, _decode_json_object, _utc_now, sha256_bytes
from .core_promotion import (
    CORE_REPOSITORY,
    PROMOTION_POLICY_VERSION,
    PromotionChange,
    PromotionError,
    PromotionPlan,
    _tree_blob,
    parse_promotion_plan,
    promotion_plan_sha256,
    verify_plan_against_core,
)
from .webdav_create import (
    WebDAVCreateError,
    _authorization,
    _connection,
    _read_password,
    build_target_url,
)


CHECKPOINT_VERSION = 1
RECEIPT_VERSION = 1
MAX_REMOTE_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_MUTATION_RESPONSE_BYTES = 64 * 1024
_COMMIT_RE = re.compile(r"^[0-9a-f]{40,64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class PromotionTransportError(RuntimeError):
    """Raised when a promotion transport cannot complete safely."""


class PromotionTransportConflict(PromotionTransportError):
    """Raised when current remote state diverges from both Core base and head."""


class PromotionTransportNetworkError(PromotionTransportError):
    """Raised when a WebDAV request has an ambiguous transport outcome."""


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    body: bytes
    etag: str | None


@dataclass(frozen=True)
class RemoteState:
    path: str
    content_sha256: str | None
    etag: str | None
    status_code: int


@dataclass(frozen=True)
class PreflightChange:
    change: PromotionChange
    remote: RemoteState
    disposition: str


@dataclass(frozen=True)
class PromotionOutcome:
    action: str
    path: str
    before_sha256: str | None
    after_sha256: str | None
    result: str


@dataclass(frozen=True)
class PromotionCheckpoint:
    source_repository: str
    last_observed_core_commit: str
    policy_version: str
    policy_sha256: str
    updated_at: str

    def to_json_bytes(self) -> bytes:
        return _canonical_json_bytes(
            {
                "record_version": CHECKPOINT_VERSION,
                "source_repository": self.source_repository,
                "last_observed_core_commit": self.last_observed_core_commit,
                "policy": {
                    "version": self.policy_version,
                    "sha256": self.policy_sha256,
                },
                "updated_at": self.updated_at,
            }
        )


@dataclass(frozen=True)
class PromotionReceipt:
    plan_sha256: str
    base_commit: str
    head_commit: str
    policy_version: str
    policy_sha256: str
    outcomes: tuple[PromotionOutcome, ...]
    completed_at: str

    def to_json_bytes(self) -> bytes:
        return _canonical_json_bytes(
            {
                "record_version": RECEIPT_VERSION,
                "plan_sha256": self.plan_sha256,
                "source_repository": CORE_REPOSITORY,
                "base_commit": self.base_commit,
                "head_commit": self.head_commit,
                "policy": {
                    "version": self.policy_version,
                    "sha256": self.policy_sha256,
                },
                "outcomes": [
                    {
                        "action": item.action,
                        "path": item.path,
                        "before_sha256": item.before_sha256,
                        "after_sha256": item.after_sha256,
                        "result": item.result,
                    }
                    for item in self.outcomes
                ],
                "completed_at": self.completed_at,
            }
        )


HTTPTransport = Callable[..., HTTPResponse]


def _require_commit(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _COMMIT_RE.fullmatch(value) is None:
        raise PromotionTransportError(f"{label} is not a supported lowercase Git commit digest")
    return value


def _require_sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise PromotionTransportError(f"{label} is not a lowercase SHA-256 digest")
    return value


def _strong_etag(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized or normalized.startswith("W/"):
        return None
    if len(normalized) < 2 or not normalized.startswith('"') or not normalized.endswith('"'):
        return None
    return normalized


def _real_http_request(
    *,
    method: str,
    target_url: str,
    username: str,
    password: str,
    headers: Mapping[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = 30.0,
    response_limit: int = MAX_REMOTE_RESPONSE_BYTES,
) -> HTTPResponse:
    parsed = urlsplit(target_url)
    auth = _authorization(username, password)
    request_headers = {"Authorization": auth, **dict(headers or {})}
    if body is not None:
        request_headers.setdefault("Content-Length", str(len(body)))
    conn = _connection(parsed, timeout=timeout)
    try:
        conn.request(method, parsed.path, body=body, headers=request_headers)
        response = conn.getresponse()
        raw = response.read(response_limit + 1)
        status = response.status
        etag = response.getheader("ETag") or response.getheader("OC-ETag")
    except OSError as exc:
        raise PromotionTransportNetworkError(
            f"WebDAV {method} request failed before a trustworthy response was obtained: {exc}"
        ) from exc
    finally:
        conn.close()
    if len(raw) > response_limit:
        raise PromotionTransportError(
            f"WebDAV {method} response exceeds {response_limit} bytes"
        )
    return HTTPResponse(status=status, body=raw, etag=etag)


def _observe_remote(
    *,
    base_url: str,
    path: str,
    username: str,
    password: str,
    timeout: float,
    transport: HTTPTransport | None,
) -> RemoteState:
    try:
        target = build_target_url(base_url, path)
    except WebDAVCreateError as exc:
        raise PromotionTransportError(str(exc)) from exc
    request = transport or _real_http_request
    response = request(
        method="GET",
        target_url=target,
        username=username,
        password=password,
        headers={"Accept": "application/octet-stream"},
        body=None,
        timeout=timeout,
        response_limit=MAX_REMOTE_RESPONSE_BYTES,
    )
    if response.status == 404:
        return RemoteState(path=path, content_sha256=None, etag=response.etag, status_code=404)
    if response.status != 200:
        raise PromotionTransportError(
            f"WebDAV GET returned unexpected HTTP status {response.status} for {path}"
        )
    digest = hashlib.sha256(response.body).hexdigest()
    return RemoteState(
        path=path,
        content_sha256=digest,
        etag=response.etag,
        status_code=response.status,
    )


def _preflight_disposition(change: PromotionChange, remote: RemoteState) -> str:
    current = remote.content_sha256
    if change.action == "create":
        if current is None:
            return "apply"
        if current == change.after_sha256:
            return "already_applied"
        return "conflict"
    if change.action == "update":
        if current == change.before_sha256:
            return "apply"
        if current == change.after_sha256:
            return "already_applied"
        return "conflict"
    if change.action == "delete":
        if current == change.before_sha256:
            return "apply"
        if current is None:
            return "already_applied"
        return "conflict"
    raise PromotionTransportError(f"unsupported promotion action: {change.action}")


def preflight_remote(
    plan: PromotionPlan,
    *,
    base_url: str,
    username: str,
    password: str,
    timeout: float = 30.0,
    transport: HTTPTransport | None = None,
) -> tuple[PreflightChange, ...]:
    if not username:
        raise PromotionTransportError("promotion WebDAV username must not be empty")
    if not password:
        raise PromotionTransportError("promotion WebDAV password must not be empty")
    if timeout <= 0 or timeout > 600:
        raise PromotionTransportError("promotion timeout must be in (0, 600] seconds")

    entries: list[PreflightChange] = []
    for change in plan.changes:
        remote = _observe_remote(
            base_url=base_url,
            path=change.path,
            username=username,
            password=password,
            timeout=timeout,
            transport=transport,
        )
        disposition = _preflight_disposition(change, remote)
        if disposition == "apply" and change.action in {"update", "delete"}:
            if _strong_etag(remote.etag) is None:
                raise PromotionTransportError(
                    f"remote {change.path} does not provide a strong ETag required for conditional {change.action}"
                )
        entries.append(PreflightChange(change=change, remote=remote, disposition=disposition))

    conflicts = [entry.change.path for entry in entries if entry.disposition == "conflict"]
    if conflicts:
        raise PromotionTransportConflict(
            "promotion preflight found divergent remote paths; no mutation attempted: "
            + ", ".join(conflicts)
        )
    return tuple(entries)


def _desired_bytes(core_repository: Path, plan: PromotionPlan, change: PromotionChange) -> bytes:
    if change.after_sha256 is None:
        raise PromotionTransportError("delete promotion has no desired bytes")
    try:
        content = _tree_blob(core_repository, plan.head_commit, change.path)
    except PromotionError as exc:
        raise PromotionTransportError(str(exc)) from exc
    if content is None:
        raise PromotionTransportError(f"Core head is missing desired promotion path: {change.path}")
    digest = hashlib.sha256(content).hexdigest()
    if digest != change.after_sha256:
        raise PromotionTransportError(
            f"Core head bytes do not match Promotion Plan after_sha256: {change.path}"
        )
    return content


def _desired_reached(change: PromotionChange, remote: RemoteState) -> bool:
    if change.action == "delete":
        return remote.content_sha256 is None
    return remote.content_sha256 == change.after_sha256


def _perform_mutation(
    *,
    change: PromotionChange,
    preflight: PreflightChange,
    plan: PromotionPlan,
    core_repository: Path,
    base_url: str,
    username: str,
    password: str,
    timeout: float,
    transport: HTTPTransport | None,
) -> str:
    request = transport or _real_http_request
    try:
        target = build_target_url(base_url, change.path)
    except WebDAVCreateError as exc:
        raise PromotionTransportError(str(exc)) from exc

    headers: dict[str, str] = {}
    body: bytes | None = None
    method: str
    if change.action == "create":
        method = "PUT"
        body = _desired_bytes(core_repository, plan, change)
        headers.update(
            {
                "If-None-Match": "*",
                "Content-Type": "application/octet-stream",
                "X-NC-WebDAV-AutoMkcol": "1",
            }
        )
    elif change.action == "update":
        method = "PUT"
        body = _desired_bytes(core_repository, plan, change)
        etag = _strong_etag(preflight.remote.etag)
        if etag is None:
            raise PromotionTransportError(
                f"remote {change.path} lost its required strong ETag before update"
            )
        headers.update(
            {
                "If-Match": etag,
                "Content-Type": "application/octet-stream",
                "X-NC-WebDAV-AutoMkcol": "1",
            }
        )
    elif change.action == "delete":
        method = "DELETE"
        etag = _strong_etag(preflight.remote.etag)
        if etag is None:
            raise PromotionTransportError(
                f"remote {change.path} lost its required strong ETag before delete"
            )
        headers["If-Match"] = etag
    else:
        raise PromotionTransportError(f"unsupported promotion action: {change.action}")

    ambiguous = False
    status: int | None = None
    try:
        response = request(
            method=method,
            target_url=target,
            username=username,
            password=password,
            headers=headers,
            body=body,
            timeout=timeout,
            response_limit=MAX_MUTATION_RESPONSE_BYTES,
        )
        status = response.status
        if not 200 <= response.status < 300:
            ambiguous = True
    except PromotionTransportNetworkError:
        ambiguous = True

    remote = _observe_remote(
        base_url=base_url,
        path=change.path,
        username=username,
        password=password,
        timeout=timeout,
        transport=transport,
    )
    if _desired_reached(change, remote):
        return "recovered" if ambiguous else "applied"

    if ambiguous:
        if remote.content_sha256 == change.before_sha256:
            detail = "no desired change is observable remotely"
            if status is not None:
                detail += f" after HTTP {status}"
            raise PromotionTransportNetworkError(f"ambiguous {change.action} for {change.path}: {detail}")
        raise PromotionTransportConflict(
            f"remote {change.path} diverged after ambiguous {change.action} outcome"
        )

    raise PromotionTransportConflict(
        f"remote exact-byte verification failed after successful {change.action}: {change.path}"
    )


def parse_checkpoint(data: bytes) -> PromotionCheckpoint:
    try:
        value = _decode_json_object(data, label="Core promotion checkpoint")
    except Exception as exc:
        if isinstance(exc, PromotionTransportError):
            raise
        raise PromotionTransportError(str(exc)) from exc
    if set(value) != {
        "record_version",
        "source_repository",
        "last_observed_core_commit",
        "policy",
        "updated_at",
    }:
        raise PromotionTransportError("promotion checkpoint properties do not match contract")
    if type(value["record_version"]) is not int or value["record_version"] != CHECKPOINT_VERSION:
        raise PromotionTransportError("promotion checkpoint record_version must be integer 1")
    if value["source_repository"] != CORE_REPOSITORY:
        raise PromotionTransportError("promotion checkpoint source repository is invalid")
    commit = _require_commit(value["last_observed_core_commit"], label="last_observed_core_commit")
    policy = value["policy"]
    if not isinstance(policy, dict) or set(policy) != {"version", "sha256"}:
        raise PromotionTransportError("promotion checkpoint policy is invalid")
    if policy["version"] != PROMOTION_POLICY_VERSION:
        raise PromotionTransportError("promotion checkpoint policy version is unsupported")
    policy_sha = _require_sha256(policy["sha256"], label="checkpoint policy sha256")
    updated_at = value["updated_at"]
    if not isinstance(updated_at, str) or not updated_at.endswith("Z"):
        raise PromotionTransportError("promotion checkpoint updated_at is invalid")
    return PromotionCheckpoint(
        source_repository=CORE_REPOSITORY,
        last_observed_core_commit=commit,
        policy_version=PROMOTION_POLICY_VERSION,
        policy_sha256=policy_sha,
        updated_at=updated_at,
    )


def load_checkpoint(path: Path) -> PromotionCheckpoint:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise PromotionTransportError(
            f"promotion checkpoint does not exist: {path}; initialize it explicitly"
        ) from exc
    if path.is_symlink() or not path.is_file():
        raise PromotionTransportError("promotion checkpoint must be a regular non-symlink file")
    data = path.read_bytes()
    checkpoint = parse_checkpoint(data)
    if checkpoint.to_json_bytes() != data:
        raise PromotionTransportError("promotion checkpoint is not canonical")
    return checkpoint


def _atomic_replace(path: Path, data: bytes) -> None:
    parent = path.parent.absolute()
    parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.is_symlink():
        raise PromotionTransportError("refusing to replace symlink checkpoint")
    fd, temp_name = tempfile.mkstemp(prefix=".promotion-checkpoint-", dir=parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        dir_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if temp.exists():
            temp.unlink()


def initialize_checkpoint(
    path: Path,
    *,
    core_commit: str,
    policy_sha256: str,
    initialized_at: str | None = None,
) -> PromotionCheckpoint:
    if path.exists() or path.is_symlink():
        raise PromotionTransportError("promotion checkpoint already exists")
    checkpoint = PromotionCheckpoint(
        source_repository=CORE_REPOSITORY,
        last_observed_core_commit=_require_commit(core_commit, label="core_commit"),
        policy_version=PROMOTION_POLICY_VERSION,
        policy_sha256=_require_sha256(policy_sha256, label="policy_sha256"),
        updated_at=initialized_at or _utc_now(),
    )
    data = parse_checkpoint(checkpoint.to_json_bytes()).to_json_bytes()
    parent = path.parent.absolute()
    parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(fd)
    return checkpoint


def _advance_checkpoint(path: Path, plan: PromotionPlan) -> PromotionCheckpoint:
    checkpoint = PromotionCheckpoint(
        source_repository=CORE_REPOSITORY,
        last_observed_core_commit=plan.head_commit,
        policy_version=plan.policy_version,
        policy_sha256=plan.policy_sha256,
        updated_at=_utc_now(),
    )
    _atomic_replace(path, checkpoint.to_json_bytes())
    return checkpoint


def parse_receipt(data: bytes) -> PromotionReceipt:
    try:
        value = _decode_json_object(data, label="Core promotion receipt")
    except Exception as exc:
        raise PromotionTransportError(str(exc)) from exc
    required = {
        "record_version",
        "plan_sha256",
        "source_repository",
        "base_commit",
        "head_commit",
        "policy",
        "outcomes",
        "completed_at",
    }
    if set(value) != required:
        raise PromotionTransportError("promotion receipt properties do not match contract")
    if type(value["record_version"]) is not int or value["record_version"] != RECEIPT_VERSION:
        raise PromotionTransportError("promotion receipt record_version must be integer 1")
    plan_sha = _require_sha256(value["plan_sha256"], label="receipt plan_sha256")
    if value["source_repository"] != CORE_REPOSITORY:
        raise PromotionTransportError("promotion receipt source repository is invalid")
    base = _require_commit(value["base_commit"], label="receipt base_commit")
    head = _require_commit(value["head_commit"], label="receipt head_commit")
    policy = value["policy"]
    if not isinstance(policy, dict) or set(policy) != {"version", "sha256"}:
        raise PromotionTransportError("promotion receipt policy is invalid")
    if policy["version"] != PROMOTION_POLICY_VERSION:
        raise PromotionTransportError("promotion receipt policy version is unsupported")
    policy_sha = _require_sha256(policy["sha256"], label="receipt policy sha256")
    raw_outcomes = value["outcomes"]
    if not isinstance(raw_outcomes, list):
        raise PromotionTransportError("promotion receipt outcomes must be a list")
    outcomes: list[PromotionOutcome] = []
    seen: set[str] = set()
    for raw in raw_outcomes:
        if not isinstance(raw, dict) or set(raw) != {
            "action",
            "path",
            "before_sha256",
            "after_sha256",
            "result",
        }:
            raise PromotionTransportError("promotion receipt outcome properties do not match contract")
        action = raw["action"]
        path = raw["path"]
        result = raw["result"]
        if action not in {"create", "update", "delete"}:
            raise PromotionTransportError("promotion receipt action is invalid")
        if not isinstance(path, str) or not path or path in seen:
            raise PromotionTransportError("promotion receipt path is invalid or duplicated")
        seen.add(path)
        before = raw["before_sha256"]
        after = raw["after_sha256"]
        if before is not None:
            before = _require_sha256(before, label="receipt before_sha256")
        if after is not None:
            after = _require_sha256(after, label="receipt after_sha256")
        if result not in {"applied", "already_applied", "recovered"}:
            raise PromotionTransportError("promotion receipt result is invalid")
        outcomes.append(PromotionOutcome(action, path, before, after, result))
    completed_at = value["completed_at"]
    if not isinstance(completed_at, str) or not completed_at.endswith("Z"):
        raise PromotionTransportError("promotion receipt completed_at is invalid")
    return PromotionReceipt(
        plan_sha256=plan_sha,
        base_commit=base,
        head_commit=head,
        policy_version=PROMOTION_POLICY_VERSION,
        policy_sha256=policy_sha,
        outcomes=tuple(outcomes),
        completed_at=completed_at,
    )


def store_receipt(directory: Path, receipt: PromotionReceipt) -> tuple[str, Path]:
    data = parse_receipt(receipt.to_json_bytes()).to_json_bytes()
    digest = sha256_bytes(data)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{digest}.promotion-receipt.json"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        existing = path.read_bytes()
        if existing != data:
            raise PromotionTransportError("existing promotion receipt hash path has different bytes")
        return digest, path
    try:
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(fd)
    return digest, path


def execute_promotion(
    *,
    plan: PromotionPlan,
    core_repository: Path,
    config_path: Path,
    base_url: str,
    username: str,
    password: str,
    checkpoint_path: Path,
    receipt_directory: Path,
    timeout: float = 30.0,
    transport: HTTPTransport | None = None,
) -> tuple[str, Path, PromotionReceipt, PromotionCheckpoint]:
    try:
        normalized_plan = parse_promotion_plan(plan.to_json_bytes())
        verify_plan_against_core(normalized_plan, core_repository, config_path=config_path)
    except PromotionError as exc:
        raise PromotionTransportError(str(exc)) from exc

    checkpoint = load_checkpoint(checkpoint_path)
    if checkpoint.last_observed_core_commit != normalized_plan.base_commit:
        raise PromotionTransportError(
            "promotion plan base_commit does not match last_observed_core_commit checkpoint"
        )
    if checkpoint.policy_version != normalized_plan.policy_version or checkpoint.policy_sha256 != normalized_plan.policy_sha256:
        raise PromotionTransportError("promotion plan policy does not match checkpoint policy")

    preflight = preflight_remote(
        normalized_plan,
        base_url=base_url,
        username=username,
        password=password,
        timeout=timeout,
        transport=transport,
    )

    outcomes: list[PromotionOutcome] = []
    for entry in preflight:
        change = entry.change
        if entry.disposition == "already_applied":
            result = "already_applied"
        elif entry.disposition == "apply":
            result = _perform_mutation(
                change=change,
                preflight=entry,
                plan=normalized_plan,
                core_repository=core_repository,
                base_url=base_url,
                username=username,
                password=password,
                timeout=timeout,
                transport=transport,
            )
        else:
            raise PromotionTransportConflict(f"unexpected conflict after preflight: {change.path}")
        outcomes.append(
            PromotionOutcome(
                action=change.action,
                path=change.path,
                before_sha256=change.before_sha256,
                after_sha256=change.after_sha256,
                result=result,
            )
        )

    receipt = PromotionReceipt(
        plan_sha256=promotion_plan_sha256(normalized_plan),
        base_commit=normalized_plan.base_commit,
        head_commit=normalized_plan.head_commit,
        policy_version=normalized_plan.policy_version,
        policy_sha256=normalized_plan.policy_sha256,
        outcomes=tuple(outcomes),
        completed_at=_utc_now(),
    )
    receipt_sha, receipt_path = store_receipt(receipt_directory, receipt)
    advanced = _advance_checkpoint(checkpoint_path, normalized_plan)
    return receipt_sha, receipt_path, receipt, advanced


def _load_plan_file(path: Path) -> PromotionPlan:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise PromotionTransportError(f"cannot read promotion plan: {path}") from exc
    plan = parse_promotion_plan(data)
    if plan.to_json_bytes() != data:
        raise PromotionTransportError("promotion plan file is not canonical")
    return plan


def init_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="obsidian-core-promotion-checkpoint-init")
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--core-commit", required=True)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        policy_sha = sha256_bytes(args.config.read_bytes())
        checkpoint = initialize_checkpoint(
            args.checkpoint,
            core_commit=args.core_commit,
            policy_sha256=policy_sha,
        )
    except (OSError, PromotionTransportError) as exc:
        print(f"error: {exc}", file=os.sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "checkpoint": str(args.checkpoint),
                "last_observed_core_commit": checkpoint.last_observed_core_commit,
                "policy_sha256": checkpoint.policy_sha256,
            },
            sort_keys=True,
        )
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="obsidian-core-promotion-transport")
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--core-repo", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--password-file", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--receipt-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)

    try:
        plan = _load_plan_file(args.plan)
        password = _read_password(args.password_file)
        receipt_sha, receipt_path, receipt, checkpoint = execute_promotion(
            plan=plan,
            core_repository=args.core_repo,
            config_path=args.config,
            base_url=args.base_url,
            username=args.username,
            password=password,
            checkpoint_path=args.checkpoint,
            receipt_directory=args.receipt_dir,
            timeout=args.timeout,
        )
    except PromotionTransportConflict as exc:
        print(f"conflict: {exc}", file=os.sys.stderr)
        return 3
    except (OSError, WebDAVCreateError, PromotionTransportError) as exc:
        print(f"error: {exc}", file=os.sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "result": "completed",
                "plan_sha256": receipt.plan_sha256,
                "receipt_sha256": receipt_sha,
                "receipt_path": str(receipt_path),
                "head_commit": receipt.head_commit,
                "checkpoint_commit": checkpoint.last_observed_core_commit,
                "outcomes": [
                    {"action": item.action, "path": item.path, "result": item.result}
                    for item in receipt.outcomes
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0
