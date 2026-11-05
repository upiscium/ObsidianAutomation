"""Read-only B3 legacy/new host preflight, NOT a Writer authorization.

A captured systemd snapshot is a point-in-time observation, not a durable
fence. Even an entirely quiescent old host may restart or retain valid
Nextcloud credentials. Consequently this module NEVER returns an
operator-approved B3CutoverObservation or invokes an effect. It reports only
an inert, bounded readiness assessment to the later privileged cutover owner.

The later source must independently verify durable legacy write-authority
removal and provide a real shared new-host Writer guard before enabling B3.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping

VERSION = "github-b3-cutover-preflight/v1"

# Fixed, scope-specific GitHub Project status and Core Promotion roles.
# Daily is intentionally not controlled by this B3 slice, but its shared
# Writer authority must still be covered by a future canonical Writer guard.
OLD_PROJECT_SERVICES = (
    "obsidian-github-sync-vault-pull.service",
    "obsidian-github-sync.service",
    "obsidian-github-writer.service",
    "obsidian-github-compactor.service",
)
OLD_TIMERS = (
    "obsidian-github-sync.timer",
    "obsidian-core-promotion.timer",
)
CORE_SERVICE = "obsidian-core-promotion.service"
SERVICES = OLD_PROJECT_SERVICES + (CORE_SERVICE,)
TIMERS = OLD_TIMERS


@dataclass(frozen=True, slots=True)
class HostUnitSnapshot:
    """Values collected by a trusted, read-only host probe, not HTTP input."""

    host: str
    captured_at: float
    services: Mapping[str, tuple[str, str, str]]
    timers: Mapping[str, tuple[str, str, str, str]]


def _sample_ok(
    value: object, *, expected: tuple[str, ...], field_count: int,
) -> bool:
    return (
        type(value) is dict
        and set(value) == set(expected)
        and all(type(k) is str for k in value)
        and all(
            type(x) is tuple
            and len(x) == field_count
            and all(type(t) is str and 0 < len(t) <= 64 for t in x)
            for x in value.values()
        )
    )


def _finite_number(value: object) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, ValueError, TypeError):
        return False


def _time_ok(value: object, now: float) -> bool:
    return (
        _finite_number(value)
        and 0 <= value <= min(now, 253402300799)
        and now - value <= 60
    )


def assess_b3_cutover_preflight(
    old: HostUnitSnapshot,
    new: HostUnitSnapshot,
    *,
    now: float,
) -> dict[str, object]:
    """Fail-closed scoped systemd inventory; cannot approve a cutover.

    Unit tuples: (load, active, sub) for services, and
    (load, unit_file_state, active, sub) for timers. The new host may have absent units or deliberately inert
    staged ones. The old host must have all required units installed and
    stopped, including the timer mechanisms that can relaunch writers.
    """
    if not _finite_number(now) or not 0 <= now <= 253402300799:
        return {"schema_version": VERSION, "state": "unknown",
                "reason": "invalid_observation", "cutover_authorized": False}
    if (
        type(old) is not HostUnitSnapshot
        or type(new) is not HostUnitSnapshot
        or old.host != "obsidian-automation"
        or new.host != "obsidian-control"
        or not _time_ok(old.captured_at, now)
        or not _time_ok(new.captured_at, now)
        or not _sample_ok(old.services, expected=SERVICES, field_count=3)
        or not _sample_ok(old.timers, expected=TIMERS, field_count=4)
        or not _sample_ok(new.services, expected=SERVICES, field_count=3)
        or not _sample_ok(new.timers, expected=TIMERS, field_count=4)
    ):
        return {"schema_version": VERSION, "state": "unknown",
                "reason": "invalid_observation", "cutover_authorized": False}

    # Unknown/masked/transitional old units cannot be treated as isolated.
    old_services_idle = all(
        fields == ("loaded", "inactive", "dead")
        for fields in old.services.values()
    )
    old_timers_disabled = all(
        fields == ("loaded", "disabled", "inactive", "dead")
        for fields in old.timers.values()
    )
    # New host is allowed to be unconfigured or inertly staged, not active.
    new_services_inert = all(
        fields in (("not-found", "inactive", "dead"),
                   ("loaded", "inactive", "dead"))
        for fields in new.services.values()
    )
    new_timers_inert = all(
        fields in (("not-found", "not-found", "inactive", "dead"),
                   ("loaded", "disabled", "inactive", "dead"))
        for fields in new.timers.values()
    )
    if not (old_services_idle and old_timers_disabled):
        state, reason = "blocked", "legacy_writer_may_run"
    elif not (new_services_inert and new_timers_inert):
        state, reason = "blocked", "new_writer_not_inert"
    else:
        # Deliberately NOT 'ready' or 'verified'. Credential and host
        # restart fencing cannot be inferred from unit states.
        state, reason = "candidate", "durable_fencing_unverified"
    return {
        "schema_version": VERSION,
        "state": state,
        "reason": reason,
        "cutover_authorized": False,
    }
