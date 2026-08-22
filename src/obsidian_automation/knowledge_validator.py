from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Sequence

from .artifact_lifecycle import (
    ArtifactLifecycleError,
    _read_exact_file,
    _require_sha256,
    ensure_artifact_layout,
    parse_validation_record,
    sha256_bytes,
    store_validated_mutation,
    store_validation_record,
)
from .canonical_mutation import MutationValidationError, validate_create_note
from .knowledge_note_policy import KNOWLEDGE_ROOT, POLICY_NAME, validate_knowledge_note_v0


def _existing_validation(ai_root: Path, proposal_sha256: str) -> dict[str, object] | None:
    path = ai_root / "10-Validation" / f"{proposal_sha256}.validation.json"
    if not os.path.lexists(path):
        return None
    record = parse_validation_record(_read_exact_file(path))
    if record.proposal_sha256 != proposal_sha256:
        raise ArtifactLifecycleError("validation record is bound to another proposal")

    if record.result == "accepted":
        if record.mutation_sha256 is None:
            raise ArtifactLifecycleError("accepted validation is missing mutation_sha256")
        mutation_path = ai_root / "10-Validation" / f"{record.mutation_sha256}.mutation.json"
        mutation_bytes = _read_exact_file(mutation_path)
        if sha256_bytes(mutation_bytes) != record.mutation_sha256:
            raise ArtifactLifecycleError("validated mutation artifact hash mismatch")

    return {
        "policy": POLICY_NAME,
        "proposal_sha256": proposal_sha256,
        "result": record.result,
        "mutation_sha256": record.mutation_sha256,
        "reason": record.reason,
        "reused": True,
    }


def validate_proposal(
    ai_root: Path,
    vault_root: Path,
    proposal_sha256: str,
) -> dict[str, object]:
    digest = _require_sha256(proposal_sha256, label="proposal_sha256")
    layout = ensure_artifact_layout(ai_root)

    existing = _existing_validation(layout.root, digest)
    if existing is not None:
        return existing

    proposal_path = layout.untrusted / f"{digest}.proposal.json"
    proposal_bytes = _read_exact_file(proposal_path)
    if sha256_bytes(proposal_bytes) != digest:
        raise ArtifactLifecycleError("untrusted proposal artifact hash mismatch")

    try:
        validated = validate_create_note(
            proposal_bytes,
            vault_root=vault_root,
            allowed_roots=[KNOWLEDGE_ROOT],
            note_policy=validate_knowledge_note_v0,
        )
    except MutationValidationError as exc:
        reason = str(exc)
        store_validation_record(
            layout.root,
            proposal_sha256=digest,
            result="rejected",
            reason=reason,
        )
        return {
            "policy": POLICY_NAME,
            "proposal_sha256": digest,
            "result": "rejected",
            "mutation_sha256": None,
            "reason": reason,
            "reused": False,
        }

    store_validated_mutation(layout.root, validated)
    store_validation_record(
        layout.root,
        proposal_sha256=digest,
        result="accepted",
        mutation_sha256=validated.mutation_sha256,
    )
    return {
        "policy": POLICY_NAME,
        "proposal_sha256": digest,
        "result": "accepted",
        "mutation_sha256": validated.mutation_sha256,
        "reason": None,
        "reused": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="obsidian-knowledge-validator")
    parser.add_argument("--ai-root", type=Path, required=True)
    parser.add_argument("--vault-root", type=Path, required=True)
    parser.add_argument("--proposal-sha256", required=True)
    args = parser.parse_args(argv)

    try:
        result = validate_proposal(
            args.ai_root,
            args.vault_root,
            args.proposal_sha256,
        )
    except (ArtifactLifecycleError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0
