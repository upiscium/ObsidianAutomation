"""C1: Source-witnessed Issue/PR entity contexts, private and nonpublishing.

This is a separate v3 contract, not an automatic promotion of the old v2
context format. A trusted caller supplies one locally admitted Capture Index
digest. EVERY entry point reopens the Collector-owned immutable CAS through
#341 and reconstructs #342 C0 source routes; the in-memory dataclass/SHAs
returned here do not constitute transferable producer authorization.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Mapping

from .artifact_lifecycle import ArtifactLifecycleError, _decode_json_object
from . import github_daily_source_route as route_source
from . import github_daily_route_cas as route_cas
from . import github_daily_source_closure as closure
from .github_daily_summary import GitHubDailySummaryError, parse_evidence_bundle
from .github_daily_issue_pr import (
    DailyEntityPreviewError, EntityKey, _event_key, _require_event_time,
)
from .github_daily_entity_context import (
    DailyEntityContextError, _date_bounds, _canonical_bytes,
    MAX_EVENTS, MAX_ENTITIES, MAX_CONTEXT_BATCHES, MAX_CONTEXT_EVENTS,
    MAX_CONTEXT_BYTES, MAX_MODEL_OUTPUT_BYTES, MAX_CANDIDATES,
    MAX_REFS_PER_CANDIDATE, MAX_SUMMARY_CHARS, MAX_SUMMARY_BYTES,
)

RECORD_VERSION = 3
CONTEXT_STAGE = "github_daily_entity_witnessed_partial_context_v3"
CANDIDATE_STAGE = "github_daily_entity_witnessed_candidate_v3"
MAX_BUNDLE_BYTES = 32 * 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class DailyWitnessedContextError(DailyEntityContextError):
    """Exact Collector-owned source and candidate/context bindings failed."""


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _assert_sha(value: object) -> None:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise DailyWitnessedContextError("source identity is not a canonical digest")


def _entity(key: EntityKey) -> dict[str, object]:
    return {
        "repository": key.repository, "entity_type": key.entity_type,
        "number": key.number,
    }


@dataclass(frozen=True)
class WitnessedPartialContext:
    capture_sha256: str
    route_receipt_sha256: str
    evidence_bundle_sha256: str
    date: str
    collection_mode: str
    key: EntityKey
    batch_index: int
    batch_count: int
    max_context_bytes: int
    events: tuple[Mapping[str, object], ...]
    # Only the four independently reverified source kinds carry these records.
    route_proofs: tuple[Mapping[str, object], ...]

    def to_json_bytes(self, *, _size_trial: bool = False) -> bytes:
        for digest in (
            self.capture_sha256, self.route_receipt_sha256,
            self.evidence_bundle_sha256,
        ):
            _assert_sha(digest)
        if (
            type(self.batch_index) is not int
            or type(self.batch_count) is not int
            or not 0 <= self.batch_index < self.batch_count <= MAX_CONTEXT_BATCHES
            or type(self.max_context_bytes) is not int
            or not 1024 <= self.max_context_bytes <= MAX_CONTEXT_BYTES
            or not 1 <= len(self.events) <= MAX_CONTEXT_EVENTS
            or type(self.key) is not EntityKey
            or self.key.entity_type not in ("issue", "pull_request")
            or type(self.key.number) is not int or self.key.number <= 0
            or not isinstance(self.key.repository, str)
        ):
            raise DailyWitnessedContextError("witnessed entity context fields invalid")
        if len(self.route_proofs) > len(self.events):
            raise DailyWitnessedContextError("more source proofs than original events")
        event_ids = [item.get("evidence_id") for item in self.events]
        if (
            any(type(eid) is not str or _SHA256.fullmatch(eid) is None
                for eid in event_ids)
            or len(event_ids) != len(set(event_ids))
        ):
            raise DailyWitnessedContextError("witnessed context event identities invalid")
        route_ids = [p.get("evidence_id") for p in self.route_proofs]
        if (any(type(eid) is not str for eid in route_ids)
                or len(set(route_ids)) != len(route_ids)
                or set(route_ids) - set(event_ids)):
            raise DailyWitnessedContextError("witnessed route proofs not bound to batch")
        if not isinstance(self.collection_mode, str) or not self.collection_mode:
            raise DailyWitnessedContextError("capture mode invalid")
        raw = _canonical_bytes({
            "record_version": RECORD_VERSION,
            "stage": CONTEXT_STAGE,
            "capture_sha256": self.capture_sha256,
            "route_receipt_sha256": self.route_receipt_sha256,
            "evidence_bundle_sha256": self.evidence_bundle_sha256,
            "date": self.date,
            "collection_mode": self.collection_mode,
            "entity": _entity(self.key),
            "batch_index": self.batch_index,
            "batch_count": self.batch_count,
            "max_context_bytes": self.max_context_bytes,
            "events": [dict(event) for event in self.events],
            "route_proofs": [dict(proof) for proof in self.route_proofs],
            "requires_human_quality_review": True,
            "publication_authorized": False,
        })
        if not _size_trial and len(raw) > self.max_context_bytes:
            raise DailyWitnessedContextError("witnessed context exceeds byte budget")
        return raw

    @property
    def sha256(self) -> str:
        return _sha(self.to_json_bytes())

    @property
    def evidence_ids(self) -> tuple[str, ...]:
        return tuple(event["evidence_id"] for event in self.events)


@dataclass(frozen=True)
class WitnessedContextSet:
    capture_sha256: str
    route_receipt_sha256: str
    evidence_bundle_sha256: str
    date: str
    collection_mode: str
    contexts: tuple[WitnessedPartialContext, ...]
    grouped_event_count: int
    witnessed_event_count: int
    structural_event_count: int
    unattributed_default_commit_count: int
    publication_authorized: bool = False
    requires_human_quality_review: bool = True

    def report(self) -> dict[str, object]:
        return {
            "record_version": RECORD_VERSION,
            "stage": "github_daily_entity_witnessed_context_report_v3",
            "date": self.date,
            "collection_mode": self.collection_mode,
            "capture_sha256": self.capture_sha256,
            "route_receipt_sha256": self.route_receipt_sha256,
            "evidence_bundle_sha256": self.evidence_bundle_sha256,
            "context_count": len(self.contexts),
            "grouped_event_count": self.grouped_event_count,
            "witnessed_event_count": self.witnessed_event_count,
            "structural_event_count": self.structural_event_count,
            "unattributed_default_commit_count": self.unattributed_default_commit_count,
            "requires_human_quality_review": True,
            "publication_authorized": False,
        }


def _trusted_capture(capture_sha256: str):
    _assert_sha(capture_sha256)
    # Never accept a caller-supplied VerifiedCapture or VerifiedSourceClosure.
    # Reopen trusted CAS at the exact checkpoint digest, twice independently
    # through the pre-existing C0 route and its original Evidence bytes.
    source_closure = closure.read_verified_source_closure(capture_sha256)
    stored = route_cas.read_verified_capture(capture_sha256)
    if (
        type(source_closure) is not closure.VerifiedSourceClosure
        or type(stored) is not route_cas.VerifiedCapture
        or stored.index_sha256 != capture_sha256
        or source_closure.capture_sha256 != capture_sha256
        or source_closure.evidence_sha256 != stored.capture_index.evidence_sha256
        or source_closure.route_receipt_sha256 != stored.capture_index.route_receipt_sha256
        or source_closure.date != stored.capture_index.date
        or source_closure.collection_mode != stored.capture_index.collection_mode
        or stored.daily_publication_authorized is not False
        or stored.collector_storage_attested is not True
    ):
        raise DailyWitnessedContextError("trusted source closure does not match CAS")
    if type(stored.evidence_bytes) is not bytes or not (
        0 < len(stored.evidence_bytes) <= MAX_BUNDLE_BYTES
    ):
        raise DailyWitnessedContextError("original Evidence exceeds safety bounds")
    bundle = parse_evidence_bundle(stored.evidence_bytes)
    if bundle.sha256 != source_closure.evidence_sha256 or bundle.date != source_closure.date:
        raise DailyWitnessedContextError("original Evidence is not the verified capture")
    return source_closure, bundle


def _group_events(bundle, trusted: closure.VerifiedSourceClosure):
    if len(bundle.events) > MAX_EVENTS:
        raise DailyWitnessedContextError("original Evidence event count exceeds limit")
    start, end = _date_bounds(bundle)
    by_id = {p.evidence_id: p for p in trusted.bindings}
    if len(by_id) != len(trusted.bindings):
        raise DailyWitnessedContextError("duplicate API route identity")
    seen: set[str] = set()
    grouped: dict[EntityKey, list[tuple[Mapping[str, object], Mapping[str, object] | None]]] = {}
    default_commits = 0
    structural_count = 0
    witnessed_count = 0
    for event in bundle.events:
        eid = event.get("evidence_id")
        if type(eid) is not str or _SHA256.fullmatch(eid) is None or eid in seen:
            raise DailyWitnessedContextError("original Evidence ID invalid or duplicate")
        seen.add(eid)
        if event.get("repository") not in bundle.repositories:
            raise DailyWitnessedContextError("event repository outside original bundle")
        try:
            _require_event_time(event.get("occurred_at"), start, end)
            key = _event_key(event)
        except DailyEntityPreviewError:
            raise DailyWitnessedContextError("original event entity or JST time unbound") from None
        kind = event.get("kind")
        proof = by_id.pop(eid, None)
        if kind in route_source.REQUIRES_WITNESS:
            if proof is None:
                # No "fallback to public URL" for conversation or PR commits.
                raise DailyWitnessedContextError("route-sensitive event lacks API witness")
            if (
                key is None
                or proof.repository != event["repository"]
                or proof.number != event["number"]
                or proof.source_kind != kind
                or proof.entity_type != key.entity_type
                or proof.source_id != event["source_id"]
                or proof.occurred_at != event["occurred_at"]
                or proof.evidence_id != eid
            ):
                raise DailyWitnessedContextError("API witness changed Issue/PR identity")
            proof_row: Mapping[str, object] | None = proof.to_json()
            witnessed_count += 1
        else:
            if proof is not None:
                raise DailyWitnessedContextError("unwitnessed event borrowed route authority")
            proof_row = None
            if kind != "default_branch_commit":
                structural_count += 1
        if key is None:
            if kind != "default_branch_commit" or proof is not None:
                raise DailyWitnessedContextError("unattributed event is not a default commit")
            default_commits += 1
            continue
        grouped.setdefault(key, []).append((event, proof_row))
        if len(grouped) > MAX_ENTITIES:
            raise DailyWitnessedContextError("too many distinct verified entities")
    if by_id:
        raise DailyWitnessedContextError("route receipt has witnesses not in Evidence")
    if len(seen) != witnessed_count + structural_count + default_commits:
        raise DailyWitnessedContextError("source accounting is incomplete")
    if witnessed_count != trusted.witnessed_count:
        raise DailyWitnessedContextError("source route count does not match C0")
    return grouped, witnessed_count, structural_count, default_commits


def build_witnessed_entity_contexts(
    capture_sha256: str, *, max_bytes: int = MAX_CONTEXT_BYTES,
) -> WitnessedContextSet:
    """Construct entity-scoped v3 contexts only from Collector-owned CAS."""
    _assert_sha(capture_sha256)
    if type(max_bytes) is not int or not 1024 <= max_bytes <= MAX_CONTEXT_BYTES:
        raise DailyWitnessedContextError("context byte budget invalid")
    try:
        trusted, bundle = _trusted_capture(capture_sha256)
        grouped, witnessed, structural, default_commits = _group_events(bundle, trusted)
        # Estimate using the full worst-case batch_count to prevent late-size
        # changes from causing partial or silently truncated source contexts.
        buckets: list[tuple[EntityKey, tuple[Mapping[str, object], ...], tuple[Mapping[str, object], ...]]] = []
        for key in sorted(grouped):
            current_events: list[Mapping[str, object]] = []
            current_proofs: list[Mapping[str, object]] = []
            for event, proof in grouped[key]:
                next_events = current_events + [event]
                next_proofs = current_proofs + ([proof] if proof is not None else [])
                fits = len(next_events) <= MAX_CONTEXT_EVENTS and len(
                    WitnessedPartialContext(
                        capture_sha256, trusted.route_receipt_sha256,
                        bundle.sha256, bundle.date, trusted.collection_mode,
                        key, 0, MAX_CONTEXT_BATCHES, max_bytes,
                        tuple(next_events), tuple(next_proofs),
                    ).to_json_bytes(_size_trial=True)
                ) <= max_bytes
                if fits:
                    current_events, current_proofs = next_events, next_proofs
                    continue
                if not current_events:
                    raise DailyWitnessedContextError("one source event exceeds context limit")
                buckets.append((key, tuple(current_events), tuple(current_proofs)))
                current_events = [event]
                current_proofs = [proof] if proof is not None else []
                WitnessedPartialContext(
                    capture_sha256, trusted.route_receipt_sha256,
                    bundle.sha256, bundle.date, trusted.collection_mode,
                    key, 0, MAX_CONTEXT_BATCHES, max_bytes,
                    tuple(current_events), tuple(current_proofs),
                ).to_json_bytes()
            if current_events:
                buckets.append((key, tuple(current_events), tuple(current_proofs)))
            if len(buckets) > MAX_CONTEXT_BATCHES:
                raise DailyWitnessedContextError("context batch count exceeds hard limit")
        contexts = tuple(
            WitnessedPartialContext(
                capture_sha256, trusted.route_receipt_sha256, bundle.sha256,
                bundle.date, trusted.collection_mode,
                key, index, len(buckets), max_bytes, events, proofs,
            )
            for index, (key, events, proofs) in enumerate(buckets)
        )
        if any(len(ctx.to_json_bytes()) > max_bytes for ctx in contexts):
            raise DailyWitnessedContextError("final context exceeds byte budget")
        actual = [eid for ctx in contexts for eid in ctx.evidence_ids]
        expected = [
            event["evidence_id"]
            for key in sorted(grouped) for event, _ in grouped[key]
        ]
        if actual != expected or len(set(actual)) != len(actual):
            raise DailyWitnessedContextError("partition dropped or duplicated source")
        if len(actual) + default_commits != len(bundle.events):
            raise DailyWitnessedContextError("partition lost nondefault original Evidence")
        return WitnessedContextSet(
            capture_sha256=capture_sha256,
            route_receipt_sha256=trusted.route_receipt_sha256,
            evidence_bundle_sha256=bundle.sha256,
            date=bundle.date, collection_mode=trusted.collection_mode,
            contexts=contexts, grouped_event_count=len(actual),
            witnessed_event_count=witnessed,
            structural_event_count=structural,
            unattributed_default_commit_count=default_commits,
        )
    except DailyWitnessedContextError:
        raise
    except (
        closure.DailySourceClosureError, route_cas.DailyRouteCASError,
        GitHubDailySummaryError, DailyEntityContextError,
        ValueError, TypeError, OverflowError, RecursionError,
    ):
        raise DailyWitnessedContextError("trusted source context unavailable") from None


@dataclass(frozen=True)
class WitnessedEntityCandidate:
    summary: str
    evidence_ids: tuple[str, ...]
    candidate_sha256: str


@dataclass(frozen=True)
class WitnessedCandidateBatch:
    key: EntityKey
    capture_sha256: str
    route_receipt_sha256: str
    evidence_bundle_sha256: str
    context_sha256: str
    response_sha256: str
    candidates: tuple[WitnessedEntityCandidate, ...]
    uncited_evidence_ids: tuple[str, ...]

    def report(self) -> dict[str, object]:
        return {
            "record_version": RECORD_VERSION,
            "stage": CANDIDATE_STAGE,
            "entity": _entity(self.key),
            "capture_sha256": self.capture_sha256,
            "route_receipt_sha256": self.route_receipt_sha256,
            "evidence_bundle_sha256": self.evidence_bundle_sha256,
            "context_sha256": self.context_sha256,
            "response_sha256": self.response_sha256,
            "candidates": [
                {
                    "summary": item.summary,  # Private, not publishable.
                    "evidence_ids": list(item.evidence_ids),
                    "candidate_sha256": item.candidate_sha256,
                }
                for item in self.candidates
            ],
            "uncited_evidence_ids": list(self.uncited_evidence_ids),
            "grounding_required": True,
            "semantic_coverage_review_required": True,
            "requires_human_quality_review": True,
            "publication_authorized": False,
        }


def _reconstruct_context(
    context: WitnessedPartialContext, *, capture_sha256: str,
) -> WitnessedPartialContext:
    """Rebuild from original Collector CAS, not caller-provided context data."""
    _assert_sha(capture_sha256)
    if type(context) is not WitnessedPartialContext:
        raise DailyWitnessedContextError("context does not have witnessed v3 type")
    if context.capture_sha256 != capture_sha256:
        raise DailyWitnessedContextError("context capture digest does not match checkpoint")
    rebuilt = build_witnessed_entity_contexts(
        capture_sha256, max_bytes=context.max_context_bytes,
    )
    if context.batch_count != len(rebuilt.contexts) or not (
        0 <= context.batch_index < len(rebuilt.contexts)
    ):
        raise DailyWitnessedContextError("context index does not match trusted source")
    expected = rebuilt.contexts[context.batch_index]
    if context != expected or context.to_json_bytes() != expected.to_json_bytes():
        raise DailyWitnessedContextError("context not bound to source-route CAS")
    return expected


def witnessed_model_candidate_schema(
    context: WitnessedPartialContext, *, capture_sha256: str,
) -> dict[str, object]:
    """Return model-output schema only after reconstructing entire v3 partition."""
    context = _reconstruct_context(context, capture_sha256=capture_sha256)
    count = len(context.events)
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["candidates"],
        "properties": {
            "candidates": {
                "type": "array", "maxItems": MAX_CANDIDATES,
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
                            "items": {"type": "integer", "enum": list(range(count))},
                        },
                    },
                },
            },
        },
    }


def normalize_witnessed_entity_candidates(
    context: WitnessedPartialContext, response_bytes: bytes,
    *, capture_sha256: str,
) -> WitnessedCandidateBatch:
    """Normalize untrusted candidate bytes under a freshly validated CAS read.

    No candidate identity, source association, status, URL or effect permission
    may be selected by the model. This deliberately cannot be passed directly
    to the original v2 Grounding API; v3 Grounding must preserve the triple SHA.
    """
    context = _reconstruct_context(context, capture_sha256=capture_sha256)
    if type(response_bytes) is not bytes or not (
        0 < len(response_bytes) <= MAX_MODEL_OUTPUT_BYTES
    ):
        raise DailyWitnessedContextError("witnessed candidate bytes out of bounds")
    try:
        reply = _decode_json_object(
            response_bytes, label="witnessed entity candidate response",
        )
    except (ArtifactLifecycleError, ValueError, TypeError, RecursionError):
        raise DailyWitnessedContextError("model candidate JSON not accepted") from None
    if set(reply) != {"candidates"} or type(reply["candidates"]) is not list:
        raise DailyWitnessedContextError("candidate schema does not match contract")
    if len(reply["candidates"]) > MAX_CANDIDATES:
        raise DailyWitnessedContextError("model returned too many candidate claims")
    context_sha = context.sha256
    seen_candidate_sha: set[str] = set()
    cited: set[str] = set()
    result: list[WitnessedEntityCandidate] = []
    for candidate in reply["candidates"]:
        if type(candidate) is not dict or set(candidate) != {"summary", "source_refs"}:
            raise DailyWitnessedContextError("candidate contains unapproved fields")
        text = candidate["summary"]
        if type(text) is not str:
            raise DailyWitnessedContextError("candidate summary has wrong type")
        try:
            text_bytes = text.encode("utf-8")
        except UnicodeError:
            raise DailyWitnessedContextError("candidate text encoding invalid") from None
        if (
            not text or text != text.strip()
            or len(text) > MAX_SUMMARY_CHARS or len(text_bytes) > MAX_SUMMARY_BYTES
            or not text.isprintable() or len(text.splitlines()) != 1
        ):
            raise DailyWitnessedContextError("candidate text is invalid or multiline")
        refs = candidate["source_refs"]
        if (
            type(refs) is not list
            or not 1 <= len(refs) <= min(MAX_REFS_PER_CANDIDATE, len(context.events))
            or any(type(ref) is not int for ref in refs)
            or len(set(refs)) != len(refs)
            or any(ref < 0 or ref >= len(context.events) for ref in refs)
        ):
            raise DailyWitnessedContextError("candidate source_refs out of range")
        ids = tuple(context.events[i]["evidence_id"] for i in refs)
        cited.update(ids)
        payload = _canonical_bytes({
            "record_version": RECORD_VERSION,
            "capture_sha256": capture_sha256,
            "route_receipt_sha256": context.route_receipt_sha256,
            "evidence_bundle_sha256": context.evidence_bundle_sha256,
            "context_sha256": context_sha,
            "entity": _entity(context.key),
            "summary": text,
            "evidence_ids": list(ids),
        })
        candidate_id = _sha(payload)
        if candidate_id in seen_candidate_sha:
            raise DailyWitnessedContextError("model repeated an entity candidate")
        seen_candidate_sha.add(candidate_id)
        result.append(WitnessedEntityCandidate(text, ids, candidate_id))
    return WitnessedCandidateBatch(
        key=context.key, capture_sha256=capture_sha256,
        route_receipt_sha256=context.route_receipt_sha256,
        evidence_bundle_sha256=context.evidence_bundle_sha256,
        context_sha256=context_sha, response_sha256=_sha(response_bytes),
        candidates=tuple(result),
        uncited_evidence_ids=tuple(
            eid for eid in context.evidence_ids if eid not in cited
        ),
    )
