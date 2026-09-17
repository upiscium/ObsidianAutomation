from __future__ import annotations

import argparse
import io
import json
import os
import stat
import sys
from pathlib import Path
from typing import Iterable, TextIO

from . import github_project_watcher as watcher
from .github_project_status_mutation import (
    ProjectStatusMutationError,
    ProjectStatusProposal,
    parse_watcher_proposal,
)
from .github_project_watcher_runtime import GitHubClient


class ProjectStatusQueueError(RuntimeError):
    """Raised when a watcher proposal cannot be durably queued."""


def _require_directory(path: Path, *, label: str) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise ProjectStatusQueueError(f"{label} does not exist: {path}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ProjectStatusQueueError(f"{label} must be a non-symlink directory: {path}")


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise ProjectStatusQueueError("short write while queuing Project status proposal")
        view = view[written:]


def enqueue_proposal(request_dir: Path, proposal: ProjectStatusProposal) -> tuple[Path, str]:
    _require_directory(request_dir, label="request directory")
    target = request_dir / f"{proposal.sha256}.github-status.json"

    try:
        info = target.lstat()
    except FileNotFoundError:
        info = None

    if info is not None:
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ProjectStatusQueueError("existing request path is not a regular file")
        try:
            existing = target.read_bytes()
        except OSError as exc:
            raise ProjectStatusQueueError("cannot read existing queued proposal") from exc
        if existing != proposal.canonical_bytes:
            raise ProjectStatusQueueError("queued proposal hash path contains different bytes")
        return target, "already_queued"

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC

    fd: int | None = None
    try:
        fd = os.open(target, flags, 0o640)
        _write_all(fd, proposal.canonical_bytes)
        os.fsync(fd)
        os.close(fd)
        fd = None
        dir_fd = os.open(request_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except FileExistsError:
        return enqueue_proposal(request_dir, proposal)
    except OSError as exc:
        raise ProjectStatusQueueError("cannot create queued Project status proposal") from exc
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    return target, "queued"


def _enqueue_output_line(
    line: str,
    *,
    request_dir: Path,
    stdout: TextIO,
) -> None:
    print(line, file=stdout)
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        return
    if not isinstance(value, dict):
        return
    if value.get("event") != "project-status-observation":
        return
    if value.get("change") is not True or value.get("pending") is not True:
        return

    proposal = parse_watcher_proposal((line + "\n").encode("utf-8"))
    path, result = enqueue_proposal(request_dir, proposal)
    print(
        json.dumps(
            {
                "event": "project-status-enqueued",
                "proposal_sha256": proposal.sha256,
                "project": proposal.project_path,
                "repository": proposal.repository,
                "queue_result": result,
                "request": path.name,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        file=stdout,
    )


def run_and_enqueue(
    config: watcher.WatcherConfig,
    *,
    request_dir: Path,
    client: GitHubClient | None = None,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    _require_directory(request_dir, label="request directory")
    token = os.environ.get(config.github_token_env)
    api = client or GitHubClient(
        api_base=config.github_api_base,
        token=token,
        timeout_seconds=config.request_timeout_seconds,
    )
    capture = io.StringIO()
    rc = watcher.run_once(config, client=api, stdout=capture, stderr=stderr)

    queue_errors = 0
    for line in capture.getvalue().splitlines():
        if not line:
            continue
        try:
            _enqueue_output_line(line, request_dir=request_dir, stdout=stdout)
        except (OSError, ProjectStatusMutationError, ProjectStatusQueueError) as exc:
            queue_errors += 1
            print(
                json.dumps(
                    {
                        "event": "project-status-enqueue-error",
                        "message": str(exc),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                file=stderr,
            )

    if queue_errors:
        return 1
    return rc


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="obsidian-github-project-watch-enqueue",
        description="Observe GitHub-backed Projects and durably enqueue pending status proposals.",
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
        ProjectStatusMutationError,
        ProjectStatusQueueError,
        watcher.GitHubProjectWatcherError,
    ) as exc:
        print(f"github-project-status-queue: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
