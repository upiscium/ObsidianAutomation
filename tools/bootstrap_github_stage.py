#!/usr/bin/env python3
"""Install-free, exact-Git-object Stage entrypoint for first two-phase rollout.

WARNING: 'stage' changes the production checkout, installed package, systemd
units and timer states. This tool must only be run with explicit exact-SHA Stage
approval. 'inspect' loads the pinned implementation in a private temporary
directory, but does not create a Stage record or change production.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
from typing import Sequence


TARGET_RE = re.compile(r"^[0-9a-f]{40}$")
# Load only the reviewed updater's three stdlib-only modules from pinned Git
# objects, NOT from the currently installed older Python package.
REQUIRED_MODULES = (
    "src/obsidian_automation/__init__.py",
    "src/obsidian_automation/github_production_update.py",
    "src/obsidian_automation/github_production_staging.py",
)
MAX_BOOTSTRAP_MODULE_BYTES = 512 * 1024


class StageBootstrapError(RuntimeError):
    """Source identity/authority precondition was not established."""


def _safe_git_env() -> dict[str, str]:
    """Ignore inherited Git settings and disable replacement objects."""
    env = {
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": "/root",
        "LC_ALL": "C.UTF-8",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }
    return env


def _git(root: Path, *arguments: str, stdin: bytes | None = None) -> bytes:
    result = subprocess.run(
        ("/usr/bin/git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
         "-C", str(root), *arguments),
        input=stdin,
        capture_output=True,
        check=False,
        timeout=30,
        env=_safe_git_env(),
    )
    if result.returncode != 0:
        # Don't expose Git stderr, local paths or private remote addresses.
        raise StageBootstrapError("pinned source Git validation failed")
    return result.stdout


def _preflight_git(root: Path, target: str) -> str:
    if not TARGET_RE.fullmatch(target):
        raise StageBootstrapError("exact lowercase 40-character SHA required")
    if (not root.is_absolute() or ".." in root.parts or root.is_symlink()
            or not root.is_dir()):
        raise StageBootstrapError("trusted absolute production checkout required")
    # Reject replacement refs, even if Git object validation would be self-consistent.
    if _git(root, "for-each-ref", "--format=%(refname)", "refs/replace").strip():
        raise StageBootstrapError("Git replacement refs are not authorized")
    if _git(root, "branch", "--show-current").strip() != b"main":
        raise StageBootstrapError("production checkout is not on main")
    if _git(root, "status", "--porcelain").strip():
        raise StageBootstrapError("production checkout contains unreviewed changes")
    current = _git(root, "rev-parse", "HEAD").decode("ascii").strip()
    if not TARGET_RE.fullmatch(current):
        raise StageBootstrapError("current production Git commit identity invalid")
    resolved = _git(root, "rev-parse", "--verify", target + "^{commit}").decode("ascii").strip()
    if resolved != target:
        raise StageBootstrapError("target does not resolve to an exact commit")
    # A bootstrap must never select an unreleased Draft PR head, an unrelated
    # branch or a rollback. Refuse until the exact reviewed SHA was merged.
    for older, newer in ((target, "refs/remotes/origin/main"), (current, target)):
        result = subprocess.run(
            ("/usr/bin/git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
             "-C", str(root), "merge-base", "--is-ancestor", older, newer),
            capture_output=True, timeout=30, check=False, env=_safe_git_env(),
        )
        if result.returncode != 0:
            raise StageBootstrapError("target is not merged and fast-forward reachable")
    return current


def _pinned_module_bytes(root: Path, target: str, path: str) -> bytes:
    if path not in REQUIRED_MODULES:
        raise StageBootstrapError("module not in reviewed bootstrap allowlist")
    tree_line = _git(root, "ls-tree", target, "--", path).decode("utf-8")
    match = re.fullmatch(r"100644 blob ([0-9a-f]{40})\t" + re.escape(path) + r"\n", tree_line)
    if not match:
        raise StageBootstrapError("pinned updater module has unexpected inode/type")
    size = _git(root, "cat-file", "-s", target + ":" + path).decode("ascii").strip()
    if not size.isdigit() or not 1 <= int(size) <= MAX_BOOTSTRAP_MODULE_BYTES:
        raise StageBootstrapError("pinned updater module exceeds budget")
    payload = _git(root, "show", target + ":" + path)
    if len(payload) != int(size):
        raise StageBootstrapError("pinned updater module length differs")
    blob = _git(root, "hash-object", "--stdin", stdin=payload).decode("ascii").strip()
    if blob != match.group(1):
        raise StageBootstrapError("pinned updater source blob mismatch")
    return payload


def _private_temp_parent(*, stage: bool) -> Path:
    """Use a root-owned stable parent; inherited TMPDIR has no authority."""
    parent = Path("/run" if stage else "/tmp")
    info = parent.lstat()
    mode = stat.S_IMODE(info.st_mode)
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != 0
            or (stage and (mode & 0o022))
            or (not stage and (mode & 0o022) and not (mode & stat.S_ISVTX))):
        raise StageBootstrapError("private source import parent is not trusted")
    return parent


def _run_from_pinned_source(args: argparse.Namespace, previous_sha: str) -> dict[str, object]:
    from tempfile import TemporaryDirectory
    with TemporaryDirectory(prefix="obsa-stage-source-",
                            dir=_private_temp_parent(stage=args.phase == "stage")) as directory:
        private_root = Path(directory)
        private_root.chmod(0o700)
        package = private_root / "obsidian_automation"
        package.mkdir(mode=0o700)
        for path in REQUIRED_MODULES:
            raw = _pinned_module_bytes(args.app_root, args.target_sha, path)
            destination = package / Path(path).name
            with destination.open("xb") as sink:
                sink.write(raw)
            destination.chmod(0o600)
        # -I -B must be supplied by the documented operator invocation.
        # Put *only* the exact target's modules first, never production's old
        # venv module, and don't allow any imported stale package instance.
        if "obsidian_automation" in sys.modules:
            raise StageBootstrapError("bootstrap Python import space already polluted")
        sys.path.insert(0, str(private_root))
        try:
            from obsidian_automation import github_production_staging as staged
            if Path(staged.__file__).parent != package:
                raise StageBootstrapError("pinned updater was not loaded privately")
            if args.phase == "inspect":
                status = staged.inspect_stage(receipt_dir=args.receipt_dir)
                return {
                    "status": "source_attested_no_effects",
                    "current_sha": previous_sha,
                    "reviewed_target_sha": args.target_sha,
                    "stage_control": status.get("status", "unknown"),
                    "stage_effects_authorized": False,
                    "installed_runtime_verified": False,
                }
            if not args.approve_stage_effects:
                raise StageBootstrapError("explicit exact-SHA Stage consent missing")
            # Stage implementation validates and records the exact target again,
            # after disabling timers, without any live Writer/Sync smoke.
            stage_receipt = staged.stage_update(
                target_sha=args.target_sha,
                app_root=args.app_root,
                venv_root=args.venv_root,
                systemd_dir=args.systemd_dir,
                receipt_dir=args.receipt_dir,
                daily_revision_env=args.daily_revision_env,
                config_files=tuple(args.bind_config_file),
            )
            return {
                "status": "staged",
                "target_sha": args.target_sha,
                "stage_sha256": stage_receipt,
                "live_writer_smoke": "not_authorized",
                "daily_timer_restore": "not_authorized",
            }
        finally:
            sys.path.remove(str(private_root))
            for key in tuple(sys.modules):
                if key == "obsidian_automation" or key.startswith("obsidian_automation."):
                    del sys.modules[key]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run a pinned, install-free first Stage source only under explicit consent"
    )
    parser.add_argument("phase", choices=("inspect", "stage"))
    parser.add_argument("--target-sha", required=True)
    parser.add_argument("--app-root", type=Path, required=True)
    parser.add_argument("--venv-root", type=Path, required=True)
    parser.add_argument("--systemd-dir", type=Path, default=Path("/etc/systemd/system"))
    parser.add_argument(
        "--receipt-dir", type=Path,
        default=Path("/var/lib/obsidian-github-sync/deployments"),
    )
    parser.add_argument(
        "--daily-revision-env", type=Path,
        default=Path("/etc/obsidian-github-summarizer/revision.env"),
    )
    parser.add_argument(
        "--bind-config-file", type=Path, action="append",
        default=None,
    )
    parser.add_argument("--approve-stage-effects", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.bind_config_file is None:
        args.bind_config_file = [Path("/etc/obsidian-github-summarizer/config.env")]
    if args.phase == "inspect" and args.approve_stage_effects:
        parser.error("inspect cannot receive Stage-effects approval")
    try:
        if not sys.flags.isolated or not sys.dont_write_bytecode:
            raise StageBootstrapError("isolated Python -I -B is required")
        paths = (
            args.app_root, args.venv_root, args.systemd_dir,
            args.receipt_dir, args.daily_revision_env, *args.bind_config_file,
        )
        if (len(paths) > 24 or any(
            not path.is_absolute() or ".." in path.parts
            for path in paths
        )):
            raise StageBootstrapError("unsafe bootstrap operator path")

        if args.phase == "stage" and not args.approve_stage_effects:
            raise StageBootstrapError("Stage requires separately authorized effects")
        if args.phase == "stage" and os.geteuid() != 0:
            raise StageBootstrapError("root required for Stage")
        previous_sha = _preflight_git(args.app_root, args.target_sha)
        result = _run_from_pinned_source(args, previous_sha)
    except Exception as exc:
        # Once Stage was authorized, even a failed command may have disabled
        # timers or partially installed reviewed code. Never imply rollback
        # or a safe automatic retry. This is intentionally conservative.
        stage_requested = args.phase == "stage" and args.approve_stage_effects
        print(json.dumps({
            "status": "failed_manual_reconciliation_required"
            if stage_requested else "blocked",
            "error_class": type(exc).__name__,
            "stage_effects_may_have_occurred": bool(stage_requested),
            "automatic_retry_authorized": False,
        }, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
