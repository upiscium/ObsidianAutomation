"""Pure B3 Writer readback *consistency* contract, not a success attestation.

This source-only module correlates two independent *candidate* canonical
readbacks with one already-admitted B3 attempt. It never fetches from
Nextcloud, claims provenance for caller-provided bytes, mutates a job/ledger,
or certifies that two historical reads were simultaneously current.

Only the future #483 Job's trusted I/O producer can bind actual remote
observations to this contract and persist them under its own authority.
The existing v2 B3AttemptJournal remains the sole local attempt store;
this module MUST NOT be used as a second ledger or as WebUI proof of a
verified whole-pipeline success.
"""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from typing import Literal, Sequence

VERSION = "github-b3-readback-candidate/v1"
MAX_CONTENT_BYTES = 2 * 1024 * 1024
MAX_FUTURE_SKEW_SECONDS = 300
_ID = re.compile(r"[0-9a-f]{32}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_STAGES = frozenset(("status", "overview"))
_OUTCOMES = {
    "status": frozenset(("applied", "recovered", "already_desired")),
    "overview": frozenset(("created", "updated", "recovered", "already_desired")),
}


@dataclass(frozen=True, slots=True)
class RunContext:
    """Already-admitted *private* v2 Job identity, not a public request body."""

    run_id: str
    binding_ref: str
    expected_revision: str
    started_at: float
    state: Literal["running", "unconfirmed"] = "running"


@dataclass(frozen=True, slots=True)
class StageReadback:
    """Private producer-candidate payload; observed must come from a real GET.

    Merely supplying bytes with this shape does not establish a remote origin.
    Never return this object or its digest and binding attributes to browsers.
    """

    stage: str
    run_id: str
    binding_ref: str
    expected_revision: str
    # SHA-256 of the canonical *proposal* payload, not Markdown bytes.
    # Authentic binding to the producer's proposal is a future I/O duty.
    proposal_sha256: str
    outcome: str
    before_sha256: str | None
    expected_bytes: bytes
    observed_bytes: bytes
    completed_at: float


class InvalidCandidate(ValueError):
    """Stable finite, nonsecret errors; never print caller bytes or paths."""

    def __init__(self, code: str = "invalid_candidate") -> None:
        self.code = code
        super().__init__(code)


def _id(value: object) -> bool:
    return type(value) is str and _ID.fullmatch(value) is not None


def _sha(value: object) -> bool:
    return type(value) is str and _SHA.fullmatch(value) is not None


def _seconds(value: object) -> bool:
    if type(value) not in (float, int):
        return False
    try:
        return math.isfinite(value) and 0 <= value <= 253402300799
    except (TypeError, ValueError, OverflowError):
        return False


def _valid_context(value: object) -> bool:
    return (
        type(value) is RunContext
        and all(_id(x) for x in (
            value.run_id, value.binding_ref, value.expected_revision,
        ))
        and _seconds(value.started_at)
        and type(value.state) is str
        and value.state in ("running", "unconfirmed")
    )


def _valid_stage(
    item: object, *, context: RunContext, now: float,
) -> bool:
    if type(item) is not StageReadback:
        return False
    if (
        type(item.stage) is not str or item.stage not in _STAGES
        or not all(_id(x) for x in (
            item.run_id, item.binding_ref, item.expected_revision,
        ))
        or item.run_id != context.run_id
        or item.binding_ref != context.binding_ref
        or item.expected_revision != context.expected_revision
        or not _sha(item.proposal_sha256)
        or type(item.outcome) is not str
        or item.outcome not in _OUTCOMES[item.stage]
        or (item.before_sha256 is not None and not _sha(item.before_sha256))
        or type(item.expected_bytes) is not bytes
        or type(item.observed_bytes) is not bytes
        or not 1 <= len(item.expected_bytes) <= MAX_CONTENT_BYTES
        or not 1 <= len(item.observed_bytes) <= MAX_CONTENT_BYTES
        or not _seconds(item.completed_at)
        or item.completed_at < context.started_at
        or item.completed_at > now + MAX_FUTURE_SKEW_SECONDS
        or item.expected_bytes != item.observed_bytes
    ):
        return False

    after = hashlib.sha256(item.observed_bytes).hexdigest()
    if item.outcome == "already_desired":
        return item.before_sha256 == after
    if item.outcome == "created":
        return item.stage == "overview" and item.before_sha256 is None
    return (
        item.before_sha256 != after
        and (item.before_sha256 is not None or (
            item.stage == "overview" and item.outcome == "recovered"
        ))
    )


def evaluate_b3_readbacks(
    context: RunContext,
    evidence: Sequence[StageReadback],
    *,
    now: float,
) -> dict[str, object]:
    """Check *consistency only*; never grant provenance or clear an incident.

    Passing fabricated but internally consistent bytes is insufficient for
    remote verification. This contract has no source_attested switch: that
    authority belongs exclusively to a later trusted I/O producer that also
    persists its own exact run-bound remote readback receipts.

    last_verified_pipeline_success intentionally remains None even for a
    consistent pair: Watcher, Mirror, Core Promotion, complete current
    convergence and timestamp overlap are outside this evidence class.
    """
    if not _valid_context(context) or not _seconds(now):
        raise InvalidCandidate("invalid_context")

    result: dict[str, object] = {
        "schema_version": VERSION,
        "evidence_scope": "private_b3_writer_candidate_only",
        "consistency": "unknown",
        "reason": "missing",
        "candidate_outcomes": {"status": None, "overview": None},
        "candidate_changed": None,
        "last_verified_pipeline_success": None,
        "overall_health": "unknown",
        "active_incident": "unknown",
    }
    if type(evidence) not in (tuple, list):
        result["reason"] = "invalid"
        return result
    if not evidence:
        return result
    if len(evidence) != 2:
        result["reason"] = "incomplete" if len(evidence) == 1 else "invalid"
        return result
    if (
        any(not _valid_stage(item, context=context, now=now) for item in evidence)
        or {item.stage for item in evidence} != _STAGES
    ):
        result["reason"] = "invalid"
        return result

    by_stage = {item.stage: item for item in evidence}
    changes = [
        None if item.outcome == "recovered" and item.before_sha256 is None
        else item.outcome != "already_desired"
        for item in evidence
    ]
    # A known changed stage establishes that at least one stage changed;
    # otherwise a missing before-state makes total change unknowable.
    changed = True if True in changes else None if None in changes else False
    result.update({
        "consistency": "candidate_consistent",
        "reason": None,
        "candidate_outcomes": {
            stage: by_stage[stage].outcome for stage in ("status", "overview")
        },
        "candidate_changed": changed,
    })
    return result
