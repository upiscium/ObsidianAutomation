"""Synthetic-only External Source adapter for the EXISTING Human Review v3.

Reuses the existing accepted Validation→Evaluation→ReviewRecord v3 writer,
with protected private Source/HMAC/revision checked at the decision time.
Only non-execution dispositions are permitted: reject and keep_as_idea.
No second Human Review ledger, new approval UI or canonical Vault executor.

This is not production authorization. A dedicated Reviewer identity/private
transport and semantic confirmation are prerequisites for real Export review.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .artifact_lifecycle import (
    ArtifactLifecycleError,
    _read_exact_file,
    _require_sha256,
    ensure_artifact_layout,
    load_review_record,
    sha256_bytes,
)
from .chatgpt_export_reader import (
    PrivateSourceReaderError,
    _existing_private_root,
    _read_locked,
)
from .chatgpt_export_review_preview import ExternalReviewPreview
from .chatgpt_external_review_evidence import (
    ExternalReviewEvidenceError,
    read_private_review_evidence,
)
from .evaluation_artifact import load_evaluation_record
from .knowledge_review import (
    KnowledgeReviewResult,
    _store_validated_evaluation_disposition,
)
from .pre_review_job import (
    PreReviewJobError,
    _connect_rw,
    _load_stage_output_conn,
)

EXTERNAL_REVIEW_RECORDED_STATE = "external_review_recorded"
_NONEXECUTION_DECISIONS = frozenset({"reject", "keep_as_idea"})


class ExternalDispositionError(ValueError):
    """Fixed error codes only, never echo private Export text or identifiers."""


@dataclass(frozen=True, repr=False)
class ExternalDispositionReceipt:
    """Reuses an authoritative ReviewRecord, but cannot authorize execution."""

    record_version: int
    job_id: str
    generation_id: str
    evaluation_sha256: str
    proposal_sha256: str
    mutation_sha256: str
    review_sha256: str
    source_revision_sha256: str
    evidence_packet_sha256: str
    decision: str
    state: str = EXTERNAL_REVIEW_RECORDED_STATE
    human_approval_granted: bool = False
    canonical_mutation_authorized: bool = False


def _sha(value: object, name: str) -> str:
    try:
        return _require_sha256(value, label=name)
    except (ArtifactLifecycleError, TypeError, ValueError):
        raise ExternalDispositionError("external_review_digest_invalid") from None


def _already_recorded_review(
    ai_root: Path,
    *,
    evaluation_sha: str,
    mutation_sha: str,
    proposal_sha: str,
    decision: str,
    approver: str,
) -> KnowledgeReviewResult | None:
    """Crash recovery after ReviewRecord persisted but before Job state."""
    path = ensure_artifact_layout(ai_root).review / f"{mutation_sha}.approval.json"
    if not os.path.lexists(path):
        return None
    try:
        record = load_review_record(ai_root, mutation_sha)
        if (
            record.record_version != 3
            or record.mutation_sha256 != mutation_sha
            or record.evaluation_sha256 != evaluation_sha
            or record.decision != decision
            or record.approver != approver
            or record.decision not in _NONEXECUTION_DECISIONS
        ):
            raise ExternalDispositionError("external_review_record_collision")
        return KnowledgeReviewResult(
            proposal_sha256=proposal_sha,
            mutation_sha256=mutation_sha,
            evaluation_sha256=evaluation_sha,
            decision=record.decision,
            review_path=path,
            review_sha256=sha256_bytes(_read_exact_file(path)),
        )
    except ExternalDispositionError:
        raise
    except (ArtifactLifecycleError, OSError):
        raise ExternalDispositionError("external_review_record_collision") from None


def record_external_nonapproval_disposition(
    ai_root: Path,
    *,
    private_root: Path,
    preview: ExternalReviewPreview,
    job_id: str,
    evaluation_sha256: str,
    expected_source_revision_sha256: str,
    expected_evidence_packet_sha256: str,
    decision: str,
    approver: str,
    decided_at: str | None = None,
) -> ExternalDispositionReceipt:
    """Record human rejection/idea disposition using normal v3 ReviewRecord.

    The Source Reader's SH lock excludes a concurrent private Import writer
    during Source reread, ReviewRecord CAS and Job transition. The existing
    Reconciler then consumes this record and transitions the Job to the
    normal non-executing human_rejected/human_kept_as_idea terminal state.

    In particular "approve" is prohibited. Real external user confirmation,
    correction chronology, real model evaluations and canonical execution
    are NOT supported by this synthetic-only MVI adapter.
    """
    if type(decision) is not str or decision not in _NONEXECUTION_DECISIONS:
        raise ExternalDispositionError("external_approval_not_enabled")
    if (
        type(approver) is not str
        or not approver.strip()
        or "\x00" in approver
        or len(approver) > 256
        or (
            decided_at is not None
            and (type(decided_at) is not str or not decided_at.endswith("Z"))
        )
    ):
        raise ExternalDispositionError("external_reviewer_identity_invalid")
    expected_job = _sha(job_id, "external_job_id")
    expected_eval = _sha(evaluation_sha256, "external_evaluation_sha256")
    expected_revision = _sha(
        expected_source_revision_sha256,
        "external_source_revision",
    )
    expected_packet = _sha(
        expected_evidence_packet_sha256,
        "external_evidence_packet",
    )
    if type(preview) is not ExternalReviewPreview or preview.scope != "fake-e2e":
        raise ExternalDispositionError("external_review_source_invalid")

    try:
        root = _existing_private_root(private_root)
        with _read_locked(root):
            evidence = read_private_review_evidence(
                ai_root,
                private_root=root,
                preview=preview,
                job_id=expected_job,
            )
            if (
                evidence.job_id != expected_job
                or evidence.source_revision_sha256 != expected_revision
                or evidence.source_preview_sha256 != preview.preview_digest
                or evidence.evaluation_sha256 != expected_eval
                or evidence.packet_sha256 != expected_packet
                or not evidence.quotations
                or evidence.semantic_status != "unknown"
                or evidence.review_state != "awaiting_human_review"
                or evidence.human_approval_granted is not False
                or evidence.canonical_vault_mutated is not False
                or any(
                    q.source_quote_verified is not True
                    or q.semantic_truth_verified is not False
                    or q.user_confirmation_verified is not False
                    or q.correction_chronology_verified is not False
                    for q in evidence.quotations
                )
            ):
                raise ExternalDispositionError("external_review_evidence_mismatch")

            evaluation = load_evaluation_record(ai_root, expected_eval)
            if (
                evaluation.model.provider != "synthetic-fake-only"
                or evaluation.assessment.recommendation != "manual_review"
                or evaluation.assessment.epistemic_status != "unknown"
            ):
                raise ExternalDispositionError("external_review_evaluator_untrusted")

            # The existing v3 Review writer checks accepted Validation →
            # exact Mutation and Evaluation provenance. Never write another
            # External-only approval/denial ledger.
            existing = _already_recorded_review(
                ai_root,
                evaluation_sha=expected_eval,
                mutation_sha=evaluation.mutation_sha256,
                proposal_sha=evaluation.proposal_sha256,
                decision=decision,
                approver=approver,
            )
            result = existing or _store_validated_evaluation_disposition(
                ai_root,
                evaluation_sha256=expected_eval,
                decision=decision,
                approver=approver,
                decided_at=decided_at,
            )

            try:
                conn = _connect_rw(ai_root)
            except (PreReviewJobError, OSError, sqlite3.Error):
                # The private reader flock context wraps raw OSError in a
                # SourceReaderError. Preserve the actual recording failure
                # classification before leaving the shared Source lock.
                raise ExternalDispositionError(
                    "external_review_recording_failed"
                ) from None
            try:
                conn.execute("BEGIN IMMEDIATE")
                rows = conn.execute(
                    "SELECT g.generation_id,g.generation_index,g.state,"
                    " j.job_id FROM generations g JOIN jobs j ON "
                    "j.job_id=g.job_id WHERE j.job_id=? ORDER BY "
                    "g.generation_index DESC LIMIT 2",
                    (expected_job,),
                ).fetchall()
                if (
                    len(rows) != 1
                    or rows[0]["generation_id"] != evidence.generation_id
                    or rows[0]["generation_index"] != 1
                    or rows[0]["state"] != "awaiting_human_review"
                ):
                    raise ExternalDispositionError("external_review_job_state_invalid")
                selected = _load_stage_output_conn(
                    conn,
                    evidence.generation_id,
                    "external_evaluation",
                )
                if (
                    selected is None
                    or selected["evaluation_sha256"] != expected_eval
                    or selected["proposal_sha256"] != evaluation.proposal_sha256
                    or selected["mutation_sha256"] != evaluation.mutation_sha256
                    or selected["recommendation"] != "manual_review"
                ):
                    raise ExternalDispositionError(
                        "external_review_selected_output_invalid"
                    )
                changed = conn.execute(
                    "UPDATE generations SET state=?,updated_at="
                    "strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                    "WHERE generation_id=? AND state='awaiting_human_review'",
                    (EXTERNAL_REVIEW_RECORDED_STATE, evidence.generation_id),
                ).rowcount
                if changed != 1:
                    raise ExternalDispositionError("external_review_job_state_invalid")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
            return ExternalDispositionReceipt(
                record_version=3,
                job_id=expected_job,
                generation_id=evidence.generation_id,
                evaluation_sha256=expected_eval,
                proposal_sha256=result.proposal_sha256,
                mutation_sha256=result.mutation_sha256,
                review_sha256=result.review_sha256,
                source_revision_sha256=expected_revision,
                evidence_packet_sha256=expected_packet,
                decision=result.decision,
            )
    except ExternalDispositionError:
        raise
    except (
        PrivateSourceReaderError,
        ExternalReviewEvidenceError,
    ):
        raise ExternalDispositionError("external_review_source_invalid") from None
    except (ArtifactLifecycleError, PreReviewJobError, OSError, sqlite3.Error):
        raise ExternalDispositionError("external_review_recording_failed") from None
