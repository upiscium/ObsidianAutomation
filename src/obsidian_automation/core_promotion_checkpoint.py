from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .artifact_lifecycle import sha256_bytes
from .core_promotion import PromotionError, _repository_root, _resolve_commit
from .core_promotion_transport import PromotionTransportError, initialize_checkpoint
from .public_export import ExportError, load_config


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="obsidian-core-promotion-checkpoint-init")
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--core-repo", type=Path, required=True)
    parser.add_argument("--core-commit", required=True)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        repository = _repository_root(args.core_repo)
        resolved = _resolve_commit(repository, args.core_commit, label="core_commit")
        # Parse the policy before binding its exact bytes into the checkpoint.
        load_config(args.config)
        policy_sha = sha256_bytes(args.config.read_bytes())
        checkpoint = initialize_checkpoint(
            args.checkpoint,
            core_commit=resolved,
            policy_sha256=policy_sha,
        )
    except (OSError, ExportError, PromotionError, PromotionTransportError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "checkpoint": str(args.checkpoint),
                "last_observed_core_commit": checkpoint.last_observed_core_commit,
                "policy_sha256": checkpoint.policy_sha256,
            },
            sort_keys=True,
        )
    )
    return 0
