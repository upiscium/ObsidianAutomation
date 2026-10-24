"""Fixed-purpose Nextcloud pull -> local snapshot -> verified private Git ref.

Each attempt retains its mirror, bare fetch and worktree under state/runs.
No arbitrary command, URL, Vault path or credential comes from an HTTP request.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .vault_snapshot import build_manifest, load_config, snapshot_vault


@dataclass(frozen=True)
class Connections:
    state: Path
    rclone_remote: str
    git_remote: str
    branch: str
    policy: Path
    rclone_config: Path
    allow_local_fixture: bool = False

    def validate(self) -> None:
        # Remote folder is an explicit, non-root shared Vault folder.
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,63}:[a-zA-Z0-9_./ -]+", self.rclone_remote):
            raise ValueError("invalid source")
        suffix = self.rclone_remote.split(":", 1)[1]
        if not suffix or suffix.startswith(("/", ".")) or any(p in ("", "..", ".") for p in suffix.split("/")):
            raise ValueError("invalid Vault folder")
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._/-]{0,100}", self.branch) or ".." in self.branch or self.branch.endswith(("/", ".lock")):
            raise ValueError("invalid branch")
        url = urlsplit(self.git_remote)
        if url.scheme == "https":
            if not url.hostname or url.username or url.password or url.query or url.fragment or not url.path.endswith(".git"):
                raise ValueError("invalid private Git endpoint")
        elif not (self.allow_local_fixture and url.scheme == "file" and url.netloc == "" and url.path.startswith("/")):
            raise ValueError("invalid private Git endpoint")
        if not self.state.is_absolute() or not self.policy.is_file() or not self.rclone_config.is_file():
            raise ValueError("missing local configuration")


def _command(args: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 120) -> tuple[bool, str]:
    try:
        result = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True,
                                timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False, ""
    # Only stdout for ref lookups; stderr can contain secrets and private paths.
    return result.returncode == 0, result.stdout.strip()


def _head_tree_paths(work: Path, env: dict[str, str]) -> set[str] | None:
    """Read the committed HEAD tree, not the working tree or Git's ignore view."""
    try:
        result = subprocess.run(
            ["git", "-C", str(work), "ls-tree", "-r", "-z", "--name-only", "HEAD"],
            cwd=work, env=env, capture_output=True, timeout=120, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.endswith(b"\0"):
        return None
    return {os.fsdecode(path) for path in result.stdout[:-1].split(b"\0")}


class SnapshotAdapter:
    def __init__(self, connections: Connections, *, settle_seconds: float = 0):
        connections.validate()
        self.config = connections
        self.settle_seconds = settle_seconds

    def __call__(self, run_id: str) -> tuple[str, str, str | None]:
        c = self.config
        if not re.fullmatch(r"[a-f0-9]{32}", run_id):
            return "failed", "sync", None
        directory = c.state / "runs" / run_id
        try:
            directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        except OSError:
            return "failed", "sync", None
        mirror, bare, work = directory / "mirror", directory / "cache.git", directory / "work"
        mirror.mkdir(mode=0o700)
        # Prevent Git hooks, prompts and git configuration supplied by a hostile
        # service environment. Credentials, if necessary, must be provisioned
        # explicitly via a separate noninteractive credential helper in P3.
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("GIT_CONFIG_", "GIT_TRACE", "GIT_SSH", "GIT_ASKPASS"))}
        env.update(GIT_TERMINAL_PROMPT="0", GIT_ALLOW_PROTOCOL="https:file" if c.allow_local_fixture else "https",
                   GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null", GIT_NO_REPLACE_OBJECTS="1",
                   GIT_LFS_SKIP_SMUDGE="1")
        rclone = ["rclone", "--config", str(c.rclone_config)]
        for action in ("sync", "sync", "check"):
            args = rclone + [action, c.rclone_remote, str(mirror), "--retries", "1", "--low-level-retries", "1"]
            if action == "sync":
                args += ["--delete-after"]  # only the new isolated local mirror
            ok, _ = _command(args, cwd=directory, env=env)
            if not ok:
                return "failed", "sync", None
            if action == "sync" and self.settle_seconds:
                time.sleep(self.settle_seconds)
        try:
            manifest = build_manifest(mirror, load_config(c.policy))
        except Exception:
            return "failed", "sync", None
        # Never interpret an empty/misaddressed remote as a deletion request.
        if not manifest:
            return "failed", "sync", None
        ok, _ = _command(["git", "init", "--bare", str(bare)], cwd=directory, env=env)
        if not ok:
            return "failed", "fetch", None
        ref = f"refs/heads/{c.branch}"
        ok, output = _command(["git", "ls-remote", "--exit-code", c.git_remote, ref], cwd=directory, env=env)
        if not ok or not re.fullmatch(r"[a-f0-9]{40}\t" + re.escape(ref), output):
            # First-run initialization is intentionally not inferred.
            return "failed", "fetch", None
        baseline = output.split("\t", 1)[0]
        ok, _ = _command(["git", "--git-dir", str(bare), "fetch", "--no-tags", c.git_remote, ref], cwd=directory, env=env)
        if not ok:
            return "failed", "fetch", None
        ok, fetched = _command(["git", "--git-dir", str(bare), "rev-parse", "FETCH_HEAD"], cwd=directory, env=env)
        if not ok or fetched != baseline:
            return "failed", "fetch", None
        ok, _ = _command(["git", "--git-dir", str(bare), "worktree", "add", "--detach", str(work), baseline], cwd=directory, env=env)
        if not ok:
            return "failed", "fetch", None
        try:
            result = snapshot_vault(source=mirror, destination=work, config_path=c.policy,
                                    commit_message="Snapshot ObsidianVault", author_name="Obsidian Snapshot",
                                    author_email="obsidian-snapshot@local.invalid")
        except Exception:
            return "failed", "snapshot", None
        # Git may silently omit a newly added managed file matched by the
        # repository's .gitignore. Even with an unrelated commit, such a run
        # cannot truthfully be called saved (nor no_changes on a no-op).
        tree_paths = _head_tree_paths(work, env)
        if tree_paths is None or not manifest.keys() <= tree_paths:
            return "failed", "snapshot", None
        expected = result.commit_sha if result.changed else baseline
        if result.changed and (not expected or not re.fullmatch(r"[a-f0-9]{40}", expected)):
            return "failed", "snapshot", None
        if result.changed:
            # Race check before push. No force, no replay on an uncertain effect.
            ok, current = _command(["git", "ls-remote", "--exit-code", c.git_remote, ref], cwd=directory, env=env)
            if not ok or current != f"{baseline}\t{ref}":
                return "unconfirmed", "push", None
            pushed, _ = _command(["git", "-C", str(work), "push", c.git_remote,
                                  f"HEAD:{ref}"], cwd=directory, env=env)
        else:
            pushed = True
        verified, remote = _command(["git", "ls-remote", "--exit-code", c.git_remote, ref], cwd=directory, env=env)
        if verified and remote == f"{expected}\t{ref}":
            return ("saved" if result.changed else "no_changes"), "verify", expected
        return ("unconfirmed" if result.changed else "failed"), ("push" if not pushed else "verify"), None
