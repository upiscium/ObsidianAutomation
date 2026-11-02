"""Bounded, read-only *real WebDAV GET* seam for the #316 B3 Writer pair.

Existing Project Status and Overview producers already perform guarded CAS
writes with immediate exact readback. This module does NOT repeat those PUTs.
It independently GETs their two canonical targets and compares the current
bytes against separately supplied, producer-authored transport-result digests.
The v3 gate then checks the run-bound two-stage candidate semantics.

No process starts automatically. Only a later #483 trusted B3 Job may call
this code with its own server-validated RunContext, canonical proposals,
producer-owned results and credentials. It MUST authenticate that the result
objects belong to that Job and persist proof in its *existing* durable journal.

These sequential GETs do not constitute an atomic cross-file snapshot, a
permanent freshness guarantee or proof of the full B3 pipeline (Watcher,
Mirror, Core Promotion, scheduler). Never use this module to mark the entire
pipeline successful, resolve incidents, or enable the WebUI by itself.
"""
from __future__ import annotations

import hashlib
import math
import re
import time
from datetime import datetime, timezone
from typing import Callable

from .core_promotion_transport import (
    HTTPResponse,
    _real_http_request,
)
from .github_b3_readback_candidate import (
    MAX_CONTENT_BYTES,
    InvalidCandidate,
    RunContext,
    StageReadback,
    evaluate_b3_readbacks,
)
from .github_project_overview import (
    ProjectOverviewProposal,
    ProjectOverviewTransportResult,
    parse_overview_proposal,
)
from .github_project_status_mutation import (
    ProjectStatusProposal,
    ProjectStatusTransportResult,
    parse_watcher_proposal,
)
from .webdav_create import build_target_url


VERSION = "github-b3-webdav-observation/v1"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
Transport = Callable[..., HTTPResponse]


class ReadbackUnavailable(RuntimeError):
    """Fixed nonsecret reason codes only; no URLs, bytes or exception payloads."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _sha(value: object) -> bool:
    return type(value) is str and _SHA256.fullmatch(value) is not None


def _bounded_number(value: object, *, lower: float, upper: float) -> bool:
    if type(value) not in (float, int):
        return False
    try:
        return math.isfinite(value) and lower <= value <= upper
    except (TypeError, ValueError, OverflowError):
        return False


def _timestamp(value: object) -> float:
    """Parse the UTC-Z transport-result format without trusting local TZ."""
    if (
        type(value) is not str or not 20 <= len(value) <= 35
        or not value.endswith("Z") or value.count("T") != 1
    ):
        raise ReadbackUnavailable("invalid_writer_receipt")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
        if parsed.tzinfo != timezone.utc:
            raise ValueError("timezone")
        seconds = parsed.timestamp()
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("timestamp")
        return seconds
    except (ValueError, TypeError, OverflowError, OSError):
        raise ReadbackUnavailable("invalid_writer_receipt") from None


def _canonical_proposals(
    project: object,
    overview: object,
) -> tuple[ProjectStatusProposal, ProjectOverviewProposal]:
    # Only the already-existing parsers may authorize canonical project paths.
    if type(project) is not ProjectStatusProposal or type(overview) is not ProjectOverviewProposal:
        raise ReadbackUnavailable("invalid_proposal")
    try:
        if (
            type(project.canonical_bytes) is not bytes
            or type(overview.canonical_bytes) is not bytes
            or len(project.canonical_bytes) > MAX_CONTENT_BYTES
            or len(overview.canonical_bytes) > MAX_CONTENT_BYTES
        ):
            raise ValueError("invalid candidate bytes")
        project2 = parse_watcher_proposal(project.canonical_bytes)
        overview2 = parse_overview_proposal(overview.canonical_bytes)
        # Reject forged outer handles; use the re-parsed, canonical objects
        # exclusively below so adversarial nested fields are never consulted.
        if (
            not _sha(project.sha256) or not _sha(overview.sha256)
            or type(project.project_path) is not str
            or type(project.repository) is not str
            or type(project.expected_status) is not str
            or type(project.desired_status) is not str
            or type(overview.project_path) is not str
            or type(overview.repository) is not str
            or project2.project_path != project.project_path
            or project2.repository != project.repository
            or project2.expected_status != project.expected_status
            or project2.desired_status != project.desired_status
            or overview2.project_path != overview.project_path
            or overview2.repository != overview.repository
            or project2.project_path != overview2.project_path
            or project2.repository != overview2.repository
            or project2.sha256 != project.sha256
            or overview2.sha256 != overview.sha256
        ):
            raise ValueError("proposal mismatch")
    except Exception:
        raise ReadbackUnavailable("invalid_proposal") from None
    return project2, overview2


def _validate_receipts(
    project: ProjectStatusProposal,
    overview: ProjectOverviewProposal,
    status_result: object,
    overview_result: object,
) -> tuple[ProjectStatusTransportResult, ProjectOverviewTransportResult]:
    if (
        type(status_result) is not ProjectStatusTransportResult
        or type(overview_result) is not ProjectOverviewTransportResult
    ):
        raise ReadbackUnavailable("invalid_writer_receipt")
    # Require built-in strings before comparisons; malformed record fields
    # must not invoke attacker-defined __eq__/__ne__ methods.
    if not all(type(x) is str for x in (
        status_result.proposal_sha256,
        status_result.project_path,
        status_result.repository,
        status_result.expected_status,
        status_result.desired_status,
        status_result.outcome,
        overview_result.proposal_sha256,
        overview_result.project_path,
        overview_result.status_path,
        overview_result.repository,
        overview_result.outcome,
    )):
        raise ReadbackUnavailable("invalid_writer_receipt")
    if (
        status_result.proposal_sha256 != project.sha256
        or status_result.project_path != project.project_path
        or status_result.repository != project.repository
        or status_result.expected_status != project.expected_status
        or status_result.desired_status != project.desired_status
        or overview_result.proposal_sha256 != overview.sha256
        or overview_result.project_path != overview.project_path
        or overview_result.status_path != overview.status_path
        or overview_result.repository != overview.repository
        or not all(_sha(h) for h in (
            status_result.after_content_sha256,
            overview_result.after_content_sha256,
            status_result.before_content_sha256,
        ))
        or (overview_result.before_content_sha256 is not None
            and not _sha(overview_result.before_content_sha256))
        or type(status_result.outcome) is not str
        or type(overview_result.outcome) is not str
    ):
        raise ReadbackUnavailable("invalid_writer_receipt")
    return status_result, overview_result


def _get_bytes(
    *,
    target_url: str,
    username: str,
    password: str,
    timeout: float,
    transport: Transport | None,
    expected_after_sha256: str,
) -> bytes:
    request = transport if transport is not None else _real_http_request
    try:
        response = request(
            method="GET",
            target_url=target_url,
            username=username,
            password=password,
            headers={"Accept": "text/markdown"},
            body=None,
            timeout=timeout,
            response_limit=MAX_CONTENT_BYTES,
        )
        if (
            type(response) is not HTTPResponse or type(response.status) is not int
            or response.status != 200 or type(response.body) is not bytes
            or not 1 <= len(response.body) <= MAX_CONTENT_BYTES
        ):
            raise ReadbackUnavailable("remote_readback_unavailable")
        if hashlib.sha256(response.body).hexdigest() != expected_after_sha256:
            raise ReadbackUnavailable("remote_readback_diverged")
        return response.body
    except ReadbackUnavailable:
        raise
    except Exception:
        # Avoid leaking URLs, credentials, payloads or provider exception text.
        raise ReadbackUnavailable("remote_readback_unavailable") from None


def observe_b3_writer_pair(
    context: RunContext,
    status_proposal: ProjectStatusProposal,
    overview_proposal: ProjectOverviewProposal,
    status_result: ProjectStatusTransportResult,
    overview_result: ProjectOverviewTransportResult,
    *,
    base_url: str,
    registered_base_url: str,
    username: str,
    password: str,
    timeout: float = 30.0,
    transport: Transport | None = None,
    now: float | None = None,
) -> dict[str, object]:
    """Perform two bounded fresh GETs; return sanitized writer evidence only.

    The result is deliberately NOT a WebUI Workflow DTO or a full-pipeline
    receipt. The future #483 producer must first bind context to the *private*
    v2 admission and these receipts to the actual writer, and must arrange
    persistence/reconciliation in that same owner. A forged receipt + forged
    fake transport can pass tests; this function does not grant authority.

    The future Job MUST supply registered_base_url from its persisted,
    owner-approved Nextcloud connection (not from the incoming Run request).
    No request-supplied URL is trusted to select a credential destination.
    """
    moment = time.time() if now is None else now
    if not _bounded_number(moment, lower=0, upper=253402300799):
        raise ReadbackUnavailable("invalid_context")
    try:
        evaluate_b3_readbacks(context, (), now=moment)
    except InvalidCandidate:
        raise ReadbackUnavailable("invalid_context") from None
    if (
        type(base_url) is not str
        or type(registered_base_url) is not str
        or base_url != registered_base_url
    ):
        raise ReadbackUnavailable("unapproved_connection")
    if (
        type(username) is not str or not username
        or type(password) is not str or not password
        or not _bounded_number(timeout, lower=0, upper=120)
        or timeout == 0
    ):
        raise ReadbackUnavailable("invalid_connection")

    project, overview = _canonical_proposals(status_proposal, overview_proposal)
    status, overview_receipt = _validate_receipts(
        project, overview, status_result, overview_result,
    )
    try:
        project_url = build_target_url(base_url, project.project_path)
        overview_url = build_target_url(base_url, overview.status_path)
    except Exception:
        raise ReadbackUnavailable("invalid_connection") from None

    # Strict validation precedes both HTTP requests, never write anything.
    completed_status = _timestamp(status.completed_at)
    completed_overview = _timestamp(overview_receipt.completed_at)
    if completed_status < context.started_at or completed_overview < context.started_at:
        raise ReadbackUnavailable("invalid_writer_receipt")
    if completed_status > moment + 300 or completed_overview > moment + 300:
        raise ReadbackUnavailable("invalid_writer_receipt")

    project_body = _get_bytes(
        target_url=project_url, username=username, password=password,
        timeout=timeout, transport=transport,
        expected_after_sha256=status.after_content_sha256,
    )
    overview_body = _get_bytes(
        target_url=overview_url, username=username, password=password,
        timeout=timeout, transport=transport,
        expected_after_sha256=overview_receipt.after_content_sha256,
    )

    # The producer receipt's SHA-256 commitment, checked above against fresh
    # GET bytes, is our independent content binding. v3 gets identical bytes
    # because producer receipts retain hashes but not complete Markdown.
    # This does NOT manufacture the origin of the untrusted transport.
    items = (
        StageReadback(
            stage="status",
            run_id=context.run_id, binding_ref=context.binding_ref,
            expected_revision=context.expected_revision,
            proposal_sha256=status.proposal_sha256,
            outcome=status.outcome,
            before_sha256=status.before_content_sha256,
            expected_bytes=project_body, observed_bytes=project_body,
            completed_at=completed_status,
        ),
        StageReadback(
            stage="overview",
            run_id=context.run_id, binding_ref=context.binding_ref,
            expected_revision=context.expected_revision,
            proposal_sha256=overview_receipt.proposal_sha256,
            outcome=overview_receipt.outcome,
            before_sha256=overview_receipt.before_content_sha256,
            expected_bytes=overview_body, observed_bytes=overview_body,
            completed_at=completed_overview,
        ),
    )
    try:
        candidate = evaluate_b3_readbacks(context, items, now=moment)
    except InvalidCandidate:
        raise ReadbackUnavailable("inconsistent_writer_receipts") from None
    if candidate["consistency"] != "candidate_consistent":
        raise ReadbackUnavailable("inconsistent_writer_receipts")

    return {
        "schema_version": VERSION,
        "writer_readback": "two_fresh_gets_match_receipt_hashes",
        "candidate_changed": candidate["candidate_changed"],
        "candidate_outcomes": candidate["candidate_outcomes"],
        "last_verified_pipeline_success": None,
        "overall_health": "unknown",
        "active_incident": "unknown",
    }
