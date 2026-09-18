from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .core_promotion_transport import (
    HTTPResponse,
    PromotionTransportNetworkError,
    _real_http_request,
    _strong_etag,
)
from .production_io import ProductionIOError, canonical_io_lock
from .webdav_create import WebDAVCreateError, _read_password, build_target_url


TRANSPORT_RESULT_VERSION = 1
MAX_PROJECT_BYTES = 4 * 1024 * 1024
VALID_PROJECT_STATUSES = frozenset({"planning", "running", "stopped", "done", "cancelled"})
AUTOMATED_TARGET_STATUSES = frozenset({"planning", "running"})
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")


class ProjectStatusMutationError(RuntimeError):
    """Raised when a GitHub Project status mutation cannot be completed safely."""


class ProjectStatusMutationConflict(ProjectStatusMutationError):
    """Raised when current canonical state no longer matches the proposal baseline."""

    def __init__(
        self,
        message: str,
        *,
        reason_code: str = "canonical_conflict",
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.http_status = http_status


class ProjectStatusMutationRejected(ProjectStatusMutationError):
    """Raised when a trustworthy HTTP response deterministically rejects a mutation."""

    def __init__(self, message: str, *, reason_code: str, http_status: int) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.http_status = http_status


_AMBIGUOUS_HTTP_STATUSES = frozenset({500, 502, 503, 504})


@dataclass(frozen=True)
class ProjectStatusProposal:
    project_path: str
    repository: str
    expected_status: str
    desired_status: str
    observed_at: str
    latest_commit_sha: str | None
    latest_commit_at: str | None
    open_issues: tuple[int, ...]
    open_prs: tuple[int, ...]
    reason: str
    canonical_bytes: bytes
    sha256: str


@dataclass(frozen=True)
class RemoteProject:
    content: bytes
    etag: str | None
    status_code: int


@dataclass(frozen=True)
class ProjectStatusTransportResult:
    proposal_sha256: str
    project_path: str
    repository: str
    expected_status: str
    desired_status: str
    outcome: str
    before_content_sha256: str
    after_content_sha256: str
    completed_at: str

    def to_json_bytes(self) -> bytes:
        return _canonical_json_bytes(
            {
                "record_version": TRANSPORT_RESULT_VERSION,
                "stage": "github_project_status_transport",
                "proposal_sha256": self.proposal_sha256,
                "project_path": self.project_path,
                "repository": self.repository,
                "expected_status": self.expected_status,
                "desired_status": self.desired_status,
                "outcome": self.outcome,
                "before_content_sha256": self.before_content_sha256,
                "after_content_sha256": self.after_content_sha256,
                "completed_at": self.completed_at,
            }
        )


HTTPTransport = Callable[..., HTTPResponse]


def _canonical_json_bytes(payload: Mapping[str, object]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ProjectStatusMutationError(f"duplicate JSON property: {key}")
        result[key] = value
    return result


def _parse_timestamp(value: object, *, label: str, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not value:
        raise ProjectStatusMutationError(f"{label} must be a non-empty timestamp string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProjectStatusMutationError(f"{label} is not a valid timestamp") from exc
    if parsed.tzinfo is None:
        raise ProjectStatusMutationError(f"{label} must include timezone information")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_number_list(value: object, *, label: str) -> tuple[int, ...]:
    if not isinstance(value, list):
        raise ProjectStatusMutationError(f"{label} must be an array")
    items: list[int] = []
    for item in value:
        if type(item) is not int or item < 1:
            raise ProjectStatusMutationError(f"{label} entries must be positive integers")
        items.append(item)
    if len(items) != len(set(items)):
        raise ProjectStatusMutationError(f"{label} must not contain duplicates")
    return tuple(sorted(items))


def _safe_project_path(value: object) -> str:
    if not isinstance(value, str) or not value.endswith(".md"):
        raise ProjectStatusMutationError("project must be a Markdown path")
    if value.startswith("/") or "\\" in value or "\x00" in value:
        raise ProjectStatusMutationError("project must be a relative POSIX path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ProjectStatusMutationError("project contains an unsafe path component")
    if len(parts) < 2 or parts[0] != "10-Project":
        raise ProjectStatusMutationError("project must be below 10-Project")
    return value


def parse_watcher_proposal(data: bytes) -> ProjectStatusProposal:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProjectStatusMutationError("proposal must be UTF-8") from exc
    try:
        value = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except ProjectStatusMutationError:
        raise
    except json.JSONDecodeError as exc:
        raise ProjectStatusMutationError("proposal is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ProjectStatusMutationError("proposal must be a JSON object")

    required = {
        "change",
        "current_status",
        "event",
        "latest_commit_at",
        "latest_commit_sha",
        "observed_at",
        "open_issues",
        "open_prs",
        "pending",
        "project",
        "proposed_status",
        "reason",
        "repository",
    }
    if set(value) != required:
        missing = sorted(required - set(value))
        unknown = sorted(set(value) - required)
        raise ProjectStatusMutationError(
            f"proposal properties do not match watcher contract; missing={missing}, unknown={unknown}"
        )
    if value["event"] != "project-status-observation":
        raise ProjectStatusMutationError("event must be project-status-observation")
    if value["change"] is not True or value["pending"] is not True:
        raise ProjectStatusMutationError("proposal must be a pending status change")

    project_path = _safe_project_path(value["project"])
    repository = value["repository"]
    if not isinstance(repository, str) or _REPOSITORY_RE.fullmatch(repository) is None:
        raise ProjectStatusMutationError("repository must be owner/name")

    expected_status = value["current_status"]
    desired_status = value["proposed_status"]
    if expected_status not in VALID_PROJECT_STATUSES:
        raise ProjectStatusMutationError("current_status is not a canonical Project status")
    if expected_status == "stopped":
        raise ProjectStatusMutationError("stopped is human-controlled and cannot be automated")
    if desired_status not in AUTOMATED_TARGET_STATUSES:
        raise ProjectStatusMutationError("proposed_status must be planning or running")
    if desired_status == expected_status:
        raise ProjectStatusMutationError("proposal must change Project status")

    observed_at = _parse_timestamp(value["observed_at"], label="observed_at")
    latest_commit_at = _parse_timestamp(
        value["latest_commit_at"], label="latest_commit_at", allow_none=True
    )
    latest_commit_sha = value["latest_commit_sha"]
    if latest_commit_sha is not None and (
        not isinstance(latest_commit_sha, str) or _SHA_RE.fullmatch(latest_commit_sha) is None
    ):
        raise ProjectStatusMutationError("latest_commit_sha must be null or a lowercase Git digest")
    if latest_commit_sha is None and latest_commit_at is not None:
        raise ProjectStatusMutationError("latest_commit_at requires latest_commit_sha")

    reason = value["reason"]
    if not isinstance(reason, str) or not reason or len(reason) > 4096:
        raise ProjectStatusMutationError("reason must be a non-empty string of at most 4096 characters")

    open_issues = _parse_number_list(value["open_issues"], label="open_issues")
    open_prs = _parse_number_list(value["open_prs"], label="open_prs")

    normalized = {
        "change": True,
        "current_status": expected_status,
        "event": "project-status-observation",
        "latest_commit_at": latest_commit_at,
        "latest_commit_sha": latest_commit_sha,
        "observed_at": observed_at,
        "open_issues": list(open_issues),
        "open_prs": list(open_prs),
        "pending": True,
        "project": project_path,
        "proposed_status": desired_status,
        "reason": reason,
        "repository": repository,
    }
    canonical = _canonical_json_bytes(normalized)
    return ProjectStatusProposal(
        project_path=project_path,
        repository=repository,
        expected_status=expected_status,
        desired_status=desired_status,
        observed_at=observed_at or "",
        latest_commit_sha=latest_commit_sha,
        latest_commit_at=latest_commit_at,
        open_issues=open_issues,
        open_prs=open_prs,
        reason=reason,
        canonical_bytes=canonical,
        sha256=hashlib.sha256(canonical).hexdigest(),
    )


def _plain_scalar(raw: str) -> str:
    value = raw.strip()
    if " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _frontmatter_project(text: str) -> tuple[dict[str, str], int, list[str]]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise ProjectStatusMutationConflict("remote Project has no YAML frontmatter")
    closing: int | None = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            closing = index
            break
    if closing is None:
        raise ProjectStatusMutationConflict("remote Project frontmatter is unterminated")

    wanted = {"type", "status", "github_repo", "github_watch"}
    values: dict[str, str] = {}
    status_line = -1
    for index, line in enumerate(lines[1:closing], start=1):
        if not line or line[0].isspace() or ":" not in line:
            continue
        key, raw = line.split(":", 1)
        key = key.strip()
        if key not in wanted:
            continue
        if key in values:
            raise ProjectStatusMutationConflict(f"remote Project has duplicate {key} frontmatter")
        values[key] = _plain_scalar(raw.rstrip("\r\n"))
        if key == "status":
            status_line = index
    if status_line < 0:
        raise ProjectStatusMutationConflict("remote Project has no top-level status field")
    return values, status_line, lines


def _watch_enabled(value: str) -> bool:
    return value.strip().lower() in {"true", "yes", "1", "on"}


def _replace_status_line(line: str, desired_status: str) -> str:
    eol = ""
    body = line
    if body.endswith("\r\n"):
        body, eol = body[:-2], "\r\n"
    elif body.endswith("\n"):
        body, eol = body[:-1], "\n"
    match = re.fullmatch(r"(status:[ \t]*)(.*?)([ \t]+#.*)?", body)
    if match is None:
        raise ProjectStatusMutationConflict("remote Project status line has unsupported formatting")
    comment = match.group(3) or ""
    return f"{match.group(1)}{desired_status}{comment}{eol}"


def prepare_project_update(
    proposal: ProjectStatusProposal,
    remote_content: bytes,
) -> tuple[str, bytes]:
    if len(remote_content) > MAX_PROJECT_BYTES:
        raise ProjectStatusMutationError("remote Project exceeds maximum supported size")
    try:
        text = remote_content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProjectStatusMutationConflict("remote Project is not valid UTF-8") from exc
    values, status_line, lines = _frontmatter_project(text)
    if values.get("type") != "project":
        raise ProjectStatusMutationConflict("remote target is not type: project")
    if values.get("github_repo") != proposal.repository:
        raise ProjectStatusMutationConflict("remote github_repo no longer matches proposal repository")
    if not _watch_enabled(values.get("github_watch", "")):
        raise ProjectStatusMutationConflict("remote Project is no longer opted in to GitHub watch")

    current_status = values.get("status", "")
    if current_status not in VALID_PROJECT_STATUSES:
        raise ProjectStatusMutationConflict("remote Project status is not canonical")
    if current_status == "stopped":
        raise ProjectStatusMutationConflict("remote Project is stopped and remains human-controlled")
    if current_status == proposal.desired_status:
        return "already_desired", remote_content
    if current_status != proposal.expected_status:
        raise ProjectStatusMutationConflict(
            f"remote Project status changed from expected {proposal.expected_status!r} to {current_status!r}"
        )

    lines[status_line] = _replace_status_line(lines[status_line], proposal.desired_status)
    updated = "".join(lines).encode("utf-8")
    return "apply", updated


def _observe_project(
    *,
    base_url: str,
    target_path: str,
    username: str,
    password: str,
    timeout: float,
    transport: HTTPTransport | None,
) -> RemoteProject:
    try:
        target_url = build_target_url(base_url, target_path)
    except WebDAVCreateError as exc:
        raise ProjectStatusMutationError(str(exc)) from exc
    request = transport or _real_http_request
    response = request(
        method="GET",
        target_url=target_url,
        username=username,
        password=password,
        headers={"Accept": "text/markdown"},
        body=None,
        timeout=timeout,
        response_limit=MAX_PROJECT_BYTES,
    )
    if response.status == 404:
        raise ProjectStatusMutationConflict("remote Project does not exist")
    if response.status != 200:
        raise ProjectStatusMutationError(
            f"WebDAV GET returned unexpected HTTP status {response.status}"
        )
    return RemoteProject(content=response.body, etag=response.etag, status_code=response.status)


def _transport_result(
    proposal: ProjectStatusProposal,
    *,
    outcome: str,
    before: bytes,
    after: bytes,
) -> ProjectStatusTransportResult:
    return ProjectStatusTransportResult(
        proposal_sha256=proposal.sha256,
        project_path=proposal.project_path,
        repository=proposal.repository,
        expected_status=proposal.expected_status,
        desired_status=proposal.desired_status,
        outcome=outcome,
        before_content_sha256=hashlib.sha256(before).hexdigest(),
        after_content_sha256=hashlib.sha256(after).hexdigest(),
        completed_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    )


def apply_project_status(
    proposal: ProjectStatusProposal,
    *,
    base_url: str,
    username: str,
    password: str,
    timeout: float = 30.0,
    transport: HTTPTransport | None = None,
) -> ProjectStatusTransportResult:
    if not username:
        raise ProjectStatusMutationError("WebDAV username must not be empty")
    if not password:
        raise ProjectStatusMutationError("WebDAV password must not be empty")
    if timeout <= 0 or timeout > 600:
        raise ProjectStatusMutationError("timeout must be in (0, 600] seconds")

    before = _observe_project(
        base_url=base_url,
        target_path=proposal.project_path,
        username=username,
        password=password,
        timeout=timeout,
        transport=transport,
    )
    disposition, desired = prepare_project_update(proposal, before.content)
    if disposition == "already_desired":
        return _transport_result(
            proposal,
            outcome="already_desired",
            before=before.content,
            after=before.content,
        )

    etag = _strong_etag(before.etag)
    if etag is None:
        raise ProjectStatusMutationError("remote Project has no strong ETag required for CAS update")

    try:
        target_url = build_target_url(base_url, proposal.project_path)
    except WebDAVCreateError as exc:
        raise ProjectStatusMutationError(str(exc)) from exc
    request = transport or _real_http_request
    response_status: int | None = None
    ambiguous = False
    try:
        response = request(
            method="PUT",
            target_url=target_url,
            username=username,
            password=password,
            headers={
                "If-Match": etag,
                "Content-Type": "text/markdown; charset=utf-8",
            },
            body=desired,
            timeout=timeout,
            response_limit=64 * 1024,
        )
        response_status = response.status
        if response.status == 412:
            raise ProjectStatusMutationConflict(
                "Project status CAS precondition failed",
                reason_code="etag_cas_conflict",
                http_status=response.status,
            )
        if response.status in {401, 403}:
            raise ProjectStatusMutationRejected(
                f"WebDAV Project status PUT authority rejected with HTTP {response.status}",
                reason_code="authority_rejection",
                http_status=response.status,
            )
        if 400 <= response.status < 500:
            raise ProjectStatusMutationRejected(
                f"WebDAV Project status PUT was rejected with HTTP {response.status}",
                reason_code="http_client_rejection",
                http_status=response.status,
            )
        if response.status in _AMBIGUOUS_HTTP_STATUSES:
            ambiguous = True
        elif not 200 <= response.status < 300:
            raise ProjectStatusMutationRejected(
                f"WebDAV Project status PUT returned deterministic HTTP {response.status}",
                reason_code="http_response_rejection",
                http_status=response.status,
            )
    except PromotionTransportNetworkError:
        ambiguous = True

    after = _observe_project(
        base_url=base_url,
        target_path=proposal.project_path,
        username=username,
        password=password,
        timeout=timeout,
        transport=transport,
    )
    if after.content == desired:
        return _transport_result(
            proposal,
            outcome="recovered" if ambiguous else "applied",
            before=before.content,
            after=after.content,
        )

    try:
        after_disposition, _ = prepare_project_update(proposal, after.content)
    except ProjectStatusMutationConflict as exc:
        raise ProjectStatusMutationConflict(
            "remote Project diverged during CAS update"
        ) from exc
    if after_disposition == "already_desired":
        raise ProjectStatusMutationConflict(
            "remote status reached desired value but exact-byte verification failed"
        )
    if ambiguous and after.content == before.content:
        detail = "no canonical effect is observable"
        if response_status is not None:
            detail += f" after HTTP {response_status}"
        raise ProjectStatusMutationError(f"ambiguous CAS outcome: {detail}")
    raise ProjectStatusMutationConflict("remote Project bytes diverged during CAS update")


def _safe_regular_file(path: Path, *, label: str) -> bytes:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise ProjectStatusMutationError(f"{label} does not exist") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ProjectStatusMutationError(f"{label} must be a regular non-symlink file")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ProjectStatusMutationError(f"cannot read {label}") from exc


def persist_transport_result(path: Path, result: ProjectStatusTransportResult) -> bytes:
    data = result.to_json_bytes()
    if path.exists() or path.is_symlink():
        existing = _safe_regular_file(path, label="transport result")
        try:
            value = json.loads(existing)
        except json.JSONDecodeError as exc:
            raise ProjectStatusMutationError("existing transport result is invalid JSON") from exc
        if not isinstance(value, dict) or value.get("proposal_sha256") != result.proposal_sha256:
            raise ProjectStatusMutationConflict(
                "transport-result path already belongs to another proposal"
            )
        return existing
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = os.open(path, flags, 0o640)
    except FileExistsError:
        return persist_transport_result(path, result)
    except OSError as exc:
        raise ProjectStatusMutationError("cannot create transport result") from exc
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise ProjectStatusMutationError("short write while persisting transport result")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    return data


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="obsidian-github-project-status-apply",
        description="Apply one GitHub watcher Project status request through Sync authority CAS.",
    )
    parser.add_argument("--proposal", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--password-file", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=30.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        proposal = parse_watcher_proposal(_safe_regular_file(args.proposal, label="proposal"))
        if args.result.exists() or args.result.is_symlink():
            existing = _safe_regular_file(args.result, label="transport result")
            value = json.loads(existing)
            if not isinstance(value, dict) or value.get("proposal_sha256") != proposal.sha256:
                raise ProjectStatusMutationConflict(
                    "existing transport result does not match proposal"
                )
            sys.stdout.buffer.write(existing)
            return 0
        password = _read_password(args.password_file)
        with canonical_io_lock(args.state_root):
            result = apply_project_status(
                proposal,
                base_url=args.base_url,
                username=args.username,
                password=password,
                timeout=args.timeout,
            )
            data = persist_transport_result(args.result, result)
        sys.stdout.buffer.write(data)
        return 0
    except ProjectStatusMutationConflict as exc:
        print(f"conflict: {exc}", file=sys.stderr)
        return 3
    except (OSError, WebDAVCreateError, ProductionIOError, ProjectStatusMutationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
