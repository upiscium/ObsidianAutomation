"""Fixed-role, read-only workflow quiescence guard for #300 service control.

A finished *final* oneshot is not enough: its prerequisite services, pending
jobs and OnSuccess successor chain can still be active. This registry is a
reviewed snapshot of the installed single-LXC role units, not a dynamic
discovery or an authorization to execute arbitrary unit names.

Positive observation is only a point-in-time readiness check. Full live
operation authority still requires the qualified within-LXC updater/launcher
maintenance gate, pinned unit revision, and a separate privileged worker.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .admin_status import SERVICES, ServiceSpec

# Same role members as tools/host_runtime_lifecycle.py SERVICES, partitioned
# along the three local authority domains. Tests cross-check that inventory.
AI_UNITS: tuple[str, ...] = (
    "obsidian-ai-vault-pull.service",
    "obsidian-semantic-index-refresh-prepare.service",
    "obsidian-semantic-index-refresh-embed.service",
    "obsidian-semantic-index-refresh-finalize.service",
    "obsidian-ai-input-planner.service",
    "obsidian-ai-human-projection-sync.service",
    "obsidian-ai-human-projection-cleanup-sync.service",
    "obsidian-ai-review-intake.service",
    "obsidian-ai-post-review-executor-prepare.service",
    "obsidian-ai-post-review-transport.service",
    "obsidian-ai-post-review-executor-finalize.service",
    "obsidian-ai-post-review-projection-sync.service",
    "obsidian-ai-post-review-reconcile.service",
    "obsidian-pre-review-generator.service",
    "obsidian-pre-review-validator.service",
    "obsidian-pre-review-reader.service",
    "obsidian-pre-review-evaluator.service",
    "obsidian-pre-review-status.service",
)
GITHUB_UNITS: tuple[str, ...] = (
    "obsidian-github-sync-vault-pull.service",
    "obsidian-github-sync.service",
    "obsidian-github-writer.service",
    "obsidian-github-compactor.service",
    "obsidian-github-daily-schedule.service",
    "obsidian-github-daily-collect.service",
    "obsidian-github-daily-summary.service",
    "obsidian-github-daily-render.service",
    "obsidian-github-daily-apply.service",
)
CORE_UNITS: tuple[str, ...] = ("obsidian-core-promotion.service",)

UNIT_NAMES = frozenset((*AI_UNITS, *GITHUB_UNITS, *CORE_UNITS))
TIMER_READ_FIELDS = (
    "LoadState", "UnitFileState", "ActiveState", "SubState",
    "Unit", "Job", "FragmentPath",
)
WORKFLOW_READ_FIELDS = (
    "LoadState", "ActiveState", "SubState", "Type",
    "Job", "MainPID", "ControlPID", "FragmentPath",
)
GROUP_UNITS: dict[str, tuple[str, ...]] = {
    "ai-vault-pull": AI_UNITS,
    "pre-review": AI_UNITS,
    "github-sync": GITHUB_UNITS,
    "github-daily": GITHUB_UNITS,
    "core-promotion": CORE_UNITS,
}
_GROUP_TIMERS: dict[str, tuple[str, ...]] = {
    "ai-vault-pull": (SERVICES[0].timer, SERVICES[1].timer),
    "pre-review": (SERVICES[0].timer, SERVICES[1].timer),
    "github-sync": (SERVICES[2].timer, SERVICES[3].timer),
    "github-daily": (SERVICES[2].timer, SERVICES[3].timer),
    "core-promotion": (SERVICES[4].timer,),
}
TIMER_TARGETS = {s.timer: s.service for s in SERVICES}
WORKFLOW_REJECTION_CODES = frozenset((
    "workflow_identity_unverified",
    "workflow_inventory_unverified",
    "workflow_timer_unverified",
    "workflow_timer_not_quiescent",
    "workflow_service_unverified",
    "workflow_service_not_quiescent",
))


class WorkflowNotQuiescent(ValueError):
    """Only stable rejection reason codes, never raw systemd data."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class WorkflowObservation:
    timers: Mapping[str, Mapping[str, str]]
    services: Mapping[str, Mapping[str, str]]


def _fixed_fields(fields: object, keys: tuple[str, ...]) -> bool:
    return (
        type(fields) is dict
        and all(type(key) is str for key in fields)
        and set(fields) == set(keys)
        and all(
            type(v) is str and len(v) <= 256
            and all(32 <= ord(c) <= 126 for c in v)
            for v in fields.values()
        )
    )


def require_run_once_quiescent(
    spec: ServiceSpec, snapshot: WorkflowObservation,
) -> None:
    """Refuse a run_once when any corresponding role unit might still run.

    All related role timers must first be deliberately disabled/inactive/dead.
    This prevents an automatic scheduling overlap, while all static role
    services must be loaded/idle and have no queued job/process. Unknown,
    partial, masked, replaced, linked and transitional states fail closed.
    Does not claim a *global* multi-role, cross-host or future-job guarantee.
    """
    if spec not in SERVICES or type(snapshot) is not WorkflowObservation:
        raise WorkflowNotQuiescent("workflow_identity_unverified")
    wanted_timers = _GROUP_TIMERS[spec.identifier]
    wanted_services = GROUP_UNITS[spec.identifier]
    if (
        type(snapshot.timers) is not dict
        or type(snapshot.services) is not dict
        or any(type(key) is not str for key in snapshot.timers)
        or any(type(key) is not str for key in snapshot.services)
        or set(snapshot.timers) != set(wanted_timers)
        or set(snapshot.services) != set(wanted_services)
    ):
        raise WorkflowNotQuiescent("workflow_inventory_unverified")

    for timer in wanted_timers:
        fields = snapshot.timers[timer]
        if not _fixed_fields(fields, TIMER_READ_FIELDS):
            raise WorkflowNotQuiescent("workflow_timer_unverified")
        if (
            fields["LoadState"] != "loaded"
            or fields["UnitFileState"] != "disabled"
            or fields["ActiveState"] != "inactive"
            or fields["SubState"] != "dead"
            or fields["Unit"] != TIMER_TARGETS[timer]
            or fields["Job"] not in ("", "0")
            or fields["FragmentPath"] != f"/etc/systemd/system/{timer}"
        ):
            raise WorkflowNotQuiescent("workflow_timer_not_quiescent")

    for name in wanted_services:
        fields = snapshot.services[name]
        if not _fixed_fields(fields, WORKFLOW_READ_FIELDS):
            raise WorkflowNotQuiescent("workflow_service_unverified")
        if (
            fields["LoadState"] != "loaded"
            or fields["Type"] != "oneshot"
            or fields["ActiveState"] != "inactive"
            or fields["SubState"] != "dead"
            or fields["Job"] not in ("", "0")
            or fields["MainPID"] != "0"
            or fields["ControlPID"] != "0"
            or fields["FragmentPath"] != f"/etc/systemd/system/{name}"
        ):
            raise WorkflowNotQuiescent("workflow_service_not_quiescent")
