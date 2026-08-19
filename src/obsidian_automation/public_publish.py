from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .public_export import Change, ExportError, apply_plan, load_config


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
        changes = tuple(apply_plan(source, destination, config))
    except ExportError as exc:
        raise PublishError(str(exc)) from exc

    if not changes:
        return PublishResult(changed=False, commit_sha=None, changes=())

    if validate_core:
        _validate_obsidian_core(destination)

    _run(["git", "add", "-A"], cwd=destination)
    _run(["git", "diff", "--cached", "--check"], cwd=destination)

    staged = _run(["git", "diff", "--cached", "--name-only"], cwd=destination)
    if not staged:
        raise PublishError("export reported changes but no staged changes remain")

    commit_env = dict(os.environ)
    commit_env.update(
        {
            "GIT_AUTHOR_NAME": author_name,
            "GIT_AUTHOR_EMAIL": author_email,
            "GIT_COMMITTER_NAME": author_name,
            "GIT_COMMITTER_EMAIL": author_email,
        }
    )
    _run(["git", "commit", "-m", commit_message], cwd=destination, env=commit_env)
    commit_sha = _run(["git", "rev-parse", "HEAD"], cwd=destination)

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
        default="Sync public projection from ObsidianVault",
        help="Commit message for a changed projection.",
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
