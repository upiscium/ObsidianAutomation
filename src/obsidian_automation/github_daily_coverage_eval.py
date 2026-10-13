"""Read-only, metadata-only Daily claim-coverage diagnostic (#290).

This module never runs models or writes files. Callers must first validate
immutable original Evidence, normalized candidate Output/Provenance, and
Grounding results. Human verdicts are not inferred from model advisories.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Sequence

SHA_RE = re.compile(r"^[a-f0-9]{64}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

Evaluator = Literal["human", "model"]
Verdict = Literal["entailed", "missing", "uncertain"]


class CoverageError(ValueError):
    """Invalid identity/provenance relation; no quality verdict can be trusted."""


@dataclass(frozen=True)
class OriginalEvent:
    evidence_id: str
    repository: str


@dataclass(frozen=True)
class BatchedClaim:
    claim_id: str
    repository: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class SupportedSingleClaim:
    claim_id: str
    repository: str
    evidence_id: str
    grounding_sha256: str


@dataclass(frozen=True)
class Adjudication:
    claim_id: str
    evaluator: Evaluator
    verdict: Verdict


@dataclass(frozen=True)
class CoverageInput:
    evidence_bundle_sha256: str
    partition_sha256: str
    batch_output_sha256: str
    original_events: tuple[OriginalEvent, ...]
    partitioned_ids: tuple[tuple[str, ...], ...]
    batched_claims: tuple[BatchedClaim, ...]
    supported_single_claims: tuple[SupportedSingleClaim, ...]
    adjudications: tuple[Adjudication, ...]


def _digest(value: str, label: str) -> None:
    if not isinstance(value, str) or not SHA_RE.fullmatch(value):
        raise CoverageError(f"{label} must be SHA-256")


def _repo(value: str) -> None:
    if not isinstance(value, str) or not REPO_RE.fullmatch(value):
        raise CoverageError("repository identity invalid")


def evaluate_coverage(inp: CoverageInput) -> dict[str, object]:
    """Report structural citation coverage and *separately* human semantic labels.

    Never treats source_ref coverage, advisory judgments, or a sampled quality
    report as authorization to publish a Daily Note.
    """
    _digest(inp.evidence_bundle_sha256, "evidence_bundle_sha256")
    _digest(inp.partition_sha256, "partition_sha256")
    _digest(inp.batch_output_sha256, "batch_output_sha256")

    event_repos: dict[str, str] = {}
    for event in inp.original_events:
        _digest(event.evidence_id, "evidence_id")
        _repo(event.repository)
        if event.evidence_id in event_repos:
            raise CoverageError("duplicate original evidence identity")
        event_repos[event.evidence_id] = event.repository

    flattened = tuple(eid for partition in inp.partitioned_ids for eid in partition)
    if not inp.partitioned_ids or any(not part for part in inp.partitioned_ids):
        raise CoverageError("empty or absent partition")
    if (len(flattened) != len(event_repos)
            or len(set(flattened)) != len(flattened)
            or set(flattened) != set(event_repos)):
        raise CoverageError("partition dropped or duplicated evidence")
    # The production partitioner groups by repository, so globally interleaved
    # original events may legitimately become consecutive per-repository groups.
    # Preserve exact *per-repository* sequence, and prohibit mixed-repo batches.
    for part in inp.partitioned_ids:
        if len({event_repos[eid] for eid in part}) != 1:
            raise CoverageError("partition mixes repositories")
    for repository in set(event_repos.values()):
        original = tuple(eid for eid, repo in event_repos.items() if repo == repository)
        observed = tuple(eid for eid in flattened if event_repos[eid] == repository)
        if observed != original:
            raise CoverageError("partition reordered evidence within repository")

    cited: set[str] = set()
    claim_ids: set[str] = set()
    for claim in inp.batched_claims:
        _digest(claim.claim_id, "claim_id")
        _repo(claim.repository)
        if claim.claim_id in claim_ids:
            raise CoverageError("duplicate batched claim")
        claim_ids.add(claim.claim_id)
        if not 1 <= len(claim.evidence_ids) <= 8:
            raise CoverageError("batched claim citation count invalid")
        if len(set(claim.evidence_ids)) != len(claim.evidence_ids):
            raise CoverageError("duplicate batched claim citation")
        for eid in claim.evidence_ids:
            if eid not in event_repos or event_repos[eid] != claim.repository:
                raise CoverageError("unknown or cross-repository citation")
            cited.add(eid)

    supported_ids: set[str] = set()
    for claim in inp.supported_single_claims:
        _digest(claim.claim_id, "standalone claim_id")
        _digest(claim.grounding_sha256, "grounding_sha256")
        _repo(claim.repository)
        if claim.claim_id in supported_ids:
            raise CoverageError("duplicate supported standalone claim")
        supported_ids.add(claim.claim_id)
        if event_repos.get(claim.evidence_id) != claim.repository:
            raise CoverageError("standalone claim is not bound to original repository")
        # The external caller supplies proof of Grounding support by digest;
        # this pure report does not read/verify Grounding artifact bytes.

    decisions: dict[tuple[str, str], str] = {}
    for label in inp.adjudications:
        if label.claim_id not in supported_ids:
            raise CoverageError("adjudication for unknown supported claim")
        if label.evaluator not in ("human", "model"):
            raise CoverageError("unknown adjudication authority")
        if label.verdict not in ("entailed", "missing", "uncertain"):
            raise CoverageError("unknown semantic verdict")
        key = (label.claim_id, label.evaluator)
        if key in decisions:
            raise CoverageError("conflicting or duplicate adjudication")
        decisions[key] = label.verdict

    human_counts = {"entailed": 0, "missing": 0, "uncertain": 0, "unreviewed": 0}
    for claim in inp.supported_single_claims:
        human_counts[decisions.get((claim.claim_id, "human"), "unreviewed")] += 1
    advisory_missing = sum(
        1 for (cid, evaluator), verdict in decisions.items()
        if evaluator == "model" and verdict == "missing"
    )
    if not inp.supported_single_claims:
        sample_status = "no_supported_single_claim_baseline"
    elif human_counts["missing"]:
        sample_status = "human_confirmed_omission"
    elif human_counts["uncertain"] or human_counts["unreviewed"]:
        sample_status = "insufficient_human_review"
    else:
        sample_status = "no_omission_in_reviewed_sample"

    # No raw source/claim text or unvalidated repo names in the diagnostic.
    return {
        "record_version": 1,
        "evidence_bundle_sha256": inp.evidence_bundle_sha256,
        "partition_sha256": inp.partition_sha256,
        "batch_output_sha256": inp.batch_output_sha256,
        "original_event_count": len(event_repos),
        "partitioned_event_count": len(flattened),
        "directly_cited_event_count": len(cited),
        "uncited_event_count": len(event_repos) - len(cited),
        "supported_single_claim_count": len(supported_ids),
        "supported_uncited_single_claim_count": sum(
            claim.evidence_id not in cited for claim in inp.supported_single_claims
        ),
        "human_semantic_counts": human_counts,
        "model_advisory_missing_count": advisory_missing,
        "sample_status": sample_status,
        "publication_authorized": False,
    }


MAX_EVIDENCE_INPUT_BYTES = 16 * 1024 * 1024


def _verify_collector_event_shape(event: dict[str, object]) -> None:
    """Check original Evidence uses the producer's complete event format.

    A self-hashed object with just repository/evidence_id must not be treated
    as an authentic producer-shaped event in the structural report. This
    verifies structure only; it does NOT cryptographically sign collector
    provenance or assert the reported GitHub facts are truthful.
    """
    from datetime import datetime, timedelta
    from .github_daily_activity import EvidenceEvent, _ENTITY_TYPES, _EVENT_KINDS

    if set(event) != set(EvidenceEvent.__dataclass_fields__):
        raise CoverageError("original Evidence event fields do not match Collector schema")
    if (
        not isinstance(event["kind"], str) or event["kind"] not in _EVENT_KINDS
        or not isinstance(event["entity_type"], str)
        or event["entity_type"] not in _ENTITY_TYPES
        or not isinstance(event["url"], str) or not event["url"]
        or not isinstance(event["source_id"], str) or not event["source_id"]
        or event["actor"] is not None and not isinstance(event["actor"], str)
        or event["sha"] is not None and not isinstance(event["sha"], str)
        or event["state"] is not None and not isinstance(event["state"], str)
        or event["draft"] is not None and type(event["draft"]) is not bool
        or event["number"] is not None
        and (type(event["number"]) is not int or event["number"] < 1)
    ):
        raise CoverageError("original Evidence event has invalid Collector field types")
    timestamp = event["occurred_at"]
    if not isinstance(timestamp, str) or not timestamp.endswith("Z"):
        raise CoverageError("original Evidence event timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CoverageError("original Evidence event timestamp is invalid") from exc
    if (
        parsed.utcoffset() != timedelta(0)
        or parsed.isoformat().replace("+00:00", "Z") != timestamp
    ):
        raise CoverageError("original Evidence event timestamp is not canonical")
    for label in ("title", "body", "message"):
        excerpt = event[label]
        if excerpt is None:
            continue
        if (
            not isinstance(excerpt, dict)
            or set(excerpt) != {"text", "truncated", "original_bytes", "included_bytes"}
            or not isinstance(excerpt["text"], str)
            or type(excerpt["truncated"]) is not bool
            or type(excerpt["original_bytes"]) is not int
            or type(excerpt["included_bytes"]) is not int
            or not 0 <= excerpt["included_bytes"] <= excerpt["original_bytes"]
        ):
            raise CoverageError("original Evidence text excerpt has invalid Collector shape")
        try:
            included = len(excerpt["text"].encode("utf-8"))
        except UnicodeError as exc:
            raise CoverageError("original Evidence text excerpt cannot be encoded") from exc
        if (
            included != excerpt["included_bytes"]
            or (not excerpt["truncated"] and included != excerpt["original_bytes"])
            or (excerpt["truncated"] and included >= excerpt["original_bytes"])
        ):
            raise CoverageError("original Evidence text excerpt byte counts mismatch")


def verified_partition_report(
    evidence_file: Path, *, max_partial_context_bytes: int,
) -> dict[str, object]:
    """Validate one immutable Evidence file and derive metadata-only partitions.

    Unlike evaluate_coverage(), this is an artifact-backed structural report.
    It does NOT accept batched model output, adjudicate semantic omissions,
    invoke Ollama, or authorize a Daily Writer.
    """
    if (not isinstance(evidence_file, Path) or not evidence_file.is_absolute()
            or ".." in evidence_file.parts):
        raise CoverageError("canonical Evidence path must be absolute")
    if (type(max_partial_context_bytes) is not int
            or not 1 <= max_partial_context_bytes <= MAX_EVIDENCE_INPUT_BYTES):
        raise CoverageError("Partial context limit is invalid")
    # One fd, one inode admission: never lstat then reopen the path through
    # load_evidence_bundle(), whose generic reader has no O_NONBLOCK/byte cap.
    # A rename to a FIFO between those two operations could otherwise hang.
    from .artifact_lifecycle import (
        ArtifactLifecycleError, _canonical_json_bytes,
        _decode_json_object, sha256_bytes,
    )
    from .github_daily_resume import _read_bounded
    from .github_daily_summary import parse_evidence_bundle, partition_evidence

    suffix = ".github-daily-evidence.json"
    if not evidence_file.name.endswith(suffix):
        raise CoverageError("canonical Evidence filename is invalid")
    try:
        data = _read_bounded(evidence_file, max_bytes=MAX_EVIDENCE_INPUT_BYTES)
    except (ArtifactLifecycleError, OSError) as exc:
        raise CoverageError("canonical Evidence inode is not safely bounded") from exc
    if not data:
        raise CoverageError("canonical Evidence inode is not safely bounded")
    # The producer emits exact sorted compact JSON with one newline. A
    # different serialization may be self-hashed, but is not canonical
    # original Collector Evidence and must not be reported as such.
    try:
        decoded = _decode_json_object(data, label="canonical Evidence")
        if _canonical_json_bytes(decoded) != data:
            raise CoverageError("original Evidence JSON is not canonical")
    except (ArtifactLifecycleError, UnicodeError) as exc:
        raise CoverageError("original Evidence JSON is not canonical") from exc

    # Reusing the production parser checks event self-hashes, repository
    # closure and immutable SHA identity, without reopening the path. A
    # second structural gate requires every event's complete Collector shape.
    bundle = parse_evidence_bundle(data)
    if evidence_file.name[:-len(suffix)] != bundle.sha256:
        raise CoverageError("canonical Evidence filename does not match content SHA")
    for event in bundle.events:
        _verify_collector_event_shape(dict(event))
    contexts = partition_evidence(bundle, max_bytes=max_partial_context_bytes)

    original_repos: dict[str, list[str]] = {}
    for event in bundle.events:
        original_repos.setdefault(str(event["repository"]), []).append(
            str(event["evidence_id"])
        )
    projected_repos: dict[str, list[str]] = {}
    for context in contexts:
        for event in context.events:
            projected_repos.setdefault(str(event["repository"]), []).append(
                str(event["evidence_id"])
            )
    if original_repos != projected_repos:
        raise CoverageError("partition changed repository Evidence identity/order")

    context_ids = [sha256_bytes(context.to_json_bytes()) for context in contexts]
    partition_sha = sha256_bytes(_canonical_json_bytes({
        "record_version": 1,
        "evidence_bundle_sha256": bundle.sha256,
        "max_partial_context_bytes": max_partial_context_bytes,
        "context_sha256s": context_ids,
    }))
    return {
        "record_version": 1,
        "stage": "read_only_partition_metadata",
        "evidence_bundle_sha256": bundle.sha256,
        "partition_sha256": partition_sha,
        "original_event_count": len(bundle.events),
        "partitioned_event_count": sum(len(context.events) for context in contexts),
        "repository_count": len(original_repos),
        "partial_context_count": len(contexts),
        "max_partial_context_bytes": max(
            (len(context.to_json_bytes()) for context in contexts), default=0
        ),
        "source_text_returned": False,
        "publication_authorized": False,
    }
