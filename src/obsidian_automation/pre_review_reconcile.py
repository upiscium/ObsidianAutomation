from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Sequence

from .artifact_lifecycle import (
    ArtifactLifecycleError,
    _read_exact_file,
    ensure_artifact_layout,
    load_review_record,
)
from .execution_orchestrator import _parse_receipt
from .pre_review_job import (
    PreReviewJobError,
    _connect_rw,
    _load_stage_output_conn,
)


ACTIVE_RECONCILE_STATES = (
    "awaiting_human_review",
    "approved_pending_execution",
)


class PreReviewReconcileError(ArtifactLifecycleError):
    """Raised when authoritative post-review state and scheduler metadata diverge."""


def reconcile_post_review(
    ai_root: Path,
    *,
    max_items: int = 64,
) -> dict[str, object]:
    if type(max_items) is not int or not 1 <= max_items <= 256:
        raise PreReviewReconcileError("max_items must be in [1, 256]")

    layout = ensure_artifact_layout(ai_root)
    conn = _connect_rw(ai_root)
    changed = 0
    approved_pending = 0
    rejected = 0
    completed = 0

    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """
            SELECT g.generation_id, g.state
            FROM generations g
            WHERE g.state IN ('awaiting_human_review', 'approved_pending_execution')
              AND NOT EXISTS (
                SELECT 1
                FROM generations newer
                WHERE newer.job_id = g.job_id
                  AND newer.generation_index > g.generation_index
              )
            ORDER BY g.created_at, g.generation_id
            LIMIT ?
            """,
            (max_items,),
        ).fetchall()

        for row in rows:
            generation_id = str(row["generation_id"])
            output = _load_stage_output_conn(
                conn,
                generation_id,
                "evaluation",
            )
            if output is None:
                raise PreReviewReconcileError(
                    "Human Review state has no selected Evaluation output"
                )

            mutation_sha = str(output["mutation_sha256"])
            evaluation_sha = str(output["evaluation_sha256"])
            review_path = layout.review / f"{mutation_sha}.approval.json"

            if not os.path.lexists(review_path):
                if row["state"] != "awaiting_human_review":
                    raise PreReviewReconcileError(
                        "approved scheduler state lost authoritative Review"
                    )
                continue

            review = load_review_record(ai_root, mutation_sha)
            if (
                review.record_version != 2
                or review.evaluation_sha256 != evaluation_sha
            ):
                raise PreReviewReconcileError(
                    "authoritative Review does not match selected Evaluation"
                )

            receipt_path = layout.receipts / f"{mutation_sha}.receipt.json"

            if review.decision == "reject":
                if os.path.lexists(receipt_path):
                    raise PreReviewReconcileError(
                        "rejected Review unexpectedly has an execution Receipt"
                    )
                target = "human_rejected"
                rejected += 1
            else:
                if os.path.lexists(receipt_path):
                    receipt = _parse_receipt(_read_exact_file(receipt_path))
                    if receipt.mutation_sha256 != mutation_sha:
                        raise PreReviewReconcileError(
                            "execution Receipt is bound to another mutation"
                        )
                    target = "completed"
                    completed += 1
                else:
                    target = "approved_pending_execution"
                    approved_pending += 1

            if row["state"] != target:
                conn.execute(
                    "UPDATE generations SET state = ?, updated_at = "
                    "strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                    "WHERE generation_id = ?",
                    (target, generation_id),
                )
                changed += 1

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {
        "event": "pre-review-post-review-reconcile",
        "status": "completed",
        "changed": changed,
        "approved_pending_execution": approved_pending,
        "human_rejected": rejected,
        "completed": completed,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="obsidian-pre-review-post-review-reconcile"
    )
    parser.add_argument("--ai-root", type=Path, required=True)
    parser.add_argument("--max-items", type=int, default=64)
    args = parser.parse_args(argv)

    try:
        result = reconcile_post_review(
            args.ai_root,
            max_items=args.max_items,
        )
    except (
        ArtifactLifecycleError,
        PreReviewJobError,
        PreReviewReconcileError,
        OSError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    import json

    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0
