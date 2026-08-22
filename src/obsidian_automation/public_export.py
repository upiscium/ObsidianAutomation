from __future__ import annotations

import argparse
import glob
import hashlib
import os
import re
import shutil
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence


class ExportError(RuntimeError):
    """Raised when an export request violates the configured safety policy."""


_ALWAYS_REPOSITORY_OWNED = (".git", ".git/**", "**/.git", "**/.git/**")


@dataclass(frozen=True)
class ExportConfig:
    include: tuple[str, ...]
    repository_owned: tuple[str, ...]
    strict_missing: bool = True
    exclude: tuple[str, ...] = ()


@dataclass(frozen=True)
class Change:
    action: str
    path: str


def _validate_pattern(pattern: str) -> None:
    if not pattern or pattern.startswith("/"):
        raise ExportError(f"unsafe path pattern: {pattern!r}")
    parts = PurePosixPath(pattern).parts
    if ".." in parts:
        raise ExportError(f"path traversal is not allowed: {pattern!r}")
    if any(part in ("", ".") for part in parts):
        raise ExportError(f"invalid path pattern: {pattern!r}")


def _compile_pattern(pattern: str) -> re.Pattern[str]:
    """Compile a small, slash-aware glob subset: *, ** and ?."""
    _validate_pattern(pattern)
    i = 0
    out: list[str] = ["^"]
    while i < len(pattern):
        char = pattern[i]
        if char == "*":
            if i + 1 < len(pattern) and pattern[i + 1] == "*":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(char))
        i += 1
    out.append("$")
    return re.compile("".join(out))


def _matches_any(path: str, patterns: Sequence[str]) -> bool:
    return any(_compile_pattern(pattern).fullmatch(path) for pattern in patterns)


def load_config(path: Path) -> ExportConfig:
    with path.open("rb") as handle:
        raw = tomllib.load(handle)

    version = raw.get("version")
    if version != 1:
        raise ExportError(f"unsupported config version: {version!r}")

    include = tuple(raw.get("include", ()))
    exclude = tuple(raw.get("exclude", ()))
    repository_owned = tuple(raw.get("repository_owned", ()))
    strict_missing = bool(raw.get("strict_missing", True))

    if not include:
        raise ExportError("config must contain at least one include pattern")

    for pattern in (*include, *exclude, *repository_owned):
        if not isinstance(pattern, str):
            raise ExportError("all path patterns must be strings")
        _validate_pattern(pattern)

    return ExportConfig(
        include=include,
        exclude=exclude,
        repository_owned=repository_owned,
        strict_missing=strict_missing,
    )


def _assert_no_symlink_components(root: Path, candidate: Path) -> None:
    root = root.resolve()
    candidate_abs = candidate.absolute()

    try:
        relative = candidate_abs.relative_to(root)
    except ValueError as exc:
        raise ExportError(f"path escapes root: {candidate}") from exc

    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ExportError(f"symlink is not allowed in managed path: {current}")


def _relative_posix(root: Path, path: Path) -> str:
    try:
        return path.absolute().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise ExportError(f"path escapes root: {path}") from exc


def _expand_files(root: Path, patterns: Sequence[str], *, strict_missing: bool) -> dict[str, Path]:
    root = root.resolve()
    selected: dict[str, Path] = {}

    for pattern in patterns:
        _validate_pattern(pattern)
        matches = [Path(item) for item in glob.glob(str(root / pattern), recursive=True)]
        files: list[Path] = []

        for match in matches:
            _assert_no_symlink_components(root, match)
            if match.is_file():
                files.append(match)

        if strict_missing and not files:
            raise ExportError(f"include pattern matched no files: {pattern}")

        for file_path in files:
            relative = _relative_posix(root, file_path)
            selected[relative] = file_path

    return selected


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _owned_patterns(config: ExportConfig) -> tuple[str, ...]:
    return (*_ALWAYS_REPOSITORY_OWNED, *config.repository_owned)


def _is_repository_owned(relative: str, config: ExportConfig) -> bool:
    return _matches_any(relative, _owned_patterns(config))


def _is_excluded(relative: str, config: ExportConfig) -> bool:
    return _matches_any(relative, config.exclude)


def _assert_not_repository_owned(relative: str, config: ExportConfig) -> None:
    if _is_repository_owned(relative, config):
        raise ExportError(f"managed path collides with repository-owned path: {relative}")


def _assert_safe_destination(destination: Path, relative: str) -> Path:
    destination_root = destination.resolve()
    target = destination_root / PurePosixPath(relative)
    _assert_no_symlink_components(destination_root, target)
    try:
        target.absolute().relative_to(destination_root)
    except ValueError as exc:
        raise ExportError(f"destination escapes export root: {relative}") from exc
    return target


def _collect_destination_files(destination: Path, config: ExportConfig) -> dict[str, Path]:
    if not destination.exists():
        return {}

    destination_root = destination.resolve()
    selected: dict[str, Path] = {}

    for directory, dirnames, filenames in os.walk(destination_root, followlinks=False):
        directory_path = Path(directory)

        # Git metadata is never part of the managed projection, even if the
        # caller forgets to list it in repository_owned. Symlink directories
        # are rejected rather than silently preserved as unmanaged content.
        safe_dirnames: list[str] = []
        for name in dirnames:
            path = directory_path / name
            if name == ".git":
                continue
            if path.is_symlink():
                raise ExportError(f"symlink is not allowed in managed path: {path}")
            safe_dirnames.append(name)
        dirnames[:] = safe_dirnames

        for filename in filenames:
            path = directory_path / filename
            relative = _relative_posix(destination_root, path)
            if _is_repository_owned(relative, config):
                continue
            _assert_no_symlink_components(destination_root, path)
            selected[relative] = path

    return selected


def build_plan(source: Path, destination: Path, config: ExportConfig) -> tuple[list[Change], dict[str, Path]]:
    source = source.resolve()
    destination = destination.resolve()

    if not source.is_dir():
        raise ExportError(f"source Vault does not exist or is not a directory: {source}")

    source_files = _expand_files(source, config.include, strict_missing=config.strict_missing)
    source_files = {
        relative: path
        for relative, path in source_files.items()
        if not _is_excluded(relative, config)
    }
    destination_files = _collect_destination_files(destination, config)

    for relative in source_files:
        _assert_not_repository_owned(relative, config)
        _assert_safe_destination(destination, relative)

    for relative in destination_files:
        _assert_not_repository_owned(relative, config)
        _assert_safe_destination(destination, relative)

    changes: list[Change] = []

    for relative in sorted(source_files):
        src = source_files[relative]
        dst = destination_files.get(relative)
        if dst is None:
            changes.append(Change("ADD", relative))
        elif _sha256(src) != _sha256(dst):
            changes.append(Change("UPDATE", relative))

    for relative in sorted(set(destination_files) - set(source_files)):
        changes.append(Change("DELETE", relative))

    changes.sort(key=lambda item: (item.path, item.action))
    return changes, source_files


def _atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    _assert_no_symlink_components(target.parent.resolve(), target)

    fd, temp_name = tempfile.mkstemp(prefix=".obsidian-export-", dir=target.parent)
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        shutil.copy2(source, temp_path)
        os.replace(temp_path, target)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def apply_plan(source: Path, destination: Path, config: ExportConfig) -> list[Change]:
    changes, source_files = build_plan(source, destination, config)
    destination.mkdir(parents=True, exist_ok=True)

    for change in changes:
        target = _assert_safe_destination(destination, change.path)
        if change.action == "DELETE":
            if target.exists() or target.is_symlink():
                target.unlink()
        elif change.action in {"ADD", "UPDATE"}:
            _atomic_copy(source_files[change.path], target)
        else:
            raise AssertionError(f"unexpected action: {change.action}")

    return changes


def _format_changes(changes: Iterable[Change]) -> str:
    items = list(changes)
    if not items:
        return "No changes."
    return "\n".join(f"{item.action:6} {item.path}" for item in items)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="obsidian-public-export",
        description="Generate an allowlist-only public projection from a private Obsidian Vault.",
    )
    parser.add_argument("--source", type=Path, required=True, help="Private Vault root.")
    parser.add_argument("--destination", type=Path, required=True, help="Public repository working tree.")
    parser.add_argument("--config", type=Path, required=True, help="Exporter TOML configuration.")
    parser.add_argument("--dry-run", action="store_true", help="Print changes without modifying the destination.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = load_config(args.config)
        if args.dry_run:
            changes, _ = build_plan(args.source, args.destination, config)
        else:
            changes = apply_plan(args.source, args.destination, config)
    except ExportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(_format_changes(changes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
