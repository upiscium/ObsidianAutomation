"""Daily v2 C1: CAS-attested Issue/PR source-bound partial contexts (#342).

This is a NEW nonpublishing model-input / candidate contract, not an upgrade of
legacy #327 Context-v2. Every public admission API reopens the Collector CAS
from its exact capture SHA and rebuilds all entity partitions. A model or
caller-supplied frozen dataclass, hash or linked GitHub URL grants no authority.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Mapping

from . import github_daily_entity_context as previous
from . import github_daily_issue_pr as entity
from . import github_daily_route_cas as cas
from . import github_daily_source_closure as c0
from . import github_daily_source_route as source
from .artifact_lifecycle import ArtifactLifecycleError, _decode_json_object
from .github_daily_summary import GitHubDailySummaryError, parse_evidence_bundle

RECORD_VERSION = 3
CONTEXT_STAGE = "github_daily_witnessed_entity_partial_context_v3"
CANDIDATE_STAGE = "github_daily_witnessed_entity_partial_candidate_v3"
MAX_CONTEXT_BYTES = previous.MAX_CONTEXT_BYTES


class DailyWitnessedContextError(ValueError):
    """C1 source membership, identity, bounds or candidate is not admissible."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _entity(key: entity.EntityKey) -> dict[str, object]:
    return {
        "repository": key.repository, "entity_type": key.entity_type,
        "number": key.number,
    }


@dataclass(frozen=True)
class ContextEvent:
    original: Mapping[str, object]
    route_proof: c0.SourceRouteBinding | None

    def to_json(self) -> dict[str, object]:
        return {
            "original_event": dict(self.original),
            # Null explicitly means original Evidence only, NOT fresh remote
            # route admission. All four sensitive kinds require a proof.
            "verified_route": (
                self.route_proof.to_json() if self.route_proof is not None else None
            ),
        }


@dataclass(frozen=True)
class WitnessedEntityContext:
    capture_sha256: str
    route_receipt_sha256: str
    evidence_sha256: str
    closure_sha256: str
    date: str
    captured_at_utc: str
    collection_mode: str
    key: entity.EntityKey
    batch_index: int
    batch_count: int
    max_context_bytes: int
    events: tuple[ContextEvent, ...]

    def to_json_bytes(self) -> bytes:
        for digest in (
            self.capture_sha256, self.route_receipt_sha256,
            self.evidence_sha256, self.closure_sha256,
        ):
            if type(digest) is not str or previous._SHA256.fullmatch(digest) is None:
                raise DailyWitnessedContextError("C1 source digest is invalid")
        if not isinstance(self.key, entity.EntityKey) or self.key.entity_type not in (
            "issue", "pull_request",
        ) or type(self.key.number) is not int or self.key.number < 1:
            raise DailyWitnessedContextError("C1 entity identity is invalid")
        if (
            type(self.batch_index) is not int
            or type(self.batch_count) is not int
            or not 0 <= self.batch_index < self.batch_count <= previous.MAX_CONTEXT_BATCHES
            or type(self.max_context_bytes) is not int
            or not 1024 <= self.max_context_bytes <= MAX_CONTEXT_BYTES
            or not 1 <= len(self.events) <= previous.MAX_CONTEXT_EVENTS
        ):
            raise DailyWitnessedContextError("C1 partial batch is invalid")
        if type(self.date) is not str or type(self.collection_mode) is not str:
            raise DailyWitnessedContextError("C1 source time/mode is invalid")
        if type(self.captured_at_utc) is not str:
            raise DailyWitnessedContextError("C1 source capture time is invalid")
        try:
            payload = previous._canonical_bytes({
                "record_version": RECORD_VERSION,
                "stage": CONTEXT_STAGE,
                "capture_sha256": self.capture_sha256,
                "route_receipt_sha256": self.route_receipt_sha256,
                "evidence_sha256": self.evidence_sha256,
                "closure_sha256": self.closure_sha256,
                "date": self.date,
                "captured_at_utc": self.captured_at_utc,
                "collection_mode": self.collection_mode,
                "entity": _entity(self.key),
                "batch_index": self.batch_index,
                "batch_count": self.batch_count,
                "max_context_bytes": self.max_context_bytes,
                "events": [part.to_json() for part in self.events],
                "publication_authorized": False,
                "requires_human_quality_review": True,
            })
        except (ValueError, TypeError, AttributeError, RecursionError):
            raise DailyWitnessedContextError("C1 event serialization is invalid") from None
        if len(payload) > MAX_CONTEXT_BYTES:
            raise DailyWitnessedContextError("C1 context exceeds hard bound")
        return payload

    @property
    def sha256(self) -> str:
        return _sha(self.to_json_bytes())

    @property
    def evidence_ids(self) -> tuple[str, ...]:
        return tuple(str(part.original["evidence_id"]) for part in self.events)


@dataclass(frozen=True)
class WitnessedContextSet:
    capture_sha256: str
    route_receipt_sha256: str
    evidence_sha256: str
    closure_sha256: str
    date: str
    collection_mode: str
    contexts: tuple[WitnessedEntityContext, ...]
    original_event_count: int
    grouped_event_count: int
    witnessed_event_count: int
    original_only_event_count: int
    unattributed_default_commit_count: int

    def report(self) -> dict[str, object]:
        return {
            "record_version": RECORD_VERSION,
            "stage": "github_daily_witnessed_context_report_v3",
            "capture_sha256": self.capture_sha256,
            "route_receipt_sha256": self.route_receipt_sha256,
            "evidence_sha256": self.evidence_sha256,
            "closure_sha256": self.closure_sha256,
            "date": self.date,
            "collection_mode": self.collection_mode,
            "context_count": len(self.contexts),
            "original_event_count": self.original_event_count,
            "grouped_event_count": self.grouped_event_count,
            "witnessed_event_count": self.witnessed_event_count,
            "original_only_event_count": self.original_only_event_count,
            "unattributed_default_commit_count": self.unattributed_default_commit_count,
            "semantic_coverage_review_required": True,
            "publication_authorized": False,
        }


def _trusted_sources(capture_sha256: str):
    """Reopen exact CAS and compare with independent C0 source closure."""
    closure = c0.read_verified_source_closure(capture_sha256)
    stored = cas.read_verified_capture(capture_sha256)
    if (
        type(closure) is not c0.VerifiedSourceClosure
        or type(stored) is not cas.VerifiedCapture
        or closure.capture_sha256 != capture_sha256
        or stored.index_sha256 != capture_sha256
        or stored.collector_storage_attested is not True
        or stored.daily_publication_authorized is not False
        or stored.capture_index.sha256 != capture_sha256
        or closure.route_receipt_sha256 != stored.capture_index.route_receipt_sha256
        or closure.evidence_sha256 != stored.capture_index.evidence_sha256
        or closure.date != stored.capture_index.date
        or closure.collection_mode != stored.capture_index.collection_mode
        or closure.captured_at_utc != stored.capture_index.captured_at_utc
        or _sha(closure.canonical_bytes) != closure.sha256
    ):
        raise DailyWitnessedContextError("CAS/C0 source closure identities differ")
    bundle = parse_evidence_bundle(stored.evidence_bytes)
    if bundle.sha256 != closure.evidence_sha256 or bundle.date != closure.date:
        raise DailyWitnessedContextError("CAS original Evidence is unbound")
    return closure, bundle, stored.evidence_bytes


def _source_key(raw: Mapping[str, object]) -> entity.EntityKey | None:
    try:
        return entity._event_key(raw)
    except entity.DailyEntityPreviewError:
        raise DailyWitnessedContextError("C1 event entity identity is invalid") from None


def _partitions(closure: c0.VerifiedSourceClosure, bundle) -> tuple[
    dict[entity.EntityKey, list[ContextEvent]], int, int, int,
]:
    if len(bundle.events) > previous.MAX_EVENTS:
        raise DailyWitnessedContextError("C1 original Evidence count exceeds bound")
    bounds = previous._date_bounds(bundle)
    by_evidence_id = {proof.evidence_id: proof for proof in closure.bindings}
    if len(by_evidence_id) != len(closure.bindings):
        raise DailyWitnessedContextError("C1 route witnesses repeat Evidence IDs")
    groups: dict[entity.EntityKey, list[ContextEvent]] = {}
    excluded = 0
    witnessed = 0
    original_only = 0
    for raw in bundle.events:
        try:
            entity._require_event_time(raw.get("occurred_at"), *bounds)
        except entity.DailyEntityPreviewError:
            raise DailyWitnessedContextError("C1 original Evidence date is unbound") from None
        eid = raw.get("evidence_id")
        if type(eid) is not str or previous._SHA256.fullmatch(eid) is None:
            raise DailyWitnessedContextError("C1 original Evidence ID is invalid")
        key = _source_key(raw)
        proof = by_evidence_id.pop(eid, None)
        needs_proof = raw.get("kind") in source.REQUIRES_WITNESS
        if needs_proof != (proof is not None):
            # No fallback to the old /issues/N public-link heuristic or an
            # unwitnessed commit; missing identity is BLOCKED, not empty Day.
            raise DailyWitnessedContextError("C1 route-sensitive event is unwitnessed")
        if key is None:
            if raw.get("kind") != "default_branch_commit" or proof is not None:
                raise DailyWitnessedContextError("C1 unattributed event has a route")
            excluded += 1
            continue
        if proof is not None:
            if (
                (proof.repository, proof.entity_type, proof.number) != (
                    key.repository, key.entity_type, key.number
                )
                or proof.source_kind != raw.get("kind")
                or proof.source_id != raw.get("source_id")
                or proof.occurred_at != raw.get("occurred_at")
            ):
                raise DailyWitnessedContextError("C1 witnessed Issue/PR entity changed")
            witnessed += 1
        else:
            original_only += 1
        groups.setdefault(key, []).append(ContextEvent(raw, proof))
        if len(groups) > previous.MAX_ENTITIES:
            raise DailyWitnessedContextError("C1 has too many distinct entities")
    if by_evidence_id:
        raise DailyWitnessedContextError("C1 contains foreign route witnesses")
    if witnessed != closure.witnessed_count or excluded != closure.unattributed_default_commit_count:
        raise DailyWitnessedContextError("C1 witnessed or excluded source count differs")
    if (
        witnessed + original_only + excluded != len(bundle.events)
        or len(bundle.events) != closure.event_count
        or original_only != closure.other_unwitnessed_event_count
    ):
        raise DailyWitnessedContextError("C1 original Evidence accounting differs")
    return groups, excluded, witnessed, original_only


def _new_context(
    closure: c0.VerifiedSourceClosure,
    *, key: entity.EntityKey, parts: tuple[ContextEvent, ...],
    index: int, count: int, budget: int,
) -> WitnessedEntityContext:
    return WitnessedEntityContext(
        capture_sha256=closure.capture_sha256,
        route_receipt_sha256=closure.route_receipt_sha256,
        evidence_sha256=closure.evidence_sha256,
        closure_sha256=closure.sha256,
        date=closure.date,
        captured_at_utc=closure.captured_at_utc,
        collection_mode=closure.collection_mode,
        key=key,
        batch_index=index,
        batch_count=count,
        max_context_bytes=budget,
        events=parts,
    )


def build_witnessed_entity_contexts(
    capture_sha256: str, *, max_bytes: int = MAX_CONTEXT_BYTES,
) -> WitnessedContextSet:
    """Regenerate whole partition from trusted CAS, never a caller-provided C0.

    This does not change v1/v2 contexts, their quarantine decisions, or any
    model/Writer/Timer code path.
    """
    if type(max_bytes) is not int or not 1024 <= max_bytes <= MAX_CONTEXT_BYTES:
        raise DailyWitnessedContextError("C1 context budget is invalid")
    try:
        closure, bundle, original_bytes = _trusted_sources(capture_sha256)
        # Reuse #327's original-Evidence time/entity validation without
        # inheriting its quarantine policy. Original-only events are still
        # deliberately labelled as such in C1. Any existing v1/v2 caller
        # continues to use #327 unchanged.
        previous.build_entity_contexts(original_bytes)
        groups, excluded, witnessed, original_only = _partitions(closure, bundle)
        provisional: list[tuple[entity.EntityKey, tuple[ContextEvent, ...]]] = []
        for key in sorted(groups):
            current: list[ContextEvent] = []
            for part in groups[key]:
                proposal = current + [part]
                if len(proposal) <= previous.MAX_CONTEXT_EVENTS:
                    trial = _new_context(
                        closure, key=key, parts=tuple(proposal), index=0,
                        count=previous.MAX_CONTEXT_BATCHES, budget=max_bytes,
                    )
                    if len(trial.to_json_bytes()) <= max_bytes:
                        current = proposal
                        continue
                if not current:
                    raise DailyWitnessedContextError(
                        "C1 original event exceeds bounded model context"
                    )
                provisional.append((key, tuple(current)))
                current = [part]
                trial = _new_context(
                    closure, key=key, parts=tuple(current), index=0,
                    count=previous.MAX_CONTEXT_BATCHES, budget=max_bytes,
                )
                if len(trial.to_json_bytes()) > max_bytes:
                    raise DailyWitnessedContextError(
                        "C1 original event exceeds bounded model context"
                    )
            if current:
                provisional.append((key, tuple(current)))
            if len(provisional) > previous.MAX_CONTEXT_BATCHES:
                raise DailyWitnessedContextError("C1 context batch count exceeds bound")
        count = len(provisional)
        contexts = tuple(
            _new_context(
                closure, key=key, parts=parts,
                index=i, count=count, budget=max_bytes,
            ) for i, (key, parts) in enumerate(provisional)
        )
        if any(len(part.to_json_bytes()) > max_bytes for part in contexts):
            raise DailyWitnessedContextError("C1 final context exceeds requested budget")
        # Canonical complete partition: no duplicate or silently skipped
        # original event, even when comments were once v1-quarantined.
        ids = [eid for part in contexts for eid in part.evidence_ids]
        if len(ids) != len(set(ids)) or len(ids) != witnessed + original_only:
            raise DailyWitnessedContextError("C1 entity partition lost events")
        expect = [
            raw["evidence_id"]
            for key in sorted(groups) for event in groups[key]
            for raw in (event.original,)
        ]
        if ids != expect:
            raise DailyWitnessedContextError("C1 entity partition changed source order")
        return WitnessedContextSet(
            capture_sha256=closure.capture_sha256,
            route_receipt_sha256=closure.route_receipt_sha256,
            evidence_sha256=closure.evidence_sha256,
            closure_sha256=closure.sha256,
            date=closure.date,
            collection_mode=closure.collection_mode,
            contexts=contexts,
            original_event_count=len(bundle.events),
            grouped_event_count=len(ids),
            witnessed_event_count=witnessed,
            original_only_event_count=original_only,
            unattributed_default_commit_count=excluded,
        )
    except DailyWitnessedContextError:
        raise
    except (
        c0.DailySourceClosureError, cas.DailyRouteCASError,
        entity.DailyEntityPreviewError, previous.DailyEntityContextError,
        ArtifactLifecycleError, GitHubDailySummaryError,
        ValueError, TypeError, KeyError, AttributeError, RecursionError,
    ):
        raise DailyWitnessedContextError(
            "C1 trusted source/context admission is unavailable"
        ) from None


def verify_witnessed_entity_context(
    context: WitnessedEntityContext, *, capture_sha256: str,
) -> str:
    """Rebuild the *entire* partition from exact CAS to verify a model input."""
    if type(context) is not WitnessedEntityContext or context.capture_sha256 != capture_sha256:
        raise DailyWitnessedContextError("C1 context is not bound to capture digest")
    # First enforce the capacity/type envelope without trusting any inner data.
    raw = context.to_json_bytes()
    expected = build_witnessed_entity_contexts(
        capture_sha256, max_bytes=context.max_context_bytes,
    )
    if (
        context.batch_count != len(expected.contexts)
        or context.batch_index >= len(expected.contexts)
        or expected.contexts[context.batch_index] != context
        or expected.contexts[context.batch_index].to_json_bytes() != raw
    ):
        raise DailyWitnessedContextError("C1 context is not an exact CAS partition")
    return _sha(raw)
