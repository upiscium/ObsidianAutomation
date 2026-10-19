"""Private, bounded external Source/Context contract for ChatGPT Export v0.

Pure memory-only Reader transformation. Does not write files, generate
Knowledge, access the Vault, submit jobs, or authorize Human Review.
Never log/serialize these private dataclasses or their text/IDs.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re

from .chatgpt_export_normalize import (
    NORMALIZER_VERSION,
    SelectedConversation,
    SelectedMessage,
    _hash,
)

SOURCE_KIND = "chatgpt-export"
SOURCE_CONTRACT_VERSION = "chatgpt-external-source-v0"
TRUST_LEVEL = "untrusted-evidence"
MAX_UNIT_BYTES = 16 * 1024
MAX_PAGE_BYTES = 64 * 1024
MAX_PAGE_UNITS = 8
MAX_UNIT_SEGMENTS = 16
MAX_MESSAGE_BYTES = 128 * 1024
MAX_PRIVATE_CONVERSATION_BYTES = 24 * 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SCOPE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")


class SourceContractError(ValueError):
    """Only constant error codes; do not embed private input values."""


@dataclass(frozen=True, repr=False)
class SourceCursor:
    message_index: int = 0
    text_offset: int = 0


@dataclass(frozen=True, repr=False)
class EvidenceSlice:
    """Exact, locally re-readable source coordinates and bounded private text."""
    message_index: int
    node_id: str
    message_id: str
    role: str
    attribution: str
    source_text_sha256: str
    start_codepoint: int
    end_codepoint: int
    text: str
    text_sha256: str


@dataclass(frozen=True, repr=False)
class ExtractionUnit:
    unit_id: str
    contract_version: str
    source_kind: str
    trust_level: str
    scope: str
    source_hmac: str
    conversation_content_sha256: str
    source_revision_sha256: str
    evidence: tuple[EvidenceSlice, ...]
    context_bytes: int
    previous_source_message_sha256: str | None
    following_source_message_sha256: str | None
    # A future Generator must make its own decisions through a Review gate.
    generation_authorized: bool = False


@dataclass(frozen=True, repr=False)
class SourceContextPage:
    contract_version: str
    source_kind: str
    trust_level: str
    source_hmac: str
    scope: str
    units: tuple[ExtractionUnit, ...]
    start_cursor: SourceCursor
    next_cursor: SourceCursor
    complete: bool
    text_bytes: int
    omitted_reasons: tuple[tuple[str, int], ...]
    unverified_child_links: int
    off_branch_nodes: int
    # Deliberately false: neither schema inspection nor Reader rendering
    # grants permission to invoke LLM or write canonical notes.
    generation_authorized: bool = False


def _validate_conversation(conv: SelectedConversation) -> None:
    if type(conv) is not SelectedConversation:
        raise SourceContractError("invalid_source_conversation")
    if (not isinstance(conv.conversation_id, str) or not conv.conversation_id or
            not isinstance(conv.content_sha256, str) or
            not isinstance(conv.source_revision_sha256, str) or
            not _SHA256.fullmatch(conv.content_sha256) or
            not _SHA256.fullmatch(conv.source_revision_sha256)):
        raise SourceContractError("invalid_source_identity")
    if len(conv.messages) > 500_000:
        raise SourceContractError("source_message_limit")
    semantic = []
    total = 0
    seen_ids: set[str] = set()
    for message in conv.messages:
        if type(message) is not SelectedMessage:
            raise SourceContractError("invalid_source_message")
        if (message.role not in ("user", "assistant") or
                message.content_type not in ("text", "multimodal_text") or
                not isinstance(message.node_id, str) or
                not isinstance(message.message_id, str) or
                not message.node_id or not message.message_id or
                type(message.text) is not str or not message.text):
            raise SourceContractError("invalid_source_message")
        if message.message_id in seen_ids:
            raise SourceContractError("duplicate_source_message_id")
        seen_ids.add(message.message_id)
        try:
            encoded = message.text.encode("utf-8")
        except UnicodeError:
            raise SourceContractError("invalid_text_encoding") from None
        if len(encoded) > MAX_MESSAGE_BYTES:
            raise SourceContractError("source_message_limit")
        total += len(encoded)
        if total > MAX_PRIVATE_CONVERSATION_BYTES:
            raise SourceContractError("source_text_limit")
        if hashlib.sha256(encoded).hexdigest() != message.text_sha256:
            raise SourceContractError("source_text_digest_mismatch")
        semantic.append({
            "role": message.role,
            "content_type": message.content_type,
            "content_hash": message.text_sha256,
        })
    actual = _hash({
        "version": NORMALIZER_VERSION, "conversation": conv.conversation_id,
        "path": semantic,
    })
    if actual != conv.content_sha256:
        raise SourceContractError("semantic_source_digest_mismatch")


def _prefix(text: str, position: int, limit_bytes: int) -> tuple[int, int]:
    """Return longest Unicode-codepoint prefix that fits available UTF-8."""
    end, consumed = position, 0
    while end < len(text):
        size = len(text[end].encode("utf-8"))
        if consumed + size > limit_bytes:
            break
        consumed += size
        end += 1
    return end, consumed


def _attribution(role: str) -> str:
    return "user-report-unverified" if role == "user" else "assistant-claim-unverified"


def _unit_id(
    *, scope: str, source_hmac: str, semantic_sha: str,
    evidence: tuple[EvidenceSlice, ...],
) -> str:
    # No plaintext or raw message IDs in unit identity. Opaque node and
    # message IDs can change without spurious semantic regeneration.
    return _hash({
        "contract": SOURCE_CONTRACT_VERSION, "scope": scope,
        "source_hmac": source_hmac, "semantic_sha": semantic_sha,
        "spans": [
            {
                "position": sl.message_index, "role": sl.role,
                "start": sl.start_codepoint, "end": sl.end_codepoint,
                "digest": sl.text_sha256,
            }
            for sl in evidence
        ],
    })


def build_source_context_page(
    conversation: SelectedConversation, *,
    scope: str, source_hmac: str,
    cursor: SourceCursor = SourceCursor(),
    max_units: int = 4,
    max_unit_bytes: int = 8192,
    max_page_bytes: int = 32768,
    max_segments: int = 8,
) -> SourceContextPage:
    """Bounded deterministic read-view over *one* selected conversation.

    The trusted caller obtains SelectedConversation in the private Importer
    process and source_hmac from its protected inventory. No filesystem/LLM,
    no automatic publish, and no v1 11-Knowledge/10-Project impersonation.
    """
    if not isinstance(scope, str) or not _SCOPE.fullmatch(scope):
        raise SourceContractError("invalid_source_scope")
    if not isinstance(source_hmac, str) or not _SHA256.fullmatch(source_hmac):
        raise SourceContractError("invalid_source_hmac")
    if (type(max_units) is not int or not 1 <= max_units <= MAX_PAGE_UNITS or
            type(max_unit_bytes) is not int or not 4 <= max_unit_bytes <= MAX_UNIT_BYTES or
            type(max_page_bytes) is not int or not 4 <= max_page_bytes <= MAX_PAGE_BYTES or
            type(max_segments) is not int or not 1 <= max_segments <= MAX_UNIT_SEGMENTS):
        raise SourceContractError("invalid_context_bounds")
    if type(cursor) is not SourceCursor:
        raise SourceContractError("invalid_source_cursor")
    _validate_conversation(conversation)
    messages = conversation.messages
    idx, pos = cursor.message_index, cursor.text_offset
    if (type(idx) is not int or type(pos) is not int or idx < 0 or
            idx > len(messages) or (idx == len(messages) and pos != 0) or
            (idx < len(messages) and (pos < 0 or pos >= len(messages[idx].text)))):
        raise SourceContractError("invalid_source_cursor")

    total_bytes = 0
    units: list[ExtractionUnit] = []
    while idx < len(messages) and len(units) < max_units:
        budget = min(max_unit_bytes, max_page_bytes - total_bytes)
        if budget < 4:
            break
        slices: list[EvidenceSlice] = []
        unit_bytes = 0
        first_index = idx
        while idx < len(messages) and len(slices) < max_segments and unit_bytes < budget:
            msg = messages[idx]
            begin = pos
            end, consumed = _prefix(msg.text, begin, budget - unit_bytes)
            if end <= begin:
                break
            text = msg.text[begin:end]
            slices.append(EvidenceSlice(
                message_index=idx,
                node_id=msg.node_id,
                message_id=msg.message_id,
                role=msg.role,
                attribution=_attribution(msg.role),
                source_text_sha256=msg.text_sha256,
                start_codepoint=begin,
                end_codepoint=end,
                text=text,
                text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            ))
            unit_bytes += consumed
            if end == len(msg.text):
                idx += 1
                pos = 0
            else:
                pos = end
                break
        if not slices:
            # If a codepoint doesn't fit the remaining page budget, resume
            # without skipping data. Fresh pages must have budget >=4 bytes.
            if total_bytes:
                break
            raise SourceContractError("source_slice_unrepresentable")
        evidence = tuple(slices)
        previous = messages[first_index - 1].text_sha256 if first_index else None
        following = messages[idx].text_sha256 if idx < len(messages) else None
        units.append(ExtractionUnit(
            unit_id=_unit_id(
                scope=scope, source_hmac=source_hmac,
                semantic_sha=conversation.content_sha256, evidence=evidence,
            ),
            contract_version=SOURCE_CONTRACT_VERSION,
            source_kind=SOURCE_KIND,
            trust_level=TRUST_LEVEL,
            scope=scope,
            source_hmac=source_hmac,
            conversation_content_sha256=conversation.content_sha256,
            source_revision_sha256=conversation.source_revision_sha256,
            evidence=evidence,
            context_bytes=unit_bytes,
            previous_source_message_sha256=previous,
            following_source_message_sha256=following,
        ))
        total_bytes += unit_bytes
        if total_bytes >= max_page_bytes:
            break

    return SourceContextPage(
        contract_version=SOURCE_CONTRACT_VERSION,
        source_kind=SOURCE_KIND,
        trust_level=TRUST_LEVEL,
        source_hmac=source_hmac,
        scope=scope,
        units=tuple(units),
        start_cursor=cursor,
        next_cursor=SourceCursor(idx, pos),
        complete=idx == len(messages),
        text_bytes=total_bytes,
        omitted_reasons=conversation.omitted_reasons,
        unverified_child_links=conversation.unverified_child_links,
        off_branch_nodes=conversation.off_branch_nodes,
    )


def validate_source_context_page(
    page: SourceContextPage,
    conversation: SelectedConversation, *, scope: str, source_hmac: str,
) -> None:
    """Re-read exact source coordinates; reject stale/misbound text context."""
    if type(page) is not SourceContextPage:
        raise SourceContractError("invalid_context_page")
    _validate_conversation(conversation)
    if (page.contract_version != SOURCE_CONTRACT_VERSION or
            page.source_kind != SOURCE_KIND or
            page.trust_level != TRUST_LEVEL or page.scope != scope or
            not isinstance(scope, str) or not _SCOPE.fullmatch(scope) or
            not isinstance(source_hmac, str) or not _SHA256.fullmatch(source_hmac)):
        # Page has no impersonatable generic Vault path/authority fields.
        raise SourceContractError("invalid_context_metadata")
    if (page.source_hmac != source_hmac or
            any(unit.source_hmac != source_hmac or unit.scope != scope
                for unit in page.units)):
        raise SourceContractError("context_source_mismatch")
    if page.generation_authorized or any(unit.generation_authorized for unit in page.units):
        raise SourceContractError("context_authority_forbidden")
    if (page.omitted_reasons != conversation.omitted_reasons or
            page.unverified_child_links != conversation.unverified_child_links or
            page.off_branch_nodes != conversation.off_branch_nodes):
        raise SourceContractError("context_coverage_mismatch")
    if (len(page.units) > MAX_PAGE_UNITS or type(page.text_bytes) is not int or
            page.text_bytes < 0 or page.text_bytes > MAX_PAGE_BYTES):
        raise SourceContractError("context_bounds_exceeded")
    if type(page.start_cursor) is not SourceCursor or type(page.next_cursor) is not SourceCursor:
        raise SourceContractError("invalid_context_cursor")
    idx, pos = page.start_cursor.message_index, page.start_cursor.text_offset
    messages = conversation.messages
    if (type(idx) is not int or type(pos) is not int or idx < 0 or
            idx > len(messages) or (idx == len(messages) and pos != 0) or
            (idx < len(messages) and (pos < 0 or pos >= len(messages[idx].text)))):
        raise SourceContractError("invalid_context_cursor")
    consumed = 0
    for unit in page.units:
        if (unit.source_kind != SOURCE_KIND or
                unit.contract_version != SOURCE_CONTRACT_VERSION or
                unit.trust_level != TRUST_LEVEL or
                unit.conversation_content_sha256 != conversation.content_sha256 or
                unit.source_revision_sha256 != conversation.source_revision_sha256):
            raise SourceContractError("context_contract_mismatch")
        if not unit.evidence or len(unit.evidence) > MAX_UNIT_SEGMENTS:
            raise SourceContractError("context_segments_invalid")
        if unit.unit_id != _unit_id(
            scope=scope, source_hmac=source_hmac,
            semantic_sha=conversation.content_sha256, evidence=unit.evidence,
        ):
            raise SourceContractError("context_unit_digest_mismatch")
        previous = messages[idx - 1].text_sha256 if idx else None
        if unit.previous_source_message_sha256 != previous:
            raise SourceContractError("context_adjacent_source_mismatch")
        subtotal = 0
        for sl in unit.evidence:
            if (type(sl.message_index) is not int or
                    sl.message_index < 0 or sl.message_index >= len(messages)):
                raise SourceContractError("context_span_invalid")
            msg = messages[sl.message_index]
            if (sl.node_id != msg.node_id or sl.message_id != msg.message_id or
                    sl.role != msg.role or sl.attribution != _attribution(msg.role) or
                    sl.source_text_sha256 != msg.text_sha256 or
                    type(sl.start_codepoint) is not int or type(sl.end_codepoint) is not int or
                    not 0 <= sl.start_codepoint < sl.end_codepoint <= len(msg.text)):
                raise SourceContractError("context_provenance_mismatch")
            if (sl.message_index, sl.start_codepoint) != (idx, pos):
                raise SourceContractError("context_gap_or_overlap")
            if sl.text != msg.text[sl.start_codepoint:sl.end_codepoint]:
                raise SourceContractError("context_content_mismatch")
            if hashlib.sha256(sl.text.encode("utf-8")).hexdigest() != sl.text_sha256:
                raise SourceContractError("context_content_digest_mismatch")
            subtotal += len(sl.text.encode("utf-8"))
            if sl.end_codepoint == len(msg.text):
                idx += 1
                pos = 0
            else:
                pos = sl.end_codepoint
        next_digest = messages[idx].text_sha256 if idx < len(messages) else None
        if unit.following_source_message_sha256 != next_digest:
            raise SourceContractError("context_adjacent_source_mismatch")
        if type(unit.context_bytes) is not int or subtotal != unit.context_bytes or subtotal > MAX_UNIT_BYTES:
            raise SourceContractError("context_unit_bytes_mismatch")
        consumed += subtotal
    if consumed != page.text_bytes:
        raise SourceContractError("context_page_bytes_mismatch")
    if (page.next_cursor.message_index, page.next_cursor.text_offset) != (idx, pos):
        raise SourceContractError("context_next_cursor_mismatch")
    if page.complete != (idx == len(messages)):
        raise SourceContractError("context_completion_mismatch")
