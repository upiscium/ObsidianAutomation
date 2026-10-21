"""Versioned bounded External Source Context for an opt-in pre-review Job.

This is *not* the ordinary Vault ContextBundle. Only one exact, quote-bound,
unverified ChatGPT Export candidate is supplied to a downstream generator.
The private Reader must revalidate the Source before Reader-owned storage.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from .artifact_lifecycle import (
    ArtifactLifecycleError,
    _canonical_json_bytes,
    _decode_json_object,
    _read_exact_file,
    _require_safe_directory,
    _require_sha256,
    _store_immutable,
    sha256_bytes,
)
from .chatgpt_export_review_preview import (
    ExternalReviewPreview,
    ReviewPreviewError,
    revalidate_private_review_preview,
)

SOURCE_CONTEXT_VERSION = 1
SOURCE_CONTEXT_KIND = "chatgpt-export"
SOURCE_CONTEXT_TRUST = "untrusted-evidence"
SOURCE_CONTEXT_SUFFIX = "external-chatgpt-context"
SOURCE_CONTEXT_STAGE = "05-Context"
MAX_CONTEXT_BYTES = 24 * 1024
MAX_QUOTE_BYTES = 4096
MAX_SUMMARY_BYTES = 4096
MAX_RISKS = 16
_MAX_KIND = frozenset(
    {
        "unclassified",
        "question_candidate",
        "hypothesis_candidate",
        "correction_candidate",
        "decision_candidate",
        "result_candidate",
        "feature_change_candidate",
    }
)
_MAX_ROLE = frozenset(("user", "assistant"))
_SHA = re.compile(r"^[0-9a-f]{64}$")
_SCOPE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class ExternalContextError(ArtifactLifecycleError):
    """Fixed diagnostic errors; untrusted message text never interpolated."""


@dataclass(frozen=True, repr=False)
class ExternalSourceContext:
    scope: str
    source_hmac: str
    archive_sha256: str
    source_run_id: str
    content_sha256: str
    revision_sha256: str
    preview_digest: str
    candidate_id: str
    anchor_sha256: str
    source_message_sha256: str
    message_index: int
    start_codepoint: int
    end_codepoint: int
    role: str
    attribution: str
    proposed_kind: str
    excerpt: str
    excerpt_sha256: str
    proposed_summary: str
    risks: tuple[str, ...]
    generation_authorized: bool = False

    def to_json_bytes(self) -> bytes:
        payload = {
            "record_version": SOURCE_CONTEXT_VERSION,
            "context_kind": SOURCE_CONTEXT_KIND,
            "trust_level": SOURCE_CONTEXT_TRUST,
            "scope": self.scope,
            "source_hmac": self.source_hmac,
            "archive_sha256": self.archive_sha256,
            "source_run_id": self.source_run_id,
            "content_sha256": self.content_sha256,
            "revision_sha256": self.revision_sha256,
            "preview_digest": self.preview_digest,
            "candidate_id": self.candidate_id,
            "anchor_sha256": self.anchor_sha256,
            "source_message_sha256": self.source_message_sha256,
            "message_index": self.message_index,
            "start_codepoint": self.start_codepoint,
            "end_codepoint": self.end_codepoint,
            "role": self.role,
            "attribution": self.attribution,
            "proposed_kind": self.proposed_kind,
            "excerpt": self.excerpt,
            "excerpt_sha256": self.excerpt_sha256,
            "proposed_summary": self.proposed_summary,
            "risks": list(self.risks),
            "generation_authorized": self.generation_authorized,
        }
        try:
            data = _canonical_json_bytes(payload)
        except (ValueError, TypeError, UnicodeError):
            raise ExternalContextError("invalid_external_context") from None
        if len(data) > MAX_CONTEXT_BYTES:
            raise ExternalContextError("external_context_size_limit")
        return data


_KEYS = frozenset(
    {
        "record_version",
        "context_kind",
        "trust_level",
        "scope",
        "source_hmac",
        "archive_sha256",
        "source_run_id",
        "content_sha256",
        "revision_sha256",
        "preview_digest",
        "candidate_id",
        "anchor_sha256",
        "source_message_sha256",
        "message_index",
        "start_codepoint",
        "end_codepoint",
        "role",
        "attribution",
        "proposed_kind",
        "excerpt",
        "excerpt_sha256",
        "proposed_summary",
        "risks",
        "generation_authorized",
    }
)


def _hex(value: object) -> str:
    if type(value) is not str or _SHA.fullmatch(value) is None:
        raise ExternalContextError("external_context_hash_invalid")
    return value


def _bounded(value: object, limit: int) -> str:
    if type(value) is not str or not value.strip() or "\x00" in value:
        raise ExternalContextError("external_context_text_invalid")
    try:
        count = len(value.encode("utf-8"))
    except UnicodeError:
        raise ExternalContextError("external_context_text_invalid") from None
    if count > limit:
        raise ExternalContextError("external_context_text_limit")
    return value


def parse_external_context(data: bytes) -> ExternalSourceContext:
    if type(data) is not bytes or len(data) > MAX_CONTEXT_BYTES:
        raise ExternalContextError("external_context_size_limit")
    try:
        value = _decode_json_object(data, label="external Source Context")
    except ArtifactLifecycleError:
        raise ExternalContextError("external_context_json_invalid") from None
    if (
        set(value) != _KEYS
        or type(value["record_version"]) is not int
        or (
            value["record_version"] != SOURCE_CONTEXT_VERSION
            or value["context_kind"] != SOURCE_CONTEXT_KIND
            or value["trust_level"] != SOURCE_CONTEXT_TRUST
            or type(value["generation_authorized"]) is not bool
            or value["generation_authorized"]
        )
    ):
        raise ExternalContextError("external_context_contract_invalid")
    scope = value["scope"]
    if type(scope) is not str or _SCOPE.fullmatch(scope) is None:
        raise ExternalContextError("external_context_scope_invalid")
    for key in (
        "source_hmac",
        "archive_sha256",
        "source_run_id",
        "content_sha256",
        "revision_sha256",
        "preview_digest",
        "candidate_id",
        "anchor_sha256",
        "source_message_sha256",
        "excerpt_sha256",
    ):
        _hex(value[key])
    if (
        type(value["message_index"]) is not int
        or type(value["start_codepoint"]) is not int
        or type(value["end_codepoint"]) is not int
        or not 0 <= value["message_index"] < 500_000
        or not 0 <= value["start_codepoint"] < value["end_codepoint"] < 10_000_000
    ):
        raise ExternalContextError("external_context_coordinates_invalid")
    role = value["role"]
    if type(role) is not str or role not in _MAX_ROLE:
        raise ExternalContextError("external_context_role_invalid")
    if value["attribution"] != (
        "user-report-unverified" if role == "user" else "assistant-claim-unverified"
    ):
        raise ExternalContextError("external_context_attribution_invalid")
    kind = value["proposed_kind"]
    if type(kind) is not str or kind not in _MAX_KIND:
        raise ExternalContextError("external_context_candidate_kind_invalid")
    excerpt = _bounded(value["excerpt"], MAX_QUOTE_BYTES)
    summary = _bounded(value["proposed_summary"], MAX_SUMMARY_BYTES)
    if hashlib.sha256(excerpt.encode("utf-8")).hexdigest() != value["excerpt_sha256"]:
        raise ExternalContextError("external_context_quote_digest_invalid")
    if value["end_codepoint"] - value["start_codepoint"] != len(excerpt):
        raise ExternalContextError("external_context_quote_span_invalid")
    risks = value["risks"]
    if (
        type(risks) is not list
        or not 1 <= len(risks) <= MAX_RISKS
        or any(
            type(signal) is not str or _SCOPE.fullmatch(signal) is None
            for signal in risks
        )
        or len(set(risks)) != len(risks)
    ):
        raise ExternalContextError("external_context_risks_invalid")
    return ExternalSourceContext(
        scope=scope,
        source_hmac=value["source_hmac"],
        archive_sha256=value["archive_sha256"],
        source_run_id=value["source_run_id"],
        content_sha256=value["content_sha256"],
        revision_sha256=value["revision_sha256"],
        preview_digest=value["preview_digest"],
        candidate_id=value["candidate_id"],
        anchor_sha256=value["anchor_sha256"],
        source_message_sha256=value["source_message_sha256"],
        message_index=value["message_index"],
        start_codepoint=value["start_codepoint"],
        end_codepoint=value["end_codepoint"],
        role=role,
        attribution=value["attribution"],
        proposed_kind=kind,
        excerpt=excerpt,
        excerpt_sha256=value["excerpt_sha256"],
        proposed_summary=summary,
        risks=tuple(risks),
    )


def external_context_directory(ai_root: Path) -> Path:
    root = ai_root.absolute()
    _require_safe_directory(root, create=False)
    context = root / SOURCE_CONTEXT_STAGE
    _require_safe_directory(context, create=False)
    return context


def load_external_context(ai_root: Path, context_sha256: str) -> ExternalSourceContext:
    digest = _require_sha256(context_sha256, label="external_context_sha256")
    path = (
        external_context_directory(ai_root) / f"{digest}.{SOURCE_CONTEXT_SUFFIX}.json"
    )
    data = _read_exact_file(path)
    if sha256_bytes(data) != digest:
        raise ExternalContextError("external_context_digest_invalid")
    return parse_external_context(data)


def store_external_context(
    ai_root: Path,
    *,
    private_root: Path,
    preview: ExternalReviewPreview,
    candidate_index: int,
) -> tuple[str, Path]:
    """Explicit private Reader admission. Never auto-submit a Job.

    This necessarily copies one bounded quote/interpretation to the
    Reader/Generator-visible 05-Context stage: DO NOT invoke on real user
    data without dedicated identity, ACL and separate Human authorization.
    """
    if type(preview) is not ExternalReviewPreview or type(candidate_index) is not int:
        raise ExternalContextError("external_context_admission_invalid")
    if preview.status != "review_required" or (
        candidate_index < 0
        or candidate_index >= len(preview.candidate_batch.candidates)
    ):
        raise ExternalContextError("external_context_candidate_not_selected")
    try:
        revalidate_private_review_preview(private_root, preview)
    except ReviewPreviewError:
        raise ExternalContextError("external_source_is_stale_or_invalid") from None
    candidate = preview.candidate_batch.candidates[candidate_index]
    evidence = preview.risks[candidate_index]
    if candidate.candidate_id != evidence.candidate_id or (
        candidate.evidence_anchor_sha256 != evidence.anchor_sha256
    ):
        raise ExternalContextError("external_context_evidence_mismatch")
    record = ExternalSourceContext(
        scope=preview.scope,
        source_hmac=preview.source_hmac,
        archive_sha256=preview.archive_sha256,
        source_run_id=preview.source_run_id,
        content_sha256=preview.content_sha256,
        revision_sha256=preview.revision_sha256,
        preview_digest=preview.preview_digest,
        candidate_id=candidate.candidate_id,
        anchor_sha256=candidate.evidence_anchor_sha256,
        source_message_sha256=candidate.source_message_sha256,
        message_index=candidate.message_index,
        start_codepoint=candidate.quote_start_codepoint,
        end_codepoint=candidate.quote_end_codepoint,
        role=candidate.role,
        attribution=candidate.attribution,
        proposed_kind=candidate.proposed_kind,
        excerpt=candidate.excerpt,
        excerpt_sha256=candidate.excerpt_sha256,
        proposed_summary=candidate.proposed_summary,
        risks=evidence.signals,
    )
    data = record.to_json_bytes()
    parsed = parse_external_context(data)
    if parsed != record:
        raise ExternalContextError("external_context_roundtrip_mismatch")
    digest = sha256_bytes(data)
    path = (
        external_context_directory(ai_root) / f"{digest}.{SOURCE_CONTEXT_SUFFIX}.json"
    )
    return digest, _store_immutable(path, data)


def validate_external_job_admission(context: ExternalSourceContext) -> None:
    """Conservative deterministic gate before any model transport or Job.

    Until independent semantic adjudication exists, only clearly provisional
    question/hypothesis/unclassified proposals from complete selected-branch
    evidence can enter the existing pipeline. Exact quotes do not certify
    adopted decisions, corrections, feature status or test results.
    """
    if type(context) is not ExternalSourceContext:
        raise ExternalContextError("external_admission_contract_invalid")
    parsed = parse_external_context(context.to_json_bytes())
    if parsed != context:
        raise ExternalContextError("external_admission_contract_invalid")
    if "source_coverage_incomplete" in context.risks:
        raise ExternalContextError("external_source_coverage_incomplete")
    if context.proposed_kind not in {
        "unclassified",
        "question_candidate",
        "hypothesis_candidate",
    }:
        raise ExternalContextError("external_source_semantic_review_required")
    if "semantic_review_required" not in context.risks:
        raise ExternalContextError("external_source_semantic_review_not_marked")
    if (
        context.role == "assistant"
        and "assistant_not_user_confirmation" not in context.risks
    ):
        raise ExternalContextError("external_source_attribution_risk_missing")
    if (
        context.role == "user"
        and "user_report_not_independent_verification" not in context.risks
    ):
        raise ExternalContextError("external_source_attribution_risk_missing")
