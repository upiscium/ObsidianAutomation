from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path
from typing import Iterable, Sequence, TextIO

from . import github_project_watcher as watcher
from .github_project_overview import (
    OverviewItem,
    ProjectOverviewError,
    ProjectOverviewProposal,
    make_overview_proposal,
)
from .github_project_watcher_runtime import GitHubClient


class ProjectOverviewQueueError(RuntimeError):
    """Raised when Project overview desired state cannot be queued safely."""


def _require_directory(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise ProjectOverviewQueueError(f"request directory does not exist: {path}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ProjectOverviewQueueError("request directory must be a non-symlink directory")


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise ProjectOverviewQueueError("short write while persisting overview request")
        view = view[written:]


def enqueue_overview(request_dir: Path, proposal: ProjectOverviewProposal) -> tuple[Path, str]:
    _require_directory(request_dir)
    target = request_dir / f"{proposal.project_key}.github-overview.json"
    try:
        info = target.lstat()
    except FileNotFoundError:
        info = None
    if info is not None:
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ProjectOverviewQueueError("existing overview request is not a regular file")
        try:
            if target.read_bytes() == proposal.canonical_bytes:
                return target, "already_queued"
        except OSError as exc:
            raise ProjectOverviewQueueError("cannot read existing overview request") from exc

    tmp = request_dir / f".{target.name}.{os.getpid()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    fd: int | None = None
    try:
        fd = os.open(tmp, flags, 0o640)
        _write_all(fd, proposal.canonical_bytes)
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.replace(tmp, target)
        dir_fd = os.open(request_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as exc:
        raise ProjectOverviewQueueError("cannot persist overview request") from exc
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    return target, "updated" if info is not None else "queued"


def overview_items_from_rows(
    repository: str,
    issue_rows: Sequence[dict[str, object]],
    pull_rows: Sequence[dict[str, object]],
) -> tuple[tuple[OverviewItem, ...], tuple[OverviewItem, ...]]:
    issues: list[OverviewItem] = []
    for row in issue_rows:
        if "pull_request" in row:
            continue
        number = row.get("number")
        title = row.get("title")
        if type(number) is not int or not isinstance(title, str) or not title.strip():
            raise ProjectOverviewQueueError(f"invalid open Issue payload for {repository}")
        issues.append(OverviewItem(number=number, title=title.strip()))

    pulls: list[OverviewItem] = []
    for row in pull_rows:
        number = row.get("number")
        title = row.get("title")
        draft = row.get("draft", False)
        if (
            type(number) is not int
            or not isinstance(title, str)
            or not title.strip()
            or type(draft) is not bool
        ):
            raise ProjectOverviewQueueError(f"invalid open Pull Request payload for {repository}")
        pulls.append(OverviewItem(number=number, title=title.strip(), draft=draft))

    return tuple(sorted(issues)), tuple(sorted(pulls))


def _collect_repository(
    client: GitHubClient,
    repository: str,
) -> tuple[tuple[OverviewItem, ...], tuple[OverviewItem, ...]]:
    repo_path = client._repo_path(repository)
    issue_rows = client._paged(f"/repos/{repo_path}/issues?state=open")
    pull_rows = client._paged(f"/repos/{repo_path}/pulls?state=open")
    return overview_items_from_rows(repository, issue_rows, pull_rows)


def prune_stale_overviews(request_dir: Path, active_names: set[str]) -> int:
    removed = 0
    for path in request_dir.glob("*.github-overview.json"):
        if path.name in active_names:
            continue
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ProjectOverviewQueueError("stale overview request path is not a regular file")
        path.unlink()
        removed += 1
    if removed:
        dir_fd = os.open(request_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    return removed


def _assert_unique_status_path(
    proposal: ProjectOverviewProposal,
    owners: dict[str, str],
) -> None:
    previous = owners.get(proposal.status_path)
    if previous is not None and previous != proposal.project_path:
        raise ProjectOverviewQueueError(
            f"multiple watched Projects target the same Status.md: {previous} and {proposal.project_path}"
        )
    owners[proposal.status_path] = proposal.project_path


def run_and_enqueue(
    config: watcher.WatcherConfig,
    *,
    request_dir: Path,
    client: GitHubClient | None = None,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    """Standalone overview collector, primarily for diagnostics and manual use.

    Production uses ``obsidian-github-project-watch-enqueue`` so the overview is
    derived from the exact Issue/PR rows already fetched by the status watcher.
    """
    _require_directory(request_dir)
    projects, warnings = watcher.scan_projects(config.vault_root, config.project_folder)
    for warning in warnings:
        print(
            json.dumps(
                {"event": "project-overview-warning", "message": warning},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=stderr,
        )

    api = client or GitHubClient(
        api_base=config.github_api_base,
        token=os.environ.get(config.github_token_env),
        timeout_seconds=config.request_timeout_seconds,
    )
    cache: dict[str, tuple[tuple[OverviewItem, ...], tuple[OverviewItem, ...]]] = {}
    active_names: set[str] = set()
    status_path_owners: dict[str, str] = {}
    failures = 0

    for project in projects:
        try:
            if project.repository not in cache:
                cache[project.repository] = _collect_repository(api, project.repository)
            issues, pulls = cache[project.repository]
            proposal = make_overview_proposal(
                project_path=project.path,
                repository=project.repository,
                issues=issues,
                pull_requests=pulls,
            )
            _assert_unique_status_path(proposal, status_path_owners)
            path, result = enqueue_overview(request_dir, proposal)
            active_names.add(path.name)
            print(
                json.dumps(
                    {
                        "event": "project-overview-enqueued",
                        "project": proposal.project_path,
                        "repository": proposal.repository,
                        "proposal_sha256": proposal.sha256,
                        "queue_result": result,
                        "request": path.name,
                        "issues": len(proposal.issues),
                        "pull_requests": len(proposal.pull_requests),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                file=stdout,
            )
        except (OSError, ProjectOverviewError, ProjectOverviewQueueError, watcher.GitHubProjectWatcherError) as exc:
            failures += 1
            print(
                json.dumps(
                    {
                        "event": "project-overview-enqueue-error",
                        "project": project.path,
                        "message": str(exc),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                file=stderr,
            )

    if failures:
        return 1
    try:
        removed = prune_stale_overviews(request_dir, active_names)
    except (OSError, ProjectOverviewQueueError) as exc:
        print(
            json.dumps(
                {"event": "project-overview-prune-error", "message": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=stderr,
        )
        return 1
    if removed:
        print(
            json.dumps(
                {"event": "project-overview-pruned", "count": removed},
                sort_keys=True,
            ),
            file=stdout,
        )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="obsidian-github-project-overview-enqueue",
        description="Queue desired Status.md overviews for GitHub-backed Projects.",
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--request-dir", type=Path, required=True)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        config = watcher.load_config(args.config)
        return run_and_enqueue(config, request_dir=args.request_dir)
    except (
        OSError,
        ProjectOverviewError,
        ProjectOverviewQueueError,
        watcher.GitHubProjectWatcherError,
    ) as exc:
        print(f"github-project-overview-queue: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
