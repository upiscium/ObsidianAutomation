"""Evidence-bound, singleton v2 Issue/PR candidate Grounding (#330).

This is a non-publishing typed normalization primitive. The model may evaluate
the evidence but never chooses entity identity, source IDs, publication status,
a GitHub URL, or a lifecycle result. A model verdict is advisory evidence,
not a human materiality decision or a permit to publish to the Daily Note.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Mapping

from .artifact_lifecycle import ArtifactLifecycleError, _decode_json_object
from .github_daily_entity_context import (
    DailyEntityContextError, EntityPartialContext,
    MAX_CONTEXT_BYTES as MAX_PARTIAL_CONTEXT_BYTES,
    normalize_entity_candidates,
)
from .github_daily_issue_pr import EntityKey


RECORD_VERSION = 2
GROUND_CONTEXT_STAGE = "github_daily_entity_singleton_ground_context_v2"
GROUND_RESULT_STAGE = "github_daily_entity_singleton_ground_result_v2"
MAX_GROUND_CONTEXT_BYTES = 256 * 1024
MAX_GROUND_OUTPUT_BYTES = 8 * 1024
MAX_REASON_CHARS = 2048
MAX_REASON_BYTES = 8 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

# The structured model is deliberately not allowed to return a candidate ID.
ENTITY_GROUND_SYSTEM_PROMPT = """Evaluate exactly one candidate progress claim.
Only the supplied original GitHub events may support the candidate.
Return exactly verdict (supported or unsupported) and a concise one-line
reason. Reject invented Issue/PR outcomes, merge, deployment, decisions,
causal relationships, or unsupported implementation completion. Do not
return entity identifiers, repository names, URLs, evidence IDs, candidate
hashes or permission to publish. A supported response is model-advisory."""


class DailyEntityGroundingError(DailyEntityContextError):
    """A singleton Grounding input or reply is invalid or cannot be rebound."""


def _canonical(value: Mapping[str, object]) -> bytes:
    return (
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _entity(key: EntityKey) -> dict[str, object]:
    return {
        "repository": key.repository,
        "entity_type": key.entity_type,
        "number": key.number,
    }


@dataclass(frozen=True)
class SingletonEntityGroundContext:
    key: EntityKey
    date: str
    evidence_bundle_sha256: str
    partial_context_sha256: str
    partial_model_response_sha256: str
    candidate_sha256: str
    summary: str
    evidence_ids: tuple[str, ...]
    original_events: tuple[Mapping[str, object], ...]

    def to_json_bytes(self) -> bytes:
        if (
            self.key.entity_type not in {"issue", "pull_request"}
            or type(self.key.number) is not int
            or self.key.number < 1
            or not isinstance(self.key.repository, str)
            or not self.key.repository
        ):
            raise DailyEntityGroundingError("grounding entity identity invalid")
        if (
            any(_SHA256.fullmatch(value) is None for value in (
                self.evidence_bundle_sha256, self.partial_context_sha256,
                self.partial_model_response_sha256, self.candidate_sha256,
            ))
            or not 1 <= len(self.evidence_ids) <= 8
            or len(self.evidence_ids) != len(self.original_events)
            or len(set(self.evidence_ids)) != len(self.evidence_ids)
            or any(
                not isinstance(event, Mapping)
                or event.get("evidence_id") != eid
                for eid, event in zip(self.evidence_ids, self.original_events)
            )
        ):
            raise DailyEntityGroundingError("grounding source closure invalid")
        raw = _canonical({
            "record_version": RECORD_VERSION,
            "stage": GROUND_CONTEXT_STAGE,
            "date": self.date,
            "entity": _entity(self.key),
            "evidence_bundle_sha256": self.evidence_bundle_sha256,
            "partial_context_sha256": self.partial_context_sha256,
            "partial_model_response_sha256": self.partial_model_response_sha256,
            "candidate_sha256": self.candidate_sha256,
            "summary": self.summary,
            "evidence_ids": list(self.evidence_ids),
            "original_events": [dict(event) for event in self.original_events],
        })
        if len(raw) > MAX_GROUND_CONTEXT_BYTES:
            raise DailyEntityGroundingError("singleton ground context too large")
        return raw

    @property
    def sha256(self) -> str:
        return _sha(self.to_json_bytes())


@dataclass(frozen=True)
class EntityGroundingAssessment:
    key: EntityKey
    date: str
    evidence_bundle_sha256: str
    ground_context_sha256: str
    candidate_sha256: str
    evidence_ids: tuple[str, ...]
    verdict: str
    reason: str
    raw_response_sha256: str

    def report(self) -> dict[str, object]:
        # This is a private normalized candidate-level result, not a rendered
        # Daily or a model-generated permission to apply a CAS write.
        return {
            "record_version": RECORD_VERSION,
            "stage": GROUND_RESULT_STAGE,
            "date": self.date,
            "entity": _entity(self.key),
            "evidence_bundle_sha256": self.evidence_bundle_sha256,
            "ground_context_sha256": self.ground_context_sha256,
            "candidate_sha256": self.candidate_sha256,
            "evidence_ids": list(self.evidence_ids),
            "verdict": self.verdict,
            "reason": self.reason,
            "raw_response_sha256": self.raw_response_sha256,
            "model_verdict_is_advisory": True,
            "requires_human_quality_review": True,
            "publication_authorized": False,
        }

    def to_json_bytes(self) -> bytes:
        return _canonical(self.report())

    @property
    def sha256(self) -> str:
        return _sha(self.to_json_bytes())


def entity_grounding_output_schema() -> dict[str, object]:
    """No model-selected Issue/PR, sources, claim ID or workflow effects."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["verdict", "reason"],
        "properties": {
            "verdict": {"enum": ["supported", "unsupported"]},
            "reason": {"type": "string", "minLength": 1,
                       "maxLength": MAX_REASON_CHARS},
        },
    }


def build_entity_grounding_contexts(
    context: EntityPartialContext,
    partial_model_bytes: bytes,
    *,
    original_evidence_bytes: bytes,
) -> tuple[SingletonEntityGroundContext, ...]:
    """Revalidate the original v1 Evidence and exact deterministic v2 batch.

    No model calls, files, provider credentials or effects. Every candidate
    receives its own context. Empty candidates cannot certify no progress.
    """
    batch = normalize_entity_candidates(
        context, partial_model_bytes,
        original_evidence_bytes=original_evidence_bytes,
    )
    if len(context.to_json_bytes()) > MAX_PARTIAL_CONTEXT_BYTES:
        raise DailyEntityGroundingError("parent partial context too large")
    source_by_id = {str(event["evidence_id"]): event for event in context.events}
    result: list[SingletonEntityGroundContext] = []
    for candidate in batch.candidates:
        if any(eid not in source_by_id for eid in candidate.evidence_ids):
            raise DailyEntityGroundingError("candidate evidence missing")
        item = SingletonEntityGroundContext(
            key=context.key, date=context.date,
            evidence_bundle_sha256=context.evidence_bundle_sha256,
            partial_context_sha256=context.sha256,
            partial_model_response_sha256=batch.response_sha256,
            candidate_sha256=candidate.candidate_sha256,
            summary=candidate.summary,
            evidence_ids=candidate.evidence_ids,
            original_events=tuple(source_by_id[eid] for eid in candidate.evidence_ids),
        )
        item.to_json_bytes()  # enforce byte bounds before returning any output
        result.append(item)
    return tuple(result)


def normalize_entity_grounding_verdict(
    ground_context: SingletonEntityGroundContext,
    model_response_bytes: bytes,
    *,
    partial_context: EntityPartialContext,
    partial_model_bytes: bytes,
    original_evidence_bytes: bytes,
) -> EntityGroundingAssessment:
    """Re-derive singleton identity and bind one provider verdict to it.

    The caller cannot pass a fabricated context, substitute another candidate
    or source, or make the model pick a different Issue/PR.
    """
    regenerated = build_entity_grounding_contexts(
        partial_context, partial_model_bytes,
        original_evidence_bytes=original_evidence_bytes,
    )
    if sum(expected == ground_context for expected in regenerated) != 1:
        raise DailyEntityGroundingError(
            "grounding input not bound to original source and candidate"
        )
    if (
        type(model_response_bytes) is not bytes
        or not 0 < len(model_response_bytes) <= MAX_GROUND_OUTPUT_BYTES
    ):
        raise DailyEntityGroundingError("grounding model output size invalid")
    try:
        value = _decode_json_object(
            model_response_bytes, label="singleton entity grounding reply",
        )
    except (ArtifactLifecycleError, ValueError, TypeError) as exc:
        raise DailyEntityGroundingError("grounding JSON rejected") from exc
    if set(value) != {"verdict", "reason"}:
        raise DailyEntityGroundingError("model grounding properties invalid")
    if value["verdict"] not in ("supported", "unsupported"):
        raise DailyEntityGroundingError("model grounding verdict invalid")
    reason = value["reason"]
    if not isinstance(reason, str):
        raise DailyEntityGroundingError("model grounding reason invalid")
    try:
        size = len(reason.encode("utf-8"))
    except UnicodeError as exc:
        raise DailyEntityGroundingError("model grounding UTF-8 invalid") from exc
    if (
        not reason or reason != reason.strip()
        or len(reason) > MAX_REASON_CHARS or size > MAX_REASON_BYTES
        or any(ord(char) < 32 or ord(char) == 127 for char in reason)
    ):
        raise DailyEntityGroundingError("model grounding reason invalid")
    output = EntityGroundingAssessment(
        key=ground_context.key,
        date=ground_context.date,
        evidence_bundle_sha256=ground_context.evidence_bundle_sha256,
        ground_context_sha256=ground_context.sha256,
        candidate_sha256=ground_context.candidate_sha256,
        evidence_ids=ground_context.evidence_ids,
        verdict=value["verdict"], reason=reason,
        raw_response_sha256=_sha(model_response_bytes),
    )
    output.to_json_bytes()
    return output
