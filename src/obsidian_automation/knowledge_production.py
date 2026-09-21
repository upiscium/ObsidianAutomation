from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path
from typing import Sequence

from .artifact_lifecycle import (
    ArtifactLifecycleError,
    _require_sha256,
    ensure_artifact_layout,
    load_review_record,
)
from .canonical_mutation import MutationValidationError
from .execution_orchestrator import ExecutionOrchestrationError, _load_context
from .knowledge_note_policy import KNOWLEDGE_ROOT, validate_knowledge_note_v0
from .production_io import ProductionIOError, canonical_io_lock
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
        # Reapply the deterministic content/path contract before taking the
        # global I/O lock or reading the production credential.
        validate_knowledge_note_v0(mutation)
        # Canonical remote effects and pull-only mirror refreshes must never
        # overlap. The transport then takes its existing per-mutation lock
        # inside this global lock (global -> mutation lock order).
        with canonical_io_lock(args.ai_root):
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
        ProductionIOError,
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


def _digest_files(directory: Path, suffix: str) -> list[tuple[str, Path]]:
    try:
        info = directory.lstat()
    except FileNotFoundError as exc:
        raise ProductionOrchestrationError(
            f"required dispatch directory is missing: {directory}"
        ) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ProductionOrchestrationError(
            f"dispatch directory is unsafe: {directory}"
        )

    rows: list[tuple[str, Path]] = []
    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        if path.name.startswith(".") or not path.name.endswith(suffix):
            continue
        item = path.lstat()
        if stat.S_ISLNK(item.st_mode) or not stat.S_ISREG(item.st_mode):
            raise ProductionOrchestrationError(
                f"dispatch queue contains unsafe entry: {path}"
            )
        digest = _require_sha256(
            path.name[: -len(suffix)],
            label="dispatch mutation_sha256",
        )
        rows.append((digest, path))
    return rows


def dispatch_pending_executor(
    ai_root: Path,
    vault_root: Path,
    *,
    max_items: int = 16,
) -> dict[str, object]:
    if type(max_items) is not int or not 1 <= max_items <= 64:
        raise ProductionOrchestrationError("max_items must be in [1, 64]")

    layout = ensure_artifact_layout(ai_root)
    processed = 0
    rejected = 0
    completed = 0
    transport_pending = 0

    for digest, _path in _digest_files(layout.review, ".approval.json"):
        review = load_review_record(ai_root, digest)
        if review.decision == "reject":
            rejected += 1
            continue

        receipt_path = layout.receipts / f"{digest}.receipt.json"
        if os.path.lexists(receipt_path):
            completed += 1
            continue

        state = advance_production_executor(
            ai_root,
            vault_root,
            digest,
            allowed_roots=[KNOWLEDGE_ROOT],
            note_policy=validate_knowledge_note_v0,
        )
        processed += 1

        if state.status == "completed":
            completed += 1
        elif state.status in {"request_pending", "transport_pending"}:
            transport_pending += 1
        elif state.status == "remote_verified_pending_receipt":
            # advance_production_executor normally consumes this state before
            # returning; retaining the guard keeps the dispatcher fail closed.
            raise ProductionOrchestrationError(
                "executor dispatcher returned an unfinalized verified remote effect"
            )
        elif state.status in {
            "remote_effect_observed_without_receipt",
            "conflict",
            "resolved_abandoned",
            "resolved_effect_adopted",
        }:
            raise ProductionOrchestrationError(
                f"executor dispatcher requires Human recovery: {state.status}"
            )
        else:
            raise ProductionOrchestrationError(
                f"executor dispatcher reached unexpected state: {state.status}"
            )

        if processed >= max_items:
            break

    return {
        "event": "knowledge-executor-dispatch",
        "status": "completed",
        "processed": processed,
        "rejected": rejected,
        "completed": completed,
        "transport_pending": transport_pending,
    }


def dispatch_pending_transport(
    ai_root: Path,
    *,
    base_url: str,
    username: str,
    password: str,
    timeout: float = 30.0,
    max_items: int = 16,
) -> dict[str, object]:
    if type(max_items) is not int or not 1 <= max_items <= 64:
        raise ProductionOrchestrationError("max_items must be in [1, 64]")

    execution = ai_root.absolute() / "25-Execution"
    transport = ai_root.absolute() / "27-Transport"
    processed = 0
    existing = 0

    for digest, _path in _digest_files(execution, ".transport-request.json"):
        result_path = transport / f"{digest}.transport-result.json"
        if os.path.lexists(result_path):
            existing += 1
            continue

        _, mutation, _, _ = _load_context(
            ai_root,
            digest,
            allowed_roots=[KNOWLEDGE_ROOT],
        )
        validate_knowledge_note_v0(mutation)

        with canonical_io_lock(ai_root):
            result = process_transport_request(
                ai_root,
                digest,
                allowed_roots=[KNOWLEDGE_ROOT],
                base_url=base_url,
                username=username,
                password=password,
                timeout=timeout,
            )

        processed += 1
        if result.result != "created_verified":
            raise ProductionOrchestrationError(
                f"transport dispatcher requires Human recovery: {result.result}"
            )
        if processed >= max_items:
            break

    return {
        "event": "knowledge-transport-dispatch",
        "status": "completed",
        "processed": processed,
        "existing": existing,
    }


def executor_dispatch_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="obsidian-production-knowledge-executor-dispatch"
    )
    parser.add_argument("--ai-root", type=Path, required=True)
    parser.add_argument("--vault-root", type=Path, required=True)
    parser.add_argument("--max-items", type=int, default=16)
    args = parser.parse_args(argv)

    try:
        result = dispatch_pending_executor(
            args.ai_root,
            args.vault_root,
            max_items=args.max_items,
        )
    except (
        ArtifactLifecycleError,
        ExecutionOrchestrationError,
        MutationValidationError,
        ProductionOrchestrationError,
        OSError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(result, sort_keys=True))
    return 0


def transport_dispatch_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="obsidian-production-knowledge-webdav-dispatch"
    )
    parser.add_argument("--ai-root", type=Path, required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--password-file", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--max-items", type=int, default=16)
    args = parser.parse_args(argv)

    try:
        result = dispatch_pending_transport(
            args.ai_root,
            base_url=args.base_url,
            username=args.username,
            password=_read_password(args.password_file),
            timeout=args.timeout,
            max_items=args.max_items,
        )
    except (
        ArtifactLifecycleError,
        ExecutionOrchestrationError,
        MutationValidationError,
        ProductionIOError,
        ProductionOrchestrationError,
        WebDAVCreateError,
        OSError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(result, sort_keys=True))
    return 0
