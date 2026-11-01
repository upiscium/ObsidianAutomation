"""Bounded Project Status terminal-result history from EXISTING producer artifacts.

Status Writer results and rejections are immutable terminal observations, not
complete attempt logs. A producer readback is a historical exact-byte check;
neither it nor a newer unrelated Project success establishes current Vault
convergence, incident resolution, or the health of a full GitHub pipeline.

No I/O, durable ledger, WebUI/router changes, providers or credential access.
The fixed-path reader must independently attest producer identity/ownership
before invoking this projection with source_attested=True.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping, Sequence

from .github_pipeline_evidence import (
    MAX_FUTURE_SKEW,
    MAX_RECEIPT_BYTES,
    MAX_RECEIPTS_PER_KIND,
    InvalidEvidence,
    _constant,
    _datetime,
    _pairs,
    _safe_path,
    _sha,
    parse_producer_receipt,
)

VERSION = "github-project-status-history/v1"
_KEYS = frozenset(("results", "rejections"))
_REJECTION_REQUIRED = frozenset((
    "record_version", "stage", "proposal_sha256", "project",
    "outcome", "reason", "completed_at",
))
_REJECTION_OPTIONAL = "http_status"
_REJECTION_STAGE = "github_project_status_transport"
_AMBIGUOUS_RESPONSE_CODES = frozenset((500, 502, 503, 504))


@dataclass(frozen=True)
class TerminalEvidence:
    proposal_sha256: str
    completed_at: datetime
    outcome: str
    category: str
    reason_code: str | None = None
    http_status: int | None = None


def parse_status_rejection(raw: bytes, *, now: datetime) -> TerminalEvidence:
    """Parse the existing github_project_status_worker rejection file exactly.

    Unknown outcomes/reasons, inconsistent HTTP status, and untrusted strings
    are invalid, not arbitrary client-visible text.
    """
    if type(raw) is not bytes or not 1 <= len(raw) <= MAX_RECEIPT_BYTES:
        raise InvalidEvidence("invalid_rejection")
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("now must be timezone aware")
    try:
        row = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant,
        )
    except (ValueError, UnicodeError, RecursionError):
        raise InvalidEvidence("invalid_rejection") from None
    if type(row) is not dict or set(row) not in (
        _REJECTION_REQUIRED, _REJECTION_REQUIRED | {_REJECTION_OPTIONAL},
    ):
        raise InvalidEvidence("invalid_rejection")
    if (
        type(row["record_version"]) is not int or row["record_version"] != 1
        or row["stage"] != _REJECTION_STAGE
        or not _sha(row["proposal_sha256"])
        or not _safe_path(row["project"])
        or type(row["outcome"]) is not str
        or type(row["reason"]) is not str
    ):
        raise InvalidEvidence("invalid_rejection")
    outcome = row["outcome"]
    reason = row["reason"]
    status = row.get("http_status")
    if _REJECTION_OPTIONAL in row and (type(status) is not int or not 100 <= status <= 599):
        raise InvalidEvidence("invalid_rejection")
    if outcome == "rejected_conflict":
        if (reason, status) not in (
            ("canonical_conflict", None), ("etag_cas_conflict", 412),
        ):
            raise InvalidEvidence("invalid_rejection")
        category = "cas_conflict"
    elif outcome == "rejected_transport":
        if status is None:
            raise InvalidEvidence("invalid_rejection")
        if reason == "authority_rejection" and status in (401, 403):
            category = "authority_rejected"
        elif (reason == "http_client_rejection" and 400 <= status < 500
              and status not in (401, 403, 412)):
            category = "client_rejected"
        elif (reason == "http_response_rejection"
              and not 200 <= status < 300
              and not 400 <= status < 500
              and status not in _AMBIGUOUS_RESPONSE_CODES):
            category = "http_rejected"
        else:
            raise InvalidEvidence("invalid_rejection")
    else:
        raise InvalidEvidence("invalid_rejection")
    completed = _datetime(row["completed_at"])
    if completed - now.astimezone(timezone.utc) > MAX_FUTURE_SKEW:
        raise InvalidEvidence("invalid_rejection")
    return TerminalEvidence(
        proposal_sha256=row["proposal_sha256"], completed_at=completed,
        outcome=outcome, category=category,
        reason_code=reason, http_status=status,
    )


def parse_status_success(raw: bytes, *, now: datetime) -> TerminalEvidence:
    """Reuse v0 exact producer-result schema, never broaden its trust."""
    receipt = parse_producer_receipt(raw, kind="status", now=now)
    return TerminalEvidence(
        proposal_sha256=receipt.proposal_sha256,
        completed_at=receipt.completed_at,
        outcome=receipt.outcome,
        category="verified_producer_readback",
    )


def _at(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _terminal_view(item: TerminalEvidence) -> dict[str, object]:
    return {
        "completed_at": _at(item.completed_at),
        "outcome": item.outcome,
        "category": item.category,
        "reason_code": item.reason_code,
        "http_status": item.http_status,
    }


def _pick_latest(items: list[TerminalEvidence]) -> dict[str, object] | None:
    if not items:
        return None
    when = max(item.completed_at for item in items)
    latest = [item for item in items if item.completed_at == when]
    if len(latest) != 1:
        # An arbitrary SHA ordering must not invent causal order.
        return {
            "completed_at": _at(when),
            "outcome": None,
            "category": "ambiguous_order",
            "reason_code": None,
            "http_status": None,
        }
    return _terminal_view(latest[0])


def summarize_status_history(
    records: Mapping[str, Sequence[bytes]], *, now: datetime,
    source_attested: bool = False, fresh_seconds: int = 86400,
) -> dict[str, object]:
    """Summarize complete bounded candidate terminal artifacts, NOT attempts.

    Attestation is a capability asserted by an independent fixed producer
    reader, never inferred from data validity. The caller must refuse incomplete
    directory listings, untrusted files, or tampered inputs before setting True.
    """
    if (
        type(records) is not dict or set(records) != _KEYS
        or type(source_attested) is not bool
        or type(fresh_seconds) is not int or not 1 <= fresh_seconds <= 604800
        or not isinstance(now, datetime) or now.tzinfo is None
    ):
        raise ValueError("invalid_history_arguments")
    observed = now.astimezone(timezone.utc)
    result: dict[str, object] = {
        "schema_version": VERSION,
        "generated_at": _at(observed),
        "evidence_state": "unknown",
        "history_completeness": "terminal_only",
        "last_terminal_observation": None,
        "last_verified_status_readback": None,
        "last_status_rejection": None,
        "last_attempt": None,
        "active_incident": "unknown",
        "retrying": "unknown",
        "overall_health": "unknown",
        "last_verified_pipeline_success": None,
        "freshness": "unknown",
    }
    streams = (records["results"], records["rejections"])
    if any(type(v) not in (tuple, list) for v in streams):
        result["evidence_state"] = "invalid"
        return result
    if sum(len(v) for v in streams) > MAX_RECEIPTS_PER_KIND:
        result["evidence_state"] = "capacity_exceeded"
        return result
    try:
        terminals: list[TerminalEvidence] = []
        seen: dict[str, bytes] = {}
        for kind, raw_values in zip(("results", "rejections"), streams):
            parse = parse_status_success if kind == "results" else parse_status_rejection
            for raw in raw_values:
                item = parse(raw, now=now)
                if item.proposal_sha256 in seen:
                    if seen[item.proposal_sha256] != raw:
                        raise InvalidEvidence("conflicting_terminal")
                    # Identical repeated records are idempotent.
                    continue
                seen[item.proposal_sha256] = raw
                terminals.append(item)
    except InvalidEvidence:
        result["evidence_state"] = "invalid"
        return result
    if not terminals:
        result["evidence_state"] = "missing" if source_attested else "untrusted"
        return result
    if not source_attested:
        result["evidence_state"] = "untrusted"
        return result
    result["evidence_state"] = "producer_attested_terminal_history"
    success = [i for i in terminals if i.category == "verified_producer_readback"]
    rejected = [i for i in terminals if i.category != "verified_producer_readback"]
    latest = max(item.completed_at for item in terminals)
    age = observed - latest
    result.update({
        "last_terminal_observation": _pick_latest(terminals),
        "last_verified_status_readback": _pick_latest(success),
        "last_status_rejection": _pick_latest(rejected),
        "freshness": ("future_skew" if age < timedelta(0) else
                      "fresh" if age <= timedelta(seconds=fresh_seconds) else
                      "stale"),
    })
    return result
