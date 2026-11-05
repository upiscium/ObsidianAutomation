"""Unattached request-correlated B3 proposal binding for a trusted service.

A future server owner supplies BOTH a registered Nextcloud Project scope and
canonical producer output. The public request can only select the matching
request_id/binding/revision. It cannot supply URL, credentials, Vault paths,
producer bytes, digests, or generation.

This module checks integrity, internal consistency and freshness; it DOES NOT
authenticate the origin of the supplied records or certify remote convergence.
A trusted production owner must separately implement immutable producer-source
acquisition and authenticated old-LXC Writer retirement. No startup or WebUI
registration is performed here.
"""
from __future__ import annotations

import hashlib
import hmac
import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from .github_b3_job import B3JobConfig
from .github_b3_trusted_port import B3RegisteredInputs
from .github_b3_webdav_readback import _canonical_proposals
from .github_project_overview import parse_overview_proposal
from .github_project_status_mutation import parse_watcher_proposal
from .webui_b3_adapter import ProjectSyncRequest

_ID = re.compile(r"[0-9a-f]{32}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_MAX_SOURCE_BYTES = 4 * 1024 * 1024
_MAX_AGE = 300
_MAX_TIMESTAMP = 253402300799


class B3BindingUnavailable(RuntimeError):
    """One fixed nonsecret failure code; no producer/credential text."""

    def __init__(self, code: str = "registration_unavailable") -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class B3RegisteredScope:
    """Registration loaded from an independent server-owned source."""

    config: B3JobConfig
    source_generation: str


@dataclass(frozen=True, slots=True)
class B3PreparedProposalPair:
    """Already-persisted producer bytes for one *specific* Sync request.

    Hashes here detect accidental/corrupt bytes; if an attacker controls the
    bytes and hashes, they can forge both. The future owner must authenticate
    source provenance before supplying this record.
    """

    request_id: str
    binding_ref: str
    expected_revision: str
    source_generation: str
    source_record_id: str
    prepared_at: float
    status_bytes: bytes
    overview_bytes: bytes
    status_sha256: str
    overview_sha256: str


def _hex(value: object, pattern: re.Pattern[str]) -> bool:
    return type(value) is str and pattern.fullmatch(value) is not None


def _time(value: object) -> bool:
    if type(value) not in (float, int):
        return False
    try:
        return math.isfinite(value) and 0 <= value <= _MAX_TIMESTAMP
    except (TypeError, ValueError, OverflowError):
        return False


def _bytes(value: object) -> bool:
    return type(value) is bytes and 0 < len(value) <= _MAX_SOURCE_BYTES


def bind_b3_registered_inputs(
    scope: B3RegisteredScope,
    pair: B3PreparedProposalPair,
    request: ProjectSyncRequest,
    *,
    now: float,
) -> B3RegisteredInputs:
    """Validate a single exact, fresh Project status/overview producer pair.

    Enforce typed request, frozen registration generation, identical producer
    request identity and canonical Project/repository path. The existing B3
    Job remains the authority for CAS Writer behavior and re-GET evidence.
    """
    if (
        type(scope) is not B3RegisteredScope
        or type(scope.config) is not B3JobConfig
        or type(pair) is not B3PreparedProposalPair
        or type(request) is not ProjectSyncRequest
    ):
        raise B3BindingUnavailable()
    try:
        request.__post_init__()
    except Exception:
        raise B3BindingUnavailable() from None
    config = scope.config
    if (
        not _hex(scope.source_generation, _ID)
        or not all(_hex(x, _ID) for x in (
            config.binding_ref, config.revision,
            pair.request_id, pair.binding_ref, pair.expected_revision,
            pair.source_generation, pair.source_record_id,
        ))
        or pair.request_id != request.requestId
        or pair.binding_ref != request.bindingRef
        or pair.expected_revision != request.expectedRevision
        or pair.binding_ref != config.binding_ref
        or pair.expected_revision != config.revision
        or pair.source_generation != scope.source_generation
        or not _time(now)
        or not _time(pair.prepared_at)
        or not 0 <= now - pair.prepared_at <= _MAX_AGE
        or not _bytes(pair.status_bytes)
        or not _bytes(pair.overview_bytes)
        or not _hex(pair.status_sha256, _SHA)
        or not _hex(pair.overview_sha256, _SHA)
    ):
        raise B3BindingUnavailable()
    if (
        not hmac.compare_digest(
            hashlib.sha256(pair.status_bytes).hexdigest(), pair.status_sha256,
        )
        or not hmac.compare_digest(
            hashlib.sha256(pair.overview_bytes).hexdigest(), pair.overview_sha256,
        )
    ):
        raise B3BindingUnavailable()
    try:
        status, overview = _canonical_proposals(
            parse_watcher_proposal(pair.status_bytes),
            parse_overview_proposal(pair.overview_bytes),
        )
        # Re-stamping an old Watcher proposal with a fresh envelope is not
        # enough. The canonical observed_at itself must be fresh and must
        # predate the producer-pair preparation time.
        observed = datetime.fromisoformat(
            status.observed_at.replace("Z", "+00:00")
        ).timestamp()
        if (
            not math.isfinite(observed)
            or not 0 <= pair.prepared_at - observed <= _MAX_AGE
            or not 0 <= now - observed <= _MAX_AGE
            or type(config.project_path) is not str
            or type(config.repository) is not str
            or status.project_path != config.project_path
            or overview.project_path != config.project_path
            or status.repository != config.repository
            or overview.repository != config.repository
        ):
            raise ValueError("mismatched or stale canonical source")
    except Exception:
        raise B3BindingUnavailable() from None
    return B3RegisteredInputs(
        config, status, overview, request.requestId,
        pair.source_generation, pair.source_record_id,
    )


def trusted_b3_registered_provider(
    scope: Callable[[], B3RegisteredScope],
    proposal_pair: Callable[[ProjectSyncRequest], B3PreparedProposalPair],
    *,
    clock: Callable[[], float],
) -> Callable[[ProjectSyncRequest], B3RegisteredInputs]:
    """Create an opt-in server-only callback; does not fetch files itself.

    The owner must securely implement both read providers. A function
    supplied from HTTP parameters is NOT a trusted registered source.
    """
    if not all(callable(x) for x in (scope, proposal_pair, clock)):
        raise TypeError("invalid_registered_source")
    def load(request: ProjectSyncRequest) -> B3RegisteredInputs:
        try:
            return bind_b3_registered_inputs(
                scope(), proposal_pair(request), request, now=clock(),
            )
        except B3BindingUnavailable:
            raise
        except Exception:
            raise B3BindingUnavailable() from None
    return load
