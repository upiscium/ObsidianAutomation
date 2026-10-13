"""Nonpublishing, content-bound capture index for fresh Daily v2 routes (#336).

A self-consistent SHA-256 index is NOT Collector authentication, an immutable
filesystem receipt, a cryptographic signature or permission to publish.
The future trusted Collector/CAS writer must take the exact verified triplet
(Evidence, source routes, index) and independently enforce OS role/ACL policy.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from . import github_daily_activity as activity
from . import github_daily_source_route as route
from .artifact_lifecycle import ArtifactLifecycleError
from .github_daily_summary import GitHubDailySummaryError


class DailyCaptureIndexError(ValueError):
    """A capture index does not exactly match independently verified sources."""


CAPTURE_INDEX_VERSION = 1
MAX_CAPTURE_INDEX_BYTES = 4096


@dataclass(frozen=True)
class CaptureIndex:
    canonical_bytes: bytes
    sha256: str
    date: str
    evidence_sha256: str
    route_receipt_sha256: str
    route_count: int
    collection_mode: str
    captured_at_utc: str


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _validated_index(evidence_bytes: bytes, receipt_bytes: bytes) -> CaptureIndex:
    if type(evidence_bytes) is not bytes or type(receipt_bytes) is not bytes:
        raise DailyCaptureIndexError("source payloads must be exact bytes")
    if len(evidence_bytes) > 32 * 1024 * 1024 or len(receipt_bytes) > 4 * 1024 * 1024:
        raise DailyCaptureIndexError("source payload exceeds capture bounds")
    # This verifies the complete original Evidence hash, every ambiguous
    # Issue/PR source witness, exact date and canonical source-route receipt.
    try:
        routes = route.parse_verified_routes(receipt_bytes, evidence_bytes)
    except (route.DailySourceRouteError, GitHubDailySummaryError, ArtifactLifecycleError, ValueError, TypeError) as exc:
        raise DailyCaptureIndexError("capture sources failed route verification") from exc
    receipt = json.loads(receipt_bytes)
    payload = {
        "record_version": CAPTURE_INDEX_VERSION,
        "source_receipt_version": route.RECORD_VERSION,
        "date": receipt["date"],
        "evidence_sha256": _sha(evidence_bytes),
        "route_receipt_sha256": _sha(receipt_bytes),
        "route_count": len(routes),
        "collection_mode": receipt["collection_mode"],
        "captured_at_utc": receipt["captured_at_utc"],
        "producer_attestation_verified": False,
        "publication_authorized": False,
    }
    raw = activity._canonical_json_bytes(payload)
    if len(raw) > MAX_CAPTURE_INDEX_BYTES:
        raise DailyCaptureIndexError("capture index exceeds byte bound")
    return CaptureIndex(
        canonical_bytes=raw,
        sha256=_sha(raw),
        date=receipt["date"],
        evidence_sha256=payload["evidence_sha256"],
        route_receipt_sha256=payload["route_receipt_sha256"],
        route_count=len(routes),
        collection_mode=receipt["collection_mode"],
        captured_at_utc=receipt["captured_at_utc"],
    )


def build_capture_index(collection: route.FreshRouteCollection) -> CaptureIndex:
    """Construct an in-memory index from a fresh collection. Does not persist."""
    if not isinstance(collection, route.FreshRouteCollection):
        raise DailyCaptureIndexError("fresh source collection is required")
    original = collection.evidence
    if (
        type(original.canonical_bytes) is not bytes
        or original.sha256 != _sha(original.canonical_bytes)
        or type(collection.receipt_bytes) is not bytes
        or collection.receipt_sha256 != _sha(collection.receipt_bytes)
    ):
        raise DailyCaptureIndexError("collection payload hash mismatch")
    record = _validated_index(original.canonical_bytes, collection.receipt_bytes)
    if (
        record.route_count != collection.route_count
        or record.evidence_sha256 != original.sha256
        or record.date != original.target_date
    ):
        raise DailyCaptureIndexError("collection metadata contradicts verified source")
    return record


def parse_capture_index(
    index_bytes: bytes, evidence_bytes: bytes, receipt_bytes: bytes,
) -> CaptureIndex:
    """Revalidate the triplet; no self-authored index field grants authority."""
    if type(index_bytes) is not bytes or len(index_bytes) > MAX_CAPTURE_INDEX_BYTES:
        raise DailyCaptureIndexError("invalid capture index byte envelope")
    try:
        expected = _validated_index(evidence_bytes, receipt_bytes)
    except (ValueError, TypeError) as exc:
        if isinstance(exc, DailyCaptureIndexError):
            raise
        raise DailyCaptureIndexError("invalid capture source inputs") from exc
    if index_bytes != expected.canonical_bytes:
        raise DailyCaptureIndexError("capture index is not canonical or source-bound")
    return expected
