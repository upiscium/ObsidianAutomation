from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Sequence

from .artifact_lifecycle import sha256_bytes
from .core_promotion_transport import PromotionTransportError, initialize_checkpoint
from .promotion_deployment import (
    CORE_REPOSITORY_URL,
    DEFAULT_POLICY_PATH,
    DEFAULT_STATE_ROOT,
    GitRunner,
    PromotionDeploymentError,
    _ensure_core_repository,
    _head_commit,
    _production_lock,
    _run_git,
    _safe_directory,
)
from .public_export import ExportError, load_config


GENERATED_PROJECTION_SUBJECT = "Sync public projection from ObsidianVault"


def bootstrap_production_checkpoint(
    *,
    state_root: Path,
    config_path: Path,
    core_commit: str,
    repository_url: str = CORE_REPOSITORY_URL,
    git_runner: GitRunner = _run_git,
) -> tuple[str, str, Path]:
    if repository_url != CORE_REPOSITORY_URL and git_runner is _run_git:
        raise PromotionDeploymentError("production Core repository URL is fixed to upiscium/ObsidianCore")
    root = _safe_directory(state_root, create=True)
    checkpoint_path = root / "checkpoint.json"

    with _production_lock(root / "promotion.lock"):
        core = _ensure_core_repository(
            root,
            repository_url=repository_url,
            git_runner=git_runner,
        )
        head = _head_commit(core, git_runner=git_runner)
        resolved = git_runner(["rev-parse", "--verify", f"{core_commit}^{{commit}}"], core)
        if resolved != core_commit:
            raise PromotionDeploymentError(
                "production bootstrap requires the full exact lowercase Core commit SHA"
            )

        ancestor = subprocess.run(
            ["git", "merge-base", "--is-ancestor", resolved, head],
            cwd=core,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        if ancestor.returncode == 1:
            raise PromotionDeploymentError(
                "bootstrap Core commit is not an ancestor of fetched ObsidianCore/main"
            )
        if ancestor.returncode != 0:
            raise PromotionDeploymentError(
                f"cannot verify bootstrap Core ancestry: {ancestor.stderr.strip()}"
            )

        subject = git_runner(["show", "-s", "--format=%s", resolved], core)
        if not subject.startswith(GENERATED_PROJECTION_SUBJECT):
            raise PromotionDeploymentError(
                "production bootstrap commit is not identified as a generated ObsidianVault projection"
            )

        try:
            load_config(config_path)
            policy_sha = sha256_bytes(config_path.read_bytes())
            initialize_checkpoint(
                checkpoint_path,
                core_commit=resolved,
                policy_sha256=policy_sha,
            )
        except (OSError, ExportError, PromotionTransportError) as exc:
            raise PromotionDeploymentError(str(exc)) from exc

    return resolved, head, checkpoint_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="obsidian-core-promotion-bootstrap",
        description=(
            "Initialize the private production promotion checkpoint from an explicit "
            "generated ObsidianVault projection commit."
        ),
    )
    parser.add_argument("--state-root", type=Path, default=DEFAULT_STATE_ROOT)
    parser.add_argument("--config", type=Path, default=DEFAULT_POLICY_PATH)
    parser.add_argument("--core-commit", required=True)
    args = parser.parse_args(argv)

    try:
        baseline, head, checkpoint = bootstrap_production_checkpoint(
            state_root=args.state_root,
            config_path=args.config,
            core_commit=args.core_commit,
        )
    except (OSError, PromotionDeploymentError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "result": "initialized",
                "checkpoint": str(checkpoint),
                "baseline_commit": baseline,
                "fetched_head_commit": head,
            },
            sort_keys=True,
        )
    )
    return 0
