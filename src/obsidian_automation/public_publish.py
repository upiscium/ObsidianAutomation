from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .public_export import (
    Change,
    ExportConfig,
    ExportError,
    _is_excluded,
    _is_repository_owned,
    _matches_any,
    apply_plan,
    build_plan,
    load_config,
)


PROJECTION_COMMIT_SUBJECT = "Sync public projection from ObsidianVault"
PROJECTION_COMMIT_MARKER = "Obsidian-Projection: v1"


class PublishError(RuntimeError):
    """Raised when a projection cannot be safely validated or committed."""


@dataclass(frozen=True)
class PublishResult:
    changed: bool
    commit_sha: str | None
    changes: tuple[Change, ...]


def _run(
    args: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
) -> str:
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
        raise PublishError(f"required command is not available: {args[0]}") from exc

    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        command = " ".join(args)
        raise PublishError(f"command failed ({completed.returncode}): {command}: {detail}")

    return completed.stdout.strip()


def _assert_clean_repository(repository: Path) -> None:
    repository = repository.resolve()
    if not repository.is_dir():
        raise PublishError(f"destination repository does not exist: {repository}")

    top_level = Path(_run(["git", "rev-parse", "--show-toplevel"], cwd=repository)).resolve()
    if top_level != repository:
        raise PublishError(
            f"destination must be the Git repository root: expected {repository}, got {top_level}"
        )

    status = _run(["git", "status", "--porcelain"], cwd=repository)
    if status:
        raise PublishError("destination repository must be clean before publishing")


def _validate_obsidian_core(repository: Path) -> None:
    repository = repository.resolve()

    _run(["git", "diff", "--check"], cwd=repository)

    validator = repository / "98-System/99-dev/validate-repo.mjs"
    if not validator.is_file():
        raise PublishError(f"ObsidianCore validator is missing: {validator}")

    test_root = repository / "98-System/99-dev/test"
    tests = sorted(test_root.glob("*.test.mjs"))
    if not tests:
        raise PublishError(f"ObsidianCore tests are missing: {test_root}")

    _run(["node", validator.relative_to(repository).as_posix()], cwd=repository)
    _run(
        ["node", "--test", *(path.relative_to(repository).as_posix() for path in tests)],
        cwd=repository,
    )
    _run(["git", "diff", "--check"], cwd=repository)


def _projection_managed(path: str, config: ExportConfig) -> bool:
    return (
        _matches_any(path, config.include)
        and not _is_excluded(path, config)
        and not _is_repository_owned(path, config)
    )


def _last_projection_commit(repository: Path) -> str | None:
    marker = _run(
        [
            "git",
            "log",
            "-n",
            "1",
            "--format=%H",
            "--extended-regexp",
            f"--grep=^{PROJECTION_COMMIT_MARKER}$",
            "HEAD",
        ],
        cwd=repository,
    )
    if marker:
        return marker

    raw = _run(["git", "log", "--format=%H%x00%s%x00", "-z", "HEAD"], cwd=repository)
    fields = [field for field in raw.split("\x00") if field]
    if len(fields) % 2 != 0:
        raise PublishError("cannot parse ObsidianCore projection commit history")
    for index in range(0, len(fields), 2):
        commit, subject = fields[index], fields[index + 1]
        if subject == PROJECTION_COMMIT_SUBJECT:
            return commit
    return None


def _managed_drift_since_projection(
    repository: Path,
    config: ExportConfig,
    baseline: str | None,
) -> tuple[str, ...]:
    if baseline is None:
        return ()
    raw = _run(
        ["git", "diff", "--name-only", "-z", "--no-renames", baseline, "HEAD", "--"],
        cwd=repository,
    )
    paths = [path for path in raw.split("\x00") if path]
    managed = sorted(
        {path for path in paths if _projection_managed(path, config)},
        key=str.casefold,
    )
    return tuple(managed)


def _commit_env(author_name: str, author_email: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": author_name,
            "GIT_AUTHOR_EMAIL": author_email,
            "GIT_COMMITTER_NAME": author_name,
            "GIT_COMMITTER_EMAIL": author_email,
        }
    )
    return env


def _projection_message(commit_message: str) -> str:
    message = commit_message.strip()
    if not message:
        raise PublishError("projection commit message must not be empty")
    if PROJECTION_COMMIT_MARKER in message.splitlines():
        return message
    return f"{message}\n\n{PROJECTION_COMMIT_MARKER}"


def _commit_projection(
    repository: Path,
    *,
    commit_message: str,
    author_name: str,
    author_email: str,
    allow_empty: bool,
) -> str:
    args = ["git", "commit"]
    if allow_empty:
        args.append("--allow-empty")
    args.extend(["-m", _projection_message(commit_message)])
    _run(args, cwd=repository, env=_commit_env(author_name, author_email))
    return _run(["git", "rev-parse", "HEAD"], cwd=repository)


def publish_projection(
    *,
    source: Path,
    destination: Path,
    config_path: Path,
    commit_message: str,
    author_name: str,
    author_email: str,
    validate_core: bool = True,
) -> PublishResult:
    destination = destination.resolve()
    _assert_clean_repository(destination)

    try:
        config = load_config(config_path)
        planned, _ = build_plan(source, destination, config)
    except ExportError as exc:
        raise PublishError(str(exc)) from exc

    baseline = _last_projection_commit(destination)
    drift = _managed_drift_since_projection(destination, config, baseline)

    if planned and baseline is None:
        raise PublishError(
            "refusing projection update without a known generated projection baseline"
        )
    if planned and drift:
        preview = ", ".join(drift[:8])
        if len(drift) > 8:
            preview += ", ..."
        raise PublishError(
            "refusing to overwrite Core-managed changes that have not yet converged through "
            f"the Live Vault promotion path: {preview}"
        )

    if not planned:
        # If Core managed files changed after the last generated projection but now
        # exactly match the Vault source, Promotion has converged. Create an empty
        # generated commit to advance the public projection baseline without
        # rewriting any repository content. A repository with no historical marker
        # can also establish its baseline only when source and destination are exact.
        if baseline is None or drift:
            if validate_core:
                _validate_obsidian_core(destination)
            commit_sha = _commit_projection(
                destination,
                commit_message=commit_message,
                author_name=author_name,
                author_email=author_email,
                allow_empty=True,
            )
            return PublishResult(changed=True, commit_sha=commit_sha, changes=())
        return PublishResult(changed=False, commit_sha=None, changes=())

    try:
        changes = tuple(apply_plan(source, destination, config))
    except ExportError as exc:
        raise PublishError(str(exc)) from exc
    if not changes:
        raise PublishError("projection plan reported changes but apply produced no changes")

    if validate_core:
        _validate_obsidian_core(destination)

    _run(["git", "add", "-A"], cwd=destination)
    _run(["git", "diff", "--cached", "--check"], cwd=destination)

    staged = _run(["git", "diff", "--cached", "--name-only"], cwd=destination)
    if not staged:
        raise PublishError("export reported changes but no staged changes remain")

    commit_sha = _commit_projection(
        destination,
        commit_message=commit_message,
        author_name=author_name,
        author_email=author_email,
        allow_empty=False,
    )
    return PublishResult(changed=True, commit_sha=commit_sha, changes=changes)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="obsidian-public-publish",
        description=(
            "Apply an allowlist-only ObsidianCore projection, run repository validation, "
            "and create a local Git commit. This command never pushes."
        ),
    )
    parser.add_argument("--source", type=Path, required=True, help="Private Vault root.")
    parser.add_argument(
        "--destination", type=Path, required=True, help="Clean ObsidianCore Git repository root."
    )
    parser.add_argument("--config", type=Path, required=True, help="Public Exporter TOML config.")
    parser.add_argument(
        "--commit-message",
        default=PROJECTION_COMMIT_SUBJECT,
        help="Commit message for a changed or acknowledged projection.",
    )
    parser.add_argument(
        "--author-name", default="Obsidian Automation", help="Git author/committer name."
    )
    parser.add_argument(
        "--author-email",
        default="obsidian-automation@users.noreply.github.com",
        help="Git author/committer email.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = publish_projection(
            source=args.source,
            destination=args.destination,
            config_path=args.config,
            commit_message=args.commit_message,
            author_name=args.author_name,
            author_email=args.author_email,
        )
    except PublishError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not result.changed:
        print("No projection changes; no commit created.")
        return 0

    print(f"Created projection commit {result.commit_sha}")
    for change in result.changes:
        print(f"{change.action:6} {change.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
