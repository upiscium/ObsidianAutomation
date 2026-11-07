"""Pure anchored cadence; no Job, timer, credentials or Vault effects."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from obsidian_automation.phase_schedule import (
    InvalidPhaseSchedule, PhaseSchedule, next_due_at, phase_slots, preview_json,
)


def spec(**changes):
    data = {
        "enabled": True,
        "timezone": "Asia/Tokyo",
        "anchor_local_time": "12:00",
        "interval_seconds": 3600,
    }
    data.update(changes)
    return PhaseSchedule(**data)


def local(s: str, zone="Asia/Tokyo"):
    return datetime.fromisoformat(s).replace(tzinfo=ZoneInfo(zone))


def labels(values, zone="Asia/Tokyo"):
    return tuple(x.astimezone(ZoneInfo(zone)).strftime("%Y-%m-%d %H:%M") for x in values)


def test_1159_starts_at_noon_then_each_hour():
    assert labels(phase_slots(spec(), local("2026-11-06T11:59"))) == (
        "2026-11-06 12:00", "2026-11-06 13:00", "2026-11-06 14:00",
    )


def test_save_at_1237_does_not_drift_to_1337():
    result = phase_slots(spec(), local("2026-11-06T12:37"))
    assert labels(result) == (
        "2026-11-06 13:00", "2026-11-06 14:00", "2026-11-06 15:00",
    )
    assert result[0] == datetime(2026, 11, 6, 4, 0, tzinfo=timezone.utc)


def test_exact_slot_is_not_duplicated_as_future_after_execution():
    now = local("2026-11-06T12:00")
    assert labels(phase_slots(spec(), now))[:2] == (
        "2026-11-06 13:00", "2026-11-06 14:00",
    )
    assert labels(phase_slots(spec(), now, count=1, include_now=True)) == (
        "2026-11-06 12:00",
    )


def test_midnight_wrap_stays_phase_locked():
    assert labels(phase_slots(spec(), local("2026-11-06T23:59"))) == (
        "2026-11-07 00:00", "2026-11-07 01:00", "2026-11-07 02:00",
    )


def test_utc_host_clock_does_not_change_japan_anchor():
    local_clock = local("2026-11-06T11:59")
    utc_clock = local_clock.astimezone(timezone.utc)
    assert phase_slots(spec(), local_clock) == phase_slots(spec(), utc_clock)


def test_pause_hides_next_due_but_preview_remains_inspectable():
    disabled = spec(enabled=False)
    assert next_due_at(disabled, local("2026-11-06T11:59")) is None
    preview = preview_json(disabled, local("2026-11-06T11:59"))
    assert preview["enabled"] is False
    assert preview["nextDueAt"] is None
    assert preview["nextThreeLocal"][0] == "2026-11-06T12:00:00+09:00"


def test_persisted_reconstructed_schedule_has_identical_slot():
    a = spec()
    b = replace(a)
    now = local("2026-11-06T12:37")
    assert phase_slots(a, now) == phase_slots(b, now)
    assert next_due_at(a, now) == next_due_at(b, now)


def test_two_hour_schedule_still_uses_anchor():
    result = phase_slots(spec(interval_seconds=7200), local("2026-11-06T11:59"))
    assert labels(result) == (
        "2026-11-06 12:00", "2026-11-06 14:00", "2026-11-06 16:00",
    )


def test_daily_schedule_stays_at_noon():
    result = phase_slots(spec(interval_seconds=86400), local("2026-11-06T12:01"))
    assert labels(result) == (
        "2026-11-07 12:00", "2026-11-08 12:00", "2026-11-09 12:00",
    )


def test_90_minutes_divides_day_and_maintains_phase():
    result = phase_slots(spec(interval_seconds=5400), local("2026-11-06T11:59"))
    assert labels(result) == (
        "2026-11-06 12:00", "2026-11-06 13:30", "2026-11-06 15:00",
    )


def test_spring_forward_skips_nonexistent_local_slot():
    ny="America/New_York"
    now=local("2026-03-08T01:30", ny)
    result=phase_slots(spec(timezone=ny), now)
    assert labels(result, ny) == (
        "2026-03-08 03:00", "2026-03-08 04:00", "2026-03-08 05:00",
    )


def test_fall_back_ambiguous_wall_hour_occurs_only_once():
    ny="America/New_York"
    now=local("2026-11-01T00:30", ny)
    result=phase_slots(spec(timezone=ny), now, count=4)
    assert labels(result, ny) == (
        "2026-11-01 01:00", "2026-11-01 02:00",
        "2026-11-01 03:00", "2026-11-01 04:00",
    )
    assert result[0].isoformat() == "2026-11-01T05:00:00+00:00"
    assert result[1].isoformat() == "2026-11-01T07:00:00+00:00"


@pytest.mark.parametrize("changes", (
    {"enabled": 1},
    {"enabled": None},
    {"timezone": "../Asia/Tokyo"},
    {"timezone": "Missing/Timezone"},
    {"timezone": ""},
    {"timezone": True},
    {"anchor_local_time": "24:00"},
    {"anchor_local_time": "12:60"},
    {"anchor_local_time": "12:0"},
    {"anchor_local_time": "12:00:00"},
    {"interval_seconds": 0},
    {"interval_seconds": 60.0},
    {"interval_seconds": True},
    {"interval_seconds": 70},
    {"interval_seconds": 4200},
    {"interval_seconds": -3600},
    {"interval_seconds": 86460},
))
def test_invalid_schedule_rejected(changes):
    with pytest.raises(InvalidPhaseSchedule, match="invalid_phase_schedule"):
        phase_slots(spec(**changes), local("2026-11-06T11:59"))


@pytest.mark.parametrize("bad_now", (
    datetime(2026,11,6,12,0),
    "2026-11-06T12:00",
    None,
    True,
    object(),
))
def test_reject_naive_or_non_datetime(bad_now):
    with pytest.raises(InvalidPhaseSchedule):
        phase_slots(spec(), bad_now)


@pytest.mark.parametrize("count", (0, 11, True, 3.0, -1))
def test_reject_unbounded_preview_count(count):
    with pytest.raises(InvalidPhaseSchedule):
        phase_slots(spec(), local("2026-11-06T11:59"), count=count)


def test_preview_shape_stable_and_redacted():
    result = preview_json(spec(), local("2026-11-06T11:59"))
    assert result == {
        "schemaVersion": "phase-schedule/v1",
        "timezone": "Asia/Tokyo",
        "anchorLocalTime": "12:00",
        "intervalSeconds": 3600,
        "enabled": True,
        "nextDueAt": "2026-11-06T03:00:00+00:00",
        "nextThreeLocal": (
            "2026-11-06T12:00:00+09:00",
            "2026-11-06T13:00:00+09:00",
            "2026-11-06T14:00:00+09:00",
        ),
    }



def test_hourly_noon_anchor_is_a_phase_not_a_first_start_bound():
    """At 08:37 it is 09/10/11; an activation bound is a separate contract."""
    assert labels(phase_slots(spec(), local("2026-11-06T08:37"))) == (
        "2026-11-06 09:00", "2026-11-06 10:00", "2026-11-06 11:00",
    )


def test_timezone_invariant_when_utc_date_differs_from_local_date():
    japan = local("2026-11-07T00:30")
    utc = japan.astimezone(timezone.utc)
    assert utc.date() != japan.date()
    assert phase_slots(spec(), japan) == phase_slots(spec(), utc)


@pytest.mark.parametrize("untrusted_key", (
    "America", "posix/Asia/Tokyo", "right/Asia/Tokyo",
    "localtime", "posixrules",
))
def test_reject_noniana_directory_or_leap_time_zone_aliases(untrusted_key):
    with pytest.raises(InvalidPhaseSchedule, match="invalid_phase_schedule"):
        phase_slots(spec(timezone=untrusted_key), local("2026-11-06T11:59"))


def test_timezone_filesystem_io_error_is_redacted(monkeypatch):
    import obsidian_automation.phase_schedule as module
    def deny_zone(_):
        raise PermissionError("secret local timezone path")
    monkeypatch.setattr(module, "ZoneInfo", deny_zone)
    with pytest.raises(InvalidPhaseSchedule, match="^invalid_phase_schedule$"):
        phase_slots(spec(), local("2026-11-06T11:59"))
