from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Callable, Mapping

from .core_promotion_transport import (
    HTTPResponse,
    PromotionTransportNetworkError,
    _real_http_request,
    _strong_etag,
)
from .webdav_create import WebDAVCreateError, build_target_url


OVERVIEW_RECORD_VERSION = 1
MAX_NOTE_BYTES = 4 * 1024 * 1024
NOTES_HEADING = "## Notes"
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_WATCH_TRUE = frozenset({"true", "yes", "1", "on"})


class ProjectOverviewError(RuntimeError):
    """Raised when a Project overview cannot be processed safely."""


class ProjectOverviewConflict(ProjectOverviewError):
    """Raised when canonical state conflicts with the desired overview."""


@dataclass(frozen=True, order=True)
class BoundIssueRef:
    repository: str
    number: int


@dataclass(frozen=True, order=True)
class OverviewItem:
    number: int
    title: str
    draft: bool = False
    bound_issues: tuple[BoundIssueRef, ...] = ()


@dataclass(frozen=True)
class ProjectOverviewProposal:
    project_path: str
    repository: str
    issues: tuple[OverviewItem, ...]
    pull_requests: tuple[OverviewItem, ...]
    canonical_bytes: bytes
    sha256: str

    @property
    def status_path(self) -> str:
        return str(PurePosixPath(self.project_path).parent / "Status.md")

    @property
    def project_key(self) -> str:
        return hashlib.sha256(self.project_path.encode("utf-8")).hexdigest()

    @property
    def project_reference(self) -> str:
        path = PurePosixPath(self.project_path)
        target = path.with_suffix("").as_posix()
        return f"[[{target}|{path.stem}]]"


@dataclass(frozen=True)
class ProjectOverviewTransportResult:
    proposal_sha256: str
    project_path: str
    status_path: str
    repository: str
    outcome: str
    before_content_sha256: str | None
    after_content_sha256: str
    completed_at: str

    def to_json_bytes(self) -> bytes:
        return _canonical_json_bytes(
            {
                "record_version": OVERVIEW_RECORD_VERSION,
                "stage": "github_project_overview_transport",
                "proposal_sha256": self.proposal_sha256,
                "project_path": self.project_path,
                "status_path": self.status_path,
                "repository": self.repository,
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


def _safe_project_path(value: object) -> str:
    if not isinstance(value, str) or not value.endswith(".md"):
        raise ProjectOverviewError("project must be a Markdown path")
    if value.startswith("/") or "\\" in value or "\x00" in value:
        raise ProjectOverviewError("project must be a relative POSIX path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ProjectOverviewError("project contains an unsafe path component")
    if len(parts) < 2 or parts[0] != "10-Project":
        raise ProjectOverviewError("project must be below 10-Project")
    if PurePosixPath(value).name == "Status.md":
        raise ProjectOverviewError("Project note itself cannot be Status.md")
    return value


def _parse_bound_issue(value: object) -> BoundIssueRef:
    if not isinstance(value, dict) or set(value) != {"repository", "number"}:
        raise ProjectOverviewError("bound issue properties do not match contract")
    repository = value.get("repository")
    number = value.get("number")
    if not isinstance(repository, str) or _REPOSITORY_RE.fullmatch(repository) is None:
        raise ProjectOverviewError("bound issue repository must be owner/name")
    if type(number) is not int or number < 1:
        raise ProjectOverviewError("bound issue number must be a positive integer")
    return BoundIssueRef(repository=repository, number=number)


def _parse_item(value: object, *, draft_allowed: bool) -> OverviewItem:
    if not isinstance(value, dict):
        raise ProjectOverviewError("overview item must be an object")
    if draft_allowed:
        allowed = (
            {"number", "title", "draft"},
            {"number", "title", "draft", "bound_issues"},
        )
        if set(value) not in allowed:
            raise ProjectOverviewError("overview item properties do not match contract")
    elif set(value) != {"number", "title"}:
        raise ProjectOverviewError("overview item properties do not match contract")
    number = value.get("number")
    title = value.get("title")
    if type(number) is not int or number < 1:
        raise ProjectOverviewError("overview item number must be a positive integer")
    if not isinstance(title, str) or not title.strip() or len(title) > 1024:
        raise ProjectOverviewError("overview item title must be a non-empty string")
    draft = value.get("draft", False)
    if type(draft) is not bool:
        raise ProjectOverviewError("pull request draft must be boolean")

    raw_bound_issues = value.get("bound_issues", [])
    if not isinstance(raw_bound_issues, list):
        raise ProjectOverviewError("pull request bound_issues must be an array")
    bound_issues = tuple(
        sorted(
            (_parse_bound_issue(item) for item in raw_bound_issues),
            key=lambda item: (item.repository.casefold(), item.number),
        )
    )
    identities = {(item.repository.casefold(), item.number) for item in bound_issues}
    if len(identities) != len(bound_issues):
        raise ProjectOverviewError("duplicate bound issue reference")
    return OverviewItem(
        number=number,
        title=title.strip(),
        draft=draft,
        bound_issues=bound_issues,
    )


def make_overview_proposal(
    *,
    project_path: str,
    repository: str,
    issues: tuple[OverviewItem, ...] | list[OverviewItem],
    pull_requests: tuple[OverviewItem, ...] | list[OverviewItem],
) -> ProjectOverviewProposal:
    project_path = _safe_project_path(project_path)
    if not isinstance(repository, str) or _REPOSITORY_RE.fullmatch(repository) is None:
        raise ProjectOverviewError("repository must be owner/name")
    issue_items = tuple(sorted(issues, key=lambda item: item.number))
    pr_items = tuple(sorted(pull_requests, key=lambda item: item.number))
    if len({item.number for item in issue_items}) != len(issue_items):
        raise ProjectOverviewError("duplicate issue number")
    if len({item.number for item in pr_items}) != len(pr_items):
        raise ProjectOverviewError("duplicate pull request number")
    normalized: dict[str, object] = {
        "event": "project-overview-desired",
        "project": project_path,
        "repository": repository,
        "issues": [
            {"number": item.number, "title": item.title}
            for item in issue_items
        ],
        "pull_requests": [
            {
                "number": item.number,
                "title": item.title,
                "draft": item.draft,
                "bound_issues": [
                    {"repository": bound.repository, "number": bound.number}
                    for bound in item.bound_issues
                ],
            }
            for item in pr_items
        ],
    }
    canonical = _canonical_json_bytes(normalized)
    return ProjectOverviewProposal(
        project_path=project_path,
        repository=repository,
        issues=issue_items,
        pull_requests=pr_items,
        canonical_bytes=canonical,
        sha256=hashlib.sha256(canonical).hexdigest(),
    )


def parse_overview_proposal(data: bytes) -> ProjectOverviewProposal:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProjectOverviewError("overview proposal must be UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ProjectOverviewError("overview proposal must be an object")
    required = {"event", "project", "repository", "issues", "pull_requests"}
    if set(value) != required or value.get("event") != "project-overview-desired":
        raise ProjectOverviewError("overview proposal properties do not match contract")
    raw_issues = value.get("issues")
    raw_prs = value.get("pull_requests")
    if not isinstance(raw_issues, list) or not isinstance(raw_prs, list):
        raise ProjectOverviewError("overview issue and pull request lists must be arrays")
    return make_overview_proposal(
        project_path=_safe_project_path(value.get("project")),
        repository=str(value.get("repository") or ""),
        issues=[_parse_item(item, draft_allowed=False) for item in raw_issues],
        pull_requests=[_parse_item(item, draft_allowed=True) for item in raw_prs],
    )


def _frontmatter_scalars(text: str) -> dict[str, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    values: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return values
        if not line or line[0].isspace() or ":" not in line:
            continue
        key, raw = line.split(":", 1)
        key = key.strip()
        if key in values:
            raise ProjectOverviewConflict(f"remote note has duplicate {key} frontmatter")
        scalar = raw.strip()
        if " #" in scalar:
            scalar = scalar.split(" #", 1)[0].rstrip()
        if len(scalar) >= 2 and scalar[0] == scalar[-1] and scalar[0] in {"'", '"'}:
            scalar = scalar[1:-1]
        values[key] = scalar
    return {}


def _frontmatter_end(text: str) -> int:
    if not text.startswith("---"):
        raise ProjectOverviewConflict("existing Status.md has no frontmatter")
    match = re.search(r"(?m)^---\s*$", text[3:])
    if match is None:
        raise ProjectOverviewConflict("existing Status.md frontmatter is not terminated")
    end = 3 + match.end()
    if end < len(text) and text[end] == "\r":
        end += 1
    if end < len(text) and text[end] == "\n":
        end += 1
    return end


def _verify_project_binding(
    content: bytes,
    proposal: ProjectOverviewProposal,
) -> str:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProjectOverviewConflict("remote Project is not valid UTF-8") from exc
    values = _frontmatter_scalars(text)
    if values.get("type") != "project":
        raise ProjectOverviewConflict("remote target is not type: project")
    if values.get("github_repo") != proposal.repository:
        raise ProjectOverviewConflict("remote github_repo no longer matches overview repository")
    if values.get("github_watch", "").strip().lower() not in _WATCH_TRUE:
        raise ProjectOverviewConflict("remote Project is no longer opted in to GitHub watch")
    return values.get("workspace", "").strip()


def _yaml_quote(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", "\\r")
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )
    return '"' + escaped + '"'


def _render_issue_frontmatter(
    proposal: ProjectOverviewProposal,
) -> list[str]:
    if not proposal.issues:
        return ["github_issues: []"]

    lines = ["github_issues:"]
    for item in proposal.issues:
        url = f"https://github.com/{proposal.repository}/issues/{item.number}"
        lines.extend(
            [
                f"  - number: {item.number}",
                f"    title: {_yaml_quote(item.title)}",
                f"    url: {_yaml_quote(url)}",
            ]
        )
    return lines


def _render_pull_request_frontmatter(
    proposal: ProjectOverviewProposal,
) -> list[str]:
    if not proposal.pull_requests:
        return ["github_pull_requests: []"]

    lines = ["github_pull_requests:"]
    for item in proposal.pull_requests:
        status = "draft" if item.draft else "ready"
        url = f"https://github.com/{proposal.repository}/pull/{item.number}"
        lines.extend(
            [
                f"  - number: {item.number}",
                f"    title: {_yaml_quote(item.title)}",
                f"    url: {_yaml_quote(url)}",
                f"    status: {status}",
            ]
        )
        if item.bound_issues:
            lines.append("    bound_issues:")
            for bound in item.bound_issues:
                issue_url = f"https://github.com/{bound.repository}/issues/{bound.number}"
                lines.extend(
                    [
                        f"      - repository: {_yaml_quote(bound.repository)}",
                        f"        number: {bound.number}",
                        f"        url: {_yaml_quote(issue_url)}",
                    ]
                )
        else:
            lines.append("    bound_issues: []")
    return lines


def _render_project_note_frontmatter(
    proposal: ProjectOverviewProposal,
    *,
    workspace: str,
    eol: str,
) -> str:
    workspace_line = f"workspace: {_yaml_quote(workspace)}" if workspace else "workspace:"
    lines = [
        "---",
        "type: project-note",
        f"project: {_yaml_quote(proposal.project_reference)}",
        workspace_line,
        "category: list",
        "lifecycle: active",
        "aliases: []",
        "tags: []",
        f"github_repo: {proposal.repository}",
        "github_status_managed: true",
    ]
    lines.extend(_render_issue_frontmatter(proposal))
    lines.extend(_render_pull_request_frontmatter(proposal))
    lines.extend(["---", ""])
    return eol.join(lines)


def _notes_body(body: str) -> str:
    matches = list(
        re.finditer(r"(?m)^## Notes[ \t]*\r?$", body)
    )
    if len(matches) != 1:
        raise ProjectOverviewConflict(
            "existing Status.md must contain exactly one Notes heading"
        )
    return body[matches[0].start():]


def _validate_status_frontmatter(
    text: str,
    proposal: ProjectOverviewProposal,
) -> None:
    values = _frontmatter_scalars(text)
    note_type = values.get("type")
    if note_type == "github-status":
        if values.get("github_repo") != proposal.repository:
            raise ProjectOverviewConflict("legacy Status.md github_repo does not match proposal")
        return
    if note_type == "project-note":
        if values.get("github_repo") != proposal.repository:
            raise ProjectOverviewConflict("Status.md github_repo does not match proposal")
        if values.get("github_status_managed", "").lower() not in _WATCH_TRUE:
            raise ProjectOverviewConflict("existing project-note Status.md is not automation-managed")
        return
    raise ProjectOverviewConflict(
        "existing Status.md is neither a legacy generated note nor a managed project-note"
    )


def render_status_note(
    proposal: ProjectOverviewProposal,
    existing: bytes | None,
    *,
    workspace: str = "",
) -> bytes:
    if existing is None:
        frontmatter = _render_project_note_frontmatter(
            proposal,
            workspace=workspace,
            eol="\n",
        )
        return (
            frontmatter
            + NOTES_HEADING
            + "\n\n"
        ).encode("utf-8")

    if len(existing) > MAX_NOTE_BYTES:
        raise ProjectOverviewError("existing Status.md exceeds maximum supported size")
    try:
        text = existing.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProjectOverviewConflict("existing Status.md is not valid UTF-8") from exc

    _validate_status_frontmatter(text, proposal)
    eol = "\r\n" if "\r\n" in text else "\n"
    fm_end = _frontmatter_end(text)
    frontmatter = _render_project_note_frontmatter(
        proposal,
        workspace=workspace,
        eol=eol,
    )
    notes = _notes_body(text[fm_end:])
    return (frontmatter + notes).encode("utf-8")

def _observe(
    *,
    base_url: str,
    path: str,
    username: str,
    password: str,
    timeout: float,
    transport: HTTPTransport | None,
) -> HTTPResponse:
    try:
        target_url = build_target_url(base_url, path)
    except WebDAVCreateError as exc:
        raise ProjectOverviewError(str(exc)) from exc
    request = transport or _real_http_request
    return request(
        method="GET",
        target_url=target_url,
        username=username,
        password=password,
        headers={"Accept": "text/markdown"},
        body=None,
        timeout=timeout,
        response_limit=MAX_NOTE_BYTES,
    )


def _result(
    proposal: ProjectOverviewProposal,
    *,
    outcome: str,
    before: bytes | None,
    after: bytes,
) -> ProjectOverviewTransportResult:
    return ProjectOverviewTransportResult(
        proposal_sha256=proposal.sha256,
        project_path=proposal.project_path,
        status_path=proposal.status_path,
        repository=proposal.repository,
        outcome=outcome,
        before_content_sha256=hashlib.sha256(before).hexdigest() if before is not None else None,
        after_content_sha256=hashlib.sha256(after).hexdigest(),
        completed_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    )


def apply_project_overview(
    proposal: ProjectOverviewProposal,
    *,
    base_url: str,
    username: str,
    password: str,
    timeout: float = 30.0,
    transport: HTTPTransport | None = None,
) -> ProjectOverviewTransportResult:
    if not username or not password:
        raise ProjectOverviewError("WebDAV credentials must not be empty")
    if timeout <= 0 or timeout > 600:
        raise ProjectOverviewError("timeout must be in (0, 600] seconds")

    project = _observe(
        base_url=base_url,
        path=proposal.project_path,
        username=username,
        password=password,
        timeout=timeout,
        transport=transport,
    )
    if project.status == 404:
        raise ProjectOverviewConflict("remote Project does not exist")
    if project.status != 200:
        raise ProjectOverviewError(f"WebDAV Project GET returned HTTP {project.status}")
    workspace = _verify_project_binding(project.body, proposal)

    current = _observe(
        base_url=base_url,
        path=proposal.status_path,
        username=username,
        password=password,
        timeout=timeout,
        transport=transport,
    )
    if current.status not in {200, 404}:
        raise ProjectOverviewError(f"WebDAV Status.md GET returned HTTP {current.status}")
    before = current.body if current.status == 200 else None
    desired = render_status_note(proposal, before, workspace=workspace)
    if before is not None and before == desired:
        return _result(proposal, outcome="already_desired", before=before, after=before)

    try:
        target_url = build_target_url(base_url, proposal.status_path)
    except WebDAVCreateError as exc:
        raise ProjectOverviewError(str(exc)) from exc
    request = transport or _real_http_request
    headers = {"Content-Type": "text/markdown; charset=utf-8"}
    if before is None:
        headers["If-None-Match"] = "*"
        expected_outcome = "created"
    else:
        etag = _strong_etag(current.etag)
        if etag is None:
            raise ProjectOverviewError("existing Status.md has no strong ETag required for CAS update")
        headers["If-Match"] = etag
        expected_outcome = "updated"

    response_status: int | None = None
    network_error = False
    try:
        response = request(
            method="PUT",
            target_url=target_url,
            username=username,
            password=password,
            headers=headers,
            body=desired,
            timeout=timeout,
            response_limit=64 * 1024,
        )
        response_status = response.status
    except PromotionTransportNetworkError:
        network_error = True

    after = _observe(
        base_url=base_url,
        path=proposal.status_path,
        username=username,
        password=password,
        timeout=timeout,
        transport=transport,
    )
    if after.status == 200 and after.body == desired:
        outcome = (
            "recovered"
            if network_error or not (response_status and 200 <= response_status < 300)
            else expected_outcome
        )
        return _result(proposal, outcome=outcome, before=before, after=after.body)
    if response_status == 412:
        raise ProjectOverviewConflict("Status.md CAS precondition failed")
    if response_status is not None and not 200 <= response_status < 300:
        raise ProjectOverviewError(f"WebDAV Status.md PUT returned HTTP {response_status}")
    if network_error:
        raise ProjectOverviewError("ambiguous Status.md PUT outcome with no desired canonical effect")
    raise ProjectOverviewConflict("Status.md bytes diverged during CAS update")
