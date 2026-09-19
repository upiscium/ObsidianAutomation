from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, TextIO

from .github_project_status_mutation import (
    ProjectStatusMutationError,
    parse_watcher_proposal,
)


_REQUEST_RE = re.compile(r"^(?P<sha>[0-9a-f]{64})\.github-status\.json$")
_RESULT_SUFFIX = ".github-status.transport-result.json"
_REJECTION_SUFFIX = ".github-status.rejection.json"
_RESULT_OUTCOMES = frozenset({"applied", "recovered", "already_desired"})
_REJECTION_OUTCOMES = frozenset({"rejected_conflict", "rejected_transport"})
MAX_ARTIFACT_BYTES = 1024 * 1024


class ProjectStatusCompactorError(RuntimeError):
    """Raised when a queued status request cannot be compacted safely."""


@dataclass(frozen=True)
class CompactionSummary:
    removed: int
    kept_pending: int
    failures: int

    @property
    def status(self) -> str:
        if self.failures:
            return "failed"
        if self.removed == 0 and self.kept_pending == 0:
            return "idle"
        return "completed"


def _require_directory(path: Path, *, label: str) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise ProjectStatusCompactorError(f"{label} does not exist") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ProjectStatusCompactorError(f"{label} must be a non-symlink directory")


def _read_regular(path: Path, *, label: str) -> tuple[bytes, os.stat_result]:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise ProjectStatusCompactorError(f"{label} does not exist") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ProjectStatusCompactorError(f"{label} must be a regular non-symlink file")
    if info.st_size > MAX_ARTIFACT_BYTES:
        raise ProjectStatusCompactorError(f"{label} exceeds maximum supported size")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ProjectStatusCompactorError(f"cannot read {label}") from exc
    return data, info


def _decode_object(data: bytes, *, label: str) -> dict[str, object]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProjectStatusCompactorError(f"{label} is not UTF-8") from exc

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ProjectStatusCompactorError(f"{label} has duplicate JSON property")
            value[key] = item
        return value

    try:
        value = json.loads(text, object_pairs_hook=reject_duplicates)
    except ProjectStatusCompactorError:
        raise
    except json.JSONDecodeError as exc:
        raise ProjectStatusCompactorError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ProjectStatusCompactorError(f"{label} must be a JSON object")
    return value


def _validate_terminal_artifact(
    path: Path,
    *,
    proposal_sha256: str,
    rejection: bool,
) -> None:
    data, _ = _read_regular(path, label="terminal artifact")
    value = _decode_object(data, label="terminal artifact")

    if value.get("record_version") != 1:
        raise ProjectStatusCompactorError("terminal artifact record_version is not supported")
    if value.get("stage") != "github_project_status_transport":
        raise ProjectStatusCompactorError("terminal artifact stage does not match status transport")
    if value.get("proposal_sha256") != proposal_sha256:
        raise ProjectStatusCompactorError("terminal artifact proposal SHA does not match request")

    outcome = value.get("outcome")
    allowed = _REJECTION_OUTCOMES if rejection else _RESULT_OUTCOMES
    if outcome not in allowed:
        raise ProjectStatusCompactorError("terminal artifact outcome is not recognized")


def _terminal_artifact(
    result_dir: Path,
    proposal_sha256: str,
) -> Path | None:
    result_path = result_dir / f"{proposal_sha256}{_RESULT_SUFFIX}"
    rejection_path = result_dir / f"{proposal_sha256}{_REJECTION_SUFFIX}"

    result_exists = result_path.exists() or result_path.is_symlink()
    rejection_exists = rejection_path.exists() or rejection_path.is_symlink()

    if result_exists and rejection_exists:
        raise ProjectStatusCompactorError(
            "both transport result and rejection exist for the same proposal"
        )
    if not result_exists and not rejection_exists:
        return None

    if result_exists:
        _validate_terminal_artifact(
            result_path,
            proposal_sha256=proposal_sha256,
            rejection=False,
        )
        return result_path

    _validate_terminal_artifact(
        rejection_path,
        proposal_sha256=proposal_sha256,
        rejection=True,
    )
    return rejection_path


def _validate_request(path: Path) -> tuple[str, bytes, os.stat_result]:
    match = _REQUEST_RE.fullmatch(path.name)
    if match is None:
        raise ProjectStatusCompactorError("status request filename is not content-addressed")
    expected_sha = match.group("sha")

    data, info = _read_regular(path, label="status request")
    try:
        proposal = parse_watcher_proposal(data)
    except ProjectStatusMutationError as exc:
        raise ProjectStatusCompactorError("status request does not satisfy watcher contract") from exc

    if proposal.sha256 != expected_sha:
        raise ProjectStatusCompactorError(
            "status request filename does not match canonical proposal SHA"
        )
    return expected_sha, data, info


def _same_file(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        before.st_dev == after.st_dev
        and before.st_ino == after.st_ino
        and before.st_size == after.st_size
        and before.st_mtime_ns == after.st_mtime_ns
    )


def _unlink_verified_request(
    path: Path,
    *,
    expected_bytes: bytes,
    expected_stat: os.stat_result,
) -> bool:
    try:
        current_bytes, current_stat = _read_regular(path, label="status request before unlink")
    except ProjectStatusCompactorError as exc:
        if not path.exists() and not path.is_symlink():
            return False
        raise exc

    if not _same_file(expected_stat, current_stat):
        raise ProjectStatusCompactorError("status request changed before compaction")
    if current_bytes != expected_bytes:
        raise ProjectStatusCompactorError("status request bytes changed before compaction")

    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ProjectStatusCompactorError("cannot remove terminal status request") from exc
    return True


def compact_status_requests(
    *,
    request_dir: Path,
    result_dir: Path,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    _require_directory(request_dir, label="request directory")
    _require_directory(result_dir, label="result directory")

    requests = sorted(request_dir.glob("*.github-status.json"))
    if not requests:
        print(
            json.dumps(
                {
                    "event": "github-project-status-compactor",
                    "status": "idle",
                    "removed": 0,
                    "kept_pending": 0,
                    "failures": 0,
                },
                sort_keys=True,
            ),
            file=stdout,
        )
        return 0

    removed = 0
    kept_pending = 0
    failures = 0

    for request_path in requests:
        try:
            proposal_sha256, request_bytes, request_stat = _validate_request(request_path)
            terminal = _terminal_artifact(result_dir, proposal_sha256)
            if terminal is None:
                kept_pending += 1
                continue

            if _unlink_verified_request(
                request_path,
                expected_bytes=request_bytes,
                expected_stat=request_stat,
            ):
                removed += 1
        except (OSError, ProjectStatusCompactorError) as exc:
            failures += 1
            print(
                json.dumps(
                    {
                        "event": "github-project-status-compactor-error",
                        "request": request_path.name,
                        "message": str(exc),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                file=stderr,
            )

    if removed:
        dir_fd = os.open(request_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    summary = CompactionSummary(
        removed=removed,
        kept_pending=kept_pending,
        failures=failures,
    )
    print(
        json.dumps(
            {
                "event": "github-project-status-compactor",
                "status": summary.status,
                "removed": summary.removed,
                "kept_pending": summary.kept_pending,
                "failures": summary.failures,
            },
            sort_keys=True,
        ),
        file=stdout,
    )
    return 1 if failures else 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="obsidian-github-project-status-compact",
        description=(
            "Remove terminal content-addressed GitHub Project status requests after "
            "validating their transport result or rejection."
        ),
    )
    parser.add_argument("--request-dir", type=Path, required=True)
    parser.add_argument("--result-dir", type=Path, required=True)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        return compact_status_requests(
            request_dir=args.request_dir,
            result_dir=args.result_dir,
        )
    except (OSError, ProjectStatusCompactorError) as exc:
        print(f"github-project-status-compactor: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
