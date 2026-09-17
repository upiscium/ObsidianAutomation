from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path
from typing import Iterable, TextIO

from .github_project_overview import (
    ProjectOverviewConflict,
    ProjectOverviewError,
    apply_project_overview,
    parse_overview_proposal,
)
from .production_io import ProductionIOError, canonical_io_lock
from .webdav_create import WebDAVCreateError, _read_password


class ProjectOverviewWorkerError(RuntimeError):
    """Raised when queued Project overviews cannot be processed safely."""


def _require_directory(path: Path, *, label: str) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise ProjectOverviewWorkerError(f"{label} does not exist: {path}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ProjectOverviewWorkerError(f"{label} must be a non-symlink directory")


def _read_regular(path: Path) -> bytes:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ProjectOverviewWorkerError("overview request must be a regular non-symlink file")
    return path.read_bytes()


def _replace_atomic(path: Path, data: bytes) -> None:
    tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    fd: int | None = None
    try:
        fd = os.open(tmp, flags, 0o640)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise ProjectOverviewWorkerError("short write while persisting overview result")
            view = view[written:]
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.replace(tmp, path)
        dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as exc:
        raise ProjectOverviewWorkerError("cannot persist overview transport result") from exc
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


def run_worker(
    *,
    request_dir: Path,
    result_dir: Path,
    state_root: Path,
    base_url: str,
    username: str,
    password_file: Path,
    timeout: float = 30.0,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    _require_directory(request_dir, label="request directory")
    _require_directory(result_dir, label="result directory")
    _require_directory(state_root, label="state root")
    requests = sorted(request_dir.glob("*.github-overview.json"))
    if not requests:
        print(
            json.dumps({"event": "github-project-overview-worker", "status": "idle"}, sort_keys=True),
            file=stdout,
        )
        return 0

    password = _read_password(password_file)
    failures = 0
    for request_path in requests:
        try:
            proposal = parse_overview_proposal(_read_regular(request_path))
            expected_name = f"{proposal.project_key}.github-overview.json"
            if request_path.name != expected_name:
                raise ProjectOverviewWorkerError(
                    "overview request filename does not match Project path binding"
                )
            with canonical_io_lock(state_root):
                result = apply_project_overview(
                    proposal,
                    base_url=base_url,
                    username=username,
                    password=password,
                    timeout=timeout,
                )
            result_path = result_dir / f"{proposal.project_key}.github-overview.transport-result.json"
            _replace_atomic(result_path, result.to_json_bytes())
            print(
                json.dumps(
                    {
                        "event": "github-project-overview-worker",
                        "project": proposal.project_path,
                        "status_path": proposal.status_path,
                        "repository": proposal.repository,
                        "proposal_sha256": proposal.sha256,
                        "status": "completed",
                        "outcome": result.outcome,
                        "result": result_path.name,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                file=stdout,
            )
        except (
            OSError,
            ProductionIOError,
            ProjectOverviewConflict,
            ProjectOverviewError,
            ProjectOverviewWorkerError,
            WebDAVCreateError,
        ) as exc:
            failures += 1
            print(
                json.dumps(
                    {
                        "event": "github-project-overview-worker-error",
                        "request": request_path.name,
                        "message": str(exc),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                file=stderr,
            )
    return 1 if failures else 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="obsidian-github-project-overview-worker",
        description="Create or update Status.md for GitHub-backed Projects.",
    )
    parser.add_argument("--request-dir", type=Path, required=True)
    parser.add_argument("--result-dir", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--password-file", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=30.0)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        return run_worker(
            request_dir=args.request_dir,
            result_dir=args.result_dir,
            state_root=args.state_root,
            base_url=args.base_url,
            username=args.username,
            password_file=args.password_file,
            timeout=args.timeout,
        )
    except (OSError, ProjectOverviewWorkerError, WebDAVCreateError) as exc:
        print(f"github-project-overview-worker: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
