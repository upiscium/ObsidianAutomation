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
    """Repository-activity client for commit-producing GitHub events."""

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

        if not candidates:
            return None, None
        return max(candidates, key=lambda item: item[1])


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
