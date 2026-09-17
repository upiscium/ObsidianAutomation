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
            pushed_at = first.get("pushed_at")
            if sha and isinstance(pushed_at, str):
                parsed = watcher._parse_timestamp(pushed_at)
                if parsed is not None:
                    candidates.append((sha, parsed))

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
