"""Nonpublishing, CAS-backed source-route closure for Daily v2 (#342 C0).

Only the trusted, fixed-namespace CAS reader may supply source bytes. A caller
passes ONE capture digest, never a VerifiedCapture object, arbitrary path,
model output, Receipt or Evidence bytes. The result is a read-only model, not
a credential, producer signature, or permission for a downstream Daily effect.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Mapping

from . import github_daily_activity as activity
from . import github_daily_route_capture_index as capture_index
from . import github_daily_route_cas as cas
from . import github_daily_source_route as source
from .artifact_lifecycle import ArtifactLifecycleError
from .github_daily_summary import GitHubDailySummaryError, parse_evidence_bundle

RECORD_VERSION = 1
MAX_CLOSURE_BYTES = 2 * 1024 * 1024


class DailySourceClosureError(ValueError):
    """Trusted local route identity is missing, changed or unverified."""


@dataclass(frozen=True)
class SourceRouteBinding:
    """One precisely witnessed route, not free-text GitHub progress."""

    evidence_id: str
    source_kind: str
    repository: str
    entity_type: str
    number: int
    source_id: str
    occurred_at: str
    verified_api_path: str
    entity_api_path: str
    remote_id: str

    def to_json(self) -> dict[str, object]:
        return {
            "evidence_id": self.evidence_id,
            "source_kind": self.source_kind,
            "repository": self.repository,
            "entity_type": self.entity_type,
            "number": self.number,
            "source_id": self.source_id,
            "occurred_at": self.occurred_at,
            "verified_api_path": self.verified_api_path,
            "entity_api_path": self.entity_api_path,
            "remote_id": self.remote_id,
        }


@dataclass(frozen=True)
class VerifiedSourceClosure:
    """Derived private read model. Not a transferable attestation capability.

    C1 must re-open trusted CAS using capture_sha256, not accept an arbitrary
    in-memory instance or its self-hash as a source-of-truth.
    """

    sha256: str
    canonical_bytes: bytes
    capture_sha256: str
    evidence_sha256: str
    route_receipt_sha256: str
    date: str
    captured_at_utc: str
    collection_mode: str
    event_count: int
    witnessed_count: int
    unattributed_default_commit_count: int
    other_unwitnessed_event_count: int
    bindings: tuple[SourceRouteBinding, ...]
    publication_authorized: bool = False
    requires_human_quality_review: bool = True


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _closure_from_trusted_cas(digest: str) -> VerifiedSourceClosure:
    # This function intentionally does not accept VerifiedCapture from its
    # caller. The reader is responsible for real process UID, pinned path, ACL,
    # regular-file, single-link, length and content-hash checks.
    stored = cas.read_verified_capture(digest)
    if (
        type(stored) is not cas.VerifiedCapture
        or stored.index_sha256 != digest
        or stored.collector_storage_attested is not True
        or stored.daily_publication_authorized is not False
        or type(stored.evidence_bytes) is not bytes
        or type(stored.receipt_bytes) is not bytes
        or type(stored.capture_index) is not capture_index.CaptureIndex
    ):
        raise DailySourceClosureError("CAS source capture is not admitted")

    index = stored.capture_index
    if (
        index.sha256 != digest
        or _sha(index.canonical_bytes) != digest
        or _sha(stored.evidence_bytes) != index.evidence_sha256
        or _sha(stored.receipt_bytes) != index.route_receipt_sha256
    ):
        raise DailySourceClosureError("CAS source identities changed")

    # Verify source routes again from the bytes: stored.routes is an ordinary
    # mutable Python structure and is NEVER independent provenance by itself.
    reconstructed = capture_index.parse_capture_index(
        index.canonical_bytes, stored.evidence_bytes, stored.receipt_bytes,
    )
    routes = source.parse_verified_routes(
        stored.receipt_bytes, stored.evidence_bytes,
    )
    if reconstructed != index or len(routes) != index.route_count:
        raise DailySourceClosureError("CAS source receipt has changed")

    evidence = parse_evidence_bundle(stored.evidence_bytes)
    if evidence.sha256 != index.evidence_sha256 or evidence.date != index.date:
        raise DailySourceClosureError("CAS original Evidence differs")
    by_id: dict[str, Mapping[str, object]] = {
        str(event["evidence_id"]): event for event in evidence.events
    }
    if len(by_id) != len(evidence.events):
        raise DailySourceClosureError("CAS Evidence IDs are repeated")

    bindings: list[SourceRouteBinding] = []
    for record in routes:
        event_id = record["evidence_id"]
        event = by_id[event_id]
        source_kind = event["kind"]
        entity = record["entity"]
        expected_entity = (
            "issue" if source_kind == "issue_comment" else "pull_request"
        )
        if (
            source_kind not in source.REQUIRES_WITNESS
            or entity != expected_entity
            or event["kind"] != record["kind"]
            or event["source_id"] != record["source_id"]
            or event["number"] != record["number"]
            or event["repository"] != record["repository"]
        ):
            raise DailySourceClosureError("CAS witness/entity mapping changed")
        bindings.append(SourceRouteBinding(
            evidence_id=event_id,
            source_kind=source_kind,
            repository=event["repository"],
            entity_type=entity,
            number=event["number"],
            source_id=event["source_id"],
            occurred_at=event["occurred_at"],
            verified_api_path=record["route"],
            entity_api_path=record["verification_route"],
            remote_id=record["remote_id"],
        ))
    if len(bindings) != sum(
        event["kind"] in source.REQUIRES_WITNESS for event in evidence.events
    ):
        raise DailySourceClosureError("CAS witness set is incomplete")
    if tuple(binding.evidence_id for binding in bindings) != tuple(
        sorted(binding.evidence_id for binding in bindings)
    ):
        raise DailySourceClosureError("CAS source route order changed")

    unattributed_commits = sum(
        event["kind"] == "default_branch_commit" for event in evidence.events
    )
    remaining = len(evidence.events) - len(bindings) - unattributed_commits
    if remaining < 0:
        raise DailySourceClosureError("CAS original Evidence count differs")

    model = {
        "record_version": RECORD_VERSION,
        "capture_sha256": digest,
        "evidence_sha256": index.evidence_sha256,
        "route_receipt_sha256": index.route_receipt_sha256,
        "date": index.date,
        "captured_at_utc": index.captured_at_utc,
        "collection_mode": index.collection_mode,
        "evidence_event_count": len(evidence.events),
        "witnessed_source_count": len(bindings),
        "unattributed_default_branch_commit_count": unattributed_commits,
        "other_unwitnessed_event_count": remaining,
        "routes": [item.to_json() for item in bindings],
        "publication_authorized": False,
        "requires_human_quality_review": True,
    }
    payload = activity._canonical_json_bytes(model)
    if len(payload) > MAX_CLOSURE_BYTES:
        # No silently truncated witnesses or dropped original events.
        raise DailySourceClosureError("verified source closure exceeds byte budget")
    return VerifiedSourceClosure(
        sha256=_sha(payload),
        canonical_bytes=payload,
        capture_sha256=digest,
        evidence_sha256=index.evidence_sha256,
        route_receipt_sha256=index.route_receipt_sha256,
        date=index.date,
        captured_at_utc=index.captured_at_utc,
        collection_mode=index.collection_mode,
        event_count=len(evidence.events),
        witnessed_count=len(bindings),
        unattributed_default_commit_count=unattributed_commits,
        other_unwitnessed_event_count=remaining,
        bindings=tuple(bindings),
    )


def read_verified_source_closure(capture_sha256: str) -> VerifiedSourceClosure:
    """Read *only* an exact Collector-owned CAS capture, with no side effects.

    An archived capture may be read privately for analysis; this never decides
    whether today's scheduled Writer is eligible. No API here accepts direct
    Evidence/Receipt blobs, an in-memory VerifiedCapture, or a filesystem path.
    """
    try:
        return _closure_from_trusted_cas(capture_sha256)
    except DailySourceClosureError:
        raise
    except (
        cas.DailyRouteCASError, capture_index.DailyCaptureIndexError,
        source.DailySourceRouteError, ArtifactLifecycleError,
        GitHubDailySummaryError, ValueError, TypeError, RecursionError,
    ):
        # Source text, filesystem path, and low-level exceptions are not part
        # of the public error surface; missing authority is not "no progress".
        raise DailySourceClosureError(
            "trusted verified source capture is unavailable"
        ) from None
