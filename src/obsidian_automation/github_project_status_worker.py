from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, TextIO

from .github_project_status_mutation import (
    ProjectStatusMutationConflict,
    ProjectStatusMutationError,
    ProjectStatusTransportResult,
    apply_project_status,
    parse_watcher_proposal,
    persist_transport_result,
)
from .production_io import ProductionIOError, canonical_io_lock
from .webdav_create import WebDAVCreateError, _read_password


class ProjectStatusWorkerError(RuntimeError):
    """Raised when the local writer queue cannot be processed safely."""


def _canonical_json_bytes(payload: Mapping[str, object]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _require_directory(path: Path, *, label: str) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise ProjectStatusWorkerError(f"{label} does not exist: {path}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ProjectStatusWorkerError(f"{label} must be a non-symlink directory: {path}")


def _read_regular(path: Path, *, label: str) -> bytes:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise ProjectStatusWorkerError(f"{label} does not exist: {path}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ProjectStatusWorkerError(f"{label} must be a regular non-symlink file")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ProjectStatusWorkerError(f"cannot read {label}: {path}") from exc


def _write_exclusive(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = os.open(path, flags, 0o640)
    except FileExistsError:
        return
    except OSError as exc:
        raise ProjectStatusWorkerError(f"cannot create result artifact: {path}") from exc
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise ProjectStatusWorkerError("short write while persisting writer artifact")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _existing_artifact_matches(path: Path, proposal_sha256: str) -> bool:
    if not path.exists() and not path.is_symlink():
        return False
    data = _read_regular(path, label="existing writer artifact")
    try:
        value = json.loads(data)
    except json.JSONDecodeError as exc:
        raise ProjectStatusWorkerError("existing writer artifact is invalid JSON") from exc
    if not isinstance(value, dict) or value.get("proposal_sha256") != proposal_sha256:
        raise ProjectStatusWorkerError("existing writer artifact belongs to another proposal")
    return True


def _persist_conflict(result_dir: Path, proposal_sha256: str, *, project: str) -> Path:
    path = result_dir / f"{proposal_sha256}.github-status.rejection.json"
    payload = _canonical_json_bytes(
        {
            "record_version": 1,
            "stage": "github_project_status_transport",
            "proposal_sha256": proposal_sha256,
            "project": project,
            "outcome": "rejected_conflict",
            "completed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
    )
    if path.exists() or path.is_symlink():
        if not _existing_artifact_matches(path, proposal_sha256):
            raise ProjectStatusWorkerError("conflict artifact hash mismatch")
        return path
    _write_exclusive(path, payload)
    return path


def _request_files(request_dir: Path) -> list[Path]:
    _require_directory(request_dir, label="request directory")
    return sorted(request_dir.glob("*.github-status.json"))


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

    requests = _request_files(request_dir)
    if not requests:
        print(
            json.dumps({"event": "github-project-status-worker", "status": "idle"}, sort_keys=True),
            file=stdout,
        )
        return 0

    password: str | None = None
    failures = 0

    for request_path in requests:
        try:
            proposal = parse_watcher_proposal(
                _read_regular(request_path, label="Project status request")
            )
            expected_name = f"{proposal.sha256}.github-status.json"
            if request_path.name != expected_name:
                raise ProjectStatusWorkerError(
                    "request filename does not match canonical proposal SHA-256"
                )

            result_path = result_dir / (
                f"{proposal.sha256}.github-status.transport-result.json"
            )
            rejection_path = result_dir / f"{proposal.sha256}.github-status.rejection.json"

            if _existing_artifact_matches(result_path, proposal.sha256):
                print(
                    json.dumps(
                        {
                            "event": "github-project-status-worker",
                            "project": proposal.project_path,
                            "proposal_sha256": proposal.sha256,
                            "status": "already_processed",
                            "result": result_path.name,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    file=stdout,
                )
                continue
            if _existing_artifact_matches(rejection_path, proposal.sha256):
                print(
                    json.dumps(
                        {
                            "event": "github-project-status-worker",
                            "project": proposal.project_path,
                            "proposal_sha256": proposal.sha256,
                            "status": "already_rejected",
                            "result": rejection_path.name,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    file=stdout,
                )
                continue

            if password is None:
                password = _read_password(password_file)

            try:
                with canonical_io_lock(state_root):
                    result = apply_project_status(
                        proposal,
                        base_url=base_url,
                        username=username,
                        password=password,
                        timeout=timeout,
                    )
                    data = persist_transport_result(result_path, result)
            except ProjectStatusMutationConflict:
                rejection = _persist_conflict(
                    result_dir,
                    proposal.sha256,
                    project=proposal.project_path,
                )
                print(
                    json.dumps(
                        {
                            "event": "github-project-status-worker",
                            "project": proposal.project_path,
                            "proposal_sha256": proposal.sha256,
                            "status": "rejected_conflict",
                            "result": rejection.name,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    file=stdout,
                )
                continue

            payload = json.loads(data)
            print(
                json.dumps(
                    {
                        "event": "github-project-status-worker",
                        "project": proposal.project_path,
                        "proposal_sha256": proposal.sha256,
                        "status": "completed",
                        "outcome": payload.get("outcome"),
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
            ProjectStatusMutationError,
            ProjectStatusWorkerError,
            WebDAVCreateError,
            json.JSONDecodeError,
        ) as exc:
            failures += 1
            print(
                json.dumps(
                    {
                        "event": "github-project-status-worker-error",
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
        prog="obsidian-github-project-status-worker",
        description="Process locally queued GitHub Project status proposals with a dedicated writer identity.",
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
    except (OSError, ProjectStatusWorkerError) as exc:
        print(f"github-project-status-worker: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
