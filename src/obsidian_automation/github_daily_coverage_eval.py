"""Read-only, metadata-only Daily claim-coverage diagnostic (#290).

This module never runs models or writes files. Callers must first validate
immutable original Evidence, normalized candidate Output/Provenance, and
Grounding results. Human verdicts are not inferred from model advisories.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
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
    if flattened != tuple(event_repos):
        raise CoverageError("partition dropped, duplicated or reordered evidence")

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
