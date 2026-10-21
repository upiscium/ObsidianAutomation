"""Private, typed External Source evidence for the existing Evaluator.

This is only an *in-memory* ground-truth quotation input, not a provider
transport, autonomous semantic verifier or a forged Vault ContextBundle.
Every caller must revalidate private Journal/ZIP before rendering a prompt.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from .chatgpt_export_reader import (
    PrivateSourceReaderError,
    _existing_private_root,
    _read_locked,
)
from .chatgpt_export_review_preview import (
    ExternalReviewPreview,
    ReviewPreviewError,
    revalidate_private_review_preview,
)

MAX_QUOTES = 16
MAX_QUOTE_BYTES = 4096
MAX_SUMMARY_BYTES = 4096


class ExternalEvaluationInputError(ValueError):
    """Constant diagnostics; no source text or machine paths in exceptions."""


@dataclass(frozen=True, repr=False)
class ExternalEvaluationQuote:
    anchor_sha256: str
    message_sha256: str
    quote_sha256: str
    exact_quote: str
    speaker_role: str
    attribution: str
    proposed_kind: str
    proposed_summary: str
    required_checks: tuple[str, ...]


@dataclass(frozen=True, repr=False)
class ExternalEvaluationSource:
    """Private data-only quotes; no original IDs or fabricated Vault paths."""
    source_kind: str
    source_revision_sha256: str
    source_run_sha256: str
    source_preview_sha256: str
    quotes: tuple[ExternalEvaluationQuote, ...]
    correction_chronology_verified: bool = False
    user_confirmation_verified: bool = False
    source_truth_verified: bool = False


def _bounded_utf8(value: object, max_bytes: int) -> bytes:
    if type(value) is not str or not value.strip() or "\x00" in value:
        raise ExternalEvaluationInputError("external_evidence_text_invalid")
    try:
        result = value.encode("utf-8")
    except UnicodeError:
        raise ExternalEvaluationInputError("external_evidence_text_invalid") from None
    if not 0 < len(result) <= max_bytes:
        raise ExternalEvaluationInputError("external_evidence_budget_exceeded")
    return result


def read_external_evaluation_source(
    private_root: Path, preview: ExternalReviewPreview,
) -> ExternalEvaluationSource:
    """Reread currently committed source, then prepare bounded low-trust quotes.

    Synthetic Scope only. This function does NOT invoke an LLM or send quotes
    over a network. A real provider path requires a private transport, model
    policy and explicit authorization; this input alone grants no authority.
    """
    if type(preview) is not ExternalReviewPreview or preview.scope != "fake-e2e":
        raise ExternalEvaluationInputError("external_evaluator_scope_not_authorized")
    try:
        root = _existing_private_root(private_root)
        with _read_locked(root):
            revalidate_private_review_preview(root, preview)
            candidates = preview.candidate_batch.candidates
            if not 0 < len(candidates) <= MAX_QUOTES:
                raise ExternalEvaluationInputError("external_quote_count_invalid")
            risk_map = {risk.candidate_id: risk for risk in preview.risks}
            if len(risk_map) != len(preview.risks):
                raise ExternalEvaluationInputError("external_evaluator_risk_mismatch")
            quotes: list[ExternalEvaluationQuote] = []
            for candidate in candidates:
                risk = risk_map.get(candidate.candidate_id)
                if (
                    risk is None
                    or risk.anchor_sha256 != candidate.evidence_anchor_sha256
                    or "semantic_review_required" not in risk.signals
                ):
                    raise ExternalEvaluationInputError("external_evaluator_risk_mismatch")
                text = _bounded_utf8(candidate.excerpt, MAX_QUOTE_BYTES)
                _bounded_utf8(candidate.proposed_summary, MAX_SUMMARY_BYTES)
                if hashlib.sha256(text).hexdigest() != candidate.excerpt_sha256:
                    raise ExternalEvaluationInputError("external_evaluator_quote_invalid")
                quotes.append(
                    ExternalEvaluationQuote(
                        anchor_sha256=candidate.evidence_anchor_sha256,
                        message_sha256=candidate.source_message_sha256,
                        quote_sha256=candidate.excerpt_sha256,
                        exact_quote=candidate.excerpt,
                        speaker_role=candidate.role,
                        attribution=candidate.attribution,
                        proposed_kind=candidate.proposed_kind,
                        proposed_summary=candidate.proposed_summary,
                        required_checks=risk.signals,
                    )
                )
            return ExternalEvaluationSource(
                source_kind="chatgpt-external-source",
                source_revision_sha256=preview.revision_sha256,
                source_run_sha256=preview.source_run_id,
                source_preview_sha256=preview.preview_digest,
                quotes=tuple(quotes),
            )
    except ExternalEvaluationInputError:
        raise
    except (PrivateSourceReaderError, ReviewPreviewError, OSError, ValueError, TypeError):
        raise ExternalEvaluationInputError("external_evaluator_source_stale") from None



def conservative_external_assessment(
    evaluated: object,
    source: ExternalEvaluationSource,
):
    """Reuse native Evaluator aggregation, forbidding unsupported approval.

    The ordinary native five-dimensional evaluator may return an all-PASS
    assessment even when original quotes are unverified conversational
    assertions. A source quote alone never establishes objective truth,
    user confirmation, or full chronological-correction coverage.
    """
    from .artifact_lifecycle import ArtifactLifecycleError
    from .evaluation_artifact import EvaluationAssessment
    from .evaluator_contract import EvaluatorOutput, to_evaluation_assessment

    if (
        type(source) is not ExternalEvaluationSource
        or source.source_kind != "chatgpt-external-source"
        or source.source_truth_verified is not False
        or source.user_confirmation_verified is not False
        or source.correction_chronology_verified is not False
    ):
        raise ExternalEvaluationInputError("external_assessment_authority_invalid")
    if type(evaluated) is not EvaluatorOutput:
        raise ExternalEvaluationInputError("external_assessment_output_invalid")
    try:
        base = to_evaluation_assessment(evaluated)
    except ArtifactLifecycleError:
        raise ExternalEvaluationInputError("external_assessment_output_invalid") from None
    groundedness = "unknown" if base.groundedness == "pass" else base.groundedness
    epistemic_status = (
        "unknown" if base.epistemic_status == "pass" else base.epistemic_status
    )
    # In particular no combination of model PASS labels can make this
    # unverified Source evidence eligible for automatic approval.
    conservative = (
        "do_not_proceed"
        if (
            groundedness == "concern"
            or base.knowledge_quality == "concern"
            or epistemic_status == "concern"
            or base.redundancy == "likely"
            or base.consistency == "concern"
        )
        else "manual_review"
    )
    return EvaluationAssessment(
        groundedness=groundedness,
        redundancy=base.redundancy,
        consistency=base.consistency,
        knowledge_quality=base.knowledge_quality,
        epistemic_status=epistemic_status,
        recommendation=conservative,
        findings=tuple(base.findings[:19]) + (
            (
                "epistemic_status: External quote provenance does not establish "
                "user approval, implementation results or later corrections"
            ),
        ),
        conflicts=base.conflicts,
    )
