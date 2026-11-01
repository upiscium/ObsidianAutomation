"""Bounded, pure projection of GitHub pipeline producer transport receipts.

This module has NO filesystem, provider, systemd, Vault, or WebUI bindings.
Input is candidate producer metadata, not an authenticated producer identity:
the eventual read adapter must verify ownership/integrity before setting
source_attested=True. A historical producer readback is NOT proof of current
remote convergence or of complete Watcher -> Writer -> Compactor health.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping, Sequence

VERSION = "github-pipeline-evidence/v0"
MAX_RECEIPT_BYTES = 8192
MAX_RECEIPTS_PER_KIND = 2048
MAX_FUTURE_SKEW = timedelta(minutes=5)
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_STATUSES = frozenset(("planning", "running", "stable"))
_TARGETS = frozenset(("planning", "running"))
_FIELDS = {
    "status": frozenset((
        "record_version", "stage", "proposal_sha256", "project_path",
        "repository", "expected_status", "desired_status", "outcome",
        "before_content_sha256", "after_content_sha256", "completed_at",
    )),
    "overview": frozenset((
        "record_version", "stage", "proposal_sha256", "project_path",
        "status_path", "repository", "outcome", "before_content_sha256",
        "after_content_sha256", "completed_at",
    )),
}
_STAGES = {
    "status": "github_project_status_transport",
    "overview": "github_project_overview_transport",
}
_OUTCOMES = {
    "status": frozenset(("applied", "recovered", "already_desired")),
    "overview": frozenset(("created", "updated", "recovered", "already_desired")),
}


class InvalidEvidence(ValueError):
    """The entire affected projection must fail closed without raw content."""


@dataclass(frozen=True)
class ProducerReceipt:
    kind: str
    proposal_sha256: str
    completed_at: datetime
    outcome: str
    changed: bool
    file_identity: str


def _pairs(items: list[tuple[str, object]]) -> dict[str, object]:
    output: dict[str, object] = {}
    for key, value in items:
        if key in output:
            raise InvalidEvidence("invalid_receipt")
        output[key] = value
    return output


def _constant(_: str) -> None:
    raise InvalidEvidence("invalid_receipt")


def _sha(value: object) -> bool:
    return type(value) is str and _SHA.fullmatch(value) is not None


def _safe_path(value: object, *, status_note: bool = False) -> bool:
    if type(value) is not str or len(value) > 1024 or "\x00" in value or "\\" in value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    pieces = value.split("/")
    return (len(pieces) >= 2 and pieces[0] == "10-Project"
            and all(p not in ("", ".", "..") for p in pieces)
            and (pieces[-1] == "Status.md" if status_note
                 else pieces[-1].endswith(".md") and pieces[-1] != "Status.md"))


def _datetime(value: object) -> datetime:
    if type(value) is not str or not 1 <= len(value) <= 64:
        raise InvalidEvidence("invalid_receipt")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise InvalidEvidence("invalid_receipt") from None
    if parsed.tzinfo is None:
        raise InvalidEvidence("invalid_receipt")
    try:
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise InvalidEvidence("invalid_receipt") from None


def parse_producer_receipt(
    raw: bytes, *, kind: str, now: datetime,
) -> ProducerReceipt:
    """Strictly check the existing producer schema, without assigning trust."""
    if kind not in _FIELDS or type(raw) is not bytes or len(raw) > MAX_RECEIPT_BYTES:
        raise InvalidEvidence("invalid_receipt")
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("now must be timezone aware")
    try:
        row = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant,
        )
    except (ValueError, UnicodeError, RecursionError):
        raise InvalidEvidence("invalid_receipt") from None
    if type(row) is not dict or set(row) != _FIELDS[kind]:
        raise InvalidEvidence("invalid_receipt")
    if (type(row["record_version"]) is not int or row["record_version"] != 1
            or row["stage"] != _STAGES[kind]
            or not _sha(row["proposal_sha256"])
            or not _sha(row["after_content_sha256"])
            or not _safe_path(row["project_path"])
            or type(row["repository"]) is not str
            or _REPO.fullmatch(row["repository"]) is None
            or type(row["outcome"]) is not str
            or row["outcome"] not in _OUTCOMES[kind]):
        raise InvalidEvidence("invalid_receipt")
    if kind == "status":
        if (not _sha(row["before_content_sha256"])
                or type(row["expected_status"]) is not str
                or type(row["desired_status"]) is not str
                or row["expected_status"] not in _STATUSES
                or row["desired_status"] not in _TARGETS
                or row["expected_status"] == row["desired_status"]):
            raise InvalidEvidence("invalid_receipt")
    elif (not _safe_path(row["status_path"], status_note=True)
          or row["status_path"] != row["project_path"].rsplit("/", 1)[0] + "/Status.md"
          or (row["before_content_sha256"] is not None
              and not _sha(row["before_content_sha256"]))):
        raise InvalidEvidence("invalid_receipt")
    before = row["before_content_sha256"]
    after = row["after_content_sha256"]
    if (row["outcome"] == "already_desired" and before != after
            or row["outcome"] in ("applied", "updated", "recovered") and before == after
            or row["outcome"] in ("updated", "already_desired") and before is None
            or row["outcome"] == "created" and before is not None):
        raise InvalidEvidence("invalid_receipt")
    completed = _datetime(row["completed_at"])
    observed = now.astimezone(timezone.utc)
    if completed > observed + MAX_FUTURE_SKEW:
        raise InvalidEvidence("invalid_receipt")
    file_identity = (row["proposal_sha256"] if kind == "status" else
                     hashlib.sha256(row["project_path"].encode("utf-8")).hexdigest())
    return ProducerReceipt(
        kind=kind, proposal_sha256=row["proposal_sha256"],
        completed_at=completed, outcome=row["outcome"], changed=before != after,
        file_identity=file_identity,
    )


def summarize_candidate_receipts(
    records: Mapping[str, Sequence[bytes]], *, now: datetime,
    source_attested: bool = False, fresh_seconds: int = 86400,
) -> dict[str, object]:
    """Privacy-safe candidate read model. No overall pipeline success inferred.

    source_attested asserts independent read-adapter verification; merely finding
    a JSON file is not such verification. Never expose paths, repository names,
    raw messages, SHA values, or credentials to clients.
    """
    if (type(records) is not dict or set(records) != set(_FIELDS)
            or type(source_attested) is not bool
            or type(fresh_seconds) is not int or not 1 <= fresh_seconds <= 604800
            or not isinstance(now, datetime) or now.tzinfo is None):
        raise ValueError("invalid projection arguments")
    observed = now.astimezone(timezone.utc)
    projected: dict[str, dict[str, object]] = {}
    for kind in ("status", "overview"):
        values = records[kind]
        if type(values) not in (tuple, list) or len(values) > MAX_RECEIPTS_PER_KIND:
            projected[kind] = _empty("invalid")
            continue
        try:
            parsed = [parse_producer_receipt(v, kind=kind, now=now) for v in values]
            same_sha: dict[str, bytes] = {}
            for item, raw in zip(parsed, values):
                old = same_sha.get(item.file_identity)
                if old is not None and old != raw:
                    raise InvalidEvidence("invalid_receipt")
                same_sha[item.file_identity] = raw
        except InvalidEvidence:
            projected[kind] = _empty("invalid")
            continue
        if not parsed:
            projected[kind] = _empty("missing")
            continue
        latest = max(parsed, key=lambda i: (i.completed_at, i.proposal_sha256))
        age = observed - latest.completed_at
        projected[kind] = {
            "evidence_state": "producer_readback" if source_attested else "untrusted",
            "last_producer_readback_at": (
                latest.completed_at.isoformat().replace("+00:00", "Z")
                if source_attested else None
            ),
            "last_outcome": latest.outcome if source_attested else None,
            "freshness": ("future_skew" if age < timedelta(0) else
                          "fresh" if age <= timedelta(seconds=fresh_seconds) else
                          "stale") if source_attested else "unknown",
        }
    return {
        "schema_version": VERSION,
        "generated_at": observed.isoformat().replace("+00:00", "Z"),
        "status": projected["status"],
        "overview": projected["overview"],
        "last_attempt": None,
        "last_verified_pipeline_success": None,
        "active_incident": "unknown",
        "overall_health": "unknown",
    }


def _empty(reason: str) -> dict[str, object]:
    return {
        "evidence_state": reason, "last_producer_readback_at": None,
        "last_outcome": None, "freshness": "unknown",
    }
