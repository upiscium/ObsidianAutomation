"""#511 P1: local-only guarded public projection preview (no remote push).

SnapshotEvidence must come from the trusted B2 producer, never user JSON.
Only disposable local Git fixtures are exercised until separate P2 approval.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

_SHA = re.compile(r"[0-9a-f]{40}\Z")
_MAIN = "refs/heads/main"
_VERSION = 1


class PreviewError(RuntimeError):
    """Stable reason code; never include private paths, output or credentials."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class SnapshotEvidence:
    status: str
    stage: str
    remote_ref: str | None
    gitea_write: str


@dataclass(frozen=True)
class PreviewConfig:
    source_url: str
    core_url: str
    policy_path: Path
    state_dir: Path
    publisher_revision: str
    allow_file_fixture: bool = False


@dataclass(frozen=True)
class PreviewResult:
    status: str
    run_key: str
    source_sha: str
    core_before_sha: str
    candidate_sha: str | None
    policy_sha256: str


def _sha(value: str | None) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise PreviewError("invalid_sha")
    return value


def _source_sha(evidence: SnapshotEvidence) -> str:
    if (
        evidence.status not in {"saved", "no_changes"}
        or evidence.stage != "verify"
        or evidence.gitea_write != "verified"
    ):
        raise PreviewError("snapshot_not_verified")
    return _sha(evidence.remote_ref)


def _validate_url(value: str, *, fixture: bool, core: bool) -> None:
    parsed = urlsplit(value)
    if (
        not value or any(ord(char) < 32 for char in value)
        or parsed.username is not None or parsed.password is not None
        or parsed.query or parsed.fragment or not parsed.path.endswith(".git")
    ):
        raise PreviewError("invalid_repository")
    if fixture and parsed.scheme == "file":
        if parsed.netloc or not parsed.path.startswith("/"):
            raise PreviewError("invalid_repository")
        return
    try:
        trusted_port = parsed.port in {None, 443}
    except ValueError as exc:
        raise PreviewError("invalid_repository") from exc
    if parsed.scheme != "https" or not parsed.hostname or not trusted_port:
        raise PreviewError("invalid_repository")
    if core and value != "https://github.com/upiscium/ObsidianCore.git":
        raise PreviewError("unexpected_core_repository")


def _git_env(home: Path, *, fixture: bool) -> dict[str, str]:
    """Do not inherit GitHub/Gitea auth, SSH agents or arbitrary Git config."""
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ALLOW_PROTOCOL": "https:file" if fixture else "https",
        "GIT_LFS_SKIP_SMUDGE": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.followRedirects",
        "GIT_CONFIG_VALUE_0": "false",
    }


def _git(args: list[str], *, env: dict[str, str], cwd: Path) -> str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=120, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PreviewError("git_unavailable_or_timeout") from exc
    if result.returncode or len(result.stdout) > 4096:
        raise PreviewError("git_failed")
    try:
        return result.stdout.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise PreviewError("git_invalid_output") from exc


def _remote_head(url: str, *, env: dict[str, str], cwd: Path) -> str:
    fields = _git(["ls-remote", url, _MAIN], env=env, cwd=cwd).split()
    if len(fields) != 2 or fields[1] != _MAIN:
        raise PreviewError("invalid_remote_ref")
    return _sha(fields[0])


def _clone_exact(
    url: str, destination: Path, sha: str, *, env: dict[str, str], cwd: Path
) -> None:
    _git(
        ["clone", "--quiet", "--no-checkout", "--no-tags",
         "--single-branch", "--branch", "main", url, str(destination)],
        env=env, cwd=cwd,
    )
    if _git(["rev-parse", "refs/remotes/origin/main"], env=env, cwd=destination) != sha:
        raise PreviewError("remote_moved_during_clone")
    _git(["checkout", "--quiet", "--detach", sha], env=env, cwd=destination)
    # --no-checkout clone may already point at the same commit with an empty
    # index/worktree; force materialization of the pinned tree in this disposable clone.
    _git(["reset", "--hard", sha], env=env, cwd=destination)
    if _git(["rev-parse", "HEAD"], env=env, cwd=destination) != sha:
        raise PreviewError("checkout_mismatch")
    # Detached preview worktrees have no configured remote to push to.
    _git(["remote", "remove", "origin"], env=env, cwd=destination)
    if _git(["status", "--porcelain"], env=env, cwd=destination):
        raise PreviewError("dirty_checkout")


def _policy_sha(path: Path) -> str:
    if not path.is_file() or path.is_symlink():
        raise PreviewError("invalid_policy")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PreviewError("invalid_policy") from exc
    if not raw or len(raw) > 128 * 1024:
        raise PreviewError("invalid_policy")
    return hashlib.sha256(raw).hexdigest()


def _private_dir(path: Path) -> None:
    if path.is_symlink():
        raise PreviewError("unsafe_state")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.is_symlink() or not path.is_dir() or path.stat().st_mode & 0o077:
        raise PreviewError("unsafe_state")


def _freeze_policy(source: Path, target: Path, digest: str) -> None:
    """Use only the exact vetted policy bytes, even if the input later changes."""
    if source.is_symlink():
        raise PreviewError("policy_changed")
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise PreviewError("policy_changed") from exc
    if hashlib.sha256(raw).hexdigest() != digest:
        raise PreviewError("policy_changed")
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise PreviewError("policy_copy_failed") from exc
    with os.fdopen(fd, "wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


def _atomic_receipt(path: Path, value: dict[str, object]) -> None:
    if path.exists() or path.is_symlink():
        raise PreviewError("receipt_exists")
    raw = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    fd, name = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        dirfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    finally:
        temp.unlink(missing_ok=True)


def _run_publisher(
    source: Path, core: Path, policy: Path, *, env: dict[str, str], cwd: Path
) -> None:
    child_env = dict(env)
    child_env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
    command = [
        sys.executable, "-m", "obsidian_automation.public_publish",
        "--source", str(source), "--destination", str(core),
        "--config", str(policy),
    ]
    try:
        child = subprocess.Popen(
            command, cwd=cwd, env=child_env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        raise PreviewError("publisher_unavailable") from exc
    try:
        code = child.wait(timeout=180)
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()
        raise PreviewError("projection_timeout") from exc
    if code != 0:
        # No private source paths, stdout/stderr or full validator logs persist.
        raise PreviewError("projection_rejected")


def _candidate_sha(core: Path, before: str, *, env: dict[str, str], policy_path: Path) -> str | None:
    if _git(["status", "--porcelain"], env=env, cwd=core):
        raise PreviewError("dirty_candidate")
    head = _sha(_git(["rev-parse", "HEAD"], env=env, cwd=core))
    if head == before:
        return None
    parents = _git(["rev-list", "--parents", "-n", "1", head],
                   env=env, cwd=core).split()
    if parents != [head, before]:
        raise PreviewError("candidate_ancestry")
    from .public_publish import PROJECTION_COMMIT_MARKER
    message = _git(["log", "-1", "--format=%B"], env=env, cwd=core)
    if PROJECTION_COMMIT_MARKER not in message.splitlines():
        raise PreviewError("candidate_unmarked")
    # The existing CLI stages with git add -A. Node validation or tests could
    # create unrelated files; a generated commit must contain managed paths
    # ONLY, even if the validator succeeded. Empty acknowledgement is valid.
    from .public_export import ExportError, load_config
    from .public_publish import _projection_managed
    try:
        config = load_config(policy_path)
    except (ExportError, OSError, ValueError) as exc:
        raise PreviewError("candidate_policy_invalid") from exc
    raw_paths = _git(["diff", "--no-renames", "--name-only", "-z", before, head, "--"],
                     env=env, cwd=core)
    paths = [path for path in raw_paths.split("\x00") if path]
    if any(not _projection_managed(path, config) for path in paths):
        raise PreviewError("candidate_out_of_scope")
    return head


def _assert_projection_converged(source: Path, core: Path, policy_path: Path) -> None:
    """Verify final generated bytes equal the deterministic source projection.

    Checking only changed *paths* does not prevent a Node validator from
    rewriting an allowed managed file after the Publisher built its plan.
    Compare using the same semantic appearance/allowlist rules as the real
    publisher rather than relying on raw-byte equality for every path.
    """
    from .public_export import ExportError, load_config
    from .public_publish import PublishError, _build_publication_plan

    try:
        policy = load_config(policy_path)
        outstanding, _, _ = _build_publication_plan(source, core, policy)
    except (OSError, ExportError, PublishError, ValueError) as exc:
        raise PreviewError("candidate_projection_unverified") from exc
    if outstanding:
        raise PreviewError("candidate_source_diverged")


def _cached(
    path: Path, run_dir: Path, *, source: str, core: str,
    policy: str, revision: str, key: str, env: dict[str, str],
) -> PreviewResult:
    if path.is_symlink():
        raise PreviewError("invalid_existing_receipt")
    try:
        raw = path.read_bytes()
        if len(raw) > 4096:
            raise ValueError("oversized")
        record = json.loads(raw)
    except (OSError, ValueError, TypeError) as exc:
        raise PreviewError("invalid_existing_receipt") from exc
    if (
        not isinstance(record, dict) or record.get("version") != _VERSION
        or record.get("run_key") != key
        or record.get("source_sha") != source
        or record.get("core_before_sha") != core
        or record.get("policy_sha256") != policy
        or record.get("publisher_revision") != revision
        or record.get("status") not in {"candidate_prepared", "no_changes"}
        or not run_dir.is_dir()
        or not (run_dir / "core" / ".git").is_dir()
        or (run_dir / "core" / ".git").is_symlink()
        or not (run_dir / "source" / ".git").is_dir()
        or (run_dir / "source" / ".git").is_symlink()
    ):
        raise PreviewError("invalid_existing_receipt")
    candidate = record.get("candidate_sha")
    if candidate is not None:
        _sha(candidate)
    if bool(candidate) != (record["status"] == "candidate_prepared"):
        raise PreviewError("invalid_existing_receipt")
    source_dir = run_dir / "source"
    core_dir = run_dir / "core"
    try:
        if (run_dir.is_symlink() or source_dir.is_symlink() or core_dir.is_symlink()
            or _policy_sha(run_dir / "policy.toml") != policy
            or _git(["rev-parse", "HEAD"], env=env, cwd=source_dir) != source
            or _git(["status", "--porcelain"], env=env, cwd=source_dir)
            or _candidate_sha(core_dir, core, env=env,
                              policy_path=run_dir / "policy.toml") != candidate):
            raise PreviewError("invalid_existing_receipt")
        _assert_projection_converged(source_dir, core_dir, run_dir / "policy.toml")
    except (PreviewError, OSError) as exc:
        raise PreviewError("invalid_existing_receipt") from exc
    return PreviewResult("already_prepared", key, source, core, candidate, policy)


def prepare_local_preview(evidence: SnapshotEvidence, config: PreviewConfig) -> PreviewResult:
    """Prepare only local Git objects; never attempt a push.

    Caller must bind evidence to the trusted B2 status provider. Merely constructing
    SnapshotEvidence does not prove authorization or authenticated provenance.
    """
    source_sha = _source_sha(evidence)
    revision = _sha(config.publisher_revision)
    _validate_url(config.source_url, fixture=config.allow_file_fixture, core=False)
    _validate_url(config.core_url, fixture=config.allow_file_fixture, core=True)
    policy = _policy_sha(config.policy_path)
    _private_dir(config.state_dir)
    state = config.state_dir
    _private_dir(state / "runs")
    _private_dir(state / "receipts")
    try:
        fd = os.open(state / "publisher.lock",
                     os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise PreviewError("unsafe_lock") from exc
    try:
        if os.fstat(fd).st_mode & 0o077:
            raise PreviewError("unsafe_lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PreviewError("busy") from exc
        with tempfile.TemporaryDirectory(prefix=".publisher-home-", dir=state) as home_name:
            env = _git_env(Path(home_name), fixture=config.allow_file_fixture)
            if _remote_head(config.source_url, env=env, cwd=state) != source_sha:
                raise PreviewError("snapshot_ref_mismatch")
            core_sha = _remote_head(config.core_url, env=env, cwd=state)
            bound = {
                "version": _VERSION, "source": source_sha,
                "core": core_sha, "policy": policy, "revision": revision,
            }
            key = hashlib.sha256(json.dumps(bound, sort_keys=True).encode()).hexdigest()
            receipt = state / "receipts" / f"{key}.json"
            run_dir = state / "runs" / key
            if receipt.exists() or receipt.is_symlink():
                return _cached(receipt, run_dir, source=source_sha, core=core_sha,
                               policy=policy, revision=revision, key=key, env=env)
            if run_dir.exists() or run_dir.is_symlink():
                raise PreviewError("unreconciled_prior_run")
            run_dir.mkdir(mode=0o700)
            _atomic_receipt(run_dir / "intent.json", {
                "version": _VERSION, "run_key": key, "state": "started",
                "source_sha": source_sha, "core_before_sha": core_sha,
                "policy_sha256": policy, "publisher_revision": revision,
            })
            try:
                frozen_policy = run_dir / "policy.toml"
                _freeze_policy(config.policy_path, frozen_policy, policy)
                _clone_exact(config.source_url, run_dir / "source", source_sha,
                             env=env, cwd=run_dir)
                _clone_exact(config.core_url, run_dir / "core", core_sha,
                             env=env, cwd=run_dir)
                _run_publisher(run_dir / "source", run_dir / "core", frozen_policy,
                               env=env, cwd=run_dir)
                # The validation step executes repository-owned Node code.
                # Reject inputs or frozen policy altered during that step.
                if (_git(["rev-parse", "HEAD"], env=env, cwd=run_dir / "source") != source_sha
                    or _git(["status", "--porcelain"], env=env, cwd=run_dir / "source")):
                    raise PreviewError("source_worktree_mutated")
                if _policy_sha(frozen_policy) != policy:
                    raise PreviewError("policy_mutated")
                candidate = _candidate_sha(run_dir / "core", core_sha, env=env,
                                           policy_path=frozen_policy)
                _assert_projection_converged(run_dir / "source", run_dir / "core",
                                             frozen_policy)
                if _remote_head(config.source_url, env=env, cwd=state) != source_sha:
                    raise PreviewError("source_moved_after_preview")
                if _remote_head(config.core_url, env=env, cwd=state) != core_sha:
                    raise PreviewError("core_moved_after_preview")
                status = "candidate_prepared" if candidate else "no_changes"
                _atomic_receipt(receipt, {
                    "version": _VERSION, "run_key": key, "status": status,
                    "source_sha": source_sha, "core_before_sha": core_sha,
                    "candidate_sha": candidate, "policy_sha256": policy,
                    "publisher_revision": revision,
                })
                return PreviewResult(status, key, source_sha, core_sha, candidate, policy)
            except PreviewError as exc:
                _atomic_receipt(run_dir / "failure.json", {
                    "version": _VERSION, "run_key": key,
                    "status": "failed", "reason": exc.reason,
                    "source_sha": source_sha, "core_before_sha": core_sha,
                    "policy_sha256": policy, "publisher_revision": revision,
                })
                raise
    finally:
        os.close(fd)
