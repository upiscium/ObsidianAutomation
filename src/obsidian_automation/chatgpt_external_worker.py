"""Dedicated, explicitly synthetic External Source Job worker, Issue #411.

Uses real pre-review Job state/attempt/output tables, but wholly distinct
external_* stages and a private Source freshness lock. No arbitrary provider
URL, model credentials, timers, shell execution or canonical Vault mutation.
Production activation requires a separately authorized service UID boundary.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .artifact_lifecycle import (
    ArtifactLifecycleError,
    _canonical_json_bytes,
    _store_immutable,
    _utc_now,
    sha256_bytes,
)
from .chatgpt_export_candidates import CandidateBatch, KnowledgeEvidenceCandidate
from .chatgpt_export_reader import (
    PrivateSourceReaderError,
    _existing_private_root,
    _read_locked,
)
from .chatgpt_export_review_preview import ExternalReviewPreview, ReviewRisk
from .chatgpt_external_context_store import ExternalContextStoreError
from .chatgpt_external_generation import (
    FAKE_PROVIDER_NAME,
    ExternalDraftError,
    run_fake_external_knowledge_draft,
)
from .chatgpt_external_job import (
    EXTERNAL_FAKE_EVALUATION_POLICY,
    EXTERNAL_FAKE_WORKER_RECIPE_VERSION,
    EXTERNAL_SYNTHETIC_WORKER_MODE,
    ExternalJobError,
    inspect_external_job_state,
    load_external_admission_recipe,
)
from .evaluation_artifact import (
    build_evaluation_record,
    load_evaluation_record,
    store_evaluation_record,
)
from .evaluator_contract import (
    EVALUATOR_PROMPT_TEMPLATE_VERSION,
)
from .evaluator_contract import (
    prompt_template_sha256 as evaluator_prompt_sha256,
)
from .generation_artifact import load_generation_record
from .knowledge_validator import validate_proposal
from .pre_review_job import (
    _STAGE_PREVIOUS,
    _STAGE_START,
    PreReviewJobError,
    _attempt_id,
    _complete_source_bound_external_attempt,
    _connect_rw,
    _load_stage_output_conn,
    stage_output,
)
from .pre_review_shared_artifacts import (
    build_bound_fresh_retrieval,
    create_bound_evaluation_request,
)
from .production_io import ProductionIOError

EXTERNAL_STAGES = (
    "external_generation",
    "external_validation",
    "external_evaluation_context",
    "external_evaluation",
)
REVIEW_PROJECTION_VERSION = "chatgpt-external-pending-review-v0"
EXTERNAL_EVALUATION_FAKE_PROVIDER = "synthetic-fake-only"
MAX_FAKE_OUTPUT_BYTES = 64 * 1024
MAX_AWAITING_REVIEW = 8


class ExternalWorkerError(ValueError):
    """No source content or provider diagnostics in error messages."""


@dataclass(frozen=True, repr=False)
class ExternalWorkerReceipt:
    job_id: str
    generation_id: str
    state: str
    completed_stages: tuple[str, ...]
    proposal_sha256: str | None = None
    generation_sha256: str | None = None
    evaluation_sha256: str | None = None
    review_projection_sha256: str | None = None
    awaiting_human_review: bool = False
    human_approval_granted: bool = False
    canonical_vault_mutated: bool = False


def _source_check(
    ai_root: Path,
    private_root: Path,
    preview: ExternalReviewPreview,
    job_id: str,
    state: str,
) -> dict[str, object]:
    try:
        result = inspect_external_job_state(
            ai_root,
            private_root=private_root,
            preview=preview,
            job_id=job_id,
            expected_state=state,
        )
        recipe = load_external_admission_recipe(
            ai_root,
            result["recipe_sha256"],
        )
        if (
            recipe.record_version != EXTERNAL_FAKE_WORKER_RECIPE_VERSION
            or recipe.admission_mode != EXTERNAL_SYNTHETIC_WORKER_MODE
            or recipe.evaluation_policy != EXTERNAL_FAKE_EVALUATION_POLICY
        ):
            raise ExternalWorkerError("external_worker_recipe_not_fake")
        return result
    except ExternalWorkerError:
        raise
    except ExternalJobError:
        raise ExternalWorkerError("external_source_or_job_not_current") from None


def _semantic_guard(preview: ExternalReviewPreview) -> None:
    """Structural epistemic checks only: never certify a model's claim."""
    if type(preview) is not ExternalReviewPreview:
        raise ExternalWorkerError("invalid_external_preview")
    if (
        type(preview.candidate_batch) is not CandidateBatch
        or type(preview.candidate_batch.candidates) is not tuple
        or type(preview.risks) is not tuple
        or not preview.candidate_batch.candidates
        or len(preview.candidate_batch.candidates) != len(preview.risks)
        or any(
            type(c) is not KnowledgeEvidenceCandidate
            for c in preview.candidate_batch.candidates
        )
        or any(
            type(r) is not ReviewRisk or type(r.signals) is not tuple
            for r in preview.risks
        )
        or preview.semantic_evaluation_completed is not False
        or preview.awaiting_human_review is not False
        or preview.generation_authorized is not False
    ):
        raise ExternalWorkerError("external_semantic_precheck_invalid")
    keyed = {risk.candidate_id: risk for risk in preview.risks}
    if len(keyed) != len(preview.risks):
        raise ExternalWorkerError("external_semantic_precheck_invalid")
    for c in preview.candidate_batch.candidates:
        risk = keyed.get(c.candidate_id)
        if (
            risk is None
            or risk.anchor_sha256 != c.evidence_anchor_sha256
            or "semantic_review_required" not in risk.signals
            or c.review_state != "needs_semantic_and_human_review"
            or c.authority != "none"
            or c.auto_approved is not False
        ):
            raise ExternalWorkerError("external_semantic_precheck_invalid")
        if c.proposed_kind == "decision_candidate" and (
            "decision_requires_user_confirmation" not in risk.signals
        ):
            raise ExternalWorkerError("external_decision_unverified")
        if c.proposed_kind == "result_candidate" and (
            "result_requires_external_verification" not in risk.signals
        ):
            raise ExternalWorkerError("external_result_unverified")
        if c.proposed_kind == "correction_candidate" and (
            "correction_requires_chronological_context" not in risk.signals
        ):
            raise ExternalWorkerError("external_correction_unverified")


def _claim_external_stage(
    ai_root: Path,
    private_root: Path,
    preview: ExternalReviewPreview,
    job_id: str,
    stage: str,
    *,
    max_awaiting_review: int,
) -> dict[str, str]:
    """Activate ONE external stage under real SQLite txn + Importer SH lock.

    This is intentionally NOT the generic legacy claim_next_attempt.
    Legacy workers cannot claim external states or distinct stage names.
    A complete private Source revision is rechecked while Intake's EX
    writer lock cannot move the ready archive or Journal.
    """
    if stage not in EXTERNAL_STAGES:
        raise ExternalWorkerError("external_stage_unsupported")
    if type(max_awaiting_review) is not int or not 1 <= max_awaiting_review <= 100:
        raise ExternalWorkerError("external_review_backpressure_limit_invalid")
    expect, active = _STAGE_START[stage]
    try:
        root = _existing_private_root(private_root)
    except PrivateSourceReaderError:
        raise ExternalWorkerError("external_source_or_job_not_current") from None
    with _read_locked(root):
        item = _source_check(ai_root, private_root, preview, job_id, expect)
        try:
            conn = _connect_rw(ai_root)
            try:
                conn.execute("BEGIN IMMEDIATE")
                current = conn.execute(
                    "SELECT g.state,g.generation_id,g.generation_index,"
                    " j.context_sha256,j.recipe_sha256 FROM generations g "
                    "JOIN jobs j ON j.job_id=g.job_id "
                    "WHERE g.job_id=? ORDER BY g.generation_index DESC LIMIT 1",
                    (job_id,),
                ).fetchone()
                if (
                    current is None
                    or current["state"] != expect
                    or current["generation_id"] != item["generation_id"]
                    or current["generation_index"] != 1
                    or current["context_sha256"] != item["context_sha256"]
                    or current["recipe_sha256"] != item["recipe_sha256"]
                ):
                    raise ExternalWorkerError("external_stage_binding_changed")
                if stage == EXTERNAL_STAGES[0]:
                    count = conn.execute(
                        "SELECT COUNT(*) AS n FROM generations g "
                        "WHERE g.state='awaiting_human_review' AND "
                        "NOT EXISTS (SELECT 1 FROM generations newer "
                        "WHERE newer.job_id=g.job_id AND "
                        "newer.generation_index>g.generation_index)"
                    ).fetchone()["n"]
                    if count >= max_awaiting_review:
                        raise ExternalWorkerError("external_review_backpressure")
                previous = _STAGE_PREVIOUS[stage]
                if (
                    previous is not None
                    and _load_stage_output_conn(
                        conn,
                        item["generation_id"],
                        previous,
                    )
                    is None
                ):
                    raise ExternalWorkerError("external_previous_stage_not_selected")
                if (
                    _load_stage_output_conn(
                        conn,
                        item["generation_id"],
                        stage,
                    )
                    is not None
                ):
                    raise ExternalWorkerError("external_stage_already_selected")
                if (
                    stage != EXTERNAL_STAGES[0]
                    and conn.execute(
                        "SELECT 1 FROM attempts WHERE generation_id=? LIMIT 1",
                        (item["generation_id"],),
                    ).fetchone()
                    is None
                ):
                    raise ExternalWorkerError("external_attempt_chain_missing")
                if (
                    conn.execute(
                        "SELECT 1 FROM attempts WHERE generation_id=? AND "
                        "(stage=? OR status='running') LIMIT 1",
                        (item["generation_id"], stage),
                    ).fetchone()
                    is not None
                ):
                    raise ExternalWorkerError("external_stage_already_started")
                attempt_id = _attempt_id(item["generation_id"], stage, 1)
                now = _utc_now()
                conn.execute(
                    "INSERT INTO attempts("
                    "attempt_id,generation_id,stage,attempt_index,status,started_at)"
                    " VALUES(?,?,?,1,'running',?)",
                    (attempt_id, item["generation_id"], stage, now),
                )
                if expect != active:
                    conn.execute(
                        "UPDATE generations SET state=?,updated_at=? "
                        "WHERE generation_id=? AND state=?",
                        (active, now, item["generation_id"], expect),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        except ExternalWorkerError:
            raise
        except (sqlite3.Error, PreReviewJobError, OSError):
            raise ExternalWorkerError("external_claim_database_error") from None
    return {
        "attempt_id": attempt_id,
        "generation_id": item["generation_id"],
        "job_id": job_id,
        "context_sha256": item["context_sha256"],
    }


def _finish_external_stage(
    ai_root: Path,
    private_root: Path,
    preview: ExternalReviewPreview,
    job_id: str,
    stage: str,
    attempt_id: str,
    output: Mapping[str, object],
) -> dict[str, object]:
    """Commit real Stage Output while private Intake writer remains excluded."""
    active = _STAGE_START[stage][1]
    root = _existing_private_root(private_root)
    with _read_locked(root):
        _source_check(ai_root, private_root, preview, job_id, active)
        try:
            return _complete_source_bound_external_attempt(
                ai_root,
                attempt_id,
                outcome="succeeded",
                output=output,
            )
        except PreReviewJobError:
            raise ExternalWorkerError("external_stage_output_rejected") from None


def _block_external_stage(
    ai_root: Path,
    attempt_id: str,
    *,
    reason: str,
) -> None:
    """Fail closed: a running attempt becomes 'blocked', never auto retried."""
    try:
        _complete_source_bound_external_attempt(
            ai_root,
            attempt_id,
            outcome="blocked",
            reason_code=reason,
        )
    except PreReviewJobError:
        raise ExternalWorkerError("external_failed_attempt_not_recorded") from None


def _make_external_pending_projection(
    ai_root: Path,
    *,
    preview: ExternalReviewPreview,
    job_id: str,
    generation_id: str,
    proposal_sha256: str,
    generation_sha256: str,
    evaluation_sha256: str,
) -> tuple[str, Path]:
    """Metadata for human-facing Review; never a Human approval decision."""
    risk_lookup = {r.candidate_id: r for r in preview.risks}
    if len(risk_lookup) != len(preview.risks) or any(
        c.candidate_id not in risk_lookup for c in preview.candidate_batch.candidates
    ):
        raise ExternalWorkerError("external_review_risk_binding_missing")
    payload: dict[str, object] = {
        "record_version": 1,
        "projection_kind": REVIEW_PROJECTION_VERSION,
        "job_id": job_id,
        "generation_id": generation_id,
        "proposal_sha256": proposal_sha256,
        "generation_sha256": generation_sha256,
        "evaluation_sha256": evaluation_sha256,
        "source_scope": preview.scope,
        "source_hmac": preview.source_hmac,
        "source_revision_sha256": preview.revision_sha256,
        "source_content_sha256": preview.content_sha256,
        "source_run_sha256": preview.source_run_id,
        "source_preview_sha256": preview.preview_digest,
        "evidence": [
            {
                "anchor_sha256": c.evidence_anchor_sha256,
                "candidate_id": c.candidate_id,
                "source_message_sha256": c.source_message_sha256,
                "role": c.role,
                "attribution": c.attribution,
                "proposed_kind": c.proposed_kind,
                "risks": list(risk_lookup[c.candidate_id].signals),
            }
            for c in preview.candidate_batch.candidates
        ],
        "status": "awaiting_human_review",
        "recommendation": "manual_review",
        "human_review_completed": False,
        "human_approval_granted": False,
        "canonical_mutation_authorized": False,
    }
    body = _canonical_json_bytes(payload)
    if len(body) > 32768:
        raise ExternalWorkerError("external_review_projection_budget_exceeded")
    digest = sha256_bytes(body)
    path = ai_root / "20-Review" / f"{digest}.external-pending.json"
    try:
        return digest, _store_immutable(path, body)
    except (ArtifactLifecycleError, OSError):
        raise ExternalWorkerError("external_review_projection_failed") from None


def inspect_external_pending_review(
    ai_root: Path,
    *,
    private_root: Path,
    preview: ExternalReviewPreview,
    job_id: str,
) -> dict[str, object]:
    """Expose only grounded private source metadata, never a Human decision."""
    root = _existing_private_root(private_root)
    with _read_locked(root):
        item = _source_check(
            ai_root,
            private_root,
            preview,
            job_id,
            "awaiting_human_review",
        )
        evaluation = stage_output(
            ai_root,
            item["generation_id"],
            "external_evaluation",
        )
        if evaluation is None or evaluation["recommendation"] != "manual_review":
            raise ExternalWorkerError("external_review_evaluation_missing")
        try:
            evaluated = load_evaluation_record(
                ai_root,
                str(evaluation["evaluation_sha256"]),
            )
            generated = load_generation_record(
                ai_root,
                str(evaluation["generation_sha256"]),
            )
        except (ArtifactLifecycleError, OSError):
            raise ExternalWorkerError("external_review_evidence_unreadable") from None
        if (
            evaluated.assessment.recommendation != "manual_review"
            or evaluated.assessment.epistemic_status != "unknown"
            or evaluated.assessment.groundedness != "unknown"
            or evaluated.assessment.knowledge_quality != "unknown"
            or evaluated.proposal_sha256 != evaluation["proposal_sha256"]
            or evaluated.generation_sha256 != evaluation["generation_sha256"]
            or generated.context_sha256 != item["context_sha256"]
            or generated.record_version != 3
            or generated.external_source is None
            or generated.model.provider != FAKE_PROVIDER_NAME
            or generated.external_source.source_revision_sha256
            != preview.revision_sha256
            or generated.external_source.source_hmac != preview.source_hmac
            or generated.external_source.source_archive_sha256 != preview.archive_sha256
            or generated.external_source.source_content_sha256 != preview.content_sha256
            or generated.external_source.source_run_sha256 != preview.source_run_id
            or generated.external_source.source_preview_sha256 != preview.preview_digest
        ):
            raise ExternalWorkerError("external_review_evidence_unverified")
        return {
            "state": "awaiting_human_review",
            "job_id": job_id,
            "generation_id": item["generation_id"],
            "source_hmac": preview.source_hmac,
            "source_revision_sha256": preview.revision_sha256,
            "source_preview_sha256": preview.preview_digest,
            "candidate_anchors": tuple(
                c.evidence_anchor_sha256 for c in preview.candidate_batch.candidates
            ),
            "evaluation_sha256": evaluation["evaluation_sha256"],
            "recommendation": "manual_review",
            "human_approval_granted": False,
            "canonical_vault_mutated": False,
        }


def run_external_synthetic_worker_once(
    ai_root: Path,
    *,
    private_root: Path,
    vault_root: Path,
    preview: ExternalReviewPreview,
    job_id: str,
    fake_generator_wire: bytes,
    max_awaiting_review: int = MAX_AWAITING_REVIEW,
    implementation_revision: str = "f" * 40,
) -> ExternalWorkerReceipt:
    """Real Job stages + Fake Provider bytes. NO network/real Export support.

    This is an explicitly synthetic-only Worker API, not installed as a
    production service and not supplied with any HTTP transport or model
    credentials. It assumes a disposable synthetic vault and source Scope.
    Any untrusted model assertion is conservatively unknown/manual_review.

    Unlike the former E2E fixture, this implementation starts/finishes the
    REAL Job's distinct external_* attempts via source-bound transactions,
    without direct SQLite override or legacy "queued" stage activation.
    """
    if (
        type(preview) is not ExternalReviewPreview
        or preview.scope != "fake-e2e"
        or type(fake_generator_wire) is not bytes
        or not 0 < len(fake_generator_wire) <= MAX_FAKE_OUTPUT_BYTES
    ):
        raise ExternalWorkerError("synthetic_worker_input_invalid")
    _semantic_guard(preview)
    previous: dict[str, object] | None = None
    completed: list[str] = []
    outputs: dict[str, object] = {}
    evaluation_sha = None
    projection_sha = None

    for stage in EXTERNAL_STAGES:
        work = _claim_external_stage(
            ai_root,
            private_root,
            preview,
            job_id,
            stage,
            max_awaiting_review=max_awaiting_review,
        )
        attempt_id = work["attempt_id"]
        generation_id = work["generation_id"]
        try:
            if stage == "external_generation":
                draft = run_fake_external_knowledge_draft(
                    ai_root,
                    private_root=private_root,
                    preview=preview,
                    job_id=job_id,
                    transport=lambda _prompt: fake_generator_wire,
                    implementation_revision=implementation_revision,
                    expected_job_state="external_generating",
                )
                rec = load_generation_record(ai_root, draft.generation_sha256)
                if (
                    rec.record_version != 3
                    or rec.context_sha256 != work["context_sha256"]
                    or rec.model.provider != FAKE_PROVIDER_NAME
                    or rec.external_source is None
                    or rec.external_source.source_revision_sha256
                    != preview.revision_sha256
                ):
                    raise ExternalWorkerError("external_generation_binding_invalid")
                outputs = {
                    "proposal_sha256": draft.proposal_sha256,
                    "generation_sha256": draft.generation_sha256,
                }
            elif stage == "external_validation":
                _semantic_guard(preview)
                assert previous is not None
                result = validate_proposal(
                    ai_root,
                    vault_root,
                    str(previous["proposal_sha256"]),
                )
                if result["result"] != "accepted":
                    raise ExternalWorkerError("external_structural_validation_rejected")
                req_sha = create_bound_evaluation_request(
                    ai_root,
                    proposal_sha256=str(previous["proposal_sha256"]),
                    mutation_sha256=str(result["mutation_sha256"]),
                )
                outputs = {
                    **previous,
                    "mutation_sha256": str(result["mutation_sha256"]),
                    "request_sha256": req_sha,
                }
            elif stage == "external_evaluation_context":
                assert previous is not None
                # Same fresh Knowledge retrieval as the legacy Reader Worker.
                # External Source admission/provenance stays separately gated.
                index_sha, ec_sha = build_bound_fresh_retrieval(
                    ai_root,
                    vault_root,
                    proposal_sha256=str(previous["proposal_sha256"]),
                    mutation_sha256=str(previous["mutation_sha256"]),
                    request_sha256=str(previous["request_sha256"]),
                    top_k=5,
                )
                outputs = {
                    **previous,
                    "index_sha256": index_sha,
                    "evaluation_context_sha256": ec_sha,
                }
            else:
                assert previous is not None
                # This is a conservative Fake evaluator, not evidence that
                # semantic truth, confirmations or corrections were verified.
                rec = build_evaluation_record(
                    ai_root,
                    proposal_sha256=str(previous["proposal_sha256"]),
                    mutation_sha256=str(previous["mutation_sha256"]),
                    generation_sha256=str(previous["generation_sha256"]),
                    evaluation_context_sha256=str(
                        previous["evaluation_context_sha256"],
                    ),
                    implementation_revision=implementation_revision,
                    prompt_template_version=EVALUATOR_PROMPT_TEMPLATE_VERSION,
                    prompt_template_sha256=evaluator_prompt_sha256(),
                    model_provider=EXTERNAL_EVALUATION_FAKE_PROVIDER,
                    model_identifier="deterministic-conservative-evaluator",
                    model_revision="fake-only-no-network",
                    model_config={
                        "adapter_version": "synthetic-external-evaluation-v0",
                        "strategy": "unknown-requires-human",
                        "options": {},
                    },
                    groundedness="unknown",
                    knowledge_quality="unknown",
                    epistemic_status="unknown",
                    redundancy="none",
                    consistency="unknown",
                    recommendation="manual_review",
                    findings=(
                        "Untrusted Source claims require independent semantic review and user confirmation",
                    ),
                )
                evaluation_sha, _ = store_evaluation_record(ai_root, rec)
                stored = load_evaluation_record(ai_root, evaluation_sha)
                if (
                    stored.assessment.recommendation != "manual_review"
                    or stored.assessment.epistemic_status != "unknown"
                ):
                    raise ExternalWorkerError(
                        "external_conservative_evaluation_invalid"
                    )
                outputs = {
                    **previous,
                    "evaluation_sha256": evaluation_sha,
                    "recommendation": "manual_review",
                }

            finished = _finish_external_stage(
                ai_root,
                private_root,
                preview,
                job_id,
                stage,
                attempt_id,
                outputs,
            )
            previous = dict(finished["output"])
            completed.append(stage)
        except (
            ExternalWorkerError,
            PrivateSourceReaderError,
            ExternalDraftError,
            ExternalJobError,
            ExternalContextStoreError,
            ArtifactLifecycleError,
            ProductionIOError,
            PreReviewJobError,
            OSError,
        ):
            # No source/model exception details enter the Job DB/log.
            _block_external_stage(
                ai_root,
                attempt_id,
                reason="external_synthetic_stage_rejected",
            )
            return ExternalWorkerReceipt(
                job_id=job_id,
                generation_id=generation_id,
                state="blocked",
                completed_stages=tuple(completed),
            )

    # This projection is metadata only and does not approve the draft.
    assert previous is not None and evaluation_sha is not None
    projection_sha, _ = _make_external_pending_projection(
        ai_root,
        preview=preview,
        job_id=job_id,
        generation_id=generation_id,
        proposal_sha256=str(previous["proposal_sha256"]),
        generation_sha256=str(previous["generation_sha256"]),
        evaluation_sha256=evaluation_sha,
    )
    inspect_external_pending_review(
        ai_root,
        private_root=private_root,
        preview=preview,
        job_id=job_id,
    )
    return ExternalWorkerReceipt(
        job_id=job_id,
        generation_id=generation_id,
        state="awaiting_human_review",
        completed_stages=tuple(completed),
        proposal_sha256=str(previous["proposal_sha256"]),
        generation_sha256=str(previous["generation_sha256"]),
        evaluation_sha256=evaluation_sha,
        review_projection_sha256=projection_sha,
        awaiting_human_review=True,
    )
