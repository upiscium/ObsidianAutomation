"""C2b: private Issue/PR rollup with complete source and response accounting.

Inputs are raw model replies located by exact context digests, not preaccepted
candidate/assessment objects. Every build reconstructs the Collector CAS and
replays C1b/C2a validation. Model support is advisory, never publication consent.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from . import github_daily_witnessed_entity_context as c1a
from . import github_daily_witnessed_entity_candidate as c1b
from . import github_daily_witnessed_entity_grounding as c2a
from . import github_daily_route_cas as cas
from .github_daily_summary import GitHubDailySummaryError, parse_evidence_bundle
from .artifact_lifecycle import ArtifactLifecycleError

RECORD_VERSION = 3
STAGE = "github_daily_witnessed_entity_rollup_v3"
MAX_INPUT_BYTES = 32 * 1024 * 1024
MAX_ROLLUP_BYTES = 16 * 1024 * 1024
# Conservative whole-partition replay budget, independent of reply byte size.
MAX_SOURCE_RECONSTRUCTIONS = 512
MAX_SOURCE_WORK_BYTES = 256 * 1024 * 1024


def _reconstruction_budget(partial_count: int, ground_count: int) -> int:
    # Initial C1a build + (C1b: 2, C2a build: 3) per partial + 3 per
    # supplied verdict. Dependency changes must update the call-count test.
    planned = 1 + 5 * partial_count + 3 * ground_count
    if planned > MAX_SOURCE_RECONSTRUCTIONS:
        raise DailyWitnessedRollupError("rollup reconstruction-count budget exhausted")
    return planned

MAX_PARTIAL_REPLIES = c1a.previous.MAX_CONTEXT_BATCHES
MAX_GROUND_REPLIES = MAX_PARTIAL_REPLIES * c1b.MAX_CANDIDATES
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class DailyWitnessedRollupError(ValueError):
    """The complete source/reply set cannot be reconciled safely."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(data: object) -> bytes:
    return (json.dumps(data, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


def _digest(value: object) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise DailyWitnessedRollupError("invalid exact source/reply digest")
    return value


@dataclass(frozen=True)
class PartialReply:
    context_sha256: str
    response_bytes: bytes


@dataclass(frozen=True)
class GroundReply:
    ground_context_sha256: str
    response_bytes: bytes


@dataclass(frozen=True)
class WitnessedEntityRollup:
    """Owned private bytes, not an attestation capability or Daily output."""
    canonical_bytes: bytes

    def to_json_bytes(self) -> bytes:
        if type(self.canonical_bytes) is not bytes or not (
            0 < len(self.canonical_bytes) <= MAX_ROLLUP_BYTES
        ):
            raise DailyWitnessedRollupError("invalid private rollup byte envelope")
        return self.canonical_bytes

    def report(self) -> dict[str, object]:
        return json.loads(self.to_json_bytes())

    @property
    def sha256(self) -> str:
        return _sha(self.to_json_bytes())


def _snapshot(records: tuple, cls: type, field: str, count_limit: int,
              byte_limit: int) -> tuple[dict[str, bytes], int]:
    # Do not retain caller record objects or virtual sequence operations past
    # admission. Only exact primitive fields are copied into a fresh map.
    if type(records) is not tuple or len(records) > count_limit:
        raise DailyWitnessedRollupError("invalid bounded reply collection")
    owned: dict[str, bytes] = {}
    total = 0
    for record in records:
        if type(record) is not cls:
            raise DailyWitnessedRollupError("reply has wrong record type")
        identity = _digest(getattr(record, field))
        raw = record.response_bytes
        if type(raw) is not bytes or not 0 < len(raw) <= byte_limit:
            raise DailyWitnessedRollupError("reply bytes exceed per-record bound")
        if identity in owned:
            # Even identical duplicates conceal ambiguous response selection.
            raise DailyWitnessedRollupError("duplicate reply identity")
        total += len(raw)
        if total > MAX_INPUT_BYTES:
            raise DailyWitnessedRollupError("reply collection exceeds total byte budget")
        owned[identity] = raw
    return owned, total


def _key(context: c1a.WitnessedEntityContext) -> tuple[str, str, int]:
    return (context.key.repository, context.key.entity_type, context.key.number)


def _identity(key: tuple[str, str, int]) -> dict[str, object]:
    return dict(zip(("repository", "entity_type", "number"), key))


def build_witnessed_entity_rollup(
    capture_sha256: str,
    partial_replies: tuple[PartialReply, ...],
    grounding_replies: tuple[GroundReply, ...],
    *, max_context_bytes: int = c1a.MAX_CONTEXT_BYTES,
) -> WitnessedEntityRollup:
    """One deterministic row per entity, with no missing-source-as-inactivity.

    Missing replies are explicit incomplete coverage. Malformed, foreign or
    duplicate replies and unavailable/corrupted original CAS are typed errors.
    All raw verdicts are re-normalized: a forged assessment object is no input.
    The caller must separately admit the capture digest and response selection.
    """
    _digest(capture_sha256)
    if type(max_context_bytes) is not int or not 1024 <= max_context_bytes <= c1a.MAX_CONTEXT_BYTES:
        raise DailyWitnessedRollupError("invalid context reconstruction budget")
    partials, partial_size = _snapshot(
        partial_replies, PartialReply, "context_sha256", MAX_PARTIAL_REPLIES,
        c1b.MAX_MODEL_REPLY_BYTES,
    )
    grounds, ground_size = _snapshot(
        grounding_replies, GroundReply, "ground_context_sha256", MAX_GROUND_REPLIES,
        c2a.MAX_GROUND_REPLY_BYTES,
    )
    if partial_size + ground_size > MAX_INPUT_BYTES:
        raise DailyWitnessedRollupError("combined model replies exceed byte budget")
    planned = _reconstruction_budget(len(partials), len(grounds))
    try:
        return _build(capture_sha256, partials, grounds, max_context_bytes, planned)
    except DailyWitnessedRollupError:
        raise
    except (GitHubDailySummaryError, ArtifactLifecycleError,
            ValueError, TypeError, AttributeError, KeyError, IndexError,
            OverflowError, RecursionError):
        raise DailyWitnessedRollupError("trusted rollup source or reply unavailable") from None


def _build(capture: str, partials: dict[str, bytes], grounds: dict[str, bytes],
           budget: int, planned_rebuilds: int) -> WitnessedEntityRollup:
    source = c1a.build_witnessed_entity_contexts(capture, max_bytes=budget)
    original = cas.read_verified_capture(capture)
    source_bytes = (len(original.evidence_bytes) + len(original.receipt_bytes)
                    + len(original.capture_index.canonical_bytes))
    if source_bytes * planned_rebuilds > MAX_SOURCE_WORK_BYTES:
        raise DailyWitnessedRollupError("rollup source-work byte budget exhausted")
    bundle = parse_evidence_bundle(original.evidence_bytes)
    if (source.capture_sha256 != capture or original.index_sha256 != capture
            or original.collector_storage_attested is not True
            or original.daily_publication_authorized is not False
            or source.evidence_sha256 != bundle.sha256
            or source.route_receipt_sha256 != original.capture_index.route_receipt_sha256
            or source.date != bundle.date
            or source.date != original.capture_index.date
            or source.collection_mode != original.capture_index.collection_mode):
        raise DailyWitnessedRollupError("source capture identity changed")
    chain = {
        "capture_sha256": capture, "route_receipt_sha256": source.route_receipt_sha256,
        "evidence_sha256": source.evidence_sha256, "closure_sha256": source.closure_sha256,
    }
    expected = {ctx.sha256 for ctx in source.contexts}
    if len(expected) != len(source.contexts) or set(partials) - expected:
        raise DailyWitnessedRollupError("foreign or repeated partial context identity")
    original_ids = tuple(event["evidence_id"] for event in bundle.events)
    defaults = tuple(event["evidence_id"] for event in bundle.events
                     if event["kind"] == "default_branch_commit")
    contextual_ids = tuple(eid for ctx in source.contexts for eid in ctx.evidence_ids)
    if (len(set(original_ids)) != len(original_ids)
            or len(set(contextual_ids)) != len(contextual_ids)
            or set(contextual_ids) & set(defaults)
            or set(contextual_ids) | set(defaults) != set(original_ids)
            or len(defaults) != source.unattributed_default_commit_count
            or len(original_ids) != source.original_event_count):
        raise DailyWitnessedRollupError("original source census is incomplete")

    entities: dict[tuple[str, str, int], dict] = {}
    used_grounds: set[str] = set()
    output_work = 0
    for ctx in source.contexts:
        ctx_sha = ctx.sha256
        key = _key(ctx)
        if (ctx.date != source.date or ctx.collection_mode != source.collection_mode
                or ctx.captured_at_utc != original.capture_index.captured_at_utc):
            raise DailyWitnessedRollupError("context capture time or mode changed")
        event_ids = tuple(ctx.evidence_ids)  # Only the freshly reconstructed Context.
        for name, value in chain.items():
            if getattr(ctx, name) != value:
                raise DailyWitnessedRollupError("context source chain changed")
        entity = entities.setdefault(key, {
            "entity": _identity(key), "context_records": [], "candidates": [],
            "source_evidence_ids": [], "supported": set(), "unsupported": set(),
            "unresolved": set(), "uncited": set(), "unprocessed": set(),
        })
        entity["source_evidence_ids"].extend(event_ids)
        raw = partials.get(ctx_sha)
        context_record = {
            "context_sha256": ctx_sha, "batch_index": ctx.batch_index,
            "partial_response_sha256": None if raw is None else _sha(raw),
            "state": "missing_partial" if raw is None else "partial_received",
        }
        entity["context_records"].append(context_record)
        if raw is None:
            entity["unprocessed"].update(event_ids)
            continue
        batch = c1b.normalize_witnessed_entity_candidates(ctx, raw, capture_sha256=capture)
        if (batch.context_sha256 != ctx_sha or batch.response_sha256 != _sha(raw)
                or (batch.repository, batch.entity_type, batch.number) != key
                or any(getattr(batch, name) != value for name, value in chain.items())):
            raise DailyWitnessedRollupError("candidate source chain changed")
        entity["uncited"].update(batch.uncited_evidence_ids)
        singletons = c2a.build_witnessed_grounding_contexts(ctx, raw, capture_sha256=capture)
        if len(singletons) != len(batch.candidates):
            raise DailyWitnessedRollupError("candidate/grounding census mismatch")
        for candidate, singleton in zip(batch.candidates, singletons):
            frozen = singleton.to_json_bytes()
            proof = json.loads(frozen)
            if (proof["partial_context_sha256"] != ctx_sha
                    or proof["partial_response_sha256"] != batch.response_sha256
                    or proof["candidate_sha256"] != candidate.candidate_sha256
                    or proof["summary"] != candidate.summary
                    or proof["evidence_ids"] != list(candidate.evidence_ids)
                    or proof["entity"] != _identity(key)
                    or any(proof[name] != value for name, value in chain.items())):
                raise DailyWitnessedRollupError("singleton source chain changed")
            ground_sha = _sha(frozen)
            if ground_sha in used_grounds:
                raise DailyWitnessedRollupError("duplicate regenerated singleton")
            used_grounds.add(ground_sha)
            verdict_raw = grounds.get(ground_sha)
            assessment_sha = response_sha = reason_sha = None
            status = "unresolved"
            if verdict_raw is not None:
                assessment = c2a.normalize_witnessed_grounding_verdict(
                    singleton, verdict_raw, partial_context=ctx,
                    partial_response_bytes=raw, capture_sha256=capture,
                )
                report = assessment.report()
                if (report["ground_context_sha256"] != ground_sha
                        or report["candidate_sha256"] != candidate.candidate_sha256
                        or report["partial_context_sha256"] != ctx_sha
                        or report["partial_response_sha256"] != batch.response_sha256
                        or report["raw_response_sha256"] != _sha(verdict_raw)
                        or report["evidence_ids"] != list(candidate.evidence_ids)
                        or report["entity"] != _identity(key)
                        or report["publication_authorized"] is not False
                        or any(report[name] != value for name, value in chain.items())):
                    raise DailyWitnessedRollupError("ground assessment identity changed")
                status = report["verdict"]
                if status not in ("supported", "unsupported"):
                    raise DailyWitnessedRollupError("unrecognized advisory verdict")
                assessment_sha = assessment.sha256
                response_sha = report["raw_response_sha256"]
                reason_sha = report["reason_sha256"]
            entity[status].update(candidate.evidence_ids)
            row = {
                "candidate_sha256": candidate.candidate_sha256,
                "partial_context_sha256": ctx_sha,
                "partial_response_sha256": batch.response_sha256,
                "ground_context_sha256": ground_sha,
                "ground_assessment_sha256": assessment_sha,
                "ground_response_sha256": response_sha,
                "reason_sha256": reason_sha,
                "verdict": status, "summary": candidate.summary,
                "evidence_ids": list(candidate.evidence_ids),
            }
            output_work += len(_canonical(row))
            if output_work > MAX_ROLLUP_BYTES:
                raise DailyWitnessedRollupError("private candidate rollup exceeds byte budget")
            entity["candidates"].append(row)
    if set(grounds) - used_grounds:
        raise DailyWitnessedRollupError("foreign or stale grounding reply identity")

    rows = []
    total_supported = total_unsupported = total_unresolved = 0
    for key in sorted(entities):
        entity = entities[key]
        ids = entity["source_evidence_ids"]
        supported, unsupported, unresolved = (entity[x] for x in (
            "supported", "unsupported", "unresolved"))
        uncited, unprocessed = entity["uncited"], entity["unprocessed"]
        cited = supported | unsupported | unresolved
        if (cited | uncited | unprocessed != set(ids)
                or cited & (uncited | unprocessed) or uncited & unprocessed):
            raise DailyWitnessedRollupError("entity coverage lost or invented source IDs")
        counts = {status: sum(row["verdict"] == status for row in entity["candidates"])
                  for status in ("supported", "unsupported", "unresolved")}
        missing = [record["context_sha256"] for record in entity["context_records"]
                   if record["state"] == "missing_partial"]
        rows.append({
            "entity": entity["entity"], "context_records": entity["context_records"],
            "candidates": entity["candidates"], "source_evidence_ids": ids,
            "supported_evidence_ids": [eid for eid in ids if eid in supported],
            "unsupported_evidence_ids": [eid for eid in ids if eid in unsupported],
            "unresolved_evidence_ids": [eid for eid in ids if eid in unresolved],
            "uncited_evidence_ids": [eid for eid in ids if eid in uncited],
            "unprocessed_evidence_ids": [eid for eid in ids if eid in unprocessed],
            "missing_partial_context_sha256s": missing,
            "candidate_counts": counts,
            "response_set_complete": not missing and counts["unresolved"] == 0,
            "semantic_coverage_review_required": True,
        })
        total_supported += counts["supported"]
        total_unsupported += counts["unsupported"]
        total_unresolved += counts["unresolved"]
    output = dict(chain)
    output.update({
        "record_version": RECORD_VERSION, "stage": STAGE,
        "date": source.date, "captured_at_utc": original.capture_index.captured_at_utc,
        "collection_mode": source.collection_mode, "max_context_bytes": budget,
        "original_event_count": len(original_ids),
        "grouped_event_count": len(contextual_ids),
        "unattributed_default_branch_commit_ids": list(defaults),
        "expected_context_count": len(expected), "received_partial_count": len(partials),
        "received_grounding_count": len(grounds), "entity_count": len(rows),
        "candidate_counts": dict(supported=total_supported, unsupported=total_unsupported,
                                 unresolved=total_unresolved),
        "response_set_complete": len(partials) == len(expected) and total_unresolved == 0,
        "entities": rows, "model_verdict_is_advisory": True,
        "requires_human_quality_review": True, "semantic_coverage_review_required": True,
        "publication_authorized": False,
    })
    result = WitnessedEntityRollup(_canonical(output))
    result.to_json_bytes()
    return result
