from __future__ import annotations

import os
import sys
from typing import Iterable

from . import github_project_watcher as watcher

class GitHubClient(watcher.GitHubClient):
    """Default-branch snapshot client that retains overview rows for reuse."""

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
