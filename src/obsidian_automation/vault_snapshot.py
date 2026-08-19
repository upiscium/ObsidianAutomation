from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping, Sequence


class SnapshotError(RuntimeError):
    """Raised when a Live Vault snapshot cannot be produced safely."""


_ALWAYS_REPOSITORY_OWNED = (".git", ".git/**", "**/.git", "**/.git/**")


@dataclass(frozen=True)
class SnapshotConfig:
    exclude: tuple[str, ...]
    repository_owned: tuple[str, ...]
    settle_seconds: float = 5.0
    stability_attempts: int = 3


@dataclass(frozen=True)
class ManifestEntry:
    sha256: str
    size: int


@dataclass(frozen=True)
class Change:
    action: str
    path: str


@dataclass(frozen=True)
class SnapshotResult:
    changed: bool
    commit_sha: str | None
    manifest_sha256: str
    changes: tuple[Change, ...]


def _validate_pattern(pattern: str) -> None:
    if not pattern or pattern.startswith("/"):
        raise SnapshotError(f"unsafe path pattern: {pattern!r}")
    parts = PurePosixPath(pattern).parts
    if ".." in parts:
        raise SnapshotError(f"path traversal is not allowed: {pattern!r}")
    if any(part in ("", ".") for part in parts):
        raise SnapshotError(f"invalid path pattern: {pattern!r}")


@lru_cache(maxsize=None)
def _compile_pattern(pattern: str) -> re.Pattern[str]:
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


def load_config(path: Path) -> SnapshotConfig:
    with path.open("rb") as handle:
        raw = tomllib.load(handle)

    version = raw.get("version")
    if version != 1:
        raise SnapshotError(f"unsupported config version: {version!r}")

    exclude = tuple(raw.get("exclude", ()))
    repository_owned = tuple(raw.get("repository_owned", ()))
    settle_seconds = raw.get("settle_seconds", 5.0)
    stability_attempts = raw.get("stability_attempts", 3)

    for pattern in (*exclude, *repository_owned):
        if not isinstance(pattern, str):
            raise SnapshotError("all path patterns must be strings")
        _validate_pattern(pattern)

    if not isinstance(settle_seconds, (int, float)) or isinstance(settle_seconds, bool):
        raise SnapshotError("settle_seconds must be a non-negative number")
    if settle_seconds < 0:
        raise SnapshotError("settle_seconds must be non-negative")
    if not isinstance(stability_attempts, int) or isinstance(stability_attempts, bool):
        raise SnapshotError("stability_attempts must be an integer")
    if stability_attempts < 1:
        raise SnapshotError("stability_attempts must be at least 1")

    return SnapshotConfig(
        exclude=exclude,
        repository_owned=repository_owned,
        settle_seconds=float(settle_seconds),
        stability_attempts=stability_attempts,
    )


def _owned_patterns(config: SnapshotConfig) -> tuple[str, ...]:
    return (*_ALWAYS_REPOSITORY_OWNED, *config.repository_owned)


def _is_repository_owned(relative: str, config: SnapshotConfig) -> bool:
    return _matches_any(relative, _owned_patterns(config))


def _is_excluded(relative: str, config: SnapshotConfig) -> bool:
    return _matches_any(relative, config.exclude)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative_posix(root: Path, path: Path) -> str:
    try:
        return path.absolute().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise SnapshotError(f"path escapes root: {path}") from exc


def _assert_separate_roots(source: Path, destination: Path) -> None:
    source = source.resolve()
    destination = destination.resolve()
    if source == destination or source in destination.parents or destination in source.parents:
        raise SnapshotError("source and destination must be separate, non-nested roots")


def _collect_source_files(source: Path, config: SnapshotConfig) -> dict[str, Path]:
    source = source.resolve()
    if not source.is_dir():
        raise SnapshotError(f"source Vault does not exist or is not a directory: {source}")

    selected: dict[str, Path] = {}
    for directory, dirnames, filenames in os.walk(source, followlinks=False):
        directory_path = Path(directory)

        next_dirnames: list[str] = []
        for name in dirnames:
            path = directory_path / name
            relative = _relative_posix(source, path)
            if path.is_symlink():
                raise SnapshotError(f"symlink is not allowed in source Vault: {path}")
            if relative == ".git" or relative.startswith(".git/"):
                continue
            next_dirnames.append(name)
        dirnames[:] = next_dirnames

        for filename in filenames:
            path = directory_path / filename
            relative = _relative_posix(source, path)
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise SnapshotError(f"symlink is not allowed in source Vault: {path}")
            if not stat.S_ISREG(info.st_mode):
                raise SnapshotError(f"special file is not allowed in source Vault: {path}")
            if _is_repository_owned(relative, config) or _is_excluded(relative, config):
                continue
            selected[relative] = path

    return selected


def build_manifest(source: Path, config: SnapshotConfig) -> dict[str, ManifestEntry]:
    files = _collect_source_files(source, config)
    return {
        relative: ManifestEntry(sha256=_sha256(path), size=path.stat().st_size)
        for relative, path in sorted(files.items())
    }


def manifest_digest(manifest: Mapping[str, ManifestEntry]) -> str:
    digest = hashlib.sha256()
    for relative in sorted(manifest):
        entry = manifest[relative]
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(entry.size).encode("ascii"))
        digest.update(b"\0")
        digest.update(entry.sha256.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def wait_for_stable_manifest(source: Path, config: SnapshotConfig) -> dict[str, ManifestEntry]:
    previous = build_manifest(source, config)
    for _ in range(config.stability_attempts):
        if config.settle_seconds:
            time.sleep(config.settle_seconds)
        current = build_manifest(source, config)
        if current == previous:
            return current
        previous = current
    raise SnapshotError("source Vault did not become stable within configured attempts")


def _copy_stable_source_to_staging(
    source: Path,
    staging: Path,
    config: SnapshotConfig,
    expected: Mapping[str, ManifestEntry],
) -> None:
    source = source.resolve()
    files = _collect_source_files(source, config)
    if set(files) != set(expected):
        raise SnapshotError("source Vault changed before staging copy")

    for relative in sorted(expected):
        src = files[relative]
        target = staging / PurePosixPath(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)

    staged_manifest = {
        relative: ManifestEntry(
            sha256=_sha256(staging / PurePosixPath(relative)),
            size=(staging / PurePosixPath(relative)).stat().st_size,
        )
        for relative in sorted(expected)
    }
    source_after = build_manifest(source, config)
    if staged_manifest != expected or source_after != expected:
        raise SnapshotError("source Vault changed while staging snapshot")


def _collect_destination_files(destination: Path, config: SnapshotConfig) -> dict[str, Path]:
    destination = destination.resolve()
    selected: dict[str, Path] = {}
    for directory, dirnames, filenames in os.walk(destination, followlinks=False):
        directory_path = Path(directory)
        next_dirnames: list[str] = []
        for name in dirnames:
            path = directory_path / name
            if name == ".git" and directory_path == destination:
                continue
            if path.is_symlink():
                raise SnapshotError(f"symlink is not allowed in destination: {path}")
            next_dirnames.append(name)
        dirnames[:] = next_dirnames

        for filename in filenames:
            path = directory_path / filename
            relative = _relative_posix(destination, path)
            if path.is_symlink():
                raise SnapshotError(f"symlink is not allowed in destination: {path}")
            if _is_repository_owned(relative, config):
                continue
            selected[relative] = path
    return selected


def build_plan(
    staging: Path, destination: Path, config: SnapshotConfig
) -> tuple[list[Change], dict[str, Path]]:
    staged_files: dict[str, Path] = {}
    for directory, _, filenames in os.walk(staging):
        directory_path = Path(directory)
        for filename in filenames:
            path = directory_path / filename
            relative = _relative_posix(staging, path)
            staged_files[relative] = path

    destination_files = _collect_destination_files(destination, config)
    changes: list[Change] = []
    for relative in sorted(staged_files):
        src = staged_files[relative]
        dst = destination_files.get(relative)
        if dst is None:
            changes.append(Change("ADD", relative))
        elif _sha256(src) != _sha256(dst):
            changes.append(Change("UPDATE", relative))
    for relative in sorted(set(destination_files) - set(staged_files)):
        changes.append(Change("DELETE", relative))
    changes.sort(key=lambda item: (item.path, item.action))
    return changes, staged_files


def _atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.is_symlink():
        raise SnapshotError(f"symlink is not allowed in destination: {target}")
    fd, temp_name = tempfile.mkstemp(prefix=".obsidian-snapshot-", dir=target.parent)
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        shutil.copy2(source, temp_path)
        os.replace(temp_path, target)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def apply_plan(staging: Path, destination: Path, config: SnapshotConfig) -> list[Change]:
    changes, staged_files = build_plan(staging, destination, config)
    for change in changes:
        target = destination / PurePosixPath(change.path)
        if change.action == "DELETE":
            if target.exists() or target.is_symlink():
                target.unlink()
        elif change.action in {"ADD", "UPDATE"}:
            _atomic_copy(staged_files[change.path], target)
        else:
            raise AssertionError(f"unexpected action: {change.action}")
    return changes


def _run(args: Sequence[str], *, cwd: Path, env: Mapping[str, str] | None = None) -> str:
    try:
        completed = subprocess.run(
            list(args),
            cwd=cwd,
            env=None if env is None else dict(env),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as exc:
        raise SnapshotError(f"required command is not available: {args[0]}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise SnapshotError(f"command failed ({completed.returncode}): {' '.join(args)}: {detail}")
    return completed.stdout.strip()


def _assert_clean_repository(destination: Path) -> None:
    destination = destination.resolve()
    if not destination.is_dir():
        raise SnapshotError(f"destination repository does not exist: {destination}")
    top_level = Path(_run(["git", "rev-parse", "--show-toplevel"], cwd=destination)).resolve()
    if top_level != destination:
        raise SnapshotError(
            f"destination must be the Git repository root: expected {destination}, got {top_level}"
        )
    if _run(["git", "status", "--porcelain"], cwd=destination):
        raise SnapshotError("destination repository must be clean before snapshot")


def snapshot_vault(
    *,
    source: Path,
    destination: Path,
    config_path: Path,
    commit_message: str,
    author_name: str,
    author_email: str,
    dry_run: bool = False,
) -> SnapshotResult:
    source = source.resolve()
    destination = destination.resolve()
    _assert_separate_roots(source, destination)
    _assert_clean_repository(destination)
    config = load_config(config_path)

    stable = wait_for_stable_manifest(source, config)
    stable_digest = manifest_digest(stable)

    with tempfile.TemporaryDirectory(prefix="obsidian-vault-snapshot-") as temp_dir:
        staging = Path(temp_dir)
        _copy_stable_source_to_staging(source, staging, config, stable)
        changes, _ = build_plan(staging, destination, config)

        if dry_run:
            return SnapshotResult(
                changed=bool(changes),
                commit_sha=None,
                manifest_sha256=stable_digest,
                changes=tuple(changes),
            )

        if not changes:
            return SnapshotResult(
                changed=False,
                commit_sha=None,
                manifest_sha256=stable_digest,
                changes=(),
            )

        applied = apply_plan(staging, destination, config)

    _run(["git", "add", "-A"], cwd=destination)
    _run(["git", "diff", "--cached", "--check"], cwd=destination)
    staged = _run(["git", "diff", "--cached", "--name-only"], cwd=destination)
    if not staged:
        return SnapshotResult(
            changed=False,
            commit_sha=None,
            manifest_sha256=stable_digest,
            changes=tuple(applied),
        )

    full_message = f"{commit_message}\n\nSource-Manifest-SHA256: {stable_digest}"
    commit_env = dict(os.environ)
    commit_env.update(
        {
            "GIT_AUTHOR_NAME": author_name,
            "GIT_AUTHOR_EMAIL": author_email,
            "GIT_COMMITTER_NAME": author_name,
            "GIT_COMMITTER_EMAIL": author_email,
        }
    )
    _run(["git", "commit", "-m", full_message], cwd=destination, env=commit_env)
    commit_sha = _run(["git", "rev-parse", "HEAD"], cwd=destination)
    return SnapshotResult(
        changed=True,
        commit_sha=commit_sha,
        manifest_sha256=stable_digest,
        changes=tuple(applied),
    )


def _format_changes(changes: Iterable[Change]) -> str:
    items = list(changes)
    if not items:
        return "No changes."
    return "\n".join(f"{item.action:6} {item.path}" for item in items)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="obsidian-vault-snapshot",
        description=(
            "Create a stable local Git snapshot of a synced Live Obsidian Vault. "
            "This command never writes to the source Vault and never pushes."
        ),
    )
    parser.add_argument("--source", type=Path, required=True, help="Read-only Live Vault root.")
    parser.add_argument(
        "--destination", type=Path, required=True, help="Clean private Git repository root."
    )
    parser.add_argument("--config", type=Path, required=True, help="Snapshot TOML configuration.")
    parser.add_argument("--dry-run", action="store_true", help="Preview the stable snapshot diff.")
    parser.add_argument(
        "--commit-message",
        default="Snapshot ObsidianVault",
        help="Commit subject for a changed snapshot.",
    )
    parser.add_argument(
        "--author-name", default="Obsidian Snapshot", help="Git author/committer name."
    )
    parser.add_argument(
        "--author-email",
        default="obsidian-snapshot@local.invalid",
        help="Git author/committer email.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = snapshot_vault(
            source=args.source,
            destination=args.destination,
            config_path=args.config,
            commit_message=args.commit_message,
            author_name=args.author_name,
            author_email=args.author_email,
            dry_run=args.dry_run,
        )
    except SnapshotError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"Source manifest: {result.manifest_sha256}")
    print(_format_changes(result.changes))
    if result.commit_sha:
        print(f"Created snapshot commit {result.commit_sha}")
    elif result.changed and args.dry_run:
        print("Dry run; no commit created.")
    else:
        print("No snapshot commit created.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
