"""Private on-demand External Source evidence for Human Review, never approval.

The existing synthetic Worker reaches the real pre-review awaiting state,
but its metadata-only projection has hashes rather than original quotations.
This module reopens protected Source/review evidence on demand to produce a
bounded, in-memory human-readable packet. It cannot submit an approval,
publish a note, or expose a plaintext conversation identifier/title.
"""

from __future__ import annotations

import hashlib
import json
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
from .chatgpt_external_worker import (
    ExternalWorkerError,
    inspect_external_pending_review,
)

PACKET_VERSION = "chatgpt-private-review-evidence-v0"
MAX_PACKET_BYTES = 48 * 1024
MAX_CANDIDATES = 16
MAX_QUOTE_BYTES = 4096
MAX_SUMMARY_BYTES = 4096


class ExternalReviewEvidenceError(ValueError):
    """Constant diagnostics; source content never appears in exceptions."""


@dataclass(frozen=True, repr=False)
class PrivateReviewQuotation:
    candidate_id: str
    source_anchor_sha256: str
    message_index: int
    message_sha256: str
    quote_start_codepoint: int
    quote_end_codepoint: int
    exact_quote: str
    exact_quote_sha256: str
    speaker_role: str
    attribution: str
    proposed_kind: str
    proposed_summary: str
    required_checks: tuple[str, ...]
    source_quote_verified: bool = True
    semantic_truth_verified: bool = False
    user_confirmation_verified: bool = False
    correction_chronology_verified: bool = False


@dataclass(frozen=True, repr=False)
class PrivateReviewEvidencePacket:
    """Must remain within authenticated Reader boundary; never public logs."""

    version: str
    job_id: str
    generation_id: str
    source_hmac: str
    source_revision_sha256: str
    source_preview_sha256: str
    evaluation_sha256: str
    quotations: tuple[PrivateReviewQuotation, ...]
    packet_sha256: str
    context_complete: bool
    semantic_status: str = "unknown"
    review_state: str = "awaiting_human_review"
    human_approval_granted: bool = False
    canonical_vault_mutated: bool = False


def _quote_bytes(value: object, *, limit: int) -> bytes:
    if type(value) is not str or not value.strip() or "\x00" in value:
        raise ExternalReviewEvidenceError("private_evidence_text_invalid")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        raise ExternalReviewEvidenceError("private_evidence_text_invalid") from None
    if not 0 < len(encoded) <= limit:
        raise ExternalReviewEvidenceError("private_evidence_text_limit")
    return encoded


def read_private_review_evidence(
    ai_root: Path,
    *,
    private_root: Path,
    preview: ExternalReviewPreview,
    job_id: str,
    max_candidates: int = MAX_CANDIDATES,
) -> PrivateReviewEvidencePacket:
    """Retrieve the exact private quotes for a pending synthetic Review Job.

    Only a Reader-authorized local caller is allowed to consume the result.
    Each quote/summary is untrusted; the packet does NOT evaluate whether
    a plan was accepted, a result was achieved, or later corrections negate
    the original. Human Review/semantic evaluation must resolve those.
    """
    if type(max_candidates) is not int or not 1 <= max_candidates <= MAX_CANDIDATES:
        raise ExternalReviewEvidenceError("private_review_limit_invalid")
    if type(preview) is not ExternalReviewPreview or preview.scope != "fake-e2e":
        # This v0 is synthetic-only. Real Export/GUI exposure must require
        # its own Reviewer UID, transport and explicit Human acceptance.
        raise ExternalReviewEvidenceError("private_review_scope_not_authorized")

    try:
        root = _existing_private_root(private_root)
        with _read_locked(root):
            revalidate_private_review_preview(root, preview)
            pending = inspect_external_pending_review(
                ai_root,
                private_root=root,
                preview=preview,
                job_id=job_id,
            )
            if (
                pending["state"] != "awaiting_human_review"
                or pending["recommendation"] != "manual_review"
                or pending["human_approval_granted"] is not False
                or pending["canonical_vault_mutated"] is not False
            ):
                raise ExternalReviewEvidenceError("private_review_not_pending")
            candidates = preview.candidate_batch.candidates
            if not candidates or len(candidates) > max_candidates:
                raise ExternalReviewEvidenceError("private_review_limit_exceeded")
            risks = {risk.candidate_id: risk for risk in preview.risks}
            if len(risks) != len(preview.risks):
                raise ExternalReviewEvidenceError("private_review_risk_mismatch")
            entries: list[PrivateReviewQuotation] = []
            for cand in candidates:
                risk = risks.get(cand.candidate_id)
                if (
                    risk is None
                    or risk.anchor_sha256 != cand.evidence_anchor_sha256
                    or "semantic_review_required" not in risk.signals
                ):
                    raise ExternalReviewEvidenceError("private_review_risk_mismatch")
                exact = _quote_bytes(cand.excerpt, limit=MAX_QUOTE_BYTES)
                _quote_bytes(cand.proposed_summary, limit=MAX_SUMMARY_BYTES)
                if hashlib.sha256(exact).hexdigest() != cand.excerpt_sha256:
                    raise ExternalReviewEvidenceError("private_review_quote_mismatch")
                entries.append(
                    PrivateReviewQuotation(
                        candidate_id=cand.candidate_id,
                        source_anchor_sha256=cand.evidence_anchor_sha256,
                        message_index=cand.message_index,
                        message_sha256=cand.source_message_sha256,
                        quote_start_codepoint=cand.quote_start_codepoint,
                        quote_end_codepoint=cand.quote_end_codepoint,
                        exact_quote=cand.excerpt,
                        exact_quote_sha256=cand.excerpt_sha256,
                        speaker_role=cand.role,
                        attribution=cand.attribution,
                        proposed_kind=cand.proposed_kind,
                        proposed_summary=cand.proposed_summary,
                        required_checks=risk.signals,
                    )
                )
            # A verified quotation on a bounded selected page is NOT
            # evidence that later user statements/corrections were reviewed.
            context_complete = not any(
                "source_coverage_incomplete" in risk.signals for risk in preview.risks
            )
            payload = {
                "packet_version": PACKET_VERSION,
                "job_id": job_id,
                "generation_id": pending["generation_id"],
                "source_hmac": preview.source_hmac,
                "source_revision_sha256": preview.revision_sha256,
                "source_preview_sha256": preview.preview_digest,
                "evaluation_sha256": pending["evaluation_sha256"],
                "quotations": [
                    {
                        "candidate_id": ent.candidate_id,
                        "source_anchor_sha256": ent.source_anchor_sha256,
                        "message_index": ent.message_index,
                        "message_sha256": ent.message_sha256,
                        "quote_start_codepoint": ent.quote_start_codepoint,
                        "quote_end_codepoint": ent.quote_end_codepoint,
                        "exact_quote": ent.exact_quote,
                        "exact_quote_sha256": ent.exact_quote_sha256,
                        "speaker_role": ent.speaker_role,
                        "attribution": ent.attribution,
                        "proposed_kind": ent.proposed_kind,
                        "proposed_summary": ent.proposed_summary,
                        "required_checks": list(ent.required_checks),
                        "source_quote_verified": True,
                        "semantic_truth_verified": False,
                        "user_confirmation_verified": False,
                        "correction_chronology_verified": False,
                    }
                    for ent in entries
                ],
                "context_complete": context_complete,
                "semantic_status": "unknown",
                "review_state": "awaiting_human_review",
                "human_approval_granted": False,
                "canonical_vault_mutated": False,
            }
            encoded = json.dumps(
                payload,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            if len(encoded) > MAX_PACKET_BYTES:
                raise ExternalReviewEvidenceError("private_review_packet_limit")
            return PrivateReviewEvidencePacket(
                version=PACKET_VERSION,
                job_id=job_id,
                generation_id=pending["generation_id"],
                source_hmac=preview.source_hmac,
                source_revision_sha256=preview.revision_sha256,
                source_preview_sha256=preview.preview_digest,
                evaluation_sha256=pending["evaluation_sha256"],
                quotations=tuple(entries),
                packet_sha256=hashlib.sha256(encoded).hexdigest(),
                context_complete=context_complete,
            )
    except ExternalReviewEvidenceError:
        raise
    except (
        PrivateSourceReaderError,
        ReviewPreviewError,
        ExternalWorkerError,
        OSError,
        ValueError,
        TypeError,
    ):
        raise ExternalReviewEvidenceError("private_review_source_unavailable") from None
