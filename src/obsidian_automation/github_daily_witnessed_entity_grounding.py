"""C2a: private singleton Grounding bound to the complete v3 source chain.

No inference or filesystem writes. All admission reconstructs Collector CAS;
caller objects and model verdicts never grant publication or source authority.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from . import github_daily_witnessed_entity_context as c1a
from . import github_daily_witnessed_entity_candidate as c1b
from .artifact_lifecycle import ArtifactLifecycleError, _decode_json_object

RECORD_VERSION = 3
CONTEXT_STAGE = "github_daily_witnessed_singleton_ground_context_v3"
RESULT_STAGE = "github_daily_witnessed_singleton_ground_result_v3"
MAX_GROUND_CONTEXT_BYTES = 256 * 1024
MAX_GROUND_REPLY_BYTES = 8 * 1024
MAX_REASON_CHARS = 2048
MAX_REASON_BYTES = 8 * 1024


class DailyWitnessedGroundingError(ValueError):
    """Grounding source, candidate or response is not admissible."""


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical(value: dict[str, object]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


@dataclass(frozen=True)
class WitnessedGroundContext:
    """Owned immutable serialization; its self-hash is not a capability."""

    canonical_bytes: bytes

    def to_json_bytes(self) -> bytes:
        if type(self.canonical_bytes) is not bytes or not (
            0 < len(self.canonical_bytes) <= MAX_GROUND_CONTEXT_BYTES
        ):
            raise DailyWitnessedGroundingError("invalid Grounding input envelope")
        return self.canonical_bytes

    @property
    def sha256(self) -> str:
        return _sha(self.to_json_bytes())


@dataclass(frozen=True)
class WitnessedGroundAssessment:
    """Private advisory result. Consumers must revalidate, never trust a class."""

    canonical_bytes: bytes

    def to_json_bytes(self) -> bytes:
        return self.canonical_bytes

    def report(self) -> dict[str, object]:
        return json.loads(self.canonical_bytes)

    @property
    def sha256(self) -> str:
        return _sha(self.canonical_bytes)


def witnessed_grounding_output_schema() -> dict[str, object]:
    return {
        "type": "object", "additionalProperties": False,
        "required": ["verdict", "reason"],
        "properties": {
            "verdict": {"enum": ["supported", "unsupported"]},
            "reason": {"type": "string", "minLength": 1,
                       "maxLength": MAX_REASON_CHARS},
        },
    }


def build_witnessed_grounding_contexts(
    partial_context: c1a.WitnessedEntityContext,
    partial_response_bytes: bytes,
    *, capture_sha256: str,
) -> tuple[WitnessedGroundContext, ...]:
    """Re-normalize C1b, independently rebuild source and freeze one claim/input.

    The caller's event containers are never consumed after C1b admission.
    An empty tuple means there are no candidates, not that the day is inactive.
    C1b's uncited IDs still require a separate complete-coverage review.
    """
    try:
        if type(partial_context) is not c1a.WitnessedEntityContext:
            raise ValueError("invalid partial Context type")
        budget = partial_context.max_context_bytes
        if type(budget) is not int or not 1024 <= budget <= c1a.MAX_CONTEXT_BYTES:
            raise ValueError("invalid partial Context budget")
        batch = c1b.normalize_witnessed_entity_candidates(
            partial_context, partial_response_bytes, capture_sha256=capture_sha256,
        )
        # This independent reconstruction supplies originals/proofs. Do not
        # resolve IDs against partial_context.events or its caller-owned key.
        rebuilt = c1a.build_witnessed_entity_contexts(capture_sha256, max_bytes=budget)
        matches = [item for item in rebuilt.contexts if item.sha256 == batch.context_sha256]
        if len(matches) != 1:
            raise ValueError("Grounding partial Context is missing or changed")
        trusted = matches[0]
        if (
            trusted.capture_sha256 != capture_sha256
            or batch.capture_sha256 != capture_sha256
            or trusted.route_receipt_sha256 != batch.route_receipt_sha256
            or trusted.evidence_sha256 != batch.evidence_sha256
            or trusted.closure_sha256 != batch.closure_sha256
            or (trusted.key.repository, trusted.key.entity_type, trusted.key.number)
            != (batch.repository, batch.entity_type, batch.number)
        ):
            raise ValueError("Grounding source identity changed")
        originals = {part.original["evidence_id"]: part for part in trusted.events}
        result = []
        for candidate in batch.candidates:
            if any(eid not in originals for eid in candidate.evidence_ids):
                raise ValueError("candidate references another original event")
            payload = {
                "record_version": RECORD_VERSION, "stage": CONTEXT_STAGE,
                "capture_sha256": capture_sha256,
                "route_receipt_sha256": batch.route_receipt_sha256,
                "evidence_sha256": batch.evidence_sha256,
                "closure_sha256": batch.closure_sha256,
                "partial_context_sha256": batch.context_sha256,
                "partial_response_sha256": batch.response_sha256,
                "candidate_sha256": candidate.candidate_sha256,
                "date": trusted.date,
                "captured_at_utc": trusted.captured_at_utc,
                "collection_mode": trusted.collection_mode,
                "entity": {"repository": batch.repository,
                           "entity_type": batch.entity_type, "number": batch.number},
                "summary": candidate.summary,
                "evidence_ids": list(candidate.evidence_ids),
                "source_events": [originals[eid].to_json() for eid in candidate.evidence_ids],
                "model_verdict_is_advisory": True,
                "requires_human_quality_review": True,
                "publication_authorized": False,
            }
            # Only owned bytes leave this API. The raw event/proof dicts used
            # to serialize them are from the private CAS reconstruction.
            item = WitnessedGroundContext(_canonical(payload))
            item.to_json_bytes()
            result.append(item)
        return tuple(result)
    except DailyWitnessedGroundingError:
        raise
    except (c1a.DailyWitnessedContextError, ArtifactLifecycleError,
            ValueError, TypeError, AttributeError, KeyError, IndexError,
            OverflowError, RecursionError):
        raise DailyWitnessedGroundingError("trusted Grounding source unavailable") from None


def normalize_witnessed_grounding_verdict(
    ground_context: WitnessedGroundContext,
    model_response_bytes: bytes,
    *, partial_context: c1a.WitnessedEntityContext,
    partial_response_bytes: bytes,
    capture_sha256: str,
) -> WitnessedGroundAssessment:
    """Rebuild exact singleton before accepting an advisory model verdict.

    No caller-provided source/proof/key data is consumed after reconstruction.
    The model's explanation is hashed, never copied to the normalized result.
    """
    if type(ground_context) is not WitnessedGroundContext:
        raise DailyWitnessedGroundingError("invalid singleton Grounding type")
    supplied = ground_context.to_json_bytes()
    regenerated = build_witnessed_grounding_contexts(
        partial_context, partial_response_bytes, capture_sha256=capture_sha256,
    )
    matches = [item for item in regenerated if item.canonical_bytes == supplied]
    if len(matches) != 1:
        raise DailyWitnessedGroundingError("singleton not bound to source and candidate")
    # Select only the freshly constructed owned serialization. Caller-owned
    # objects are irrelevant even if changed during subsequent JSON parsing.
    verified_bytes = matches[0].canonical_bytes
    source = _decode_json_object(verified_bytes, label="owned Grounding context")
    if type(model_response_bytes) is not bytes or not (
        0 < len(model_response_bytes) <= MAX_GROUND_REPLY_BYTES
    ):
        raise DailyWitnessedGroundingError("Grounding reply exceeds byte bound")
    try:
        reply = _decode_json_object(model_response_bytes, label="Grounding reply")
        if set(reply) != {"verdict", "reason"}:
            raise ValueError("unexpected Grounding reply fields")
        if type(reply["verdict"]) is not str or reply["verdict"] not in (
            "supported", "unsupported",
        ):
            raise ValueError("invalid Grounding verdict")
        reason = reply["reason"]
        if type(reason) is not str:
            raise ValueError("invalid Grounding reason type")
        reason_bytes = reason.encode("utf-8")
        if (not reason or reason != reason.strip() or not reason.isprintable()
                or len(reason.splitlines()) != 1 or len(reason) > MAX_REASON_CHARS
                or len(reason_bytes) > MAX_REASON_BYTES):
            raise ValueError("invalid Grounding reason")
    except (ArtifactLifecycleError, ValueError, TypeError, RecursionError):
        raise DailyWitnessedGroundingError("Grounding model reply rejected") from None
    report = {key: source[key] for key in (
        "capture_sha256", "route_receipt_sha256", "evidence_sha256", "closure_sha256",
        "partial_context_sha256", "partial_response_sha256", "candidate_sha256",
        "date", "captured_at_utc", "collection_mode", "entity", "evidence_ids",
    )}
    report.update({
        "record_version": RECORD_VERSION, "stage": RESULT_STAGE,
        "ground_context_sha256": _sha(verified_bytes),
        "raw_response_sha256": _sha(model_response_bytes),
        "verdict": reply["verdict"], "reason_sha256": _sha(reason_bytes),
        "model_verdict_is_advisory": True,
        "requires_human_quality_review": True, "publication_authorized": False,
    })
    return WitnessedGroundAssessment(_canonical(report))
