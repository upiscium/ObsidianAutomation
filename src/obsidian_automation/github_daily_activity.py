from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import urllib.parse
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from . import github_project_watcher as watcher


RECORD_VERSION = 1
CANONICAL_TIMEZONE = "Asia/Tokyo"

TITLE_MAX_BYTES = 2 * 1024
BODY_MAX_BYTES = 16 * 1024
COMMENT_MAX_BYTES = 8 * 1024
REVIEW_MAX_BYTES = 8 * 1024
COMMIT_MESSAGE_MAX_BYTES = 4 * 1024

_EVENT_KINDS = frozenset(
    {
        "issue_created",
        "issue_snapshot",
        "issue_closed",
        "issue_reopened",
        "issue_comment",
        "pull_request_created",
        "pull_request_snapshot",
        "pull_request_closed",
        "pull_request_reopened",
        "pull_request_merged",
        "pull_request_ready_for_review",
        "pull_request_converted_to_draft",
        "pull_request_comment",
        "pull_request_review",
        "pull_request_review_comment",
        "pull_request_commit",
        "default_branch_commit",
    }
)
_ENTITY_TYPES = frozenset(
    {
        "issue",
        "issue_comment",
        "pull_request",
        "pull_request_comment",
        "pull_request_review",
        "pull_request_review_comment",
        "commit",
    }
)
_ISSUE_EVENT_KIND = {
    "closed": ("issue_closed", "pull_request_closed"),
    "reopened": ("issue_reopened", "pull_request_reopened"),
    "merged": (None, "pull_request_merged"),
    "ready_for_review": (None, "pull_request_ready_for_review"),
    "convert_to_draft": (None, "pull_request_converted_to_draft"),
}


class GitHubDailyActivityError(RuntimeError):
    """Raised when one Daily GitHub evidence bundle cannot be built safely."""


@dataclass(frozen=True)
class TextExcerpt:
    text: str
    truncated: bool
    original_bytes: int
    included_bytes: int

    def to_json(self) -> dict[str, object]:
        return {
            "text": self.text,
            "truncated": self.truncated,
            "original_bytes": self.original_bytes,
            "included_bytes": self.included_bytes,
        }


@dataclass(frozen=True)
class EvidenceEvent:
    evidence_id: str
    kind: str
    repository: str
    occurred_at: str
    url: str
    actor: str | None
    entity_type: str
    number: int | None
    source_id: str
    sha: str | None
    state: str | None
    draft: bool | None
    title: TextExcerpt | None
    body: TextExcerpt | None
    message: TextExcerpt | None

    def to_json(self) -> dict[str, object]:
        return {
            "evidence_id": self.evidence_id,
            "kind": self.kind,
            "repository": self.repository,
            "occurred_at": self.occurred_at,
            "url": self.url,
            "actor": self.actor,
            "entity_type": self.entity_type,
            "number": self.number,
            "source_id": self.source_id,
            "sha": self.sha,
            "state": self.state,
            "draft": self.draft,
            "title": self.title.to_json() if self.title is not None else None,
            "body": self.body.to_json() if self.body is not None else None,
            "message": self.message.to_json() if self.message is not None else None,
        }


@dataclass(frozen=True)
class ProjectBinding:
    project_path: str
    repository: str

    def to_json(self) -> dict[str, object]:
        return {
            "project_path": self.project_path,
            "repository": self.repository,
        }


@dataclass(frozen=True)
class DailyEvidenceBundle:
    target_date: str
    window_start: str
    window_end: str
    projects: tuple[ProjectBinding, ...]
    repositories: tuple[str, ...]
    events: tuple[EvidenceEvent, ...]
    canonical_bytes: bytes
    sha256: str


def _canonical_json_bytes(payload: Mapping[str, object]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _timestamp(value: object, *, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise GitHubDailyActivityError(f"{label} must be a timestamp string")
    parsed = watcher._parse_timestamp(value)
    if parsed is None:
        raise GitHubDailyActivityError(f"{label} must be a timestamp string")
    return parsed


def _optional_timestamp(value: object, *, label: str) -> datetime | None:
    if value is None or value == "":
        return None
    return _timestamp(value, label=label)


def _inside(value: datetime, *, start: datetime, end: datetime) -> bool:
    return start <= value < end


def _bounded_text(value: object, *, limit: int, label: str) -> TextExcerpt | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise GitHubDailyActivityError(f"{label} must be a string or null")
    encoded = value.encode("utf-8")
    original = len(encoded)
    if original <= limit:
        return TextExcerpt(
            text=value,
            truncated=False,
            original_bytes=original,
            included_bytes=original,
        )
    bounded = encoded[:limit].decode("utf-8", errors="ignore")
    included = len(bounded.encode("utf-8"))
    return TextExcerpt(
        text=bounded,
        truncated=True,
        original_bytes=original,
        included_bytes=included,
    )


def _actor(row: Mapping[str, object]) -> str | None:
    raw = row.get("user")
    if not isinstance(raw, dict):
        raw = row.get("actor")
    if not isinstance(raw, dict):
        raw = row.get("author")
    if not isinstance(raw, dict):
        raw = row.get("committer")
    if not isinstance(raw, dict):
        return None
    login = raw.get("login")
    return login if isinstance(login, str) and login else None


def _positive_number(value: object, *, label: str) -> int:
    if type(value) is not int or value < 1:
        raise GitHubDailyActivityError(f"{label} must be a positive integer")
    return value


def _source_id(value: object, *, fallback: str) -> str:
    if isinstance(value, int) and value >= 0:
        return str(value)
    if isinstance(value, str) and value:
        return value
    return fallback


def _event_payload(
    *,
    kind: str,
    repository: str,
    occurred_at: datetime,
    url: object,
    actor: str | None,
    entity_type: str,
    number: int | None,
    source_id: str,
    sha: str | None = None,
    state: str | None = None,
    draft: bool | None = None,
    title: TextExcerpt | None = None,
    body: TextExcerpt | None = None,
    message: TextExcerpt | None = None,
) -> dict[str, object]:
    if kind not in _EVENT_KINDS:
        raise GitHubDailyActivityError(f"unsupported Daily GitHub event kind: {kind}")
    if entity_type not in _ENTITY_TYPES:
        raise GitHubDailyActivityError(f"unsupported Daily GitHub entity type: {entity_type}")
    if not isinstance(url, str) or not url:
        raise GitHubDailyActivityError("GitHub evidence URL is missing")
    if number is not None and (type(number) is not int or number < 1):
        raise GitHubDailyActivityError("GitHub evidence number must be positive")
    if draft is not None and type(draft) is not bool:
        raise GitHubDailyActivityError("GitHub evidence draft must be boolean or null")
    return {
        "kind": kind,
        "repository": repository,
        "occurred_at": _format_timestamp(occurred_at),
        "url": url,
        "actor": actor,
        "entity_type": entity_type,
        "number": number,
        "source_id": source_id,
        "sha": sha,
        "state": state,
        "draft": draft,
        "title": title.to_json() if title is not None else None,
        "body": body.to_json() if body is not None else None,
        "message": message.to_json() if message is not None else None,
    }


def _make_event(**kwargs: object) -> EvidenceEvent:
    payload = _event_payload(**kwargs)
    evidence_id = hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()
    return EvidenceEvent(
        evidence_id=evidence_id,
        kind=str(payload["kind"]),
        repository=str(payload["repository"]),
        occurred_at=str(payload["occurred_at"]),
        url=str(payload["url"]),
        actor=payload["actor"] if isinstance(payload["actor"], str) else None,
        entity_type=str(payload["entity_type"]),
        number=payload["number"] if type(payload["number"]) is int else None,
        source_id=str(payload["source_id"]),
        sha=payload["sha"] if isinstance(payload["sha"], str) else None,
        state=payload["state"] if isinstance(payload["state"], str) else None,
        draft=payload["draft"] if type(payload["draft"]) is bool else None,
        title=kwargs.get("title") if isinstance(kwargs.get("title"), TextExcerpt) else None,
        body=kwargs.get("body") if isinstance(kwargs.get("body"), TextExcerpt) else None,
        message=kwargs.get("message") if isinstance(kwargs.get("message"), TextExcerpt) else None,
    )


def _date_window(target: date) -> tuple[datetime, datetime]:
    zone = ZoneInfo(CANONICAL_TIMEZONE)
    local_start = datetime.combine(target, time.min, tzinfo=zone)
    local_end = datetime.combine(
        date.fromordinal(target.toordinal() + 1),
        time.min,
        tzinfo=zone,
    )
    return local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)


def _query(**params: str) -> str:
    return urllib.parse.urlencode(params, quote_via=urllib.parse.quote)


def _issue_number_from_url(value: object, *, label: str) -> int:
    if not isinstance(value, str) or not value:
        raise GitHubDailyActivityError(f"{label} URL is missing")
    tail = value.rstrip("/").rsplit("/", 1)[-1]
    try:
        number = int(tail)
    except ValueError as exc:
        raise GitHubDailyActivityError(f"{label} URL has no numeric issue number") from exc
    if number < 1:
        raise GitHubDailyActivityError(f"{label} URL has no positive issue number")
    return number


def _collect_issue_rows(
    client: watcher.GitHubClient,
    *,
    repository: str,
    repo_path: str,
    start: datetime,
    end: datetime,
) -> tuple[list[EvidenceEvent], set[int], set[int]]:
    query = _query(
        state="all",
        since=_format_timestamp(start),
        sort="updated",
        direction="asc",
    )
    rows = client._paged(f"/repos/{repo_path}/issues?{query}")
    events: list[EvidenceEvent] = []
    pr_numbers: set[int] = set()
    issue_numbers: set[int] = set()

    for row in rows:
        number = _positive_number(row.get("number"), label="Issue number")
        issue_numbers.add(number)
        is_pr = "pull_request" in row
        if is_pr:
            pr_numbers.add(number)
        entity = "pull_request" if is_pr else "issue"
        created_at = _timestamp(row.get("created_at"), label=f"{entity} created_at")
        updated_at = _timestamp(row.get("updated_at"), label=f"{entity} updated_at")
        title = _bounded_text(
            row.get("title"),
            limit=TITLE_MAX_BYTES,
            label=f"{entity} title",
        )
        body = _bounded_text(
            row.get("body"),
            limit=BODY_MAX_BYTES,
            label=f"{entity} body",
        )
        url = row.get("html_url")
        actor = _actor(row)
        state = row.get("state") if isinstance(row.get("state"), str) else None
        draft_value = row.get("draft")
        draft = draft_value if type(draft_value) is bool else None

        if _inside(created_at, start=start, end=end):
            kind = "pull_request_created" if is_pr else "issue_created"
            events.append(
                _make_event(
                    kind=kind,
                    repository=repository,
                    occurred_at=created_at,
                    url=url,
                    actor=actor,
                    entity_type=entity,
                    number=number,
                    source_id=f"{entity}:{number}:created:{_format_timestamp(created_at)}",
                    state=state,
                    draft=draft,
                    title=title,
                    body=body,
                )
            )
        elif _inside(updated_at, start=start, end=end):
            kind = "pull_request_snapshot" if is_pr else "issue_snapshot"
            events.append(
                _make_event(
                    kind=kind,
                    repository=repository,
                    occurred_at=updated_at,
                    url=url,
                    actor=actor,
                    entity_type=entity,
                    number=number,
                    source_id=f"{entity}:{number}:snapshot:{_format_timestamp(updated_at)}",
                    state=state,
                    draft=draft,
                    title=title,
                    body=body,
                )
            )

    return events, pr_numbers, issue_numbers


def _collect_issue_lifecycle(
    client: watcher.GitHubClient,
    *,
    repository: str,
    repo_path: str,
    start: datetime,
    end: datetime,
    issue_numbers: Sequence[int],
    known_pr_numbers: set[int],
) -> tuple[list[EvidenceEvent], set[int]]:
    events: list[EvidenceEvent] = []
    pr_numbers = set(known_pr_numbers)

    for issue_number in sorted(set(issue_numbers)):
        rows = client._paged(f"/repos/{repo_path}/issues/{issue_number}/events")
        for row in rows:
            occurred_at = _optional_timestamp(
                row.get("created_at"),
                label="issue event created_at",
            )
            if occurred_at is None or not _inside(occurred_at, start=start, end=end):
                continue
            action = row.get("event")
            if not isinstance(action, str) or action not in _ISSUE_EVENT_KIND:
                continue
            issue = row.get("issue")
            if not isinstance(issue, dict):
                issue = {"number": issue_number}
            number = _positive_number(
                issue.get("number", issue_number),
                label="issue event number",
            )
            is_pr = "pull_request" in issue or number in pr_numbers
            if is_pr:
                pr_numbers.add(number)
            pair = _ISSUE_EVENT_KIND[action]
            kind = pair[1] if is_pr else pair[0]
            if kind is None:
                continue
            entity = "pull_request" if is_pr else "issue"
            title = _bounded_text(
                issue.get("title"),
                limit=TITLE_MAX_BYTES,
                label=f"{entity} title",
            )
            url = issue.get("html_url")
            if not isinstance(url, str) or not url:
                suffix = "pull" if is_pr else "issues"
                url = f"https://github.com/{repository}/{suffix}/{number}"
            events.append(
                _make_event(
                    kind=kind,
                    repository=repository,
                    occurred_at=occurred_at,
                    url=url,
                    actor=_actor(row),
                    entity_type=entity,
                    number=number,
                    source_id=_source_id(
                        row.get("id"),
                        fallback=f"{entity}:{number}:{action}:{_format_timestamp(occurred_at)}",
                    ),
                    state=issue.get("state") if isinstance(issue.get("state"), str) else None,
                    title=title,
                )
            )
    return events, pr_numbers


def _collect_issue_comments(
    client: watcher.GitHubClient,
    *,
    repository: str,
    repo_path: str,
    start: datetime,
    end: datetime,
    known_pr_numbers: set[int],
) -> list[EvidenceEvent]:
    query = _query(since=_format_timestamp(start), sort="created", direction="asc")
    rows = client._paged(f"/repos/{repo_path}/issues/comments?{query}")
    events: list[EvidenceEvent] = []
    for row in rows:
        occurred_at = _timestamp(row.get("created_at"), label="issue comment created_at")
        if not _inside(occurred_at, start=start, end=end):
            continue
        number = _issue_number_from_url(row.get("issue_url"), label="issue comment")
        is_pr = number in known_pr_numbers or (
            isinstance(row.get("html_url"), str) and "/pull/" in str(row.get("html_url"))
        )
        kind = "pull_request_comment" if is_pr else "issue_comment"
        entity = "pull_request_comment" if is_pr else "issue_comment"
        events.append(
            _make_event(
                kind=kind,
                repository=repository,
                occurred_at=occurred_at,
                url=row.get("html_url"),
                actor=_actor(row),
                entity_type=entity,
                number=number,
                source_id=_source_id(
                    row.get("id"),
                    fallback=f"{entity}:{number}:{_format_timestamp(occurred_at)}",
                ),
                body=_bounded_text(
                    row.get("body"),
                    limit=COMMENT_MAX_BYTES,
                    label=f"{entity} body",
                ),
            )
        )
    return events


def _collect_pr_review_comments(
    client: watcher.GitHubClient,
    *,
    repository: str,
    repo_path: str,
    start: datetime,
    end: datetime,
) -> tuple[list[EvidenceEvent], set[int]]:
    query = _query(since=_format_timestamp(start), sort="created", direction="asc")
    rows = client._paged(f"/repos/{repo_path}/pulls/comments?{query}")
    events: list[EvidenceEvent] = []
    pr_numbers: set[int] = set()
    for row in rows:
        occurred_at = _timestamp(row.get("created_at"), label="review comment created_at")
        if not _inside(occurred_at, start=start, end=end):
            continue
        number = _issue_number_from_url(
            row.get("pull_request_url"),
            label="review comment",
        )
        pr_numbers.add(number)
        events.append(
            _make_event(
                kind="pull_request_review_comment",
                repository=repository,
                occurred_at=occurred_at,
                url=row.get("html_url"),
                actor=_actor(row),
                entity_type="pull_request_review_comment",
                number=number,
                source_id=_source_id(
                    row.get("id"),
                    fallback=f"pull_request_review_comment:{number}:{_format_timestamp(occurred_at)}",
                ),
                body=_bounded_text(
                    row.get("body"),
                    limit=COMMENT_MAX_BYTES,
                    label="pull request review comment body",
                ),
            )
        )
    return events, pr_numbers


def _collect_pr_reviews_and_commits(
    client: watcher.GitHubClient,
    *,
    repository: str,
    repo_path: str,
    start: datetime,
    end: datetime,
    pr_numbers: Sequence[int],
) -> list[EvidenceEvent]:
    events: list[EvidenceEvent] = []
    for number in sorted(set(pr_numbers)):
        reviews = client._paged(f"/repos/{repo_path}/pulls/{number}/reviews")
        for row in reviews:
            submitted_at = _optional_timestamp(
                row.get("submitted_at"),
                label="pull request review submitted_at",
            )
            if submitted_at is None or not _inside(
                submitted_at,
                start=start,
                end=end,
            ):
                continue
            state = row.get("state") if isinstance(row.get("state"), str) else None
            events.append(
                _make_event(
                    kind="pull_request_review",
                    repository=repository,
                    occurred_at=submitted_at,
                    url=row.get("html_url"),
                    actor=_actor(row),
                    entity_type="pull_request_review",
                    number=number,
                    source_id=_source_id(
                        row.get("id"),
                        fallback=f"pull_request_review:{number}:{_format_timestamp(submitted_at)}",
                    ),
                    state=state,
                    body=_bounded_text(
                        row.get("body"),
                        limit=REVIEW_MAX_BYTES,
                        label="pull request review body",
                    ),
                )
            )

        commits = client._paged(f"/repos/{repo_path}/pulls/{number}/commits")
        for row in commits:
            committed_at = watcher.GitHubClient._timestamp_from_commit_payload(row)
            if committed_at is None or not _inside(
                committed_at,
                start=start,
                end=end,
            ):
                continue
            sha = row.get("sha")
            if not isinstance(sha, str) or not sha:
                raise GitHubDailyActivityError("pull request commit is missing SHA")
            commit = row.get("commit")
            if not isinstance(commit, dict):
                raise GitHubDailyActivityError(
                    "pull request commit is missing commit payload"
                )
            events.append(
                _make_event(
                    kind="pull_request_commit",
                    repository=repository,
                    occurred_at=committed_at,
                    url=row.get("html_url"),
                    actor=_actor(row),
                    entity_type="commit",
                    number=number,
                    source_id=sha,
                    sha=sha,
                    message=_bounded_text(
                        commit.get("message"),
                        limit=COMMIT_MESSAGE_MAX_BYTES,
                        label="pull request commit message",
                    ),
                )
            )
    return events


def _collect_default_branch_commits(
    client: watcher.GitHubClient,
    *,
    repository: str,
    repo_path: str,
    start: datetime,
    end: datetime,
) -> list[EvidenceEvent]:
    query = _query(since=_format_timestamp(start), until=_format_timestamp(end))
    rows = client._paged(f"/repos/{repo_path}/commits?{query}")
    events: list[EvidenceEvent] = []
    for row in rows:
        committed_at = watcher.GitHubClient._timestamp_from_commit_payload(row)
        if committed_at is None or not _inside(
            committed_at,
            start=start,
            end=end,
        ):
            continue
        sha = row.get("sha")
        if not isinstance(sha, str) or not sha:
            raise GitHubDailyActivityError("default-branch commit is missing SHA")
        commit = row.get("commit")
        if not isinstance(commit, dict):
            raise GitHubDailyActivityError(
                "default-branch commit is missing commit payload"
            )
        events.append(
            _make_event(
                kind="default_branch_commit",
                repository=repository,
                occurred_at=committed_at,
                url=row.get("html_url"),
                actor=_actor(row),
                entity_type="commit",
                number=None,
                source_id=sha,
                sha=sha,
                message=_bounded_text(
                    commit.get("message"),
                    limit=COMMIT_MESSAGE_MAX_BYTES,
                    label="default branch commit message",
                ),
            )
        )
    return events


def _collect_repository(
    client: watcher.GitHubClient,
    *,
    repository: str,
    start: datetime,
    end: datetime,
) -> list[EvidenceEvent]:
    repo_path = client._repo_path(repository)
    events, pr_numbers, issue_numbers = _collect_issue_rows(
        client,
        repository=repository,
        repo_path=repo_path,
        start=start,
        end=end,
    )
    lifecycle, pr_numbers = _collect_issue_lifecycle(
        client,
        repository=repository,
        repo_path=repo_path,
        start=start,
        end=end,
        issue_numbers=sorted(issue_numbers),
        known_pr_numbers=pr_numbers,
    )
    events.extend(lifecycle)
    events.extend(
        _collect_issue_comments(
            client,
            repository=repository,
            repo_path=repo_path,
            start=start,
            end=end,
            known_pr_numbers=pr_numbers,
        )
    )
    review_comments, review_pr_numbers = _collect_pr_review_comments(
        client,
        repository=repository,
        repo_path=repo_path,
        start=start,
        end=end,
    )
    events.extend(review_comments)
    pr_numbers.update(review_pr_numbers)
    events.extend(
        _collect_pr_reviews_and_commits(
            client,
            repository=repository,
            repo_path=repo_path,
            start=start,
            end=end,
            pr_numbers=sorted(pr_numbers),
        )
    )
    events.extend(
        _collect_default_branch_commits(
            client,
            repository=repository,
            repo_path=repo_path,
            start=start,
            end=end,
        )
    )
    return events


def make_daily_evidence_bundle(
    *,
    target_date: date,
    projects: Sequence[ProjectBinding],
    events: Sequence[EvidenceEvent],
) -> DailyEvidenceBundle:
    start, end = _date_window(target_date)
    project_rows = tuple(
        sorted(
            projects,
            key=lambda item: (item.repository.casefold(), item.project_path),
        )
    )
    repositories = tuple(
        sorted({item.repository for item in project_rows}, key=str.casefold)
    )
    deduped = {item.evidence_id: item for item in events}
    event_rows = tuple(
        sorted(
            deduped.values(),
            key=lambda item: (
                item.occurred_at,
                item.repository.casefold(),
                item.kind,
                item.source_id,
                item.evidence_id,
            ),
        )
    )
    payload: dict[str, object] = {
        "record_version": RECORD_VERSION,
        "date": target_date.isoformat(),
        "timezone": CANONICAL_TIMEZONE,
        "window_start": _format_timestamp(start),
        "window_end": _format_timestamp(end),
        "projects": [item.to_json() for item in project_rows],
        "repositories": list(repositories),
        "events": [item.to_json() for item in event_rows],
    }
    canonical = _canonical_json_bytes(payload)
    return DailyEvidenceBundle(
        target_date=target_date.isoformat(),
        window_start=_format_timestamp(start),
        window_end=_format_timestamp(end),
        projects=project_rows,
        repositories=repositories,
        events=event_rows,
        canonical_bytes=canonical,
        sha256=hashlib.sha256(canonical).hexdigest(),
    )


def collect_daily_evidence(
    config: watcher.WatcherConfig,
    *,
    target_date: date,
    client: watcher.GitHubClient | None = None,
) -> DailyEvidenceBundle:
    scanned, warnings = watcher.scan_projects(
        config.vault_root,
        config.project_folder,
    )
    if warnings:
        raise GitHubDailyActivityError(
            "Project binding scan is incomplete: " + "; ".join(warnings)
        )
    projects = [
        ProjectBinding(project_path=item.path, repository=item.repository)
        for item in scanned
    ]
    start, end = _date_window(target_date)
    api = client or watcher.GitHubClient(
        api_base=config.github_api_base,
        token=os.environ.get(config.github_token_env),
        timeout_seconds=config.request_timeout_seconds,
    )
    events: list[EvidenceEvent] = []
    for repository in sorted(
        {item.repository for item in projects},
        key=str.casefold,
    ):
        events.extend(
            _collect_repository(
                api,
                repository=repository,
                start=start,
                end=end,
            )
        )
    return make_daily_evidence_bundle(
        target_date=target_date,
        projects=projects,
        events=events,
    )


def _require_directory(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise GitHubDailyActivityError(
            f"output directory does not exist: {path}"
        ) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise GitHubDailyActivityError(
            "output directory must be a non-symlink directory"
        )


def persist_daily_evidence(
    output_dir: Path,
    bundle: DailyEvidenceBundle,
) -> Path:
    _require_directory(output_dir)
    target = output_dir / f"{bundle.sha256}.github-daily-evidence.json"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = os.open(target, flags, 0o640)
    except FileExistsError:
        if target.is_symlink() or not target.is_file():
            raise GitHubDailyActivityError(
                "existing evidence artifact is not a regular file"
            )
        if target.read_bytes() != bundle.canonical_bytes:
            raise GitHubDailyActivityError(
                "existing evidence artifact does not match its SHA path"
            )
        return target
    try:
        view = memoryview(bundle.canonical_bytes)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise GitHubDailyActivityError(
                    "short write while persisting Daily evidence"
                )
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    dir_fd = os.open(output_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return target


def _parse_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "date must be YYYY-MM-DD"
        ) from exc
    if parsed.isoformat() != value:
        raise argparse.ArgumentTypeError(
            "date must be canonical YYYY-MM-DD"
        )
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="obsidian-github-daily-activity-collect",
        description=(
            "Collect one JST day of GitHub Project activity "
            "into immutable evidence."
        ),
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--date", type=_parse_date, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _build_parser().parse_args(
        list(argv) if argv is not None else None
    )
    try:
        config = watcher.load_config(args.config)
        bundle = collect_daily_evidence(
            config,
            target_date=args.date,
        )
        path = persist_daily_evidence(
            args.output_dir,
            bundle,
        )
    except (
        OSError,
        GitHubDailyActivityError,
        watcher.GitHubProjectWatcherError,
    ) as exc:
        print(f"github-daily-activity: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "event": "github-daily-evidence-collected",
                "date": bundle.target_date,
                "sha256": bundle.sha256,
                "artifact": path.name,
                "projects": len(bundle.projects),
                "repositories": len(bundle.repositories),
                "events": len(bundle.events),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
