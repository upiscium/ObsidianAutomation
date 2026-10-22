"""Bounded later-user-turn context for a private External Human Reviewer.

This is a supplemental, entirely non-authoritative view of later USER
messages on the currently selected ChatGPT branch. It is not semantic
correction/confirmation detection, does not claim global completeness,
and never creates a Human approval, LLM Job or Vault write.
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
    read_admitted_source_page,
)
from .chatgpt_export_review_preview import ExternalReviewPreview
from .chatgpt_external_review_evidence import (
    ExternalReviewEvidenceError,
    read_private_review_evidence,
)

CHRONOLOGY_VERSION = "chatgpt-private-later-user-context-v0"
DEFAULT_LATER_TURNS = 4
MAX_LATER_TURNS = 8
DEFAULT_SNIPPET_BYTES = 512
MAX_SNIPPET_BYTES = 1024
MAX_CHRONOLOGY_BYTES = 48 * 1024


class PrivateChronologyError(ValueError):
    """Fixed failure codes; never echo private source or host paths."""


@dataclass(frozen=True, repr=False)
class PrivateLaterUserSnippet:
    selected_message_index: int
    text_sha256: str
    preview_text: str
    truncated: bool
    source_role: str = "user"
    message_after_evidence: bool = True
    semantic_correction_verified: bool = False
    user_confirmation_verified: bool = False


@dataclass(frozen=True, repr=False)
class CandidateLaterUserContext:
    candidate_id: str
    source_anchor_sha256: str
    reference_message_index: int
    later_user_turns: tuple[PrivateLaterUserSnippet, ...]
    more_later_user_turns_unshown: bool
    later_selected_branch_only: bool = True
    complete_chronological_correction_check: bool = False


@dataclass(frozen=True, repr=False)
class PrivateReviewChronology:
    """Private process memory only; no public Dashboard or Provider access."""

    version: str
    job_id: str
    source_revision_sha256: str
    evidence_packet_sha256: str
    candidate_context: tuple[CandidateLaterUserContext, ...]
    chronology_sha256: str
    semantic_status: str = "unknown"
    user_confirmation_verified: bool = False
    complete_cross_conversation_correction_check: bool = False
    human_approval_granted: bool = False
    canonical_vault_mutated: bool = False


def _shortened(text: str, max_bytes: int) -> tuple[str, bool]:
    """UTF-8 byte-bounded prefix ending at a complete Unicode character."""
    if type(text) is not str:
        raise PrivateChronologyError("later_user_text_invalid")
    try:
        encoded = text.encode("utf-8")
    except UnicodeError:
        raise PrivateChronologyError("later_user_text_invalid") from None
    if len(encoded) <= max_bytes:
        return text, False
    prefix = encoded[:max_bytes].decode("utf-8", errors="ignore")
    if not prefix:
        raise PrivateChronologyError("later_user_text_boundary_invalid")
    return prefix, True


def read_private_later_user_context(
    ai_root: Path,
    *,
    private_root: Path,
    preview: ExternalReviewPreview,
    job_id: str,
    max_later_turns: int = DEFAULT_LATER_TURNS,
    max_snippet_bytes: int = DEFAULT_SNIPPET_BYTES,
) -> PrivateReviewChronology:
    """Later *selected-branch* user excerpts accompanying an exact Review.

    A user's later message MAY be a correction or confirmation. It may also
    be irrelevant. This returns the literal text and its original digest,
    never an invented semantic label. The full Export, other branches,
    other conversations and future messages are not exhausted/verified.
    """
    if type(max_later_turns) is not int or not 1 <= max_later_turns <= MAX_LATER_TURNS:
        raise PrivateChronologyError("later_user_turn_limit_invalid")
    if (
        type(max_snippet_bytes) is not int
        or not 16 <= max_snippet_bytes <= MAX_SNIPPET_BYTES
    ):
        raise PrivateChronologyError("later_user_snippet_limit_invalid")
    if type(preview) is not ExternalReviewPreview or preview.scope != "fake-e2e":
        raise PrivateChronologyError("later_user_scope_not_authorized")
    try:
        root = _existing_private_root(private_root)
        with _read_locked(root):
            exact_review = read_private_review_evidence(
                ai_root,
                private_root=root,
                preview=preview,
                job_id=job_id,
            )
            admitted = read_admitted_source_page(
                root,
                scope=preview.scope,
                source_hmac=preview.source_hmac,
                expected_content_sha256=preview.content_sha256,
                expected_revision_sha256=preview.revision_sha256,
                cursor=preview.start_cursor,
            )
            if (
                admitted.archive_sha256 != preview.archive_sha256
                or admitted.run_id != preview.source_run_id
                or admitted.revision_sha256 != exact_review.source_revision_sha256
            ):
                raise PrivateChronologyError("later_user_source_binding_invalid")
            candidate_by_id = {
                c.candidate_id: c for c in preview.candidate_batch.candidates
            }
            if len(candidate_by_id) != len(preview.candidate_batch.candidates):
                raise PrivateChronologyError("later_user_candidate_identity_invalid")
            messages = admitted.conversation.messages
            candidate_context: list[CandidateLaterUserContext] = []
            for evidence in exact_review.quotations:
                candidate = candidate_by_id.get(evidence.candidate_id)
                if (
                    candidate is None
                    or evidence.source_anchor_sha256 != candidate.evidence_anchor_sha256
                    or type(candidate.message_index) is not int
                    or not 0 <= candidate.message_index < len(messages)
                ):
                    raise PrivateChronologyError("later_user_candidate_binding_invalid")
                initial = messages[candidate.message_index]
                if (
                    initial.text_sha256 != candidate.source_message_sha256
                    or initial.role != candidate.role
                    or initial.text[
                        candidate.quote_start_codepoint : candidate.quote_end_codepoint
                    ]
                    != candidate.excerpt
                ):
                    raise PrivateChronologyError("later_user_source_quote_invalid")
                snippets: list[PrivateLaterUserSnippet] = []
                more_unshown = False
                for pos in range(candidate.message_index + 1, len(messages)):
                    message = messages[pos]
                    if message.role != "user":
                        continue
                    if len(snippets) >= max_later_turns:
                        more_unshown = True
                        break
                    try:
                        full_bytes = message.text.encode("utf-8")
                    except UnicodeError:
                        raise PrivateChronologyError(
                            "later_user_text_invalid"
                        ) from None
                    if hashlib.sha256(full_bytes).hexdigest() != message.text_sha256:
                        raise PrivateChronologyError(
                            "later_user_message_digest_invalid"
                        )
                    short_text, truncated = _shortened(
                        message.text,
                        max_snippet_bytes,
                    )
                    snippets.append(
                        PrivateLaterUserSnippet(
                            selected_message_index=pos,
                            text_sha256=message.text_sha256,
                            preview_text=short_text,
                            truncated=truncated,
                        )
                    )
                candidate_context.append(
                    CandidateLaterUserContext(
                        candidate_id=candidate.candidate_id,
                        source_anchor_sha256=candidate.evidence_anchor_sha256,
                        reference_message_index=candidate.message_index,
                        later_user_turns=tuple(snippets),
                        more_later_user_turns_unshown=more_unshown,
                    )
                )
            payload = {
                "contract_version": CHRONOLOGY_VERSION,
                "job_id": job_id,
                "source_revision_sha256": admitted.revision_sha256,
                "review_evidence_packet_sha256": exact_review.packet_sha256,
                "candidate_context": [
                    {
                        "candidate_id": item.candidate_id,
                        "anchor_sha256": item.source_anchor_sha256,
                        "reference_message_index": item.reference_message_index,
                        "selected_branch_only": True,
                        "more_unshown": item.more_later_user_turns_unshown,
                        "snippets": [
                            {
                                "message_index": sl.selected_message_index,
                                "text_sha256": sl.text_sha256,
                                "text": sl.preview_text,
                                "truncated": sl.truncated,
                                "semantic_correction_verified": False,
                                "user_confirmation_verified": False,
                            }
                            for sl in item.later_user_turns
                        ],
                    }
                    for item in candidate_context
                ],
                "semantic_status": "unknown",
                "complete_cross_conversation_correction_check": False,
                "human_approval_granted": False,
                "canonical_vault_mutated": False,
            }
            try:
                encoded = json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            except (TypeError, ValueError, UnicodeError):
                raise PrivateChronologyError(
                    "later_user_serialization_invalid"
                ) from None
            if len(encoded) > MAX_CHRONOLOGY_BYTES:
                raise PrivateChronologyError("later_user_context_budget_exceeded")
            return PrivateReviewChronology(
                version=CHRONOLOGY_VERSION,
                job_id=job_id,
                source_revision_sha256=admitted.revision_sha256,
                evidence_packet_sha256=exact_review.packet_sha256,
                candidate_context=tuple(candidate_context),
                chronology_sha256=hashlib.sha256(encoded).hexdigest(),
            )
    except PrivateChronologyError:
        raise
    except (
        PrivateSourceReaderError,
        ExternalReviewEvidenceError,
        OSError,
        UnicodeError,
    ):
        raise PrivateChronologyError("later_user_source_unavailable") from None
