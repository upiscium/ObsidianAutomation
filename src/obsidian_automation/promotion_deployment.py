from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Sequence

from .artifact_lifecycle import sha256_bytes
from .core_promotion import (
    CORE_REPOSITORY,
    PROMOTION_POLICY_VERSION,
    PromotionError,
    build_promotion_plan,
    promotion_plan_sha256,
)
from .core_promotion_transport import (
    HTTPTransport,
    PromotionTransportConflict,
    PromotionTransportError,
    execute_promotion,
    load_checkpoint,
)
from .webdav_create import WebDAVCreateError, _read_password


CORE_REPOSITORY_URL = "https://github.com/upiscium/ObsidianCore.git"
CORE_BRANCH = "main"
DEFAULT_STATE_ROOT = Path("/var/lib/obsidian-core-promotion")
DEFAULT_POLICY_PATH = Path("/etc/obsidian-core-promotion/public-export.toml")
DEFAULT_PASSWORD_FILE = Path("/etc/obsidian-core-promotion/nextcloud.password")


class PromotionDeploymentError(RuntimeError):
    """Raised when production promotion orchestration cannot proceed safely."""


@dataclass(frozen=True)
class PromotionCycleResult:
    result: str
    base_commit: str
    head_commit: str
    plan_sha256: str | None
    plan_path: Path | None
    receipt_sha256: str | None
    receipt_path: Path | None


GitRunner = Callable[[Sequence[str], Path | None], str]


def _run_git(args: Sequence[str], cwd: Path | None = None) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as exc:
        raise PromotionDeploymentError("git is required for Core promotion deployment") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise PromotionDeploymentError(
            f"git command failed ({completed.returncode}): git {' '.join(args)}: {detail}"
        )
    return completed.stdout.strip()


def _safe_directory(path: Path, *, create: bool = False, mode: int = 0o700) -> Path:
    absolute = path.absolute()
    if create:
        absolute.mkdir(parents=True, exist_ok=True, mode=mode)
    try:
        info = absolute.lstat()
    except FileNotFoundError as exc:
        raise PromotionDeploymentError(f"required directory does not exist: {absolute}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise PromotionDeploymentError(f"path must be a non-symlink directory: {absolute}")
    return absolute


@contextmanager
def _production_lock(path: Path) -> Iterator[None]:
    parent = _safe_directory(path.parent, create=True)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(parent / path.name, flags, 0o600)
    except OSError as exc:
        raise PromotionDeploymentError("cannot open Core promotion production lock") from exc
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _assert_clean_core_repository(repository: Path, *, git_runner: GitRunner) -> None:
    top = Path(git_runner(["rev-parse", "--show-toplevel"], repository)).resolve()
    if top != repository.resolve():
        raise PromotionDeploymentError("Core cache must be the Git repository root")
    status = git_runner(["status", "--porcelain"], repository)
    if status:
        raise PromotionDeploymentError("Core cache must be clean before promotion fetch")


def _ensure_core_repository(
    state_root: Path,
    *,
    repository_url: str = CORE_REPOSITORY_URL,
    git_runner: GitRunner = _run_git,
) -> Path:
    core = state_root / "ObsidianCore"
    if not core.exists():
        temp = Path(tempfile.mkdtemp(prefix=".core-clone-", dir=state_root))
        try:
            shutil.rmtree(temp)
            git_runner(["clone", "--no-tags", repository_url, str(temp)], state_root)
            os.replace(temp, core)
        finally:
            if temp.exists():
                shutil.rmtree(temp, ignore_errors=True)
    if core.is_symlink() or not core.is_dir():
        raise PromotionDeploymentError("Core cache must be a non-symlink directory")

    _assert_clean_core_repository(core, git_runner=git_runner)
    remote = git_runner(["remote", "get-url", "origin"], core)
    if remote != repository_url:
        raise PromotionDeploymentError(
            f"Core cache origin mismatch: expected {repository_url!r}, got {remote!r}"
        )
    git_runner(
        ["fetch", "--no-tags", "--prune", "origin", "+refs/heads/main:refs/remotes/origin/main"],
        core,
    )
    return core


def _head_commit(repository: Path, *, git_runner: GitRunner) -> str:
    head = git_runner(["rev-parse", "--verify", "refs/remotes/origin/main^{commit}"], repository)
    if len(head) != 40 or any(ch not in "0123456789abcdef" for ch in head):
        raise PromotionDeploymentError("fetched ObsidianCore/main did not resolve to a lowercase SHA-1 commit")
    return head


def _policy_sha256(path: Path) -> str:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise PromotionDeploymentError(f"cannot read promotion policy: {path}") from exc
    return hashlib.sha256(data).hexdigest()


def _store_plan(directory: Path, plan) -> tuple[str, Path]:
    directory = _safe_directory(directory, create=True)
    data = plan.to_json_bytes()
    digest = promotion_plan_sha256(plan)
    if sha256_bytes(data) != digest:
        raise PromotionDeploymentError("promotion plan digest mismatch before persistence")
    path = directory / f"{digest}.promotion-plan.json"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        if path.is_symlink() or path.read_bytes() != data:
            raise PromotionDeploymentError("existing promotion plan hash path has different bytes")
        return digest, path
    try:
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(fd)
    return digest, path


def run_promotion_cycle(
    *,
    state_root: Path,
    config_path: Path,
    base_url: str,
    username: str,
    password_file: Path,
    timeout: float = 30.0,
    repository_url: str = CORE_REPOSITORY_URL,
    git_runner: GitRunner = _run_git,
    http_transport: HTTPTransport | None = None,
) -> PromotionCycleResult:
    if repository_url != CORE_REPOSITORY_URL and git_runner is _run_git:
        raise PromotionDeploymentError("production Core repository URL is fixed to upiscium/ObsidianCore")
    if not base_url or not username:
        raise PromotionDeploymentError("Nextcloud promotion base URL and username are required")
    root = _safe_directory(state_root, create=True)
    plans = _safe_directory(root / "plans", create=True)
    receipts = _safe_directory(root / "receipts", create=True)
    checkpoint_path = root / "checkpoint.json"

    with _production_lock(root / "promotion.lock"):
        core = _ensure_core_repository(
            root,
            repository_url=repository_url,
            git_runner=git_runner,
        )
        head = _head_commit(core, git_runner=git_runner)
        checkpoint = load_checkpoint(checkpoint_path)
        if checkpoint.source_repository != CORE_REPOSITORY:
            raise PromotionDeploymentError("promotion checkpoint source repository mismatch")
        policy_sha = _policy_sha256(config_path)
        if checkpoint.policy_version != PROMOTION_POLICY_VERSION or checkpoint.policy_sha256 != policy_sha:
            raise PromotionDeploymentError(
                "promotion checkpoint policy does not match the configured public-export policy"
            )
        base = checkpoint.last_observed_core_commit
        if base == head:
            return PromotionCycleResult(
                result="up_to_date",
                base_commit=base,
                head_commit=head,
                plan_sha256=None,
                plan_path=None,
                receipt_sha256=None,
                receipt_path=None,
            )

        try:
            plan = build_promotion_plan(
                core,
                base_ref=base,
                head_ref=head,
                config_path=config_path,
            )
        except PromotionError as exc:
            raise PromotionDeploymentError(str(exc)) from exc
        plan_sha, plan_path = _store_plan(plans, plan)
        try:
            password = _read_password(password_file)
            receipt_sha, receipt_path, _receipt, advanced = execute_promotion(
                plan=plan,
                core_repository=core,
                config_path=config_path,
                base_url=base_url,
                username=username,
                password=password,
                checkpoint_path=checkpoint_path,
                receipt_directory=receipts,
                timeout=timeout,
                transport=http_transport,
            )
        except (WebDAVCreateError, PromotionTransportError) as exc:
            raise
        if advanced.last_observed_core_commit != head:
            raise PromotionDeploymentError("promotion transport returned an unexpected checkpoint commit")
        return PromotionCycleResult(
            result="promoted",
            base_commit=base,
            head_commit=head,
            plan_sha256=plan_sha,
            plan_path=plan_path,
            receipt_sha256=receipt_sha,
            receipt_path=receipt_path,
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="obsidian-core-promotion-run",
        description=(
            "Fetch public ObsidianCore/main and run one serialized Core-to-Live-Vault "
            "promotion transaction from the private deployment checkpoint."
        ),
    )
    parser.add_argument("--state-root", type=Path, default=DEFAULT_STATE_ROOT)
    parser.add_argument("--config", type=Path, default=DEFAULT_POLICY_PATH)
    parser.add_argument("--base-url", default=os.environ.get("OBSIDIAN_PROMOTION_BASE_URL"))
    parser.add_argument("--username", default=os.environ.get("OBSIDIAN_PROMOTION_USERNAME"))
    parser.add_argument("--password-file", type=Path, default=DEFAULT_PASSWORD_FILE)
    parser.add_argument("--timeout", type=float, default=30.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = run_promotion_cycle(
            state_root=args.state_root,
            config_path=args.config,
            base_url=args.base_url or "",
            username=args.username or "",
            password_file=args.password_file,
            timeout=args.timeout,
        )
    except PromotionTransportConflict as exc:
        print(f"conflict: {exc}", file=sys.stderr)
        return 3
    except (OSError, PromotionDeploymentError, PromotionTransportError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "result": result.result,
                "base_commit": result.base_commit,
                "head_commit": result.head_commit,
                "plan_sha256": result.plan_sha256,
                "plan_path": None if result.plan_path is None else str(result.plan_path),
                "receipt_sha256": result.receipt_sha256,
                "receipt_path": None if result.receipt_path is None else str(result.receipt_path),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0
