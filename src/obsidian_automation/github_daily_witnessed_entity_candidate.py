"""C1b: Source-witnessed Issue/PR model-candidate schema and normalization.

Every admission opens #348's exact Collector-owned CAS-derived v3 Context
and reconstructs its *entire* deterministic original Evidence partition.
No raw model text/refs may decide an Issue/PR identity, origin, status or
publication authority. No inference/network/Writer/Timer effect occurs here.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .artifact_lifecycle import ArtifactLifecycleError, _decode_json_object
from . import github_daily_witnessed_entity_context as c1a
from . import github_daily_entity_context as prior


class DailyWitnessedCandidateError(c1a.DailyWitnessedContextError):
    """Untrusted model candidate response is not source-bound or admissible."""


MAX_MODEL_REPLY_BYTES = prior.MAX_MODEL_OUTPUT_BYTES
MAX_CANDIDATES = prior.MAX_CANDIDATES
MAX_REFS_PER_CANDIDATE = prior.MAX_REFS_PER_CANDIDATE
MAX_SUMMARY_CHARS = prior.MAX_SUMMARY_CHARS
MAX_SUMMARY_BYTES = prior.MAX_SUMMARY_BYTES
MAX_PRIVATE_REPORT_BYTES = 256 * 1024


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class WitnessedCandidate:
    summary: str
    evidence_ids: tuple[str, ...]
    candidate_sha256: str


@dataclass(frozen=True)
class WitnessedCandidateBatch:
    capture_sha256: str
    route_receipt_sha256: str
    evidence_sha256: str
    closure_sha256: str
    context_sha256: str
    response_sha256: str
    repository: str
    entity_type: str
    number: int
    candidates: tuple[WitnessedCandidate, ...]
    uncited_evidence_ids: tuple[str, ...]
    publication_authorized: bool = False
    requires_human_quality_review: bool = True

    def report(self) -> dict[str, object]:
        return {
            "record_version": c1a.RECORD_VERSION,
            "stage": c1a.CANDIDATE_STAGE,
            "capture_sha256": self.capture_sha256,
            "route_receipt_sha256": self.route_receipt_sha256,
            "evidence_sha256": self.evidence_sha256,
            "closure_sha256": self.closure_sha256,
            "context_sha256": self.context_sha256,
            "response_sha256": self.response_sha256,
            "entity": {
                "repository": self.repository,
                "entity_type": self.entity_type,
                "number": self.number,
            },
            "candidates": [
                {
                    "summary": x.summary,  # Private, not a published Daily.
                    "evidence_ids": list(x.evidence_ids),
                    "candidate_sha256": x.candidate_sha256,
                } for x in self.candidates
            ],
            "uncited_evidence_ids": list(self.uncited_evidence_ids),
            "grounding_required": True,
            "semantic_coverage_review_required": True,
            "requires_human_quality_review": True,
            "publication_authorized": False,
        }

    def to_json_bytes(self) -> bytes:
        raw = prior._canonical_bytes(self.report())
        if len(raw) > MAX_PRIVATE_REPORT_BYTES:
            raise DailyWitnessedCandidateError("normalized candidate report too large")
        return raw


@dataclass(frozen=True)
class _VerifiedCandidateInput:
    """Owned scalar snapshot; never retain caller-controlled event containers."""

    capture_sha256: str
    route_receipt_sha256: str
    evidence_sha256: str
    closure_sha256: str
    context_sha256: str
    repository: str
    entity_type: str
    number: int
    evidence_ids: tuple[str, ...]


def _verify(
    context: c1a.WitnessedEntityContext, *, capture_sha256: str,
) -> _VerifiedCandidateInput:
    # Verifying a caller object does not make its later indexing safe. Keep
    # only scalar selection parameters, reopen CAS, and consume rebuilt data.
    try:
        if (
            type(context) is not c1a.WitnessedEntityContext
            or type(capture_sha256) is not str
        ):
            raise ValueError("invalid Context envelope")
        batch_index = context.batch_index
        batch_count = context.batch_count
        max_bytes = context.max_context_bytes
        if (
            type(batch_index) is not int or type(batch_count) is not int
            or not 0 <= batch_index < batch_count <= prior.MAX_CONTEXT_BATCHES
            or type(max_bytes) is not int
            or not 1024 <= max_bytes <= c1a.MAX_CONTEXT_BYTES
        ):
            raise ValueError("invalid Context selection")
        verified_sha = c1a.verify_witnessed_entity_context(
            context, capture_sha256=capture_sha256,
        )
        # The parent verifier returns a hash, not owned source data. A second
        # trusted reconstruction supplies the data actually used below; no
        # caller Context/event/key is accessed after this verification point.
        rebuilt = c1a.build_witnessed_entity_contexts(
            capture_sha256, max_bytes=max_bytes,
        )
        if len(rebuilt.contexts) != batch_count:
            raise ValueError("verified partition changed")
        trusted = rebuilt.contexts[batch_index]
        if trusted.capture_sha256 != capture_sha256 or trusted.sha256 != verified_sha:
            raise ValueError("verified Context changed")
        return _VerifiedCandidateInput(
            capture_sha256=trusted.capture_sha256,
            route_receipt_sha256=trusted.route_receipt_sha256,
            evidence_sha256=trusted.evidence_sha256,
            closure_sha256=trusted.closure_sha256,
            context_sha256=verified_sha,
            repository=trusted.key.repository,
            entity_type=trusted.key.entity_type,
            number=trusted.key.number,
            evidence_ids=tuple(part.original["evidence_id"] for part in trusted.events),
        )
    except (
        c1a.DailyWitnessedContextError, ValueError, TypeError,
        AttributeError, KeyError, IndexError, OverflowError, RecursionError,
    ):
        raise DailyWitnessedCandidateError(
            "witnessed Context cannot be verified against Collector CAS"
        ) from None


def witnessed_model_candidate_schema(
    context: c1a.WitnessedEntityContext, *, capture_sha256: str,
) -> dict[str, object]:
    """Restrict the model to private source refs within exactly one v3 entity."""
    source = _verify(context, capture_sha256=capture_sha256)
    count = len(source.evidence_ids)
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["candidates"],
        "properties": {
            "candidates": {
                "type": "array",
                "maxItems": MAX_CANDIDATES,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["summary", "source_refs"],
                    "properties": {
                        "summary": {
                            "type": "string", "minLength": 1,
                            "maxLength": MAX_SUMMARY_CHARS,
                        },
                        "source_refs": {
                            "type": "array", "minItems": 1,
                            "maxItems": min(MAX_REFS_PER_CANDIDATE, count),
                            "uniqueItems": True,
                            "items": {
                                "type": "integer",
                                "enum": list(range(count)),
                            },
                        },
                    },
                },
            },
        },
    }


def normalize_witnessed_entity_candidates(
    context: c1a.WitnessedEntityContext,
    model_response_bytes: bytes,
    *,
    capture_sha256: str,
) -> WitnessedCandidateBatch:
    """Bind model summaries to reverified C1 Context and original Evidence.

    Empty candidates do not attest an empty day. C2 must re-open CAS again
    before any singleton Grounding; this v3 batch must not be passed to the
    old Evidence-only #331 Grounding API as if it were its v2 candidate.
    """
    source = _verify(context, capture_sha256=capture_sha256)
    context_sha = source.context_sha256
    if type(model_response_bytes) is not bytes or not (
        0 < len(model_response_bytes) <= MAX_MODEL_REPLY_BYTES
    ):
        raise DailyWitnessedCandidateError("model output bytes exceed limit")
    try:
        raw = _decode_json_object(
            model_response_bytes, label="witnessed entity candidate reply",
        )
    except (ArtifactLifecycleError, ValueError, TypeError, RecursionError):
        raise DailyWitnessedCandidateError("model candidate JSON rejected") from None
    if set(raw) != {"candidates"} or type(raw["candidates"]) is not list:
        raise DailyWitnessedCandidateError("model candidate properties not allowed")
    if len(raw["candidates"]) > MAX_CANDIDATES:
        raise DailyWitnessedCandidateError("too many entity candidate claims")
    result: list[WitnessedCandidate] = []
    seen_candidate: set[str] = set()
    cited: set[str] = set()
    for candidate in raw["candidates"]:
        if type(candidate) is not dict or set(candidate) != {"summary", "source_refs"}:
            raise DailyWitnessedCandidateError("model candidate has invalid keys")
        summary = candidate["summary"]
        if type(summary) is not str:
            raise DailyWitnessedCandidateError("model summary must be one string")
        try:
            summary_bytes = summary.encode("utf-8")
        except UnicodeError:
            raise DailyWitnessedCandidateError("model summary UTF-8 invalid") from None
        if (
            not summary or summary != summary.strip()
            or len(summary) > MAX_SUMMARY_CHARS
            or len(summary_bytes) > MAX_SUMMARY_BYTES
            or not summary.isprintable() or len(summary.splitlines()) != 1
        ):
            raise DailyWitnessedCandidateError("model summary is not one valid line")
        refs = candidate["source_refs"]
        if (
            type(refs) is not list
            or not 1 <= len(refs) <= min(MAX_REFS_PER_CANDIDATE, len(source.evidence_ids))
            or any(type(i) is not int for i in refs)
            or len(set(refs)) != len(refs)
            or any(i < 0 or i >= len(source.evidence_ids) for i in refs)
        ):
            raise DailyWitnessedCandidateError("model source refs are unbound")
        ids = tuple(source.evidence_ids[i] for i in refs)
        cited.update(ids)
        candidate_data = prior._canonical_bytes({
            "record_version": c1a.RECORD_VERSION,
            "capture_sha256": capture_sha256,
            "route_receipt_sha256": source.route_receipt_sha256,
            "evidence_sha256": source.evidence_sha256,
            "closure_sha256": source.closure_sha256,
            "context_sha256": context_sha,
            "entity": {
                "repository": source.repository,
                "entity_type": source.entity_type,
                "number": source.number,
            },
            "summary": summary,
            "evidence_ids": list(ids),
        })
        candidate_sha = _sha(candidate_data)
        if candidate_sha in seen_candidate:
            raise DailyWitnessedCandidateError("model duplicate candidate")
        seen_candidate.add(candidate_sha)
        result.append(WitnessedCandidate(summary, ids, candidate_sha))
    normalized = WitnessedCandidateBatch(
        capture_sha256=capture_sha256,
        route_receipt_sha256=source.route_receipt_sha256,
        evidence_sha256=source.evidence_sha256,
        closure_sha256=source.closure_sha256,
        context_sha256=context_sha,
        response_sha256=_sha(model_response_bytes),
        repository=source.repository,
        entity_type=source.entity_type,
        number=source.number,
        candidates=tuple(result),
        uncited_evidence_ids=tuple(
            eid for eid in source.evidence_ids if eid not in cited
        ),
    )
    normalized.to_json_bytes()  # exact bounded private output, no effect
    return normalized
