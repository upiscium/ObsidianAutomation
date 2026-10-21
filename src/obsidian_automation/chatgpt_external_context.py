"""Strict, non-authoritative external ChatGPT Source Context contract.

This is NOT ContextBundle v1 and cannot be submitted to pre_review_job.
Only metadata, exact Source quote coordinates and bounded untrusted
summaries are represented. No Vault path or credential authority.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .chatgpt_export_reader import PrivateSourceReaderError
from .chatgpt_export_review_preview import (
    REVIEW_PREVIEW_VERSION,
    ExternalReviewPreview,
    ReviewPreviewError,
    revalidate_private_review_preview,
)

EXTERNAL_CONTEXT_VERSION = 1
EXTERNAL_CONTEXT_KIND = "chatgpt-external-source"
MAX_CONTEXT_BYTES = 64 * 1024
MAX_CANDIDATES = 16
MAX_SUMMARY_BYTES = 4096
_SHA = re.compile(r"[0-9a-f]{64}\Z")


class ExternalContextError(ValueError):
    """Fixed diagnostics only; never quote private Source or proposed text."""


@dataclass(frozen=True, repr=False)
class ExternalEvidenceReference:
    candidate_id: str
    anchor_sha256: str
    source_message_sha256: str
    excerpt_sha256: str
    role: str
    attribution: str
    proposed_kind: str
    proposed_summary: str
    risks: tuple[str, ...]

    def payload(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "anchor_sha256": self.anchor_sha256,
            "source_message_sha256": self.source_message_sha256,
            "excerpt_sha256": self.excerpt_sha256,
            "role": self.role,
            "attribution": self.attribution,
            "proposed_kind": self.proposed_kind,
            "proposed_summary": self.proposed_summary,
            "risks": list(self.risks),
        }


@dataclass(frozen=True, repr=False)
class ExternalSourceContext:
    version: int
    context_kind: str
    source_scope: str
    source_hmac: str
    source_archive_sha256: str
    source_content_sha256: str
    source_revision_sha256: str
    source_run_sha256: str
    source_preview_sha256: str
    source_cursor: tuple[int, int]
    next_cursor: tuple[int, int]
    evidence: tuple[ExternalEvidenceReference, ...]
    generation_authorized: bool = False
    human_review_approved: bool = False
    vault_mutation_authorized: bool = False

    def to_json_bytes(self) -> bytes:
        return _encode(self.payload())

    def payload(self) -> dict[str, object]:
        return {
            "record_version": self.version,
            "context_kind": self.context_kind,
            "source_scope": self.source_scope,
            "source_hmac": self.source_hmac,
            "source_archive_sha256": self.source_archive_sha256,
            "source_content_sha256": self.source_content_sha256,
            "source_revision_sha256": self.source_revision_sha256,
            "source_run_sha256": self.source_run_sha256,
            "source_preview_sha256": self.source_preview_sha256,
            "source_cursor": list(self.source_cursor),
            "next_cursor": list(self.next_cursor),
            "evidence": [item.payload() for item in self.evidence],
            "generation_authorized": self.generation_authorized,
            "human_review_approved": self.human_review_approved,
            "vault_mutation_authorized": self.vault_mutation_authorized,
        }


def _encode(value: object) -> bytes:
    try:
        result = (
            json.dumps(
                value,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (ValueError, TypeError, UnicodeError):
        raise ExternalContextError("invalid_external_context_json") from None
    if len(result) > MAX_CONTEXT_BYTES:
        raise ExternalContextError("external_context_too_large")
    return result


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ExternalContextError("duplicate_external_context_property")
        result[key] = value
    return result


def _decode(data: bytes) -> dict[str, Any]:
    if type(data) is not bytes or len(data) > MAX_CONTEXT_BYTES:
        raise ExternalContextError("invalid_external_context_bytes")
    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_unique,
            parse_constant=lambda _: (_ for _ in ()).throw(
                ExternalContextError("invalid_external_context_number")
            ),
        )
    except ExternalContextError:
        raise
    except (UnicodeError, ValueError, TypeError):
        raise ExternalContextError("invalid_external_context_json") from None
    if type(value) is not dict:
        raise ExternalContextError("invalid_external_context_shape")
    return value


def _sha(value: object, code: str) -> str:
    if type(value) is not str or _SHA.fullmatch(value) is None:
        raise ExternalContextError(code)
    return value


def _cursor(value: object) -> tuple[int, int]:
    if (
        type(value) is not list
        or len(value) != 2
        or any(type(n) is not int or n < 0 or n > 1_000_000 for n in value)
    ):
        raise ExternalContextError("invalid_external_context_cursor")
    return (value[0], value[1])


def _str(value: object, code: str, bound: int) -> str:
    if type(value) is not str or not value.strip() or "\x00" in value:
        raise ExternalContextError(code)
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        raise ExternalContextError(code) from None
    if size > bound:
        raise ExternalContextError(code)
    return value


def parse_external_context(data: bytes) -> ExternalSourceContext:
    value = _decode(data)
    expected = {
        "record_version",
        "context_kind",
        "source_scope",
        "source_hmac",
        "source_archive_sha256",
        "source_content_sha256",
        "source_revision_sha256",
        "source_run_sha256",
        "source_preview_sha256",
        "source_cursor",
        "next_cursor",
        "evidence",
        "generation_authorized",
        "human_review_approved",
        "vault_mutation_authorized",
    }
    if set(value) != expected:
        raise ExternalContextError("external_context_properties_invalid")
    if type(value["record_version"]) is not int or value["record_version"] != 1:
        raise ExternalContextError("external_context_version_invalid")
    if value["context_kind"] != EXTERNAL_CONTEXT_KIND:
        raise ExternalContextError("external_context_kind_invalid")
    if any(
        value[key] is not False
        for key in (
            "generation_authorized",
            "human_review_approved",
            "vault_mutation_authorized",
        )
    ):
        raise ExternalContextError("external_context_authority_forbidden")
    source_scope = _str(value["source_scope"], "invalid_external_scope", 64)
    sha_keys = (
        "source_hmac",
        "source_archive_sha256",
        "source_content_sha256",
        "source_revision_sha256",
        "source_run_sha256",
        "source_preview_sha256",
    )
    sha_values = [_sha(value[key], "invalid_external_source_sha") for key in sha_keys]
    evidence = value["evidence"]
    if type(evidence) is not list or len(evidence) > MAX_CANDIDATES:
        raise ExternalContextError("external_evidence_limit")
    seen = set()
    parsed = []
    required_evidence = {
        "candidate_id",
        "anchor_sha256",
        "source_message_sha256",
        "excerpt_sha256",
        "role",
        "attribution",
        "proposed_kind",
        "proposed_summary",
        "risks",
    }
    for entry in evidence:
        if type(entry) is not dict or set(entry) != required_evidence:
            raise ExternalContextError("external_evidence_shape_invalid")
        candidate_id = _sha(entry["candidate_id"], "invalid_candidate_id")
        if candidate_id in seen:
            raise ExternalContextError("duplicate_external_candidate")
        seen.add(candidate_id)
        risk_values = entry["risks"]
        if type(risk_values) is not list or not risk_values or len(risk_values) > 12:
            raise ExternalContextError("invalid_external_risks")
        risks = tuple(_str(r, "invalid_external_risk", 80) for r in risk_values)
        if len(set(risks)) != len(risks):
            raise ExternalContextError("duplicate_external_risk")
        parsed.append(
            ExternalEvidenceReference(
                candidate_id=candidate_id,
                anchor_sha256=_sha(entry["anchor_sha256"], "invalid_anchor_sha"),
                source_message_sha256=_sha(
                    entry["source_message_sha256"],
                    "invalid_message_sha",
                ),
                excerpt_sha256=_sha(entry["excerpt_sha256"], "invalid_excerpt_sha"),
                role=_str(entry["role"], "invalid_evidence_role", 32),
                attribution=_str(
                    entry["attribution"],
                    "invalid_evidence_attribution",
                    64,
                ),
                proposed_kind=_str(entry["proposed_kind"], "invalid_evidence_kind", 64),
                proposed_summary=_str(
                    entry["proposed_summary"],
                    "invalid_evidence_summary",
                    MAX_SUMMARY_BYTES,
                ),
                risks=risks,
            )
        )
    return ExternalSourceContext(
        version=1,
        context_kind=EXTERNAL_CONTEXT_KIND,
        source_scope=source_scope,
        source_hmac=sha_values[0],
        source_archive_sha256=sha_values[1],
        source_content_sha256=sha_values[2],
        source_revision_sha256=sha_values[3],
        source_run_sha256=sha_values[4],
        source_preview_sha256=sha_values[5],
        source_cursor=_cursor(value["source_cursor"]),
        next_cursor=_cursor(value["next_cursor"]),
        evidence=tuple(parsed),
    )


def build_external_context(preview: ExternalReviewPreview) -> ExternalSourceContext:
    """Build private, non-authoritative Context from an already validated preview.

    Not a durable or authenticated artifact; revalidate preview at its private
    Reader before a future guarded transfer/commit.
    """
    if type(preview) is not ExternalReviewPreview:
        raise ExternalContextError("invalid_review_preview")
    if (
        preview.version != REVIEW_PREVIEW_VERSION
        or preview.job_status != "not_submitted"
        or preview.awaiting_human_review is not False
        or preview.generation_authorized is not False
        or preview.canonical_mutation_authorized is not False
        or preview.semantic_evaluation_completed is not False
    ):
        raise ExternalContextError("review_preview_authority_forbidden")
    risks = {item.candidate_id: item for item in preview.risks}
    if len(risks) != len(preview.risks):
        raise ExternalContextError("duplicate_review_risk")
    result = ExternalSourceContext(
        version=1,
        context_kind=EXTERNAL_CONTEXT_KIND,
        source_scope=preview.scope,
        source_hmac=preview.source_hmac,
        source_archive_sha256=preview.archive_sha256,
        source_content_sha256=preview.content_sha256,
        source_revision_sha256=preview.revision_sha256,
        source_run_sha256=preview.source_run_id,
        source_preview_sha256=preview.preview_digest,
        source_cursor=(
            preview.start_cursor.message_index,
            preview.start_cursor.text_offset,
        ),
        next_cursor=(
            preview.next_cursor.message_index,
            preview.next_cursor.text_offset,
        ),
        evidence=tuple(
            ExternalEvidenceReference(
                candidate_id=c.candidate_id,
                anchor_sha256=c.evidence_anchor_sha256,
                source_message_sha256=c.source_message_sha256,
                excerpt_sha256=c.excerpt_sha256,
                role=c.role,
                attribution=c.attribution,
                proposed_kind=c.proposed_kind,
                proposed_summary=c.proposed_summary,
                risks=risks[c.candidate_id].signals,
            )
            for c in preview.candidate_batch.candidates
        ),
    )
    normalized = parse_external_context(result.to_json_bytes())
    if normalized != result:
        raise ExternalContextError("external_context_roundtrip_mismatch")
    return normalized


def revalidate_external_context(
    root: Path,
    preview: ExternalReviewPreview,
    context: ExternalSourceContext,
) -> None:
    """Read Source again and reject any stale/forged candidate/context."""
    if type(context) is not ExternalSourceContext:
        raise ExternalContextError("invalid_external_context")
    try:
        revalidate_private_review_preview(root, preview)
    except (ReviewPreviewError, PrivateSourceReaderError):
        raise ExternalContextError("external_source_stale_or_forged") from None
    expected = build_external_context(preview)
    if context != expected:
        raise ExternalContextError("external_context_provenance_mismatch")
