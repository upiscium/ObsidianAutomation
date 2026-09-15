from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .artifact_lifecycle import (
    ArtifactLifecycleError,
    _read_exact_file,
    _require_sha256,
    load_review_record,
    sha256_bytes,
    store_evaluation_bound_review_record,
)
from .evaluation_artifact import _load_accepted_mutation, load_evaluation_record


@dataclass(frozen=True)
class KnowledgeReviewResult:
    proposal_sha256: str
    mutation_sha256: str
    evaluation_sha256: str
    decision: str
    review_path: Path
    review_sha256: str


def create_evaluation_bound_review(
    ai_root: Path,
    *,
    evaluation_sha256: str,
    decision: str,
    approver: str,
    decided_at: str | None = None,
) -> KnowledgeReviewResult:
    evaluation_digest = _require_sha256(
        evaluation_sha256,
        label="evaluation_sha256",
    )
    evaluation = load_evaluation_record(ai_root, evaluation_digest)

    accepted_mutation, _, _ = _load_accepted_mutation(
        ai_root,
        evaluation.proposal_sha256,
    )
    if accepted_mutation != evaluation.mutation_sha256:
        raise ArtifactLifecycleError(
            "evaluation mutation does not match accepted validation"
        )

    review_path = store_evaluation_bound_review_record(
        ai_root,
        mutation_sha256=evaluation.mutation_sha256,
        evaluation_sha256=evaluation_digest,
        decision=decision,
        approver=approver,
        decided_at=decided_at,
    )
    review_bytes = _read_exact_file(review_path)
    review = load_review_record(ai_root, evaluation.mutation_sha256)
    if review.record_version != 2:
        raise ArtifactLifecycleError("new Human Review must use record_version 2")
    if review.evaluation_sha256 != evaluation_digest:
        raise ArtifactLifecycleError("review record is bound to another evaluation")

    return KnowledgeReviewResult(
        proposal_sha256=evaluation.proposal_sha256,
        mutation_sha256=evaluation.mutation_sha256,
        evaluation_sha256=evaluation_digest,
        decision=review.decision,
        review_path=review_path,
        review_sha256=sha256_bytes(review_bytes),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="obsidian-knowledge-review")
    parser.add_argument("--ai-root", type=Path, required=True)
    parser.add_argument("--evaluation-sha256", required=True)
    parser.add_argument("--decision", choices=("approve", "reject"), required=True)
    parser.add_argument("--approver", required=True)
    args = parser.parse_args(argv)

    try:
        result = create_evaluation_bound_review(
            args.ai_root,
            evaluation_sha256=args.evaluation_sha256,
            decision=args.decision,
            approver=args.approver,
        )
    except (ArtifactLifecycleError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "proposal_sha256": result.proposal_sha256,
                "mutation_sha256": result.mutation_sha256,
                "evaluation_sha256": result.evaluation_sha256,
                "decision": result.decision,
                "review_path": str(result.review_path),
                "review_sha256": result.review_sha256,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0
