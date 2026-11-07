"""Pure, private review preview from untrusted ChatGPT Source proposals.

A synthetic/demo-only companion to the private Reader. This cannot submit
a pre-review Job or represent Human Review authorization/approval. It is an
admission boundary for a future *separately versioned* external Source flow.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .chatgpt_export_candidates import (
    CandidateBatch,
    CandidateContractError,
    KnowledgeEvidenceCandidate,
    build_candidate_batch,
    parse_candidate_proposals,
    validate_candidate_batch,
)
from .chatgpt_export_reader import (
    READER_VERSION,
    PrivateAdmittedPage,
    PrivateSourceReaderError,
    read_admitted_source_page,
)
from .chatgpt_export_source import (
    SourceContractError,
    SourceCursor,
    validate_source_context_page,
)

REVIEW_PREVIEW_VERSION = "chatgpt-external-review-preview-v0"
_VALID_OUTCOMES = frozenset({"review_required", "no_candidate"})
_CANDIDATE_GENERATION_BUDGET = 16
# For local in-memory proto only. This is NOT production external Job state.
_EXTERNAL_JOB_STATUS = "not_submitted"


class ReviewPreviewError(ValueError):
    """Fixed diagnostic codes, no echo of source text/model output."""


@dataclass(frozen=True, repr=False)
class ReviewRisk:
    candidate_id: str
    anchor_sha256: str
    signals: tuple[str, ...]


@dataclass(frozen=True, repr=False)
class ExternalReviewPreview:
    """Private and untrusted. Never serialize into public AI, logs or Vault."""

    version: str
    scope: str
    source_hmac: str
    archive_sha256: str
    content_sha256: str
    revision_sha256: str
    source_run_id: str
    start_cursor: SourceCursor
    next_cursor: SourceCursor
    candidate_batch: CandidateBatch
    risks: tuple[ReviewRisk, ...]
    preview_digest: str
    status: str
    job_status: str = _EXTERNAL_JOB_STATUS
    semantic_evaluation_completed: bool = False
    awaiting_human_review: bool = False
    generation_authorized: bool = False
    canonical_mutation_authorized: bool = False


def _risks(
    candidate: KnowledgeEvidenceCandidate, *, coverage_gap: bool
) -> tuple[str, ...]:
    signals = ["semantic_review_required"]
    if candidate.attribution == "assistant-claim-unverified":
        signals.append("assistant_not_user_confirmation")
    else:
        signals.append("user_report_not_independent_verification")
    if candidate.proposed_kind == "decision_candidate":
        signals.append("decision_requires_user_confirmation")
    elif candidate.proposed_kind == "result_candidate":
        signals.append("result_requires_external_verification")
    elif candidate.proposed_kind == "correction_candidate":
        signals.append("correction_requires_chronological_context")
    elif candidate.proposed_kind == "hypothesis_candidate":
        signals.append("hypothesis_unconfirmed")
    elif candidate.proposed_kind == "feature_change_candidate":
        signals.append("feature_change_requires_status_evidence")
    if coverage_gap:
        signals.append("source_coverage_incomplete")
    return tuple(signals)


def _preview_digest(admitted: PrivateAdmittedPage, batch: CandidateBatch) -> str:
    payload = {
        "version": REVIEW_PREVIEW_VERSION,
        "reader": admitted.reader_version,
        "scope": admitted.scope,
        "source_hmac": admitted.source_hmac,
        "archive_sha256": admitted.archive_sha256,
        "content_sha256": admitted.content_sha256,
        "revision_sha256": admitted.revision_sha256,
        "run_id": admitted.run_id,
        "start_cursor": [
            admitted.page.start_cursor.message_index,
            admitted.page.start_cursor.text_offset,
        ],
        "next_cursor": [
            admitted.page.next_cursor.message_index,
            admitted.page.next_cursor.text_offset,
        ],
        "candidate_ids": [c.candidate_id for c in batch.candidates],
        "status": "review_required" if batch.candidates else "no_candidate",
    }
    return hashlib.sha256(
        json.dumps(
            payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def build_private_review_preview(
    admitted: PrivateAdmittedPage,
    *,
    proposal_wire_bytes: bytes,
) -> ExternalReviewPreview:
    """Bind output from an untrusted proposer to *exact* admitted Source bytes.

    Caller supplies bounded model/fake-provider JSON bytes, never credentials.
    This function does not call a provider, persist, queue or authorize notes.
    Model assertions are not accepted as facts or Human decisions.
    """
    if type(admitted) is not PrivateAdmittedPage:
        raise ReviewPreviewError("invalid_private_source")
    if (
        admitted.reader_version != READER_VERSION
        or type(admitted.generation_authorized) is not bool
        or admitted.generation_authorized
        or type(admitted.approved_for_human_review) is not bool
        or admitted.approved_for_human_review
    ):
        raise ReviewPreviewError("private_source_authority_forbidden")
    if (
        type(admitted.scope) is not str
        or type(admitted.inventory_status) is not str
        or admitted.inventory_status
        not in ("inventory_only", "inventory_review_required")
        or any(
            type(field) is not str
            or len(field) != 64
            or any(c not in "0123456789abcdef" for c in field)
            for field in (
                admitted.source_hmac,
                admitted.archive_sha256,
                admitted.content_sha256,
                admitted.revision_sha256,
                admitted.run_id,
            )
        )
    ):
        raise ReviewPreviewError("private_source_metadata_invalid")
    try:
        validate_source_context_page(
            admitted.page,
            admitted.conversation,
            scope=admitted.scope,
            source_hmac=admitted.source_hmac,
        )
    except (SourceContractError, TypeError, ValueError):
        raise ReviewPreviewError("private_source_context_invalid") from None
    if (
        admitted.content_sha256 != admitted.conversation.content_sha256
        or admitted.revision_sha256 != admitted.conversation.source_revision_sha256
        or not admitted.page.units
    ):
        raise ReviewPreviewError("source_evidence_or_revision_invalid")
    try:
        proposals = parse_candidate_proposals(proposal_wire_bytes)
        if len(proposals) > _CANDIDATE_GENERATION_BUDGET:
            raise ReviewPreviewError("candidate_budget_exceeded")
        batch = build_candidate_batch(
            admitted.page,
            admitted.conversation,
            scope=admitted.scope,
            source_hmac=admitted.source_hmac,
            proposals=proposals,
        )
        validate_candidate_batch(
            batch,
            admitted.page,
            admitted.conversation,
            scope=admitted.scope,
            source_hmac=admitted.source_hmac,
        )
    except (CandidateContractError, SourceContractError):
        raise ReviewPreviewError("candidate_evidence_invalid") from None

    # Policy-excluded placeholders and internal/nonfinal content are not
    # missing *visible* evidence. Unsupported text/multimodal payloads are.
    ignored_omissions = frozenset(
        {
            "placeholder",
            "internal_reasoning",
            "nonconversation_role",
            "nonfinal_assistant_message",
            "hidden_message",
        }
    )
    missing_visible = any(
        reason not in ignored_omissions and count > 0
        for reason, count in admitted.page.omitted_reasons
    )
    gap = (
        admitted.inventory_status != "inventory_only"
        or missing_visible
        or admitted.page.unverified_child_links > 0
        or not admitted.page.complete
    )
    risks = tuple(
        ReviewRisk(
            candidate_id=candidate.candidate_id,
            anchor_sha256=candidate.evidence_anchor_sha256,
            signals=_risks(candidate, coverage_gap=gap),
        )
        for candidate in batch.candidates
    )
    return ExternalReviewPreview(
        version=REVIEW_PREVIEW_VERSION,
        scope=admitted.scope,
        source_hmac=admitted.source_hmac,
        archive_sha256=admitted.archive_sha256,
        content_sha256=admitted.content_sha256,
        revision_sha256=admitted.revision_sha256,
        source_run_id=admitted.run_id,
        start_cursor=admitted.page.start_cursor,
        next_cursor=admitted.page.next_cursor,
        candidate_batch=batch,
        risks=risks,
        preview_digest=_preview_digest(admitted, batch),
        status="review_required" if batch.candidates else "no_candidate",
    )


def prepare_private_review_preview(
    root: Path,
    *,
    scope: str,
    source_hmac: str,
    expected_content_sha256: str,
    expected_revision_sha256: str,
    proposal_wire_bytes: bytes,
    cursor: SourceCursor | None = None,
    members: Sequence[str] = (),
) -> ExternalReviewPreview:
    """Private one-shot read→strict JSON→exact evidence binding→review preview.

    A stale rev or uncommitted archive fails before proposal parsing. This
    returns an in-memory *preview*, never an awaiting_human_review Job.
    """
    try:
        admitted = read_admitted_source_page(
            root,
            scope=scope,
            source_hmac=source_hmac,
            expected_content_sha256=expected_content_sha256,
            expected_revision_sha256=expected_revision_sha256,
            cursor=cursor,
            members=members,
        )
    except PrivateSourceReaderError:
        raise ReviewPreviewError("private_source_read_rejected") from None
    return build_private_review_preview(
        admitted,
        proposal_wire_bytes=proposal_wire_bytes,
    )


def revalidate_private_review_preview(
    root: Path,
    preview: ExternalReviewPreview,
    *,
    members: Sequence[str] = (),
) -> None:
    """Independently check freshness and every review-only preview invariant.

    Invoke again immediately before any *future* Job adapter is allowed to
    materialize an external Source. This still authorizes NO transition;
    it only returns successfully when the private Reader's evidence matches.
    """
    if type(preview) is not ExternalReviewPreview:
        raise ReviewPreviewError("invalid_review_preview")
    if (
        preview.version != REVIEW_PREVIEW_VERSION
        or preview.job_status != _EXTERNAL_JOB_STATUS
        or type(preview.semantic_evaluation_completed) is not bool
        or preview.semantic_evaluation_completed
        or type(preview.awaiting_human_review) is not bool
        or preview.awaiting_human_review
        or type(preview.generation_authorized) is not bool
        or preview.generation_authorized
        or type(preview.canonical_mutation_authorized) is not bool
        or preview.canonical_mutation_authorized
    ):
        raise ReviewPreviewError("review_preview_authority_forbidden")

    try:
        admitted = read_admitted_source_page(
            root,
            scope=preview.scope,
            source_hmac=preview.source_hmac,
            expected_content_sha256=preview.content_sha256,
            expected_revision_sha256=preview.revision_sha256,
            cursor=preview.start_cursor,
            members=members,
        )
    except PrivateSourceReaderError:
        raise ReviewPreviewError("review_preview_source_stale") from None

    if (
        preview.archive_sha256 != admitted.archive_sha256
        or preview.source_run_id != admitted.run_id
        or preview.next_cursor != admitted.page.next_cursor
        or type(preview.candidate_batch) is not CandidateBatch
        or type(preview.risks) is not tuple
    ):
        raise ReviewPreviewError("review_preview_provenance_mismatch")
    try:
        validate_candidate_batch(
            preview.candidate_batch,
            admitted.page,
            admitted.conversation,
            scope=admitted.scope,
            source_hmac=admitted.source_hmac,
        )
    except (CandidateContractError, SourceContractError, TypeError, ValueError):
        raise ReviewPreviewError("review_preview_candidate_mismatch") from None

    expected_status = (
        "review_required" if preview.candidate_batch.candidates else "no_candidate"
    )
    missing_visible = any(
        reason
        not in {
            "placeholder",
            "internal_reasoning",
            "nonconversation_role",
            "nonfinal_assistant_message",
            "hidden_message",
        }
        and count > 0
        for reason, count in admitted.page.omitted_reasons
    )
    gap = (
        admitted.inventory_status != "inventory_only"
        or missing_visible
        or admitted.page.unverified_child_links > 0
        or not admitted.page.complete
    )
    expected_risks = tuple(
        ReviewRisk(
            candidate_id=c.candidate_id,
            anchor_sha256=c.evidence_anchor_sha256,
            signals=_risks(c, coverage_gap=gap),
        )
        for c in preview.candidate_batch.candidates
    )
    if (
        preview.status != expected_status
        or preview.risks != expected_risks
        or preview.preview_digest
        != _preview_digest(
            admitted,
            preview.candidate_batch,
        )
    ):
        raise ReviewPreviewError("review_preview_integrity_mismatch")
