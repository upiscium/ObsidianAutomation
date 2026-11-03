"""Unattached single-LXC B3 Writer vertical slice.

This is a *source-only* producer adapter, not a WebUI route or live scheduled
service. A trusted #483 integration owner must construct B3JobConfig from
server-owned connection/binding and secret settings, inject a shared Writer
guard that serializes effects with all other canonical Writers, and explicitly
authorize switching off any legacy B3 Writer before enabling this job.

This code reuses:
- the private, single-owner B3AttemptJournal (one SQLite DB);
- existing Project status and Overview canonical CAS Writers; and
- the independent bounded two-GET WebDAV observation.

A matching readback is only a **bounded Writer subeffect observation**:
this Job never asserts complete GitHub pipeline success, Watcher/Mirror
freshness, Core Promotion success or a healthy live WebUI. Interrupted or
ambiguous writes block retry until an independently approved reconciliation.
"""
from __future__ import annotations

import math
import re
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Callable

from .github_b3_attempts import B3AttemptJournal
from .github_b3_readback_candidate import RunContext
from .github_b3_webdav_readback import (
    ReadbackUnavailable,
    _canonical_proposals,
    observe_b3_writer_pair,
)
from .github_project_overview import (
    ProjectOverviewProposal,
    apply_project_overview,
)
from .github_project_status_mutation import (
    ProjectStatusProposal,
    apply_project_status,
)
from .webdav_create import build_target_url


_ID = re.compile(r"[0-9a-f]{32}\Z")
_OBSERVED = "two_fresh_gets_match_receipt_hashes"
VERSION = "github-b3-producer-job/v1"


class JobNotAvailable(RuntimeError):
    """Fixed nonsecret refusal/uncertainty, no arbitrary provider error text."""

    def __init__(self, code: str = "job_unavailable") -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class B3JobConfig:
    """TRUSTED server-owned configuration, never directly from an HTTP body."""

    binding_ref: str
    revision: str
    project_path: str
    repository: str
    registered_base_url: str
    username: str
    password: str = field(repr=False)
    timeout: float = 30.0


@dataclass(frozen=True, slots=True)
class B3RunRequest:
    """The only allowed public-origin data (validated again by the job)."""

    request_id: str
    binding_ref: str
    expected_revision: str
    trigger: str = "manual"
    retry_of: str | None = None


WriterGuard = Callable[[], AbstractContextManager[object]]


def _hex32(x: object) -> bool:
    return type(x) is str and _ID.fullmatch(x) is not None


def _ready(
    config: B3JobConfig,
    request: B3RunRequest,
    status_proposal: ProjectStatusProposal,
    overview_proposal: ProjectOverviewProposal,
    guard: WriterGuard,
) -> tuple[ProjectStatusProposal, ProjectOverviewProposal]:
    if type(config) is not B3JobConfig or type(request) is not B3RunRequest:
        raise JobNotAvailable("invalid_configuration")
    if not all(_hex32(v) for v in (config.binding_ref, config.revision)):
        raise JobNotAvailable("invalid_configuration")
    if (
        not all(type(v) is str for v in (
            config.project_path, config.repository, config.registered_base_url,
            config.username, config.password,
        ))
        or not config.username or not config.password
        or type(config.timeout) not in (int, float)
        or not 0 < config.timeout <= 120
    ):
        raise JobNotAvailable("invalid_configuration")
    try:
        if not math.isfinite(config.timeout):
            raise ValueError("nonfinite")
        build_target_url(config.registered_base_url, config.project_path)
    except Exception:
        raise JobNotAvailable("invalid_configuration") from None
    if (
        not _hex32(request.request_id) or not _hex32(request.binding_ref)
        or not _hex32(request.expected_revision)
        or type(request.trigger) is not str
        or request.trigger not in ("manual", "scheduled")
        or (request.retry_of is not None and not _hex32(request.retry_of))
    ):
        raise JobNotAvailable("invalid_request")
    if (request.binding_ref != config.binding_ref
            or request.expected_revision != config.revision):
        raise JobNotAvailable("stale_or_unbound_request")
    if not callable(guard):
        raise JobNotAvailable("writer_guard_unavailable")
    try:
        status, overview = _canonical_proposals(status_proposal, overview_proposal)
    except ReadbackUnavailable:
        raise JobNotAvailable("invalid_proposals") from None
    if (
        status.project_path != config.project_path
        or status.repository != config.repository
        or overview.project_path != config.project_path
        or overview.repository != config.repository
        or str(PurePosixPath(config.project_path).parent / "Status.md")
        != overview.status_path
    ):
        raise JobNotAvailable("unbound_proposals")
    return status, overview


def run_b3_writer_once(
    journal: B3AttemptJournal,
    *,
    config: B3JobConfig,
    request: B3RunRequest,
    status_proposal: ProjectStatusProposal,
    overview_proposal: ProjectOverviewProposal,
    writer_guard: WriterGuard,
    transport: Callable[..., object] | None = None,
) -> dict[str, object]:
    """Admit, perform existing Writer CAS effects, re-GET, commit one proof.

    The callable has no registration or route. The caller is the sole trusted
    service Job owner; neither the approved connection, Writer guard nor
    canonical proposals may be created from a user-supplied request object.
    """
    if type(journal) is not B3AttemptJournal:
        raise JobNotAvailable("invalid_journal")
    status_proposal, overview_proposal = _ready(
        config, request, status_proposal, overview_proposal, writer_guard,
    )

    # A duplicate request must not reacquire a Writer or perform any I/O.
    decision, entry = journal.admit(
        request.request_id, config.binding_ref, config.revision,
        trigger=request.trigger, retry_of=request.retry_of,
    )
    if decision == "replayed":
        return {
            "schema_version": VERSION,
            "decision": "replayed",
            "receipt": entry,
            "writer_readback": None,
            "last_verified_pipeline_success": None,
        }

    run_id = entry["run_id"]
    started = False
    try:
        # The integrator must use its one shared canonical Writer guard.
        # No effects happen until start() has been durably committed.
        with writer_guard():
            running = journal.start(run_id)
            started = True
            status_result = apply_project_status(
                status_proposal,
                base_url=config.registered_base_url,
                username=config.username,
                password=config.password,
                timeout=config.timeout,
                transport=transport,
            )
            overview_result = apply_project_overview(
                overview_proposal,
                base_url=config.registered_base_url,
                username=config.username,
                password=config.password,
                timeout=config.timeout,
                transport=transport,
            )
            observed_at = journal.clock()
            candidate = observe_b3_writer_pair(
                RunContext(
                    run_id=run_id,
                    binding_ref=config.binding_ref,
                    expected_revision=config.revision,
                    started_at=running["started_at"],
                ),
                status_proposal,
                overview_proposal,
                status_result,
                overview_result,
                base_url=config.registered_base_url,
                registered_base_url=config.registered_base_url,
                username=config.username,
                password=config.password,
                timeout=config.timeout,
                transport=transport,
                now=observed_at,
            )
            if (
                candidate["writer_readback"] != _OBSERVED
                or candidate["candidate_outcomes"] != {
                    "status": status_result.outcome,
                    "overview": overview_result.outcome,
                }
                or candidate["last_verified_pipeline_success"] is not None
            ):
                raise JobNotAvailable("writer_readback_unconfirmed")

        # Do NOT persist the proof from inside the shared Writer guard.
        # A normal return from __exit__ is required before the run becomes
        # completed; BaseException on release leaves this run running, and
        # restart conservatively marks it interrupted_unknown.
        result = journal.record_writer_readback(
            run_id,
            status_proposal_sha256=status_result.proposal_sha256,
            status_after_sha256=status_result.after_content_sha256,
            overview_proposal_sha256=overview_result.proposal_sha256,
            overview_after_sha256=overview_result.after_content_sha256,
            observed_at=observed_at,
        )
        return {
            "schema_version": VERSION,
            "decision": "accepted",
            "receipt": result,
            "writer_readback": _OBSERVED,
            "last_verified_pipeline_success": None,
        }
    except Exception:
        # Once start was persisted, no exception can establish no remote
        # effect: hold the run unconfirmed and never retry automatically.
        if started:
            try:
                journal.finish(run_id, terminal_code="effect_unknown")
            except Exception:
                # If the DB failed, refuse to advertise safe continuation.
                raise JobNotAvailable("journal_unavailable") from None
        raise JobNotAvailable(
            "writer_effect_unknown" if started else "writer_not_started"
        ) from None
    # BaseException / process interruption deliberately escapes so restart
    # marks a running attempt as interrupted_unknown, without replay.
