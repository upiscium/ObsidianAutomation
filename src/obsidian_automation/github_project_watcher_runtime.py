from __future__ import annotations

import os
import sys
from datetime import datetime
from typing import Iterable

from . import github_project_watcher as watcher


COMMIT_ACTIVITY_TYPES = (
    "push",
    "force_push",
    "pr_merge",
    "merge_queue_merge",
)


class GitHubClient(watcher.GitHubClient):
    """Repository-activity client that also retains overview rows from each snapshot."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._overview_cache: dict[tuple[str, str], tuple[dict[str, object], ...]] = {}

    def _paged(self, path: str) -> list[dict[str, object]]:
        rows = super()._paged(path)
        prefix = "/repos/"
        issue_suffix = "/issues?state=open"
        pull_suffix = "/pulls?state=open"
        if path.startswith(prefix) and path.endswith(issue_suffix):
            repo_path = path[len(prefix) : -len(issue_suffix)]
            self._overview_cache[(repo_path, "issues")] = tuple(dict(row) for row in rows)
        elif path.startswith(prefix) and path.endswith(pull_suffix):
            repo_path = path[len(prefix) : -len(pull_suffix)]
            self._overview_cache[(repo_path, "pulls")] = tuple(dict(row) for row in rows)
        return rows

    def overview_rows(
        self,
        repository: str,
    ) -> tuple[tuple[dict[str, object], ...], tuple[dict[str, object], ...]]:
        repo_path = self._repo_path(repository)
        try:
            issues = self._overview_cache[(repo_path, "issues")]
            pulls = self._overview_cache[(repo_path, "pulls")]
        except KeyError as exc:
            raise watcher.GitHubProjectWatcherError(
                f"overview rows are unavailable before a successful snapshot for {repository}"
            ) from exc
        return issues, pulls

    @staticmethod
    def _timestamp_from_commit_payload(value: object) -> datetime | None:
        if not isinstance(value, dict):
            return None
        commit = value.get("commit")
        if not isinstance(commit, dict):
            return None
        for identity_key in ("committer", "author"):
            identity = commit.get(identity_key)
            if not isinstance(identity, dict):
                continue
            date_value = identity.get("date")
            if isinstance(date_value, str):
                parsed = watcher._parse_timestamp(date_value)
                if parsed is not None:
                    return parsed
        return None

    def _activity_timestamp(
        self,
        *,
        repo_path: str,
        activity: dict[str, object],
        sha: str,
    ) -> datetime | None:
        # GitHub's repository activity response has changed across API/schema
        # representations. Prefer an event timestamp when present, retain the
        # documented pushed_at field as a compatibility fallback, and finally
        # resolve the commit object referenced by `after`.
        for key in ("timestamp", "pushed_at"):
            value = activity.get(key)
            if isinstance(value, str):
                parsed = watcher._parse_timestamp(value)
                if parsed is not None:
                    return parsed

        commit_payload = self._request_json(f"/repos/{repo_path}/commits/{sha}")
        return self._timestamp_from_commit_payload(commit_payload)

    def _latest_default_branch_commit(
        self,
        repo_path: str,
    ) -> tuple[str | None, datetime | None]:
        value = self._request_json(f"/repos/{repo_path}/commits?per_page=1")
        if not isinstance(value, list):
            raise watcher.GitHubProjectWatcherError(
                f"invalid latest commit payload for {repo_path}"
            )
        if not value:
            return None, None
        first = value[0]
        if not isinstance(first, dict):
            raise watcher.GitHubProjectWatcherError(
                f"invalid latest commit row for {repo_path}"
            )
        sha = str(first.get("sha") or "").strip()
        committed_at = self._timestamp_from_commit_payload(first)
        if not sha or committed_at is None:
            raise watcher.GitHubProjectWatcherError(
                f"latest commit row is missing SHA or timestamp for {repo_path}"
            )
        return sha, committed_at

    def _latest_push(self, repo_path: str) -> tuple[str | None, datetime | None]:
        candidates: list[tuple[str, datetime]] = []
        for activity_type in COMMIT_ACTIVITY_TYPES:
            value = self._request_json(
                f"/repos/{repo_path}/activity?activity_type={activity_type}"
                "&time_period=year&per_page=1&direction=desc"
            )
            if not isinstance(value, list):
                raise watcher.GitHubProjectWatcherError(
                    f"invalid repository activity payload for {repo_path}"
                )
            if not value:
                continue
            first = value[0]
            if not isinstance(first, dict):
                raise watcher.GitHubProjectWatcherError(
                    f"invalid repository activity row for {repo_path}"
                )
            sha = str(first.get("after") or "").strip()
            if not sha:
                continue
            committed_at = self._activity_timestamp(
                repo_path=repo_path,
                activity=first,
                sha=sha,
            )
            if committed_at is not None:
                candidates.append((sha, committed_at))

        if candidates:
            return max(candidates, key=lambda item: item[1])
        return self._latest_default_branch_commit(repo_path)


def main(argv: Iterable[str] | None = None) -> int:
    args = watcher._build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        config = watcher.load_config(args.config)
        client = GitHubClient(
            api_base=config.github_api_base,
            token=os.environ.get(config.github_token_env),
            timeout_seconds=config.request_timeout_seconds,
        )
        return watcher.run_once(config, client=client)
    except watcher.GitHubProjectWatcherError as exc:
        print(f"github-project-watcher: {exc}", file=sys.stderr)
        return 2
