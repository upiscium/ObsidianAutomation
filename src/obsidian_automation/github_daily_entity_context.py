"""Issue/PR-scoped, non-publishing candidate extraction contexts (#326).

A v2 context holds only original GitHub Evidence for one attested entity.
The model cannot select a repository, issue number, URL, lifecycle status or
publication authority. Candidate text is never a grounded or approved Daily.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Mapping

from .artifact_lifecycle import _decode_json_object
from .github_daily_activity import _date_window
from .github_daily_issue_pr import (
    DailyEntityPreviewError,
    EntityKey,
    _event_key,
    _require_event_time,
)
from .github_daily_summary import EvidenceBundle, parse_evidence_bundle


RECORD_VERSION = 2
CONTEXT_STAGE = "github_daily_entity_partial_context_v2"
CANDIDATE_STAGE = "github_daily_entity_partial_candidate_v2"
MAX_BUNDLE_BYTES = 32 * 1024 * 1024
MAX_EVENTS = 50000
MAX_ENTITIES = 10000
MAX_CONTEXT_BATCHES = 4096
MAX_CONTEXT_EVENTS = 64
MAX_CONTEXT_BYTES = 64 * 1024
MAX_MODEL_OUTPUT_BYTES = 64 * 1024
MAX_CANDIDATES = 12
MAX_REFS_PER_CANDIDATE = 8
MAX_SUMMARY_CHARS = 2048
MAX_SUMMARY_BYTES = 8192
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_UNVERIFIED_PR_COMMIT = "pull_request_commit"


class DailyEntityContextError(DailyEntityPreviewError):
    """A context or candidate failed exact source and capacity checks."""


def _canonical_bytes(value: Mapping[str, object]) -> bytes:
    return (
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ) + "\n"
    ).encode("utf-8")


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _date_bounds(bundle: EvidenceBundle):
    try:
        target = date.fromisoformat(bundle.date)
    except (TypeError, ValueError) as exc:
        raise DailyEntityContextError("invalid canonical JST date") from exc
    if target.isoformat() != bundle.date or bundle.timezone != "Asia/Tokyo":
        raise DailyEntityContextError("noncanonical daily date or timezone")
    start, end = _date_window(target)
    if (
        bundle.window_start != start.isoformat().replace("+00:00", "Z")
        or bundle.window_end != end.isoformat().replace("+00:00", "Z")
    ):
        raise DailyEntityContextError("daily Evidence has a wrong JST window")
    return start, end


@dataclass(frozen=True)
class EntityPartialContext:
    evidence_bundle_sha256: str
    date: str
    key: EntityKey
    batch_index: int
    batch_count: int
    events: tuple[Mapping[str, object], ...]

    def to_json_bytes(self) -> bytes:
        if _SHA256.fullmatch(self.evidence_bundle_sha256) is None:
            raise DailyEntityContextError("context Evidence digest is invalid")
        if (
            self.key.entity_type not in {"issue", "pull_request"}
            or type(self.key.number) is not int or self.key.number < 1
            or not isinstance(self.key.repository, str)
            or len(self.key.repository) > 200
        ):
            raise DailyEntityContextError("context entity key is invalid")
        if (
            type(self.batch_index) is not int
            or type(self.batch_count) is not int
            or not 0 <= self.batch_index < self.batch_count <= MAX_CONTEXT_BATCHES
            or not 1 <= len(self.events) <= MAX_CONTEXT_EVENTS
        ):
            raise DailyEntityContextError("context batch bounds are invalid")
        return _canonical_bytes({
            "record_version": RECORD_VERSION,
            "stage": CONTEXT_STAGE,
            "evidence_bundle_sha256": self.evidence_bundle_sha256,
            "date": self.date,
            "entity": {
                "repository": self.key.repository,
                "entity_type": self.key.entity_type,
                "number": self.key.number,
            },
            "batch_index": self.batch_index,
            "batch_count": self.batch_count,
            "events": [dict(event) for event in self.events],
        })

    @property
    def sha256(self) -> str:
        value = self.to_json_bytes()
        if len(value) > MAX_CONTEXT_BYTES:
            raise DailyEntityContextError("context exceeds hard byte ceiling")
        return _sha(value)

    @property
    def evidence_ids(self) -> tuple[str, ...]:
        return tuple(str(event["evidence_id"]) for event in self.events)


@dataclass(frozen=True)
class EntityContextSet:
    evidence_bundle_sha256: str
    date: str
    contexts: tuple[EntityPartialContext, ...]
    excluded_default_commits: int
    unverified_pr_commits: int
    grouped_event_count: int

    def report(self) -> dict[str, object]:
        # No original event prose, actor, URLs or arbitrary source paths.
        return {
            "record_version": RECORD_VERSION,
            "stage": "github_daily_entity_context_report_v2",
            "date": self.date,
            "evidence_bundle_sha256": self.evidence_bundle_sha256,
            "context_count": len(self.contexts),
            "grouped_event_count": self.grouped_event_count,
            "excluded_default_commits": self.excluded_default_commits,
            "unverified_pr_commits": self.unverified_pr_commits,
            "requires_human_quality_review": True,
            "publication_authorized": False,
        }


def _group_validated_events(
    bundle: EvidenceBundle,
) -> tuple[dict[EntityKey, list[Mapping[str, object]]], int, int]:
    if len(bundle.events) > MAX_EVENTS:
        raise DailyEntityContextError("daily event count exceeds limit")
    start, end = _date_bounds(bundle)
    grouped: dict[EntityKey, list[Mapping[str, object]]] = {}
    default_count = 0
    pr_commit_count = 0
    seen: set[str] = set()
    for event in bundle.events:
        try:
            _require_event_time(event.get("occurred_at"), start, end)
        except DailyEntityPreviewError as exc:
            raise DailyEntityContextError("source time is unbound") from exc
        original_id = event.get("evidence_id")
        if not isinstance(original_id, str) or _SHA256.fullmatch(original_id) is None:
            raise DailyEntityContextError("original Evidence ID is invalid")
        if original_id in seen:
            raise DailyEntityContextError("duplicate original Evidence ID")
        seen.add(original_id)
        if event.get("repository") not in bundle.repositories:
            raise DailyEntityContextError("event repository is unbound")
        # Reuse #325's strict kind, URL, entity-type and number validation.
        # Do not infer an entity from a textual mention of #N or from a
        # pull-commit URL that lacks an authenticated /pull/N association.
        try:
            key = _event_key(event)
        except DailyEntityPreviewError as exc:
            raise DailyEntityContextError("source entity is unbound") from exc
        if event.get("kind") == _UNVERIFIED_PR_COMMIT:
            pr_commit_count += 1
            continue
        if key is None:
            if event.get("kind") != "default_branch_commit":
                raise DailyEntityContextError("unsupported unassigned event")
            default_count += 1
            continue
        grouped.setdefault(key, []).append(event)
        if len(grouped) > MAX_ENTITIES:
            raise DailyEntityContextError("too many distinct Issue/PR entities")
    return grouped, default_count, pr_commit_count


def build_entity_contexts(
    evidence_bytes: bytes, *, max_bytes: int = MAX_CONTEXT_BYTES,
) -> EntityContextSet:
    """Parse v1 original immutable Evidence, partition losslessly by entity.

    This consumes provided bytes and never opens files, runs a model, or
    writes a projection. Contexts remain nonauthoritative pending Grounding.
    """
    if (
        not isinstance(evidence_bytes, bytes)
        or not 0 < len(evidence_bytes) <= MAX_BUNDLE_BYTES
    ):
        raise DailyEntityContextError("original Evidence bytes are invalid")
    if type(max_bytes) is not int or not 1024 <= max_bytes <= MAX_CONTEXT_BYTES:
        raise DailyEntityContextError("context size budget is invalid")
    bundle = parse_evidence_bundle(evidence_bytes)
    grouped, excluded, unverified = _group_validated_events(bundle)

    # Use a conservative temporary batch-count width. Final encoded contexts
    # are checked again with their exact real batch counts and indexes.
    buckets: list[tuple[EntityKey, tuple[Mapping[str, object], ...]]] = []
    for key in sorted(grouped):
        current: list[Mapping[str, object]] = []
        for event in grouped[key]:
            proposal = current + [event]
            if len(proposal) <= MAX_CONTEXT_EVENTS:
                trial = EntityPartialContext(
                    bundle.sha256, bundle.date, key, 0,
                    MAX_CONTEXT_BATCHES, tuple(proposal),
                )
                if len(trial.to_json_bytes()) <= max_bytes:
                    current = proposal
                    continue
            if not current:
                raise DailyEntityContextError(
                    "single original Evidence event exceeds context budget"
                )
            buckets.append((key, tuple(current)))
            current = [event]
            if len(EntityPartialContext(
                bundle.sha256, bundle.date, key, 0,
                MAX_CONTEXT_BATCHES, tuple(current),
            ).to_json_bytes()) > max_bytes:
                raise DailyEntityContextError(
                    "single original Evidence event exceeds context budget"
                )
        if current:
            buckets.append((key, tuple(current)))
        if len(buckets) > MAX_CONTEXT_BATCHES:
            raise DailyEntityContextError("too many entity partial contexts")

    contexts = tuple(
        EntityPartialContext(
            bundle.sha256, bundle.date, key, index, len(buckets), events,
        )
        for index, (key, events) in enumerate(buckets)
    )
    if any(len(context.to_json_bytes()) > max_bytes for context in contexts):
        raise DailyEntityContextError("final entity context exceeds budget")
    actual_ids = [eid for context in contexts for eid in context.evidence_ids]
    expected_ids = [
        str(event["evidence_id"])
        for key in sorted(grouped)
        for event in grouped[key]
    ]
    if actual_ids != expected_ids or len(set(actual_ids)) != len(actual_ids):
        raise DailyEntityContextError("entity partition lost or reordered Evidence")
    if len(actual_ids) + excluded + unverified != len(bundle.events):
        raise DailyEntityContextError("original Evidence accounting differs")
    return EntityContextSet(
        evidence_bundle_sha256=bundle.sha256, date=bundle.date,
        contexts=contexts, excluded_default_commits=excluded,
        unverified_pr_commits=unverified, grouped_event_count=len(actual_ids),
    )


def _validate_context_sources(context: EntityPartialContext) -> None:
    """Reject forged in-memory contexts, including an empty model result."""
    try:
        target = date.fromisoformat(context.date)
    except (TypeError, ValueError) as exc:
        raise DailyEntityContextError("context JST date is invalid") from exc
    if target.isoformat() != context.date:
        raise DailyEntityContextError("context JST date is not canonical")
    start, end = _date_window(target)
    seen: set[str] = set()
    for event in context.events:
        try:
            _require_event_time(event.get("occurred_at"), start, end)
            key = _event_key(event)
        except DailyEntityPreviewError as exc:
            raise DailyEntityContextError("context original event is invalid") from exc
        if key != context.key or event.get("kind") == _UNVERIFIED_PR_COMMIT:
            raise DailyEntityContextError("context includes an unbound entity")
        original_id = event.get("evidence_id")
        if not isinstance(original_id, str) or _SHA256.fullmatch(original_id) is None:
            raise DailyEntityContextError("context original Evidence ID is invalid")
        source = dict(event)
        source.pop("evidence_id", None)
        if _sha(_canonical_bytes(source)) != original_id or original_id in seen:
            raise DailyEntityContextError("context original Evidence changed")
        seen.add(original_id)


def model_candidate_schema(context: EntityPartialContext) -> dict[str, object]:
    """Constrain integer refs to *one* entity context, not any repository."""
    _validate_context_sources(context)
    if len(context.to_json_bytes()) > MAX_CONTEXT_BYTES:
        raise DailyEntityContextError("invalid model-facing context capacity")
    count = len(context.events)
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
                            "type": "array",
                            "minItems": 1,
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


@dataclass(frozen=True)
class EntityCandidate:
    summary: str
    evidence_ids: tuple[str, ...]
    candidate_sha256: str


@dataclass(frozen=True)
class EntityCandidateBatch:
    key: EntityKey
    context_sha256: str
    response_sha256: str
    candidates: tuple[EntityCandidate, ...]
    uncited_evidence_ids: tuple[str, ...]

    def report(self) -> dict[str, object]:
        return {
            "record_version": RECORD_VERSION,
            "stage": CANDIDATE_STAGE,
            "entity": {
                "repository": self.key.repository,
                "entity_type": self.key.entity_type,
                "number": self.key.number,
            },
            "context_sha256": self.context_sha256,
            "response_sha256": self.response_sha256,
            "candidates": [
                {
                    "summary": candidate.summary,
                    "evidence_ids": list(candidate.evidence_ids),
                    "candidate_sha256": candidate.candidate_sha256,
                }
                for candidate in self.candidates
            ],
            "uncited_evidence_ids": list(self.uncited_evidence_ids),
            "grounding_required": True,
            "semantic_coverage_review_required": True,
            "publication_authorized": False,
        }


def _candidate_event_key(event: Mapping[str, object]) -> EntityKey | None:
    try:
        return _event_key(event)
    except DailyEntityPreviewError as exc:
        raise DailyEntityContextError("candidate source entity is invalid") from exc


def normalize_entity_candidates(
    context: EntityPartialContext, model_bytes: bytes,
) -> EntityCandidateBatch:
    """Validate untrusted model candidates; this NEVER marks progress proven."""
    _validate_context_sources(context)
    context_bytes = context.to_json_bytes()
    if len(context_bytes) > MAX_CONTEXT_BYTES:
        raise DailyEntityContextError("source context exceeds byte limit")
    if (
        not isinstance(model_bytes, bytes)
        or not 0 < len(model_bytes) <= MAX_MODEL_OUTPUT_BYTES
    ):
        raise DailyEntityContextError("model response size is invalid")
    raw = _decode_json_object(model_bytes, label="entity partial candidate response")
    if set(raw) != {"candidates"} or not isinstance(raw["candidates"], list):
        raise DailyEntityContextError("entity candidate schema is invalid")
    if len(raw["candidates"]) > MAX_CANDIDATES:
        raise DailyEntityContextError("too many model candidate claims")
    context_sha = _sha(context_bytes)
    candidates: list[EntityCandidate] = []
    cited_all: set[str] = set()
    for candidate in raw["candidates"]:
        if not isinstance(candidate, dict) or set(candidate) != {"summary", "source_refs"}:
            raise DailyEntityContextError("candidate properties are invalid")
        text = candidate["summary"]
        if not isinstance(text, str):
            raise DailyEntityContextError("candidate prose is invalid")
        try:
            size = len(text.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise DailyEntityContextError("candidate UTF-8 is invalid") from exc
        if (
            not text or text != text.strip()
            or "\n" in text or "\r" in text
            or len(text) > MAX_SUMMARY_CHARS
            or size > MAX_SUMMARY_BYTES
            or any(ord(character) < 32 or ord(character) == 127 for character in text)
        ):
            raise DailyEntityContextError("candidate prose is invalid")
        refs = candidate["source_refs"]
        if (
            not isinstance(refs, list)
            or not 1 <= len(refs) <= min(
                MAX_REFS_PER_CANDIDATE, len(context.events),
            )
            or any(type(ref) is not int for ref in refs)
            or len(set(refs)) != len(refs)
            or any(ref < 0 or ref >= len(context.events) for ref in refs)
        ):
            raise DailyEntityContextError("model source_refs are unbound")
        ids: list[str] = []
        for source_index in refs:
            event = context.events[source_index]
            if (
                event.get("kind") == _UNVERIFIED_PR_COMMIT
                or _candidate_event_key(event) != context.key
            ):
                raise DailyEntityContextError("candidate changed entity identity")
            eid = event.get("evidence_id")
            if not isinstance(eid, str) or _SHA256.fullmatch(eid) is None:
                raise DailyEntityContextError("candidate source digest invalid")
            ids.append(eid)
        cited_all.update(ids)
        normalized = _canonical_bytes({
            "entity": {
                "repository": context.key.repository,
                "entity_type": context.key.entity_type,
                "number": context.key.number,
            },
            "context_sha256": context_sha,
            "summary": text,
            "evidence_ids": ids,
        })
        candidates.append(EntityCandidate(text, tuple(ids), _sha(normalized)))
    uncited = tuple(eid for eid in context.evidence_ids if eid not in cited_all)
    return EntityCandidateBatch(
        key=context.key, context_sha256=context_sha,
        response_sha256=_sha(model_bytes), candidates=tuple(candidates),
        uncited_evidence_ids=uncited,
    )
