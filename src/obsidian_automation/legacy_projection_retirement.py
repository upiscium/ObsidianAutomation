from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from .artifact_lifecycle import (
    ArtifactLifecycleError,
    _canonical_json_bytes,
    _decode_json_object,
    _read_exact_file,
    _require_safe_directory,
    _store_immutable,
    _utc_now,
    sha256_bytes,
)
from .human_projection import (
    LEGACY_PROJECTION_ROOT,
    PROJECTION_ROOT,
    REQUEST_STAGE,
    RESULT_STAGE,
    ROLE_NAMES,
    parse_request,
    parse_result,
    projection_root_from_target_path,
)
from .human_projection_cleanup import (
    CLEANUP_REQUEST_SUFFIX,
    CLEANUP_RESULT_SUFFIX,
    TERMINAL_CLEANUP_REQUEST_SUFFIX,
    parse_cleanup_request,
    parse_cleanup_result,
    parse_terminal_cleanup_request,
)
from .production_io import ProductionIOError, canonical_io_lock


RECORD_VERSION = 1
ARCHIVE_STAGE = "18-Human-Projection-History"
ARCHIVE_ROOT = LEGACY_PROJECTION_ROOT
INTENT_NAME = "retirement-intent.json"
COMPLETED_NAME = "retirement-completed.json"


class LegacyProjectionRetirementError(ArtifactLifecycleError):
    """Raised when legacy projection state cannot be retired safely."""


@dataclass(frozen=True)
class RetirementEntry:
    kind: str
    source: str
    destination: str
    sha256: str

    def to_object(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "source": self.source,
            "destination": self.destination,
            "sha256": self.sha256,
        }


def _regular_file(path: Path, *, label: str) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise LegacyProjectionRetirementError(f"{label} is missing: {path}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise LegacyProjectionRetirementError(f"{label} is not a safe regular file: {path}")


def _relative(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError as exc:
        raise LegacyProjectionRetirementError(
            f"retirement path escapes AI state root: {path}"
        ) from exc


def _archive_root(root: Path) -> Path:
    return root / ARCHIVE_STAGE / ARCHIVE_ROOT


def _archive_destination(root: Path, source: Path) -> Path:
    return _archive_root(root) / source.relative_to(root)


def _ensure_private_directory(root: Path, path: Path) -> None:
    root = root.absolute()
    path = path.absolute()
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise LegacyProjectionRetirementError(
            "archive directory escapes AI state root"
        ) from exc

    cursor = root
    _require_safe_directory(cursor, create=False)
    for component in relative.parts:
        cursor = cursor / component
        try:
            info = cursor.lstat()
        except FileNotFoundError:
            try:
                os.mkdir(cursor, 0o700)
            except OSError as exc:
                raise LegacyProjectionRetirementError(
                    f"cannot create archive directory: {cursor}"
                ) from exc
            info = cursor.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise LegacyProjectionRetirementError(
                f"archive path is not a safe directory: {cursor}"
            )


def _entry(root: Path, source: Path, *, kind: str) -> RetirementEntry:
    _regular_file(source, label=kind)
    data = _read_exact_file(source)
    destination = _archive_destination(root, source)
    return RetirementEntry(
        kind=kind,
        source=_relative(root, source),
        destination=_relative(root, destination),
        sha256=sha256_bytes(data),
    )


def _add_entry(
    entries: dict[str, RetirementEntry],
    entry: RetirementEntry,
) -> None:
    previous = entries.get(entry.source)
    if previous is not None and previous != entry:
        raise LegacyProjectionRetirementError(
            f"retirement plan contains conflicting entries for {entry.source}"
        )
    entries[entry.source] = entry


def _request_digest(path: Path, suffix: str) -> str:
    if not path.name.endswith(suffix):
        raise LegacyProjectionRetirementError(
            f"retirement request filename has unexpected suffix: {path.name}"
        )
    digest = path.name[: -len(suffix)]
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise LegacyProjectionRetirementError(
            f"retirement request filename has invalid digest: {path.name}"
        )
    return digest


def _projection_requests(root: Path) -> Iterable[tuple[str, Path]]:
    request_root = root / REQUEST_STAGE
    _require_safe_directory(request_root, create=False)
    for role in ROLE_NAMES:
        directory = request_root / role
        _require_safe_directory(directory, create=False)
        for path in sorted(directory.iterdir(), key=lambda item: item.name):
            if path.name.startswith(".") or not path.name.endswith(".projection.json"):
                continue
            _regular_file(path, label="projection request")
            yield role, path


def _projection_result_path(root: Path, digest: str) -> Path:
    return root / RESULT_STAGE / f"{digest}.projection-result.json"


def _cleanup_result_path(root: Path, digest: str) -> Path:
    return root / RESULT_STAGE / f"{digest}{CLEANUP_RESULT_SUFFIX}"


def _is_legacy_target(target_path: str) -> bool:
    return projection_root_from_target_path(target_path) == LEGACY_PROJECTION_ROOT


def _require_resolved_projection_pair(
    root: Path,
    request_path: Path,
    *,
    role: str,
    entries: dict[str, RetirementEntry],
) -> None:
    digest = _request_digest(request_path, ".projection.json")
    request = parse_request(_read_exact_file(request_path))
    if not _is_legacy_target(request.target_path):
        return

    result_path = _projection_result_path(root, digest)
    _regular_file(result_path, label="legacy projection result")
    result = parse_result(_read_exact_file(result_path))
    if (
        result.request_sha256 != digest
        or result.target_path != request.target_path
        or result.content_sha256 != request.content_sha256
        or result.result not in {"created", "already_matching"}
    ):
        raise LegacyProjectionRetirementError(
            f"legacy projection request is not terminally resolved: {request_path}"
        )

    _add_entry(
        entries,
        _entry(root, request_path, kind=f"projection-request:{role}"),
    )
    _add_entry(
        entries,
        _entry(root, result_path, kind="projection-result"),
    )


def _legacy_review_cleanup_root(root: Path, request_path: Path) -> bool:
    request = parse_cleanup_request(_read_exact_file(request_path))
    review_path = (
        root
        / REQUEST_STAGE
        / "evaluator"
        / f"{request.review_projection_request_sha256}.projection.json"
    )
    _regular_file(review_path, label="review projection referenced by cleanup")
    review = parse_request(_read_exact_file(review_path))
    return _is_legacy_target(review.target_path)


def _legacy_terminal_cleanup_root(root: Path, request_path: Path) -> bool:
    request = parse_terminal_cleanup_request(_read_exact_file(request_path))
    completed_path = (
        root
        / REQUEST_STAGE
        / "executor"
        / f"{request.completed_projection_request_sha256}.projection.json"
    )
    _regular_file(
        completed_path,
        label="completed projection referenced by terminal cleanup",
    )
    completed = parse_request(_read_exact_file(completed_path))
    return _is_legacy_target(completed.target_path)


def _require_resolved_cleanup_pair(
    root: Path,
    request_path: Path,
    *,
    terminal: bool,
    entries: dict[str, RetirementEntry],
) -> None:
    suffix = TERMINAL_CLEANUP_REQUEST_SUFFIX if terminal else CLEANUP_REQUEST_SUFFIX
    digest = _request_digest(request_path, suffix)
    legacy = (
        _legacy_terminal_cleanup_root(root, request_path)
        if terminal
        else _legacy_review_cleanup_root(root, request_path)
    )
    if not legacy:
        return

    result_path = _cleanup_result_path(root, digest)
    _regular_file(result_path, label="legacy cleanup result")
    result = parse_cleanup_result(_read_exact_file(result_path))
    if result.cleanup_request_sha256 != digest:
        raise LegacyProjectionRetirementError(
            f"legacy cleanup result is bound to another request: {result_path}"
        )
    if not result.targets or any(
        not item.target_path.startswith(f"{LEGACY_PROJECTION_ROOT}/")
        for item in result.targets
    ):
        raise LegacyProjectionRetirementError(
            f"legacy cleanup result contains a non-legacy target: {result_path}"
        )

    _add_entry(
        entries,
        _entry(
            root,
            request_path,
            kind="terminal-cleanup-request" if terminal else "cleanup-request",
        ),
    )
    _add_entry(
        entries,
        _entry(root, result_path, kind="cleanup-result"),
    )


def build_retirement_plan(ai_root: Path) -> tuple[RetirementEntry, ...]:
    root = ai_root.absolute()
    _require_safe_directory(root, create=False)
    _require_safe_directory(root / RESULT_STAGE, create=False)

    entries: dict[str, RetirementEntry] = {}
    for role, request_path in _projection_requests(root):
        _require_resolved_projection_pair(
            root,
            request_path,
            role=role,
            entries=entries,
        )

    reviewer = root / REQUEST_STAGE / "reviewer"
    executor = root / REQUEST_STAGE / "executor"
    for path in sorted(reviewer.glob(f"*{CLEANUP_REQUEST_SUFFIX}")):
        _regular_file(path, label="cleanup request")
        _require_resolved_cleanup_pair(
            root,
            path,
            terminal=False,
            entries=entries,
        )
    for path in sorted(executor.glob(f"*{TERMINAL_CLEANUP_REQUEST_SUFFIX}")):
        _regular_file(path, label="terminal cleanup request")
        _require_resolved_cleanup_pair(
            root,
            path,
            terminal=True,
            entries=entries,
        )

    return tuple(entries[key] for key in sorted(entries))


def _manifest_bytes(entries: Sequence[RetirementEntry]) -> bytes:
    return _canonical_json_bytes(
        {
            "record_version": RECORD_VERSION,
            "event": "legacy-03-ai-projection-retirement",
            "legacy_root": LEGACY_PROJECTION_ROOT,
            "canonical_root": PROJECTION_ROOT,
            "entries": [entry.to_object() for entry in entries],
        }
    )


def _load_manifest(path: Path) -> tuple[RetirementEntry, ...]:
    value = _decode_json_object(
        _read_exact_file(path),
        label="legacy projection retirement manifest",
    )
    required = {
        "record_version",
        "event",
        "legacy_root",
        "canonical_root",
        "entries",
    }
    if (
        set(value) != required
        or value["record_version"] != RECORD_VERSION
        or value["event"] != "legacy-03-ai-projection-retirement"
        or value["legacy_root"] != LEGACY_PROJECTION_ROOT
        or value["canonical_root"] != PROJECTION_ROOT
        or not isinstance(value["entries"], list)
    ):
        raise LegacyProjectionRetirementError(
            "legacy projection retirement manifest does not match contract"
        )

    result: list[RetirementEntry] = []
    seen: set[str] = set()
    archive_prefix = f"{ARCHIVE_STAGE}/{ARCHIVE_ROOT}/"
    for raw in value["entries"]:
        if not isinstance(raw, dict) or set(raw) != {
            "kind",
            "source",
            "destination",
            "sha256",
        }:
            raise LegacyProjectionRetirementError(
                "legacy projection retirement manifest entry is invalid"
            )
        kind = raw["kind"]
        source = raw["source"]
        destination = raw["destination"]
        digest = raw["sha256"]
        if (
            not isinstance(kind, str)
            or not kind
            or not isinstance(source, str)
            or not source
            or source.startswith("/")
            or ".." in Path(source).parts
            or not isinstance(destination, str)
            or not destination.startswith(archive_prefix)
            or ".." in Path(destination).parts
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(ch not in "0123456789abcdef" for ch in digest)
            or source in seen
        ):
            raise LegacyProjectionRetirementError(
                "legacy projection retirement manifest entry is unsafe"
            )
        seen.add(source)
        result.append(
            RetirementEntry(
                kind=kind,
                source=source,
                destination=destination,
                sha256=digest,
            )
        )
    return tuple(result)


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _verify_bytes(path: Path, expected_sha256: str, *, label: str) -> None:
    _regular_file(path, label=label)
    if sha256_bytes(_read_exact_file(path)) != expected_sha256:
        raise LegacyProjectionRetirementError(
            f"{label} bytes do not match retirement manifest: {path}"
        )


def _replay_entry(root: Path, entry: RetirementEntry) -> str:
    source = root / entry.source
    destination = root / entry.destination
    source_exists = os.path.lexists(source)
    destination_exists = os.path.lexists(destination)

    if source_exists and destination_exists:
        raise LegacyProjectionRetirementError(
            f"retirement source and destination both exist: {entry.source}"
        )
    if destination_exists:
        _verify_bytes(
            destination,
            entry.sha256,
            label="archived legacy artifact",
        )
        return "already_archived"
    if not source_exists:
        raise LegacyProjectionRetirementError(
            f"retirement artifact is missing from source and archive: {entry.source}"
        )

    _verify_bytes(source, entry.sha256, label="legacy retirement source")
    _ensure_private_directory(root, destination.parent)
    try:
        os.replace(source, destination)
    except OSError as exc:
        raise LegacyProjectionRetirementError(
            f"cannot archive legacy projection artifact: {entry.source}"
        ) from exc
    _fsync_directory(source.parent)
    if destination.parent != source.parent:
        _fsync_directory(destination.parent)
    return "archived"


def _completion_bytes(intent_sha256: str, entries: int) -> bytes:
    return _canonical_json_bytes(
        {
            "record_version": RECORD_VERSION,
            "event": "legacy-03-ai-projection-retirement-completed",
            "intent_sha256": intent_sha256,
            "archived_entries": entries,
            "completed_at": _utc_now(),
        }
    )


def retirement_status(ai_root: Path) -> dict[str, object]:
    entries = build_retirement_plan(ai_root)
    kinds: dict[str, int] = {}
    for entry in entries:
        kinds[entry.kind] = kinds.get(entry.kind, 0) + 1
    return {
        "event": "legacy-03-ai-projection-retirement-check",
        "status": "ready",
        "legacy_entries": len(entries),
        "kinds": dict(sorted(kinds.items())),
    }


def apply_retirement(
    ai_root: Path,
    *,
    require_root: bool = True,
) -> dict[str, object]:
    if require_root and os.geteuid() != 0:
        raise LegacyProjectionRetirementError(
            "legacy projection retirement requires root"
        )

    root = ai_root.absolute()
    _require_safe_directory(root, create=False)
    with canonical_io_lock(root):
        archive = _archive_root(root)
        _ensure_private_directory(root, archive)
        intent_path = archive / INTENT_NAME
        completed_path = archive / COMPLETED_NAME

        if os.path.lexists(intent_path):
            entries = _load_manifest(intent_path)
        else:
            entries = build_retirement_plan(root)
            if not entries:
                return {
                    "event": "legacy-03-ai-projection-retirement",
                    "status": "nothing_to_do",
                    "archived": 0,
                    "already_archived": 0,
                }
            _store_immutable(intent_path, _manifest_bytes(entries))

        intent_sha = sha256_bytes(_read_exact_file(intent_path))
        if os.path.lexists(completed_path):
            for entry in entries:
                _verify_bytes(
                    root / entry.destination,
                    entry.sha256,
                    label="completed legacy archive artifact",
                )
            return {
                "event": "legacy-03-ai-projection-retirement",
                "status": "already_completed",
                "archived": 0,
                "already_archived": len(entries),
                "intent_sha256": intent_sha,
            }

        archived = 0
        already_archived = 0
        for entry in entries:
            outcome = _replay_entry(root, entry)
            if outcome == "archived":
                archived += 1
            else:
                already_archived += 1

        remaining = build_retirement_plan(root)
        if remaining:
            raise LegacyProjectionRetirementError(
                "legacy projection active queue still contains retirement candidates"
            )

        _store_immutable(
            completed_path,
            _completion_bytes(intent_sha, len(entries)),
        )
        return {
            "event": "legacy-03-ai-projection-retirement",
            "status": "completed",
            "archived": archived,
            "already_archived": already_archived,
            "intent_sha256": intent_sha,
        }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="obsidian-ai-retire-legacy-projections",
        description=(
            "Retire fully resolved 03-AI projection artifacts from active runtime "
            "queues while preserving their exact bytes in private history."
        ),
    )
    parser.add_argument(
        "--ai-root",
        type=Path,
        default=Path("/var/lib/obsidian-ai/state"),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        result = (
            apply_retirement(args.ai_root)
            if args.apply
            else retirement_status(args.ai_root)
        )
    except (
        ArtifactLifecycleError,
        LegacyProjectionRetirementError,
        ProductionIOError,
        OSError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
