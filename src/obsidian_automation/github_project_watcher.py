from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, TextIO


VALID_STATUSES = frozenset({"planning", "running", "stopped", "done", "cancelled"})
TERMINAL_STATUSES = frozenset({"done", "cancelled"})
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class GitHubProjectWatcherError(RuntimeError):
    """Raised when watcher input or remote state cannot be processed safely."""


@dataclass(frozen=True)
class WatcherConfig:
    vault_root: Path
    state_db: Path
    project_folder: str = "10-Project"
    active_window_days: int = 7
    github_api_base: str = "https://api.github.com"
    github_token_env: str = "GITHUB_TOKEN"
    request_timeout_seconds: int = 15


@dataclass(frozen=True)
class ProjectBinding:
    path: str
    repository: str
    status: str


@dataclass(frozen=True)
class RepositorySnapshot:
    repository: str
    latest_commit_sha: str | None
    latest_commit_at: datetime | None
    open_issues: frozenset[int]
    open_prs: frozenset[int]
    observed_at: datetime


@dataclass(frozen=True)
class ProjectState:
    project_path: str
    repository: str
    last_status: str
    latest_commit_sha: str | None
    latest_commit_at: datetime | None
    open_issues: frozenset[int]
    open_prs: frozenset[int]
    observed_at: datetime
    pending_status: str | None
    pending_reason: str | None


@dataclass(frozen=True)
class StatusDecision:
    proposed_status: str
    reason: str
    pending: bool


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GitHubProjectWatcherError(f"invalid timestamp: {value}") from exc
    if parsed.tzinfo is None:
        raise GitHubProjectWatcherError(f"timestamp must include timezone: {value}")
    return parsed.astimezone(timezone.utc)


def _plain_scalar(raw: str) -> str:
    value = raw.strip()
    if not value:
        return ""
    if " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


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
        if not key or key in values:
            continue
        values[key] = _plain_scalar(raw)
    return {}


def _watch_enabled(value: str) -> bool:
    return value.strip().lower() in {"true", "yes", "1", "on"}


def scan_projects(vault_root: Path, project_folder: str = "10-Project") -> tuple[list[ProjectBinding], list[str]]:
    root = (vault_root / project_folder).resolve()
    try:
        root.relative_to(vault_root.resolve())
    except ValueError as exc:
        raise GitHubProjectWatcherError("project_folder escapes vault_root") from exc
    if not root.is_dir():
        raise GitHubProjectWatcherError(f"project folder does not exist: {root}")

    projects: list[ProjectBinding] = []
    warnings: list[str] = []
    for path in sorted(root.rglob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            warnings.append(f"{path}: cannot read project note: {exc}")
            continue
        fm = _frontmatter_scalars(text)
        if fm.get("type") != "project" or not _watch_enabled(fm.get("github_watch", "")):
            continue
        repository = fm.get("github_repo", "").strip()
        status = fm.get("status", "").strip()
        relative = path.relative_to(vault_root).as_posix()
        if not _REPOSITORY_RE.fullmatch(repository):
            warnings.append(f"{relative}: invalid github_repo: {repository!r}")
            continue
        if status not in VALID_STATUSES:
            warnings.append(f"{relative}: invalid Project status: {status!r}")
            continue
        projects.append(ProjectBinding(path=relative, repository=repository, status=status))
    return projects, warnings


def load_config(path: Path) -> WatcherConfig:
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise GitHubProjectWatcherError(f"cannot read config: {path}") from exc
    raw = payload.get("watcher")
    if not isinstance(raw, dict):
        raise GitHubProjectWatcherError("config must contain [watcher]")

    try:
        vault_root = Path(str(raw["vault_root"])).expanduser()
        state_db = Path(str(raw["state_db"])).expanduser()
    except KeyError as exc:
        raise GitHubProjectWatcherError(f"missing watcher config key: {exc.args[0]}") from exc

    active_window_days = int(raw.get("active_window_days", 7))
    timeout = int(raw.get("request_timeout_seconds", 15))
    if active_window_days < 1:
        raise GitHubProjectWatcherError("active_window_days must be >= 1")
    if timeout < 1:
        raise GitHubProjectWatcherError("request_timeout_seconds must be >= 1")

    return WatcherConfig(
        vault_root=vault_root,
        state_db=state_db,
        project_folder=str(raw.get("project_folder", "10-Project")),
        active_window_days=active_window_days,
        github_api_base=str(raw.get("github_api_base", "https://api.github.com")).rstrip("/"),
        github_token_env=str(raw.get("github_token_env", "GITHUB_TOKEN")),
        request_timeout_seconds=timeout,
    )


class GitHubClient:
    def __init__(
        self,
        *,
        api_base: str = "https://api.github.com",
        token: str | None = None,
        timeout_seconds: int = 15,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.token = token.strip() if token else None
        self.timeout_seconds = timeout_seconds

    def _request_json(self, path: str) -> object:
        request = urllib.request.Request(
            f"{self.api_base}{path}",
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "ObsidianAutomation-GitHubProjectWatcher/0.1",
                "X-GitHub-Api-Version": "2022-11-28",
                **({"Authorization": f"Bearer {self.token}"} if self.token else {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                data = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
            raise GitHubProjectWatcherError(
                f"GitHub API returned HTTP {exc.code} for {path}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise GitHubProjectWatcherError(f"GitHub API request failed for {path}: {exc.reason}") from exc
        try:
            return json.loads(data)
        except json.JSONDecodeError as exc:
            raise GitHubProjectWatcherError(f"GitHub API returned invalid JSON for {path}") from exc

    @staticmethod
    def _repo_path(repository: str) -> str:
        owner, name = repository.split("/", 1)
        return f"{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(name, safe='')}"

    def _paged(self, path: str) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        page = 1
        separator = "&" if "?" in path else "?"
        while True:
            value = self._request_json(f"{path}{separator}per_page=100&page={page}")
            if not isinstance(value, list):
                raise GitHubProjectWatcherError(f"GitHub API list expected for {path}")
            typed = [item for item in value if isinstance(item, dict)]
            rows.extend(typed)
            if len(value) < 100:
                return rows
            page += 1

    def _latest_push(self, repo_path: str) -> tuple[str | None, datetime | None]:
        candidates: list[tuple[str, datetime]] = []
        for activity_type in ("push", "force_push"):
            value = self._request_json(
                f"/repos/{repo_path}/activity?activity_type={activity_type}&time_period=year&per_page=1&direction=desc"
            )
            if not isinstance(value, list):
                raise GitHubProjectWatcherError(
                    f"invalid repository activity payload for {repo_path}"
                )
            if not value:
                continue
            first = value[0]
            if not isinstance(first, dict):
                raise GitHubProjectWatcherError(
                    f"invalid repository activity row for {repo_path}"
                )
            sha = str(first.get("after") or "").strip()
            pushed_at = first.get("pushed_at")
            if sha and isinstance(pushed_at, str):
                parsed = _parse_timestamp(pushed_at)
                if parsed is not None:
                    candidates.append((sha, parsed))
        if not candidates:
            return None, None
        return max(candidates, key=lambda item: item[1])

    def snapshot(self, repository: str, *, observed_at: datetime | None = None) -> RepositorySnapshot:
        repo_path = self._repo_path(repository)
        latest_sha, latest_at = self._latest_push(repo_path)

        issues = self._paged(f"/repos/{repo_path}/issues?state=open")
        open_issues = frozenset(
            int(item["number"])
            for item in issues
            if "pull_request" not in item and isinstance(item.get("number"), int)
        )
        pulls = self._paged(f"/repos/{repo_path}/pulls?state=open")
        open_prs = frozenset(
            int(item["number"])
            for item in pulls
            if isinstance(item.get("number"), int)
        )
        return RepositorySnapshot(
            repository=repository,
            latest_commit_sha=latest_sha,
            latest_commit_at=latest_at,
            open_issues=open_issues,
            open_prs=open_prs,
            observed_at=(observed_at or _utc_now()).astimezone(timezone.utc),
        )


class StateStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS project_state (
                project_path TEXT PRIMARY KEY,
                repository TEXT NOT NULL,
                last_status TEXT NOT NULL,
                latest_commit_sha TEXT,
                latest_commit_at TEXT,
                open_issues_json TEXT NOT NULL,
                open_prs_json TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                pending_status TEXT,
                pending_reason TEXT
            )
            """
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> StateStore:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def load(self, project_path: str) -> ProjectState | None:
        row = self.connection.execute(
            """
            SELECT project_path, repository, last_status, latest_commit_sha,
                   latest_commit_at, open_issues_json, open_prs_json, observed_at,
                   pending_status, pending_reason
            FROM project_state WHERE project_path = ?
            """,
            (project_path,),
        ).fetchone()
        if row is None:
            return None
        return ProjectState(
            project_path=str(row[0]),
            repository=str(row[1]),
            last_status=str(row[2]),
            latest_commit_sha=str(row[3]) if row[3] else None,
            latest_commit_at=_parse_timestamp(str(row[4])) if row[4] else None,
            open_issues=frozenset(int(value) for value in json.loads(str(row[5]))),
            open_prs=frozenset(int(value) for value in json.loads(str(row[6]))),
            observed_at=_parse_timestamp(str(row[7])) or _utc_now(),
            pending_status=str(row[8]) if row[8] else None,
            pending_reason=str(row[9]) if row[9] else None,
        )

    def save(
        self,
        project: ProjectBinding,
        snapshot: RepositorySnapshot,
        *,
        pending_status: str | None,
        pending_reason: str | None,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO project_state (
                project_path, repository, last_status, latest_commit_sha,
                latest_commit_at, open_issues_json, open_prs_json, observed_at,
                pending_status, pending_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_path) DO UPDATE SET
                repository = excluded.repository,
                last_status = excluded.last_status,
                latest_commit_sha = excluded.latest_commit_sha,
                latest_commit_at = excluded.latest_commit_at,
                open_issues_json = excluded.open_issues_json,
                open_prs_json = excluded.open_prs_json,
                observed_at = excluded.observed_at,
                pending_status = excluded.pending_status,
                pending_reason = excluded.pending_reason
            """,
            (
                project.path,
                project.repository,
                project.status,
                snapshot.latest_commit_sha,
                _format_timestamp(snapshot.latest_commit_at),
                json.dumps(sorted(snapshot.open_issues), separators=(",", ":")),
                json.dumps(sorted(snapshot.open_prs), separators=(",", ":")),
                _format_timestamp(snapshot.observed_at),
                pending_status,
                pending_reason,
            ),
        )
        self.connection.commit()


def _has_new_commit(snapshot: RepositorySnapshot, previous: ProjectState) -> bool:
    if not snapshot.latest_commit_sha or snapshot.latest_commit_sha == previous.latest_commit_sha:
        return False
    if snapshot.latest_commit_at is None or previous.latest_commit_at is None:
        return True
    return snapshot.latest_commit_at >= previous.latest_commit_at


def decide_status(
    project: ProjectBinding,
    snapshot: RepositorySnapshot,
    previous: ProjectState | None,
    *,
    now: datetime,
    active_window_days: int,
) -> StatusDecision:
    if project.status == "stopped":
        return StatusDecision("stopped", "stopped is human-controlled", False)

    if project.status in TERMINAL_STATUSES:
        if previous is None or previous.last_status != project.status or previous.repository != project.repository:
            return StatusDecision(project.status, "terminal status baseline initialized", False)
        if _has_new_commit(snapshot, previous):
            return StatusDecision("running", "new commit observed after terminal baseline", True)
        if previous.pending_status in {"running", "planning"}:
            return StatusDecision(
                previous.pending_status,
                previous.pending_reason or "pending terminal reactivation",
                True,
            )
        new_issues = snapshot.open_issues - previous.open_issues
        new_prs = snapshot.open_prs - previous.open_prs
        if new_issues or new_prs:
            details: list[str] = []
            if new_issues:
                details.append("issues=" + ",".join(str(value) for value in sorted(new_issues)))
            if new_prs:
                details.append("prs=" + ",".join(str(value) for value in sorted(new_prs)))
            return StatusDecision(
                "planning",
                "new open GitHub activity after terminal baseline: " + " ".join(details),
                True,
            )
        return StatusDecision(project.status, "no new activity after terminal baseline", False)

    cutoff = now.astimezone(timezone.utc) - timedelta(days=active_window_days)
    if snapshot.latest_commit_at is not None and snapshot.latest_commit_at >= cutoff:
        status = "running"
        reason = f"latest commit is within {active_window_days} days"
    else:
        status = "planning"
        reason = f"no commit within {active_window_days} days"
    return StatusDecision(status, reason, status != project.status)


def _event_payload(
    project: ProjectBinding,
    snapshot: RepositorySnapshot,
    decision: StatusDecision,
) -> dict[str, object]:
    return {
        "event": "project-status-observation",
        "project": project.path,
        "repository": project.repository,
        "current_status": project.status,
        "proposed_status": decision.proposed_status,
        "change": decision.proposed_status != project.status,
        "pending": decision.pending,
        "reason": decision.reason,
        "latest_commit_sha": snapshot.latest_commit_sha,
        "latest_commit_at": _format_timestamp(snapshot.latest_commit_at),
        "open_issues": sorted(snapshot.open_issues),
        "open_prs": sorted(snapshot.open_prs),
        "observed_at": _format_timestamp(snapshot.observed_at),
    }


def run_once(
    config: WatcherConfig,
    *,
    client: GitHubClient | None = None,
    now: datetime | None = None,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    current_time = (now or _utc_now()).astimezone(timezone.utc)
    projects, warnings = scan_projects(config.vault_root, config.project_folder)
    for warning in warnings:
        print(json.dumps({"event": "warning", "message": warning}, ensure_ascii=False), file=stderr)

    token = os.environ.get(config.github_token_env)
    api = client or GitHubClient(
        api_base=config.github_api_base,
        token=token,
        timeout_seconds=config.request_timeout_seconds,
    )
    snapshot_cache: dict[str, RepositorySnapshot] = {}
    errors = 0

    with StateStore(config.state_db) as store:
        for project in projects:
            try:
                snapshot = snapshot_cache.get(project.repository)
                if snapshot is None:
                    snapshot = api.snapshot(project.repository, observed_at=current_time)
                    snapshot_cache[project.repository] = snapshot
                previous = store.load(project.path)
                decision = decide_status(
                    project,
                    snapshot,
                    previous,
                    now=current_time,
                    active_window_days=config.active_window_days,
                )
                pending_status = decision.proposed_status if decision.proposed_status != project.status else None
                pending_reason = decision.reason if pending_status else None
                store.save(
                    project,
                    snapshot,
                    pending_status=pending_status,
                    pending_reason=pending_reason,
                )
                print(
                    json.dumps(_event_payload(project, snapshot, decision), ensure_ascii=False, sort_keys=True),
                    file=stdout,
                )
            except (GitHubProjectWatcherError, OSError, sqlite3.Error, ValueError) as exc:
                errors += 1
                print(
                    json.dumps(
                        {
                            "event": "error",
                            "project": project.path,
                            "repository": project.repository,
                            "message": str(exc),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    file=stderr,
                )
    return 1 if errors else 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Observe GitHub-backed Obsidian Projects and emit deterministic status proposals."
    )
    parser.add_argument("--config", required=True, type=Path)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        config = load_config(args.config)
        return run_once(config)
    except GitHubProjectWatcherError as exc:
        print(f"github-project-watcher: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())