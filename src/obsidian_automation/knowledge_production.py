from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .artifact_lifecycle import ArtifactLifecycleError, _require_sha256
from .canonical_mutation import MutationValidationError
from .execution_orchestrator import ExecutionOrchestrationError, _load_context
from .knowledge_note_policy import KNOWLEDGE_ROOT, validate_knowledge_note_v0
from .production_orchestrator import (
    ProductionOrchestrationError,
    advance_production_executor,
    process_transport_request,
)
from .webdav_create import WebDAVCreateError, _read_password


def executor_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="obsidian-production-knowledge-executor")
    parser.add_argument("--ai-root", type=Path, required=True)
    parser.add_argument("--vault-root", type=Path, required=True)
    parser.add_argument("--mutation-sha256", required=True)
    args = parser.parse_args(argv)

    try:
        state = advance_production_executor(
            args.ai_root,
            args.vault_root,
            args.mutation_sha256,
            allowed_roots=[KNOWLEDGE_ROOT],
            note_policy=validate_knowledge_note_v0,
        )
    except (
        ArtifactLifecycleError,
        ExecutionOrchestrationError,
        MutationValidationError,
        ProductionOrchestrationError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(json.dumps({"status": state.status, "reason": state.reason}, sort_keys=True))
    return 0


def worker_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="obsidian-production-knowledge-webdav-worker")
    parser.add_argument("--ai-root", type=Path, required=True)
    parser.add_argument("--mutation-sha256", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--password-file", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)

    try:
        digest = _require_sha256(args.mutation_sha256, label="mutation_sha256")
        _, mutation, _, _ = _load_context(
            args.ai_root,
            digest,
            allowed_roots=[KNOWLEDGE_ROOT],
        )
        # Reapply the deterministic content/path contract immediately before
        # the credential-holding transport is allowed to contact Nextcloud.
        validate_knowledge_note_v0(mutation)
        password = _read_password(args.password_file)
        result = process_transport_request(
            args.ai_root,
            digest,
            allowed_roots=[KNOWLEDGE_ROOT],
            base_url=args.base_url,
            username=args.username,
            password=password,
            timeout=args.timeout,
        )
    except (
        ArtifactLifecycleError,
        ExecutionOrchestrationError,
        MutationValidationError,
        ProductionOrchestrationError,
        WebDAVCreateError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "result": result.result,
                "target_path": result.target_path,
                "content_sha256": result.expected_content_sha256,
            },
            sort_keys=True,
        )
    )
    return 0
