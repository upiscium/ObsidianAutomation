"""Unattached trusted-source B3 execution owner, not a WebUI action.

Only a future reviewed, server-owned service may supply canonical proposals,
registered Nextcloud connection, the shared new-host Writer guard and an
operator-approved *durable* old-writer exclusion observation.

Stopping a legacy timer for one instant is NOT cross-LXC exclusion. A trusted
cutover observer must independently establish that the old writer cannot gain
canonical Vault write authority again (e.g. verified host isolation or actual
credential revocation), even across legacy host restart. This module cannot
verify such an external fact on its own; it refuses the default/unobserved
state, never creates authority or exposes a route/scheduler.

The existing CAS Writers still own effects, with local B3AttemptJournal
admission and Writer-pair GET proof. A Writer-pair result is not certified
Watcher/Mirror/Core Promotion freshness or full pipeline success.
"""
from __future__ import annotations

import re
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Literal

from .github_b3_attempts import B3AttemptJournal
from .github_b3_job import (
    B3JobConfig,
    B3RunRequest,
    WriterGuard,
    run_b3_writer_once,
)
from .github_project_overview import ProjectOverviewProposal
from .github_project_status_mutation import ProjectStatusProposal
from .webui_b3_adapter import ProjectSyncRequest

_ID = re.compile(r"[0-9a-f]{32}\Z")


class B3ExecutionUnavailable(RuntimeError):
    """Fixed redacted refusal, never report provider/configuration details."""

    def __init__(self, code: str = "b3_execution_unavailable") -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class B3RegisteredInputs:
    """Only from the trusted owner: cannot be built from an HTTP request."""

    config: B3JobConfig
    status_proposal: ProjectStatusProposal
    overview_proposal: ProjectOverviewProposal
    # Bound to the exact admitted request and producer snapshot identity.
    request_id: str
    source_generation: str
    source_record_id: str


@dataclass(frozen=True, slots=True)
class B3CutoverObservation:
    """An operator/host-owned claim, not an in-band authorization token.

    'host_isolated' and 'credential_revoked' mean the old LXC has *lost*
    canonical write authority, not merely that one timer reads inactive.
    The future deployment must independently verify this claim, its source,
    and persistence over legacy restarts before constructing this observer.
    """

    record_id: str
    binding_ref: str
    revision: str
    operator_approved: bool
    old_writer_exclusion: Literal[
        "host_isolated", "credential_revoked", "unverified",
    ]


def _id(value: object) -> bool:
    return type(value) is str and _ID.fullmatch(value) is not None


class B3TrustedJobPort:
    """Explicitly injected, non-HTTP execution port for a single B3 owner.

    No default registry, scheduled runner, startup side effects, or spare
    Writer. The approved service owner must inject ALL three providers.
    """

    def __init__(
        self,
        journal: B3AttemptJournal,
        *,
        registered_inputs: Callable[[ProjectSyncRequest], B3RegisteredInputs],
        cutover_observation: Callable[[], B3CutoverObservation],
        shared_writer_guard: WriterGuard,
    ) -> None:
        if type(journal) is not B3AttemptJournal:
            raise TypeError("invalid_b3_journal")
        if not all(
            callable(v)
            for v in (registered_inputs, cutover_observation, shared_writer_guard)
        ):
            raise TypeError("invalid_b3_execution_sources")
        self._journal = journal
        self._registered_inputs = registered_inputs
        self._cutover_observation = cutover_observation
        self._shared_writer_guard = shared_writer_guard

    @staticmethod
    def _validate_request(request: object) -> ProjectSyncRequest:
        if type(request) is not ProjectSyncRequest:
            raise B3ExecutionUnavailable("invalid_request")
        try:
            request.__post_init__()
        except Exception:
            raise B3ExecutionUnavailable("invalid_request") from None
        return request

    def _check_cutover(
        self,
        request: ProjectSyncRequest,
        expected: B3CutoverObservation | None = None,
    ) -> B3CutoverObservation:
        try:
            observed = self._cutover_observation()
        except Exception:
            raise B3ExecutionUnavailable("cutover_unavailable") from None
        if (
            type(observed) is not B3CutoverObservation
            or not all(_id(x) for x in (
                observed.record_id, observed.binding_ref, observed.revision,
            ))
            or observed.binding_ref != request.bindingRef
            or observed.revision != request.expectedRevision
            or type(observed.operator_approved) is not bool
            or observed.operator_approved is not True
            or type(observed.old_writer_exclusion) is not str
            or observed.old_writer_exclusion not in (
                "host_isolated", "credential_revoked",
            )
            or (expected is not None and observed != expected)
        ):
            raise B3ExecutionUnavailable("cutover_unverified")
        return observed

    def sync_project(
        self,
        request: ProjectSyncRequest,
        *,
        transport: Callable[..., object] | None = None,
    ) -> dict[str, object]:
        """Call the existing B3 Job only when trusted sources are bound.

        The request contributes only requestId, bindingRef and revision.
        Paths, repository, credentials, proposals and cutover authority are
        obtained from independent trusted server-side providers. No effect
        is admitted while the old writer is unverified; the observation is
        checked AGAIN *inside* the shared new-host Writer exclusion guard.
        """
        req = self._validate_request(request)
        cutover = self._check_cutover(req)  # refuse before reading secrets
        try:
            registered = self._registered_inputs(req)
        except Exception:
            raise B3ExecutionUnavailable("registered_inputs_unavailable") from None
        if (
            type(registered) is not B3RegisteredInputs
            or type(registered.config) is not B3JobConfig
            or type(registered.request_id) is not str
            or registered.request_id != req.requestId
            or not _id(registered.source_generation)
            or not _id(registered.source_record_id)
            or registered.config.binding_ref != req.bindingRef
            or registered.config.revision != req.expectedRevision
        ):
            raise B3ExecutionUnavailable("registered_inputs_unavailable")

        @contextmanager
        def guarded_writer():
            # The shared guard is acquired first; do not release it until
            # the existing CAS Writers and fresh GET checks are finished.
            with self._shared_writer_guard():
                self._check_cutover(req, cutover)
                try:
                    current = self._registered_inputs(req)
                    if (type(current) is not B3RegisteredInputs
                            or current != registered):
                        raise B3ExecutionUnavailable(
                            "registered_inputs_changed"
                        )
                except B3ExecutionUnavailable:
                    raise
                except Exception:
                    # A changed or unreadable producer must be refused
                    # before journal.start() establishes an effect boundary.
                    raise B3ExecutionUnavailable(
                        "registered_inputs_unavailable"
                    ) from None
                yield

        return run_b3_writer_once(
            self._journal,
            config=registered.config,
            request=B3RunRequest(
                request_id=req.requestId, binding_ref=req.bindingRef,
                expected_revision=req.expectedRevision, trigger="manual",
            ),
            status_proposal=registered.status_proposal,
            overview_proposal=registered.overview_proposal,
            writer_guard=guarded_writer,
            transport=transport,
        )
