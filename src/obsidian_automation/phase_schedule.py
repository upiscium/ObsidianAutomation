"""JST wall-clock phase scheduler for the three persisted Jobs.

V1 supports Asia/Tokyo only (no DST). The duration must divide a full
local day, avoiding phase drift at midnight. Every operation is pure.
The existing legacy interval mode is preserved until an explicit update.
"""
from __future__ import annotations

import math
import re
import sqlite3
from typing import Mapping

TZ = "Asia/Tokyo"
_JST_OFFSET = 9 * 3600
_DAY = 86400
_ANCHOR = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]\Z")
_GRACE_SECONDS = 5.0


class StaleScheduleError(ValueError):
    """An HTTP settings update was based on an obsolete revision."""


def validate_phase(timezone: object, anchor: object,
                   interval: object, *, minimum: int = 60):
    if (timezone != TZ or type(anchor) is not str or not _ANCHOR.fullmatch(anchor)
        or type(interval) is not int or interval < minimum
        or interval > _DAY or interval % 60 != 0
        or _DAY % interval != 0):
        raise ValueError("invalid_anchored_schedule")
    return timezone, anchor


def next_slot_after(now: float, interval: int, anchor: str,
                    timezone: str = TZ) -> float:
    validate_phase(timezone, anchor, interval)
    if not isinstance(now, (int, float)) or not math.isfinite(now):
        raise ValueError("invalid_schedule_clock")
    hours, minutes = map(int, anchor.split(":"))
    midnight = math.floor((now + _JST_OFFSET) / _DAY) * _DAY - _JST_OFFSET
    offset = midnight + (hours * 60 + minutes) * 60
    phase = math.floor((now - offset) / interval) + 1
    return float(offset + phase * interval)


def preview(timezone: object, anchor: object, interval: object,
            now: float, *, minimum: int = 60) -> dict:
    validate_phase(timezone, anchor, interval, minimum=minimum)
    first = next_slot_after(now, interval, anchor)
    return {"version": 1, "mode": "anchored", "timezone": TZ,
            "anchor_local_time": anchor, "interval_seconds": interval,
            "next_slots": [first + i * interval for i in range(3)]}


def upgrade_settings(db: sqlite3.Connection):
    """Add schema columns without touching existing enabled/next_due values."""
    existing = {r[1] for r in db.execute("PRAGMA table_info(settings)")}
    for column, definition in (
        ("schedule_tz", "TEXT"),
        ("schedule_anchor", "TEXT"),
        ("schedule_revision", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if column not in existing:
            db.execute(f"ALTER TABLE settings ADD COLUMN {column} {definition}")


def schedule_status(timezone: str | None, anchor: str | None,
                    revision: int) -> dict:
    return {"mode": "anchored" if timezone == TZ and anchor is not None
            else "legacy_interval",
            "timezone": timezone,
            "anchor_local_time": anchor,
            "revision": revision}


def parse_update(payload: object, *, prefix: frozenset[str],
                 minimum: int, old: Mapping[str, object]):
    """Return (enabled, interval, timezone, anchor) with revision check.

    Old exact payloads preserve the current phase (internal pause/back-compat).
    HTTP-level legacy writes to anchored settings are rejected by the server.
    """
    if type(payload) is not dict or not prefix <= payload.keys():
        raise ValueError("invalid_schedule_payload")
    legacy = set(payload) == prefix
    anchored = set(payload) == prefix | {
        "timezone", "anchor_local_time", "expected_revision"}
    if not (legacy or anchored):
        raise ValueError("invalid_schedule_payload")
    enabled, interval = payload["enabled"], payload["interval_seconds"]
    if type(enabled) is not bool or type(interval) is not int or not minimum <= interval <= _DAY:
        raise ValueError("invalid_schedule_payload")
    current = old["schedule"]
    if anchored:
        validate_phase(payload["timezone"],payload["anchor_local_time"],interval,
                       minimum=minimum)
        revision = payload["expected_revision"]
        if type(revision) is not int or revision < 0:
            raise ValueError("invalid_schedule_revision")
        if revision != current["revision"]:
            raise StaleScheduleError("stale_settings")
        return enabled, interval, TZ, payload["anchor_local_time"]
    tz = current["timezone"]
    anchor = current["anchor_local_time"]
    if tz is not None:
        # Trusted internal pauses use the old schema. Do not drop the phase.
        validate_phase(tz, anchor, interval, minimum=minimum)
    return enabled, interval, tz, anchor


def due_for_update(now: float, old: Mapping[str, object], enabled: bool,
                   interval: int, tz: str | None, anchor: str | None) -> float | None:
    if not enabled:
        return None
    current = old["schedule"]
    if (old["enabled"] and old["interval_seconds"] == interval
        and current["timezone"] == tz
        and current["anchor_local_time"] == anchor
        and old["next_due"] is not None):
        return old["next_due"]
    return next_slot_after(now,interval,anchor,tz) if tz else now + interval


def due_after_tick(now: float, interval: int,
                   schedule: Mapping[str, object]) -> float:
    if schedule["mode"] == "anchored":
        return next_slot_after(now, interval, schedule["anchor_local_time"],
                               schedule["timezone"])
    return now + interval


def missed(now: float, due: float, schedule: Mapping[str, object]) -> bool:
    """Skip missed phase slots rather than catching up after restart/outage."""
    return (schedule["mode"] == "anchored"
            and now - due > _GRACE_SECONDS)
