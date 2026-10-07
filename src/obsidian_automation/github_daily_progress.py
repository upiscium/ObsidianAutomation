from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Mapping, Sequence
from urllib.parse import unquote, urlsplit

from .artifact_lifecycle import (
    ArtifactLifecycleError,
    _canonical_json_bytes,
    _decode_json_object,
    _read_exact_file,
    _store_immutable,
    sha256_bytes,
)
from .core_promotion_transport import (
    HTTPResponse,
    PromotionTransportNetworkError,
    _real_http_request,
    _strong_etag,
)
from .github_daily_summary import (
    CLAIM_KINDS,
    MAX_CLAIM_EVIDENCE_IDS,
    MAX_CLAIM_SUMMARY_BYTES,
    MAX_CLAIM_SUMMARY_CHARS,
    EvidenceBundle,
    SummaryClaim,
    load_evidence_bundle,
)
from .production_io import ProductionIOError, canonical_io_lock
from .webdav_create import (
    WebDAVCreateError,
    _read_password,
    build_target_url,
)


PROJECTION_VERSION = 1
TRANSPORT_RESULT_VERSION = 1
PROJECTION_SUFFIX = "github-daily-progress"
TRANSPORT_SUFFIX = "github-daily-progress.transport-result"
PROJECTION_STAGE = "github_daily_progress_projection"
TRANSPORT_STAGE = "github_daily_progress_transport"
PROJECT_PROGRESS_HEADING = "Project Progress"
DAILY_ROOT = "00-DailyNote"

MAX_SECTION_BYTES = 2 * 1024 * 1024
MAX_DAILY_BYTES = 4 * 1024 * 1024
MAX_GROUNDED_SUMMARY_BYTES = 4 * 1024 * 1024
MAX_REJECTED_CLAIMS = 4096
MAX_GROUNDING_OUTPUTS = 4096
MAX_REASON_CHARS = 2048
MAX_REASON_BYTES = 8 * 1024

_KIND_ORDER = {
    "decision": 0,
    "implementation": 1,
    "bugfix": 2,
    "issue_pr_progress": 3,
}
_KIND_LABEL = {
    "decision": "決定",
    "implementation": "実装",
    "bugfix": "バグ修正",
    "issue_pr_progress": "Issue/PR",
}

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_H1_RE = re.compile(r"^#[ \t]+(.+?)[ \t]*$")
_FENCE_RE = re.compile(
    r"^[ \t]{0,3}(?P<fence>(?:"
    + re.escape(chr(96))
    + r"){3,}|~{3,})(?P<rest>.*)$"
)
_AMBIGUOUS_HTTP_STATUSES = frozenset({500, 502, 503, 504})
_MARKDOWN_ESCAPE = frozenset("\\*_[]<>|~" + chr(96))


class DailyProgressError(RuntimeError):
    """Raised when Daily Project Progress cannot be rendered or written safely."""

    def __init__(
        self,
        message: str,
        *,
        reason_code: str = "daily_progress_error",
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.http_status = http_status


class DailyProgressConflict(DailyProgressError):
    """Raised when canonical Daily state conflicts with the projection."""

    def __init__(
        self,
        message: str,
        *,
        reason_code: str = "canonical_conflict",
        http_status: int | None = None,
    ) -> None:
        super().__init__(
            message,
            reason_code=reason_code,
            http_status=http_status,
        )


class DailyProgressRejected(DailyProgressError):
    """Raised after a trustworthy HTTP response rejects the write."""


class DailyProgressTargetMissing(DailyProgressError):
    """Retryable state: the requested Daily Note does not exist yet."""

    def __init__(self, message: str = "target Daily Note does not exist") -> None:
        super().__init__(message, reason_code="target_missing")


@dataclass(frozen=True)
class GroundedSummaryView:
    sha256: str
    evidence_bundle_sha256: str
    claims: tuple[SummaryClaim, ...]
    rejected_claim_ids: tuple[str, ...]
    grounding_output_sha256s: tuple[str, ...]


@dataclass(frozen=True)
class DailyProgressProjection:
    date: str
    target_path: str
    evidence_bundle_sha256: str
    grounded_summary_sha256: str
    section_body: str
    section_body_sha256: str
    canonical_bytes: bytes
    sha256: str


@dataclass(frozen=True)
class RemoteDaily:
    content: bytes
    etag: str | None
    status_code: int


@dataclass(frozen=True)
class DailyProgressTransportResult:
    projection_sha256: str
    date: str
    target_path: str
    evidence_bundle_sha256: str
    grounded_summary_sha256: str
    section_body_sha256: str
    outcome: str
    before_content_sha256: str
    after_content_sha256: str
    completed_at: str

    def to_json_bytes(self) -> bytes:
        return _canonical_json_bytes(
            {
                "record_version": TRANSPORT_RESULT_VERSION,
                "stage": TRANSPORT_STAGE,
                "projection_sha256": self.projection_sha256,
                "date": self.date,
                "target_path": self.target_path,
                "evidence_bundle_sha256": self.evidence_bundle_sha256,
                "grounded_summary_sha256": self.grounded_summary_sha256,
                "section_body_sha256": self.section_body_sha256,
                "outcome": self.outcome,
                "before_content_sha256": self.before_content_sha256,
                "after_content_sha256": self.after_content_sha256,
                "completed_at": self.completed_at,
            }
        )


HTTPTransport = Callable[..., HTTPResponse]


def _require_sha(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise DailyProgressError(f"{label} must be lowercase SHA-256")
    return value


def _plain_line(
    value: object,
    *,
    label: str,
    max_chars: int,
    max_bytes: int,
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\n" in value
        or "\r" in value
        or len(value) > max_chars
    ):
        raise DailyProgressError(f"{label} must be one trimmed line")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise DailyProgressError(f"{label} must be UTF-8") from exc
    if len(encoded) > max_bytes:
        raise DailyProgressError(f"{label} exceeds byte limit")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise DailyProgressError(f"{label} contains control characters")
    return value


def _canonical_date(value: object) -> str:
    if not isinstance(value, str):
        raise DailyProgressError("date must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise DailyProgressError("date must be YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise DailyProgressError("date must be canonical YYYY-MM-DD")
    return value


def daily_target_path(target_date: str) -> str:
    canonical = _canonical_date(target_date)
    parsed = date.fromisoformat(canonical)
    return (
        f"{DAILY_ROOT}/{parsed.year:04d}/{parsed.month:02d}/"
        f"{canonical}.md"
    )


def _claim_id(
    *,
    kind: str,
    repository: str,
    summary: str,
    evidence_ids: Sequence[str],
) -> str:
    return sha256_bytes(
        _canonical_json_bytes(
            {
                "kind": kind,
                "repository": repository,
                "summary": summary,
                "evidence_ids": list(evidence_ids),
            }
        )
    )


def parse_grounded_summary(
    data: bytes,
    *,
    bundle: EvidenceBundle,
) -> GroundedSummaryView:
    if len(data) > MAX_GROUNDED_SUMMARY_BYTES:
        raise DailyProgressError("grounded summary exceeds byte limit")
    value = _decode_json_object(data, label="GitHub Daily grounded summary")
    required = {
        "record_version",
        "stage",
        "evidence_bundle_sha256",
        "claims",
        "rejected_claims",
        "grounding_output_sha256s",
    }
    if (
        set(value) != required
        or value.get("record_version") != 1
        or value.get("stage") != "grounded_summary"
    ):
        raise DailyProgressError(
            "grounded summary properties do not match contract"
        )
    evidence_sha = _require_sha(
        value["evidence_bundle_sha256"],
        label="grounded summary evidence SHA",
    )
    if evidence_sha != bundle.sha256:
        raise DailyProgressError(
            "grounded summary does not bind the supplied evidence bundle"
        )
    raw_claims = value["claims"]
    raw_rejected = value["rejected_claims"]
    raw_grounding = value["grounding_output_sha256s"]
    if not isinstance(raw_claims, list):
        raise DailyProgressError("grounded summary claims are invalid")
    if (
        not isinstance(raw_rejected, list)
        or len(raw_rejected) > MAX_REJECTED_CLAIMS
    ):
        raise DailyProgressError("grounded summary rejected claims are invalid")
    if (
        not isinstance(raw_grounding, list)
        or len(raw_grounding) > MAX_GROUNDING_OUTPUTS
    ):
        raise DailyProgressError(
            "grounded summary grounding output list is invalid"
        )

    events_by_id = bundle.events_by_id
    allowed_ids = set(events_by_id)
    claims: list[SummaryClaim] = []
    seen_claims: set[str] = set()
    for raw in raw_claims:
        if not isinstance(raw, dict) or set(raw) != {
            "claim_id",
            "kind",
            "repository",
            "summary",
            "evidence_ids",
        }:
            raise DailyProgressError(
                "grounded summary claim properties do not match contract"
            )
        claim_id = _require_sha(raw["claim_id"], label="claim_id")
        kind = raw["kind"]
        if kind not in CLAIM_KINDS:
            raise DailyProgressError("grounded summary claim kind is invalid")
        repository = raw["repository"]
        if (
            not isinstance(repository, str)
            or _REPOSITORY_RE.fullmatch(repository) is None
        ):
            raise DailyProgressError(
                "grounded summary claim repository is invalid"
            )
        summary = _plain_line(
            raw["summary"],
            label="grounded summary claim",
            max_chars=MAX_CLAIM_SUMMARY_CHARS,
            max_bytes=MAX_CLAIM_SUMMARY_BYTES,
        )
        evidence_ids = raw["evidence_ids"]
        if (
            not isinstance(evidence_ids, list)
            or not 1 <= len(evidence_ids) <= MAX_CLAIM_EVIDENCE_IDS
            or not all(isinstance(item, str) for item in evidence_ids)
            or len(set(evidence_ids)) != len(evidence_ids)
        ):
            raise DailyProgressError(
                "grounded summary claim evidence IDs are invalid"
            )
        if any(item not in allowed_ids for item in evidence_ids):
            raise DailyProgressError(
                "grounded summary claim cites unknown evidence"
            )
        for evidence_id in evidence_ids:
            if events_by_id[evidence_id].get("repository") != repository:
                raise DailyProgressError(
                    "grounded summary claim repository/evidence mismatch"
                )
        expected_claim_id = _claim_id(
            kind=str(kind),
            repository=repository,
            summary=summary,
            evidence_ids=evidence_ids,
        )
        if claim_id != expected_claim_id:
            raise DailyProgressError(
                "grounded summary claim identity mismatch"
            )
        if claim_id in seen_claims:
            raise DailyProgressError(
                "grounded summary contains duplicate accepted claim"
            )
        seen_claims.add(claim_id)
        claims.append(
            SummaryClaim(
                claim_id=claim_id,
                kind=str(kind),
                repository=repository,
                summary=summary,
                evidence_ids=tuple(evidence_ids),
            )
        )

    rejected_ids: list[str] = []
    seen_rejected: set[str] = set()
    for raw in raw_rejected:
        if not isinstance(raw, dict) or set(raw) != {"claim_id", "reason"}:
            raise DailyProgressError(
                "grounded summary rejected claim properties are invalid"
            )
        claim_id = _require_sha(
            raw["claim_id"],
            label="rejected claim_id",
        )
        _plain_line(
            raw["reason"],
            label="rejected claim reason",
            max_chars=MAX_REASON_CHARS,
            max_bytes=MAX_REASON_BYTES,
        )
        if claim_id in seen_claims or claim_id in seen_rejected:
            raise DailyProgressError(
                "grounded summary rejected claim identity is duplicated"
            )
        seen_rejected.add(claim_id)
        rejected_ids.append(claim_id)

    if (raw_claims or raw_rejected) and not raw_grounding:
        raise DailyProgressError(
            "grounded summary with claims requires grounding output identity"
        )

    grounding: list[str] = []
    seen_grounding: set[str] = set()
    for item in raw_grounding:
        digest = _require_sha(item, label="grounding output SHA")
        if digest in seen_grounding:
            raise DailyProgressError(
                "grounded summary grounding outputs contain duplicates"
            )
        seen_grounding.add(digest)
        grounding.append(digest)

    return GroundedSummaryView(
        sha256=sha256_bytes(data),
        evidence_bundle_sha256=evidence_sha,
        claims=tuple(claims),
        rejected_claim_ids=tuple(rejected_ids),
        grounding_output_sha256s=tuple(grounding),
    )


def load_grounded_summary(
    path: Path,
    *,
    bundle: EvidenceBundle,
) -> GroundedSummaryView:
    data = _read_exact_file(path)
    summary = parse_grounded_summary(data, bundle=bundle)
    suffix = ".github-daily-grounded-summary.json"
    if not path.name.endswith(suffix):
        raise DailyProgressError("grounded summary filename is invalid")
    expected = path.name[: -len(suffix)]
    if expected != summary.sha256:
        raise DailyProgressError(
            "grounded summary filename does not match content SHA"
        )
    return summary


def _escape_markdown_text(value: str) -> str:
    escaped: list[str] = []
    for char in value:
        if char in _MARKDOWN_ESCAPE:
            escaped.append("\\")
        escaped.append(char)
    return "".join(escaped)


def _project_display_names(
    bundle: EvidenceBundle,
    repository: str,
) -> tuple[str, ...]:
    paths = sorted(
        {
            str(item["project_path"])
            for item in bundle.projects
            if item.get("repository") == repository
        },
        key=lambda value: (value.casefold(), value),
    )
    if not paths:
        raise DailyProgressError(
            f"repository has no Project binding: {repository}"
        )
    stems = [PurePosixPath(path).stem for path in paths]
    counts: dict[str, int] = {}
    for stem in stems:
        counts[stem.casefold()] = counts.get(stem.casefold(), 0) + 1
    labels = [
        (
            path[:-3]
            if counts[PurePosixPath(path).stem.casefold()] > 1
            else PurePosixPath(path).stem
        )
        for path in paths
    ]
    return tuple(labels)


def _validated_github_url(
    event: Mapping[str, object],
    *,
    repository: str,
) -> str:
    raw = event.get("url")
    if not isinstance(raw, str) or not raw:
        raise DailyProgressError("evidence event has no GitHub URL")
    parsed = urlsplit(raw)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
    ):
        raise DailyProgressError("evidence URL is not canonical GitHub HTTPS")
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    owner, name = repository.split("/", 1)
    if (
        len(parts) < 2
        or parts[0].casefold() != owner.casefold()
        or parts[1].casefold() != name.casefold()
    ):
        raise DailyProgressError(
            "evidence URL does not match claim repository"
        )
    return raw


def _source_label(event: Mapping[str, object]) -> str:
    entity_type = event.get("entity_type")
    number = event.get("number")
    if (
        isinstance(entity_type, str)
        and entity_type.startswith("pull_request")
        and type(number) is int
    ):
        return f"PR #{number}"
    if entity_type in {"issue", "issue_comment"} and type(number) is int:
        return f"#{number}"
    if entity_type == "commit":
        sha = event.get("sha")
        if not isinstance(sha, str) or len(sha) < 7:
            sha = event.get("source_id")
        if isinstance(sha, str) and len(sha) >= 7:
            return f"commit {sha[:7]}"
    return "GitHub"


def _claim_sources(
    claim: SummaryClaim,
    bundle: EvidenceBundle,
) -> tuple[tuple[str, str], ...]:
    events = bundle.events_by_id
    refs: set[tuple[str, str]] = set()
    for evidence_id in claim.evidence_ids:
        event = events[evidence_id]
        refs.add(
            (
                _source_label(event),
                _validated_github_url(
                    event,
                    repository=claim.repository,
                ),
            )
        )
    return tuple(
        sorted(refs, key=lambda item: (item[0].casefold(), item[1]))
    )


def render_section_body(
    bundle: EvidenceBundle,
    summary: GroundedSummaryView,
) -> str:
    if summary.evidence_bundle_sha256 != bundle.sha256:
        raise DailyProgressError(
            "grounded summary/evidence binding mismatch"
        )
    if not summary.claims:
        return "\n"

    by_repository: dict[str, list[SummaryClaim]] = {}
    for claim in summary.claims:
        _project_display_names(bundle, claim.repository)
        by_repository.setdefault(claim.repository, []).append(claim)

    lines: list[str] = []
    for repository in sorted(
        by_repository,
        key=lambda value: (value.casefold(), value),
    ):
        lines.append(f"## {repository}")
        projects = _project_display_names(bundle, repository)
        project_label = "Project" if len(projects) == 1 else "Projects"
        rendered_projects = ", ".join(
            _escape_markdown_text(item) for item in projects
        )
        lines.append(f"{project_label}: {rendered_projects}")
        lines.append("")
        claims = sorted(
            by_repository[repository],
            key=lambda item: (
                _KIND_ORDER[item.kind],
                item.summary.casefold(),
                item.summary,
                item.claim_id,
            ),
        )
        for claim in claims:
            summary_text = _escape_markdown_text(claim.summary)
            sources = _claim_sources(claim, bundle)
            links = ", ".join(
                f"[{label}]({url})" for label, url in sources
            )
            suffix = f" ({links})" if links else ""
            lines.append(
                f"- {_KIND_LABEL[claim.kind]}: {summary_text}{suffix}"
            )
        lines.append("")

    body = "\n".join(lines).rstrip() + "\n\n"
    if len(body.encode("utf-8")) > MAX_SECTION_BYTES:
        raise DailyProgressError(
            "rendered Project Progress section exceeds byte limit"
        )
    if any(_H1_RE.fullmatch(line) for line in body.splitlines()):
        raise DailyProgressError(
            "rendered Project Progress body unexpectedly contains H1"
        )
    return body


def make_projection(
    *,
    bundle: EvidenceBundle,
    summary: GroundedSummaryView,
) -> DailyProgressProjection:
    target_date = _canonical_date(bundle.date)
    target_path = daily_target_path(target_date)
    section_body = render_section_body(bundle, summary)
    section_sha = sha256_bytes(section_body.encode("utf-8"))
    payload = {
        "record_version": PROJECTION_VERSION,
        "stage": PROJECTION_STAGE,
        "date": target_date,
        "target_path": target_path,
        "evidence_bundle_sha256": bundle.sha256,
        "grounded_summary_sha256": summary.sha256,
        "section_body_sha256": section_sha,
        "section_body": section_body,
    }
    canonical = _canonical_json_bytes(payload)
    return DailyProgressProjection(
        date=target_date,
        target_path=target_path,
        evidence_bundle_sha256=bundle.sha256,
        grounded_summary_sha256=summary.sha256,
        section_body=section_body,
        section_body_sha256=section_sha,
        canonical_bytes=canonical,
        sha256=sha256_bytes(canonical),
    )


def parse_projection(data: bytes) -> DailyProgressProjection:
    value = _decode_json_object(data, label="Daily Progress projection")
    required = {
        "record_version",
        "stage",
        "date",
        "target_path",
        "evidence_bundle_sha256",
        "grounded_summary_sha256",
        "section_body_sha256",
        "section_body",
    }
    if (
        set(value) != required
        or value.get("record_version") != PROJECTION_VERSION
        or value.get("stage") != PROJECTION_STAGE
    ):
        raise DailyProgressError(
            "Daily Progress projection properties do not match contract"
        )
    target_date = _canonical_date(value["date"])
    target_path = value["target_path"]
    if (
        not isinstance(target_path, str)
        or target_path != daily_target_path(target_date)
    ):
        raise DailyProgressError(
            "Daily Progress target path/date binding is invalid"
        )
    evidence_sha = _require_sha(
        value["evidence_bundle_sha256"],
        label="projection evidence SHA",
    )
    summary_sha = _require_sha(
        value["grounded_summary_sha256"],
        label="projection grounded summary SHA",
    )
    section_sha = _require_sha(
        value["section_body_sha256"],
        label="projection section SHA",
    )
    section_body = value["section_body"]
    if not isinstance(section_body, str) or "\r" in section_body:
        raise DailyProgressError(
            "projection section body must be LF UTF-8 text"
        )
    encoded = section_body.encode("utf-8")
    if (
        not section_body.endswith("\n")
        or len(encoded) > MAX_SECTION_BYTES
        or sha256_bytes(encoded) != section_sha
    ):
        raise DailyProgressError(
            "projection section body does not match contract"
        )
    if any(_H1_RE.fullmatch(line) for line in section_body.splitlines()):
        raise DailyProgressError(
            "projection section body must not contain H1 headings"
        )

    canonical = _canonical_json_bytes(
        {
            "record_version": PROJECTION_VERSION,
            "stage": PROJECTION_STAGE,
            "date": target_date,
            "target_path": target_path,
            "evidence_bundle_sha256": evidence_sha,
            "grounded_summary_sha256": summary_sha,
            "section_body_sha256": section_sha,
            "section_body": section_body,
        }
    )
    return DailyProgressProjection(
        date=target_date,
        target_path=target_path,
        evidence_bundle_sha256=evidence_sha,
        grounded_summary_sha256=summary_sha,
        section_body=section_body,
        section_body_sha256=section_sha,
        canonical_bytes=canonical,
        sha256=sha256_bytes(canonical),
    )


def load_projection(path: Path) -> DailyProgressProjection:
    data = _read_exact_file(path)
    projection = parse_projection(data)
    suffix = f".{PROJECTION_SUFFIX}.json"
    if not path.name.endswith(suffix):
        raise DailyProgressError("Daily Progress projection filename is invalid")
    expected = path.name[: -len(suffix)]
    if expected != projection.sha256:
        raise DailyProgressError(
            "Daily Progress projection filename does not match content SHA"
        )
    return projection


def _require_directory(path: Path, *, label: str) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise DailyProgressError(f"{label} does not exist") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise DailyProgressError(
            f"{label} must be a non-symlink directory"
        )


def store_projection(
    output_dir: Path,
    projection: DailyProgressProjection,
) -> Path:
    _require_directory(output_dir, label="projection output directory")
    normalized = parse_projection(projection.canonical_bytes)
    if normalized != projection:
        raise DailyProgressError(
            "Daily Progress projection canonical round-trip mismatch"
        )
    path = output_dir / f"{projection.sha256}.{PROJECTION_SUFFIX}.json"
    return _store_immutable(path, projection.canonical_bytes)


def _frontmatter_type(lines: Sequence[str]) -> tuple[str, int]:
    if not lines or lines[0].strip() != "---":
        raise DailyProgressConflict("remote Daily has no YAML frontmatter")
    closing: int | None = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            closing = index
            break
    if closing is None:
        raise DailyProgressConflict(
            "remote Daily frontmatter is unterminated"
        )
    daily_type: str | None = None
    for line in lines[1:closing]:
        body = line.rstrip("\r\n")
        if not body or body[0].isspace() or ":" not in body:
            continue
        key, raw = body.split(":", 1)
        if key.strip() != "type":
            continue
        if daily_type is not None:
            raise DailyProgressConflict(
                "remote Daily has duplicate type frontmatter"
            )
        value = raw.strip()
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]
        daily_type = value
    if daily_type is None:
        raise DailyProgressConflict(
            "remote Daily has no top-level type frontmatter"
        )
    return daily_type, closing


def _line_body(line: str) -> str:
    if line.endswith("\r\n"):
        return line[:-2]
    if line.endswith("\n"):
        return line[:-1]
    return line


def _heading_positions(
    lines: Sequence[str],
    *,
    start: int,
) -> list[tuple[int, str]]:
    headings: list[tuple[int, str]] = []
    fence_char: str | None = None
    fence_length = 0
    for index in range(start, len(lines)):
        body = _line_body(lines[index])
        match = _FENCE_RE.match(body)
        if fence_char is not None:
            if match is not None:
                fence = match.group("fence")
                if (
                    fence[0] == fence_char
                    and len(fence) >= fence_length
                    and not match.group("rest").strip()
                ):
                    fence_char = None
                    fence_length = 0
            continue
        if match is not None:
            fence = match.group("fence")
            fence_char = fence[0]
            fence_length = len(fence)
            continue
        heading = _H1_RE.fullmatch(body)
        if heading is not None:
            headings.append((index, heading.group(1).strip()))
    return headings


def _heading_eol(line: str) -> str:
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith("\n"):
        return "\n"
    raise DailyProgressConflict(
        "Project Progress heading must terminate with a newline"
    )


def prepare_daily_update(
    projection: DailyProgressProjection,
    remote_content: bytes,
) -> tuple[str, bytes]:
    if len(remote_content) > MAX_DAILY_BYTES:
        raise DailyProgressError(
            "remote Daily exceeds maximum supported size"
        )
    try:
        text = remote_content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DailyProgressConflict(
            "remote Daily is not valid UTF-8"
        ) from exc
    if "\r" in text.replace("\r\n", ""):
        raise DailyProgressConflict("remote Daily contains lone CR")
    lines = text.splitlines(keepends=True)
    daily_type, frontmatter_end = _frontmatter_type(lines)
    if daily_type != "daily-review":
        raise DailyProgressConflict(
            "remote target is not type: daily-review"
        )

    headings = _heading_positions(
        lines,
        start=frontmatter_end + 1,
    )
    targets = [
        index
        for index, title in headings
        if title == PROJECT_PROGRESS_HEADING
    ]
    if len(targets) != 1:
        raise DailyProgressConflict(
            "remote Daily must contain exactly one Project Progress H1"
        )
    heading_index = targets[0]
    next_h1 = next(
        (
            index
            for index, _title in headings
            if index > heading_index
        ),
        len(lines),
    )
    eol = _heading_eol(lines[heading_index])
    body = projection.section_body
    if eol == "\r\n":
        body = body.replace("\n", "\r\n")
    inserted = body.splitlines(keepends=True)
    desired_text = "".join(
        [
            *lines[: heading_index + 1],
            *inserted,
            *lines[next_h1:],
        ]
    )
    desired = desired_text.encode("utf-8")
    if len(desired) > MAX_DAILY_BYTES:
        raise DailyProgressError(
            "updated Daily exceeds maximum supported size"
        )
    if desired == remote_content:
        return "already_desired", remote_content
    return "apply", desired


def _observe_daily(
    *,
    projection: DailyProgressProjection,
    base_url: str,
    username: str,
    password: str,
    timeout: float,
    transport: HTTPTransport | None,
) -> RemoteDaily:
    try:
        target_url = build_target_url(
            base_url,
            projection.target_path,
        )
    except WebDAVCreateError as exc:
        raise DailyProgressError(str(exc)) from exc
    request = transport or _real_http_request
    try:
        response = request(
            method="GET",
            target_url=target_url,
            username=username,
            password=password,
            headers={"Accept": "text/markdown"},
            body=None,
            timeout=timeout,
            response_limit=MAX_DAILY_BYTES,
        )
    except PromotionTransportNetworkError as exc:
        raise DailyProgressError(
            "Daily observation network failure",
            reason_code="transport_network",
        ) from exc
    if response.status == 404:
        raise DailyProgressTargetMissing()
    if response.status in {401, 403}:
        raise DailyProgressRejected(
            f"Daily observation authority rejected with HTTP {response.status}",
            reason_code="authority_rejection",
            http_status=response.status,
        )
    if 400 <= response.status < 500:
        raise DailyProgressRejected(
            f"Daily observation rejected with HTTP {response.status}",
            reason_code="http_client_rejection",
            http_status=response.status,
        )
    if response.status != 200:
        raise DailyProgressError(
            f"Daily observation returned HTTP {response.status}",
            reason_code="http_get_failure",
            http_status=response.status,
        )
    return RemoteDaily(
        content=response.body,
        etag=response.etag,
        status_code=response.status,
    )


def _result(
    projection: DailyProgressProjection,
    *,
    outcome: str,
    before: bytes,
    after: bytes,
) -> DailyProgressTransportResult:
    return DailyProgressTransportResult(
        projection_sha256=projection.sha256,
        date=projection.date,
        target_path=projection.target_path,
        evidence_bundle_sha256=projection.evidence_bundle_sha256,
        grounded_summary_sha256=projection.grounded_summary_sha256,
        section_body_sha256=projection.section_body_sha256,
        outcome=outcome,
        before_content_sha256=hashlib.sha256(before).hexdigest(),
        after_content_sha256=hashlib.sha256(after).hexdigest(),
        completed_at=datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
    )


def apply_projection(
    projection: DailyProgressProjection,
    *,
    base_url: str,
    username: str,
    password: str,
    timeout: float = 30.0,
    transport: HTTPTransport | None = None,
) -> DailyProgressTransportResult:
    if not username:
        raise DailyProgressError("WebDAV username must not be empty")
    if not password:
        raise DailyProgressError("WebDAV password must not be empty")
    if timeout <= 0 or timeout > 600:
        raise DailyProgressError(
            "timeout must be in (0, 600] seconds"
        )

    before = _observe_daily(
        projection=projection,
        base_url=base_url,
        username=username,
        password=password,
        timeout=timeout,
        transport=transport,
    )
    disposition, desired = prepare_daily_update(
        projection,
        before.content,
    )
    if disposition == "already_desired":
        return _result(
            projection,
            outcome="already_desired",
            before=before.content,
            after=before.content,
        )

    etag = _strong_etag(before.etag)
    if etag is None:
        raise DailyProgressError(
            "remote Daily has no strong ETag required for CAS update",
            reason_code="strong_etag_required",
        )
    try:
        target_url = build_target_url(
            base_url,
            projection.target_path,
        )
    except WebDAVCreateError as exc:
        raise DailyProgressError(str(exc)) from exc

    request = transport or _real_http_request
    ambiguous = False
    response_status: int | None = None
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
            raise DailyProgressConflict(
                "Daily CAS precondition failed",
                reason_code="etag_cas_conflict",
                http_status=response.status,
            )
        if response.status in {401, 403}:
            raise DailyProgressRejected(
                f"Daily PUT authority rejected with HTTP {response.status}",
                reason_code="authority_rejection",
                http_status=response.status,
            )
        if 400 <= response.status < 500:
            raise DailyProgressRejected(
                f"Daily PUT rejected with HTTP {response.status}",
                reason_code="http_client_rejection",
                http_status=response.status,
            )
        if response.status in _AMBIGUOUS_HTTP_STATUSES:
            ambiguous = True
        elif not 200 <= response.status < 300:
            raise DailyProgressRejected(
                f"Daily PUT returned deterministic HTTP {response.status}",
                reason_code="http_response_rejection",
                http_status=response.status,
            )
    except PromotionTransportNetworkError:
        ambiguous = True

    after = _observe_daily(
        projection=projection,
        base_url=base_url,
        username=username,
        password=password,
        timeout=timeout,
        transport=transport,
    )
    if after.content == desired:
        return _result(
            projection,
            outcome="recovered" if ambiguous else "applied",
            before=before.content,
            after=after.content,
        )
    if ambiguous and after.content == before.content:
        detail = "no canonical effect is observable"
        if response_status is not None:
            detail += f" after HTTP {response_status}"
        raise DailyProgressError(
            f"ambiguous Daily CAS outcome: {detail}",
            reason_code="ambiguous_transport",
            http_status=response_status,
        )
    raise DailyProgressConflict(
        "remote Daily bytes diverged during CAS update"
    )


def _safe_regular_file(path: Path, *, label: str) -> bytes:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise DailyProgressError(f"{label} does not exist") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise DailyProgressError(
            f"{label} must be a regular non-symlink file"
        )
    try:
        return path.read_bytes()
    except OSError as exc:
        raise DailyProgressError(f"cannot read {label}") from exc


def persist_transport_result(
    path: Path,
    result: DailyProgressTransportResult,
) -> bytes:
    data = result.to_json_bytes()
    if path.exists() or path.is_symlink():
        existing = _safe_regular_file(
            path,
            label="Daily Progress transport result",
        )
        try:
            value = json.loads(existing)
        except json.JSONDecodeError as exc:
            raise DailyProgressError(
                "existing Daily Progress transport result is invalid JSON"
            ) from exc
        if (
            not isinstance(value, dict)
            or value.get("projection_sha256")
            != result.projection_sha256
        ):
            raise DailyProgressConflict(
                "transport result path belongs to another projection"
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
        raise DailyProgressError(
            "cannot create Daily Progress transport result"
        ) from exc
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise DailyProgressError(
                    "short write while persisting transport result"
                )
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    return data


def render_main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="obsidian-github-daily-progress-render"
    )
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(
        list(argv) if argv is not None else None
    )
    try:
        bundle = load_evidence_bundle(args.evidence)
        summary = load_grounded_summary(
            args.summary,
            bundle=bundle,
        )
        projection = make_projection(
            bundle=bundle,
            summary=summary,
        )
        path = store_projection(args.output_dir, projection)
    except (
        ArtifactLifecycleError,
        DailyProgressError,
        OSError,
    ) as exc:
        print(f"github-daily-progress-render: {exc}", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "event": "github-daily-progress-projected",
                "date": projection.date,
                "target_path": projection.target_path,
                "projection_sha256": projection.sha256,
                "projection_path": str(path),
                "section_body_sha256": projection.section_body_sha256,
                "claims": len(summary.claims),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def _error_event(exc: DailyProgressError) -> str:
    return json.dumps(
        {
            "event": "github-daily-progress-error",
            "reason_code": exc.reason_code,
            "http_status": exc.http_status,
            "message": str(exc),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def apply_main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="obsidian-github-daily-progress-apply"
    )
    parser.add_argument("--projection", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--password-file", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(
        list(argv) if argv is not None else None
    )

    try:
        projection = load_projection(args.projection)
        password = _read_password(args.password_file)
        with canonical_io_lock(args.state_root):
            result = apply_projection(
                projection,
                base_url=args.base_url,
                username=args.username,
                password=password,
                timeout=args.timeout,
            )
            persisted = persist_transport_result(
                args.result,
                result,
            )
    except DailyProgressTargetMissing as exc:
        print(_error_event(exc), file=sys.stderr)
        return 4
    except DailyProgressConflict as exc:
        print(_error_event(exc), file=sys.stderr)
        return 3
    except DailyProgressError as exc:
        print(_error_event(exc), file=sys.stderr)
        return 2
    except (
        ArtifactLifecycleError,
        ProductionIOError,
        WebDAVCreateError,
        OSError,
    ) as exc:
        wrapped = DailyProgressError(str(exc))
        print(_error_event(wrapped), file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "event": "github-daily-progress-applied",
                "projection_sha256": result.projection_sha256,
                "date": result.date,
                "target_path": result.target_path,
                "outcome": result.outcome,
                "transport_result_sha256": sha256_bytes(persisted),
                "result_path": str(args.result),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(render_main())
