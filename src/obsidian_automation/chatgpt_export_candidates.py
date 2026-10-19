"""Private, evidence-bound *unverified* Knowledge candidate contract (#62).

Pure transformation of an already validated external SourceContextPage and
an untrusted proposal. No model calls, Vault writes, job submission, or
approval. This module must not be used as a generator authorization token.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass

from .chatgpt_export_inspect import InspectionError, _json
from .chatgpt_export_normalize import SelectedConversation, _hash
from .chatgpt_export_source import (
    SOURCE_CONTRACT_VERSION,
    SOURCE_KIND,
    TRUST_LEVEL,
    SourceContextPage,
    validate_source_context_page,
)

CANDIDATE_CONTRACT_VERSION = "chatgpt-evidence-candidate-v0"
CANDIDATE_REVIEW_STATE = "needs_semantic_and_human_review"
CANDIDATE_AUTHORITY = "none"
PROPOSAL_ORIGIN = "untrusted_proposal"
MAX_CANDIDATES_PER_PAGE = 16
MAX_EXCERPT_BYTES = 4096
MAX_PROPOSAL_BYTES = 4096
MAX_TOTAL_PROPOSAL_BYTES = 32768
MAX_MODEL_RESPONSE_BYTES = 64 * 1024
_SHA = re.compile(r"[0-9a-f]{64}\Z")

# A classification is a *suggestion* that still requires comparison with
# surrounding context, later corrections, and direct human acceptance.
POTENTIAL_KINDS = frozenset({
    "unclassified",
    "question_candidate",
    "hypothesis_candidate",
    "correction_candidate",
    "decision_candidate",
    "result_candidate",
    "feature_change_candidate",
})


class CandidateContractError(ValueError):
    """Fixed diagnostic codes; never interpolate source/proposal text."""


@dataclass(frozen=True, repr=False)
class EvidenceProposal:
    """Future model/user proposal; all fields are untrusted input."""
    unit_index: int
    slice_index: int
    quote_start: int
    quote_end: int
    quote: str
    proposed_summary: str
    proposed_kind: str


@dataclass(frozen=True, repr=False)
class KnowledgeEvidenceCandidate:
    candidate_id: str
    evidence_anchor_sha256: str
    contract_version: str
    source_contract_version: str
    source_kind: str
    source_trust_level: str
    scope: str
    source_hmac: str
    conversation_content_sha256: str
    source_revision_sha256: str
    unit_id: str
    unit_index: int
    slice_index: int
    message_index: int
    node_id: str
    message_id: str
    role: str
    attribution: str
    quote_start_codepoint: int
    quote_end_codepoint: int
    source_message_sha256: str
    excerpt: str
    excerpt_sha256: str
    proposed_summary: str
    proposed_kind: str
    review_state: str = CANDIDATE_REVIEW_STATE
    authority: str = CANDIDATE_AUTHORITY
    auto_approved: bool = False
    generation_authorized: bool = False


@dataclass(frozen=True, repr=False)
class CandidateBatch:
    contract_version: str
    source_kind: str
    source_contract_version: str
    proposal_origin: str
    scope: str
    source_hmac: str
    candidates: tuple[KnowledgeEvidenceCandidate, ...]
    total_excerpt_bytes: int
    total_proposal_bytes: int
    human_review_completed: bool = False
    generation_authorized: bool = False


def _safe_text(text: object, *, bound: int, code: str) -> tuple[str, int]:
    if type(text) is not str or not text.strip() or "\x00" in text:
        raise CandidateContractError(code)
    try:
        n = len(text.encode("utf-8"))
    except UnicodeError:
        raise CandidateContractError(code) from None
    if n > bound:
        raise CandidateContractError(code)
    return text, n


def _safe_proposal(p: EvidenceProposal, page: SourceContextPage) -> tuple:
    if type(p) is not EvidenceProposal:
        raise CandidateContractError("invalid_proposal")
    if any(type(x) is not int for x in (
        p.unit_index, p.slice_index, p.quote_start, p.quote_end
    )):
        raise CandidateContractError("invalid_proposal_coordinate")
    if p.unit_index < 0 or p.unit_index >= len(page.units):
        raise CandidateContractError("invalid_proposal_unit")
    unit = page.units[p.unit_index]
    if p.slice_index < 0 or p.slice_index >= len(unit.evidence):
        raise CandidateContractError("invalid_proposal_slice")
    sl = unit.evidence[p.slice_index]
    if not 0 <= p.quote_start < p.quote_end <= len(sl.text):
        raise CandidateContractError("invalid_proposal_span")
    excerpt = sl.text[p.quote_start:p.quote_end]
    _safe_text(excerpt, bound=MAX_EXCERPT_BYTES, code="proposal_excerpt_limit")
    _safe_text(p.quote, bound=MAX_EXCERPT_BYTES, code="proposal_excerpt_limit")
    if excerpt != p.quote:
        raise CandidateContractError("proposal_quote_mismatch")
    summary, summary_bytes = _safe_text(
        p.proposed_summary, bound=MAX_PROPOSAL_BYTES, code="invalid_proposed_summary",
    )
    if type(p.proposed_kind) is not str or p.proposed_kind not in POTENTIAL_KINDS:
        raise CandidateContractError("invalid_proposed_kind")
    return unit, sl, excerpt, summary, summary_bytes


def _make_candidate(
    page: SourceContextPage, proposal: EvidenceProposal,
) -> tuple[KnowledgeEvidenceCandidate, int, int]:
    unit, sl, excerpt, summary, summary_bytes = _safe_proposal(proposal, page)
    excerpt_bytes = excerpt.encode("utf-8")
    excerpt_sha = hashlib.sha256(excerpt_bytes).hexdigest()
    start = sl.start_codepoint + proposal.quote_start
    end = sl.start_codepoint + proposal.quote_end
    anchor = _hash({
        "contract": CANDIDATE_CONTRACT_VERSION,
        "source_kind": SOURCE_KIND,
        "scope": page.scope,
        "source_hmac": page.source_hmac,
        "semantic_sha": unit.conversation_content_sha256,
        "unit_id": unit.unit_id,
        "message_index": sl.message_index,
        "role": sl.role,
        "start": start, "end": end,
        "excerpt_sha": excerpt_sha,
    })
    ident = _hash({
        "anchor": anchor,
        "proposed_kind": proposal.proposed_kind,
        "summary_sha": hashlib.sha256(summary.encode("utf-8")).hexdigest(),
    })
    candidate = KnowledgeEvidenceCandidate(
        candidate_id=ident,
        evidence_anchor_sha256=anchor,
        contract_version=CANDIDATE_CONTRACT_VERSION,
        source_contract_version=SOURCE_CONTRACT_VERSION,
        source_kind=SOURCE_KIND,
        source_trust_level=TRUST_LEVEL,
        scope=page.scope,
        source_hmac=page.source_hmac,
        conversation_content_sha256=unit.conversation_content_sha256,
        source_revision_sha256=unit.source_revision_sha256,
        unit_id=unit.unit_id,
        unit_index=proposal.unit_index,
        slice_index=proposal.slice_index,
        message_index=sl.message_index,
        node_id=sl.node_id,
        message_id=sl.message_id,
        role=sl.role,
        attribution=sl.attribution,
        quote_start_codepoint=start,
        quote_end_codepoint=end,
        source_message_sha256=sl.source_text_sha256,
        excerpt=excerpt,
        excerpt_sha256=excerpt_sha,
        proposed_summary=summary,
        proposed_kind=proposal.proposed_kind,
    )
    return candidate, len(excerpt_bytes), summary_bytes


def build_candidate_batch(
    page: SourceContextPage,
    conversation: SelectedConversation, *,
    scope: str, source_hmac: str,
    proposals: Sequence[EvidenceProposal],
) -> CandidateBatch:
    """Validate source first, then exact-quote-bind every untrusted proposal.

    A proposed kind, quote or summary is NOT independently true. Even "decision"
    is only a candidate and carries zero authority. Future model output must
    be passed through this validator before any optional private review step.
    """
    validate_source_context_page(
        page, conversation, scope=scope, source_hmac=source_hmac,
    )
    if (not isinstance(proposals, (tuple, list)) or
            len(proposals) > MAX_CANDIDATES_PER_PAGE):
        raise CandidateContractError("candidate_batch_limit")
    out = []
    seen_coordinates: set[tuple[int, int, int, int]] = set()
    excerpt_total, summary_total = 0, 0
    for proposal in proposals:
        candidate, e_bytes, s_bytes = _make_candidate(page, proposal)
        key = (
            proposal.unit_index, proposal.slice_index,
            proposal.quote_start, proposal.quote_end,
        )
        if key in seen_coordinates:
            raise CandidateContractError("duplicate_evidence_proposal")
        seen_coordinates.add(key)
        excerpt_total += e_bytes
        summary_total += s_bytes
        if excerpt_total > MAX_TOTAL_PROPOSAL_BYTES or summary_total > MAX_TOTAL_PROPOSAL_BYTES:
            raise CandidateContractError("candidate_batch_limit")
        out.append(candidate)
    return CandidateBatch(
        contract_version=CANDIDATE_CONTRACT_VERSION,
        source_kind=SOURCE_KIND,
        source_contract_version=SOURCE_CONTRACT_VERSION,
        proposal_origin=PROPOSAL_ORIGIN,
        scope=scope,
        source_hmac=source_hmac,
        candidates=tuple(out),
        total_excerpt_bytes=excerpt_total,
        total_proposal_bytes=summary_total,
    )


def validate_candidate_batch(
    batch: CandidateBatch,
    page: SourceContextPage, conversation: SelectedConversation, *,
    scope: str, source_hmac: str,
) -> None:
    """Recompute exact bound candidates and reject forged claims/authority."""
    if type(batch) is not CandidateBatch:
        raise CandidateContractError("invalid_candidate_batch")
    if (batch.contract_version != CANDIDATE_CONTRACT_VERSION or
            batch.source_kind != SOURCE_KIND or
            batch.source_contract_version != SOURCE_CONTRACT_VERSION or
            batch.proposal_origin != PROPOSAL_ORIGIN or
            batch.scope != scope or batch.source_hmac != source_hmac):
        raise CandidateContractError("candidate_contract_mismatch")
    if (type(batch.human_review_completed) is not bool or
            batch.human_review_completed or
            type(batch.generation_authorized) is not bool or batch.generation_authorized):
        raise CandidateContractError("candidate_authority_forbidden")
    if len(batch.candidates) > MAX_CANDIDATES_PER_PAGE:
        raise CandidateContractError("candidate_batch_limit")
    # Never trust a supplied batch just because its hashes look plausible:
    # re-read the exact current private source and compare every field.
    proposals = []
    for candidate in batch.candidates:
        if type(candidate) is not KnowledgeEvidenceCandidate:
            raise CandidateContractError("invalid_candidate")
        if (candidate.authority != CANDIDATE_AUTHORITY or
                candidate.review_state != CANDIDATE_REVIEW_STATE or
                type(candidate.auto_approved) is not bool or
                candidate.auto_approved or
                type(candidate.generation_authorized) is not bool or
                candidate.generation_authorized):
            raise CandidateContractError("candidate_authority_forbidden")
        if type(candidate.unit_index) is not int or type(candidate.slice_index) is not int:
            raise CandidateContractError("invalid_proposal_coordinate")
        if not 0 <= candidate.unit_index < len(page.units):
            raise CandidateContractError("invalid_proposal_unit")
        unit = page.units[candidate.unit_index]
        if not 0 <= candidate.slice_index < len(unit.evidence):
            raise CandidateContractError("invalid_proposal_slice")
        sl = unit.evidence[candidate.slice_index]
        proposals.append(EvidenceProposal(
            unit_index=candidate.unit_index,
            slice_index=candidate.slice_index,
            quote_start=candidate.quote_start_codepoint - sl.start_codepoint
            if type(candidate.quote_start_codepoint) is int else -1,
            quote_end=candidate.quote_end_codepoint - sl.start_codepoint
            if type(candidate.quote_end_codepoint) is int else -1,
            quote=candidate.excerpt,
            proposed_summary=candidate.proposed_summary,
            proposed_kind=candidate.proposed_kind,
        ))
    expected = build_candidate_batch(
        page, conversation, scope=scope, source_hmac=source_hmac,
        proposals=proposals,
    )
    if batch != expected:
        raise CandidateContractError("candidate_evidence_mismatch")


def parse_candidate_proposals(data: bytes) -> tuple[EvidenceProposal, ...]:
    """Strict, size-limited decoder for untrusted future model output.

    This is only syntax/schema parsing, not support for claim correctness.
    Exact quote/source/role binding requires build_candidate_batch().
    """
    if type(data) is not bytes or len(data) > MAX_MODEL_RESPONSE_BYTES:
        raise CandidateContractError("proposal_wire_limit")
    try:
        raw = _json(data)
    except (InspectionError, ValueError, UnicodeError, RecursionError):
        raise CandidateContractError("invalid_proposal_json") from None
    if (type(raw) is not dict or set(raw) != {"contract_version", "proposals"} or
            raw["contract_version"] != CANDIDATE_CONTRACT_VERSION or
            type(raw["proposals"]) is not list or
            len(raw["proposals"]) > MAX_CANDIDATES_PER_PAGE):
        raise CandidateContractError("invalid_proposal_envelope")
    keys = {
        "unit_index", "slice_index", "quote_start", "quote_end", "quote",
        "proposed_summary", "proposed_kind",
    }
    proposals = []
    for item in raw["proposals"]:
        if type(item) is not dict or set(item) != keys:
            raise CandidateContractError("invalid_proposal_fields")
        if any(type(item[key]) is not int for key in (
            "unit_index", "slice_index", "quote_start", "quote_end"
        )):
            raise CandidateContractError("invalid_proposal_coordinate")
        _safe_text(
            item["quote"], bound=MAX_EXCERPT_BYTES,
            code="proposal_excerpt_limit",
        )
        _safe_text(
            item["proposed_summary"],
            bound=MAX_PROPOSAL_BYTES,
            code="invalid_proposed_summary",
        )
        if (type(item["proposed_kind"]) is not str or
                item["proposed_kind"] not in POTENTIAL_KINDS):
            raise CandidateContractError("invalid_proposed_kind")
        proposals.append(EvidenceProposal(**item))
    return tuple(proposals)
