"""Pure, unattached phase-locked wall-clock schedule preview for #533.

This code computes candidate slots only. It does NOT schedule a timer,
persist settings, admit a job, touch a Vault, or authorize Writer effects.

The anchor is a *repeating daily clock phase*, not a first-activation
lower bound. For hourly Asia/Tokyo / 12:00 the phase includes every whole
hour (00:00 through 23:00); at 08:37 the future slots are 09/10/11,
not 12/13/14. A product wanting "first-ever execution no earlier than
the next noon" needs a separate persisted activation/start boundary.
This module must not silently infer that boundary from the saved time.

The first MVI accepts minute-granularity intervals that divide a local day
exactly; this guarantees a stable daily anchor even across service restarts.
For IANA regions with DST, nonexistent wall slots are skipped and ambiguous
wall slots are represented once (first occurrence / fold=0). The runtime
scheduler must separately decide catch-up policy and durably fence each slot.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_VERSION = "phase-schedule/v1"
_HHMM = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])\Z")
_ZONE = re.compile(r"[A-Za-z0-9_.+-]+(?:/[A-Za-z0-9_.+-]+)*\Z")
_MINUTE_SECONDS = 60
_DAY_SECONDS = 86400
_MAX_PREVIEW = 10


class InvalidPhaseSchedule(ValueError):
    """Deterministic nonsecret invalid-input refusal."""

    def __init__(self) -> None:
        super().__init__("invalid_phase_schedule")


@dataclass(frozen=True, slots=True)
class PhaseSchedule:
    """User-configurable desired cadence, not an execution permission."""

    enabled: bool
    timezone: str
    anchor_local_time: str
    interval_seconds: int


def _validate(spec: object) -> tuple[PhaseSchedule, ZoneInfo, int]:
    if (
        type(spec) is not PhaseSchedule
        or type(spec.enabled) is not bool
        or type(spec.timezone) is not str
        or not 1 <= len(spec.timezone) <= 64
        or _ZONE.fullmatch(spec.timezone) is None
        or ".." in spec.timezone.split("/")
        or spec.timezone.startswith(("posix/", "right/"))
        or spec.timezone in ("localtime", "posixrules")
        or type(spec.anchor_local_time) is not str
        or _HHMM.fullmatch(spec.anchor_local_time) is None
        or type(spec.interval_seconds) is not int
        or spec.interval_seconds < _MINUTE_SECONDS
        or spec.interval_seconds > _DAY_SECONDS
        or spec.interval_seconds % _MINUTE_SECONDS != 0
        or _DAY_SECONDS % spec.interval_seconds != 0
    ):
        raise InvalidPhaseSchedule()
    try:
        zone = ZoneInfo(spec.timezone)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        # An IANA lookup must never leak filesystem/provider exceptions to
        # an eventual WebUI preview endpoint.
        raise InvalidPhaseSchedule() from None
    hour, minute = (int(x) for x in spec.anchor_local_time.split(":"))
    return spec, zone, hour * 60 + minute


def phase_slots(
    spec: PhaseSchedule,
    now: datetime,
    *,
    count: int = 3,
    include_now: bool = False,
) -> tuple[datetime, ...]:
    """Return fixed future UTC slots, not relative to last run or save time.

    No catch-up, persistence or executor is part of this pure preview.
    `now` must be timezone-aware; the selected timezone determines the
    local dates and wall-clock anchor. Timezone can be IANA but not inferred
    from the host's TZ setting.
    """
    _, zone, anchor_minute = _validate(spec)
    if (
        type(now) is not datetime
        or now.tzinfo is None
        or now.utcoffset() is None
        or type(count) is not int
        or not 1 <= count <= _MAX_PREVIEW
        or type(include_now) is not bool
    ):
        raise InvalidPhaseSchedule()
    try:
        now_utc = now.astimezone(timezone.utc)
        today = now.astimezone(zone).date()
        day_step = spec.interval_seconds // 60
        candidates: list[datetime] = []
        for day_offset in range(count + 2):
            local_day = today + timedelta(days=day_offset)
            for i in range(_DAY_SECONDS // spec.interval_seconds):
                minute_of_day = (anchor_minute + i * day_step) % 1440
                local_naive = datetime.combine(
                    local_day,
                    time(minute_of_day // 60, minute_of_day % 60),
                )
                local_aware = local_naive.replace(tzinfo=zone, fold=0)
                instant = local_aware.astimezone(timezone.utc)
                # A nonexistent spring-forward wall time cannot produce
                # the same local date/hour after a UTC roundtrip.
                if instant.astimezone(zone).replace(tzinfo=None) != local_naive:
                    continue
                if instant > now_utc or (include_now and instant == now_utc):
                    candidates.append(instant)
            if len(candidates) >= count:
                break
        candidates.sort()
        if len(candidates) < count or len(set(candidates)) < count:
            raise InvalidPhaseSchedule()
        return tuple(candidates[:count])
    except (OverflowError, ValueError, TypeError):
        raise InvalidPhaseSchedule() from None


def next_due_at(spec: PhaseSchedule, now: datetime) -> datetime | None:
    """First strictly future slot when enabled; Off has no next admission."""
    if not _validate(spec)[0].enabled:
        return None
    return phase_slots(spec, now, count=1)[0]


def preview_json(spec: PhaseSchedule, now: datetime) -> dict[str, object]:
    """Versioned, bounded schedule preview; no persistence or side effects."""
    _, zone, _ = _validate(spec)
    due = phase_slots(spec, now, count=3)
    return {
        "schemaVersion": _VERSION,
        "timezone": spec.timezone,
        "anchorLocalTime": spec.anchor_local_time,
        "intervalSeconds": spec.interval_seconds,
        "enabled": spec.enabled,
        "nextDueAt": due[0].isoformat() if spec.enabled else None,
        "nextThreeLocal": tuple(
            value.astimezone(zone).isoformat() for value in due
        ),
    }
