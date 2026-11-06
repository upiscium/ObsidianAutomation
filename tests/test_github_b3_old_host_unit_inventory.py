"""Pure old-host unit state parser; no access to systemctl or any LXC."""
from __future__ import annotations

from dataclasses import replace

import pytest

from obsidian_automation.github_b3_old_host_unit_inventory import (
    VERSION, REQUIRED_UNITS, B3OldHostUnitCapture,
    assess_legacy_old_host_units, fixed_systemctl_show_args,
)

NOW = 1_800_000_000
BOOT = "12345678-1234-1234-1234-1234567890ab"
PREV = "abcdef12-abcd-abcd-abcd-abcdef123456"


def capture(unit, *, state="inactive", load="masked", enabled="masked"):
    return B3OldHostUnitCapture(
        unit=unit,
        command=fixed_systemctl_show_args(unit),
        exit_code=0,
        stdout=(
            f"Id={unit}\nLoadState={load}\nActiveState={state}\n"
            f"UnitFileState={enabled}\n"
        ).encode(),
        stderr=b"",
        observed_at=NOW - 1,
        boot_id=BOOT,
    )


def fixture():
    return tuple(capture(x) for x in REQUIRED_UNITS)


def assess(captures, *, boot_id=BOOT, previous_boot_id=PREV,
           fenced_at=NOW - 300, restarted_at=NOW - 60, now=NOW):
    result = assess_legacy_old_host_units(
        captures, boot_id=boot_id, previous_boot_id=previous_boot_id,
        fenced_at=fenced_at, restarted_at=restarted_at, now=now,
    )
    assert result["schema_version"] == VERSION
    assert result["cutover_authorized"] is False
    assert set(result) == {
        "schema_version", "state", "reason", "cutover_authorized",
    }
    return result


def test_synthetic_masked_inactive_units_are_not_actual_host_exclusion():
    assert assess(fixture()) == {
        "schema_version": VERSION,
        "state": "candidate",
        "reason": "old_host_origin_and_nonunit_writers_unverified",
        "cutover_authorized": False,
    }


@pytest.mark.parametrize("unit", REQUIRED_UNITS)
def test_missing_unit_never_claims_complete_old_host(unit):
    values = tuple(c for c in fixture() if c.unit != unit)
    assert assess(values)["state"] == "unknown"


@pytest.mark.parametrize("unit", REQUIRED_UNITS)
def test_one_active_writer_or_timer_blocks(unit):
    rows = list(fixture())
    i = REQUIRED_UNITS.index(unit)
    rows[i] = capture(unit, state="active")
    assert assess(tuple(rows))["state"] == "blocked"


@pytest.mark.parametrize("unit", REQUIRED_UNITS)
def test_one_disabled_but_not_masked_is_insufficient(unit):
    rows = list(fixture())
    i = REQUIRED_UNITS.index(unit)
    rows[i] = capture(unit, load="loaded", enabled="disabled")
    assert assess(tuple(rows))["state"] == "blocked"


@pytest.mark.parametrize("change", (
    {"exit_code": 1},
    {"exit_code": True},
    {"stderr": b"error"},
    {"observed_at": NOW - 120},
    {"observed_at": NOW + 1},
    {"boot_id": PREV},
    {"command": ("systemctl", "stop", "obsidian-github-writer.service")},
    {"command": ()},
    {"unit": "unrelated.service"},
))
def test_bad_capture_metadata_refused(change):
    rows = list(fixture())
    rows[0] = replace(rows[0], **change)
    assert assess(tuple(rows))["state"] == "unknown"


@pytest.mark.parametrize("bad", (
    b"", b"Id=whatever\n", b"not-key-value\n",
    b"Id=test.service\nId=test.service\n",
    b"Id=x\r\nLoadState=masked\n",
    b"Id=x\n" + b"x" * 1100,
    b"Id=x\nLoadState=masked\nActiveState=inactive\n"
    b"UnitFileState=masked\nExtra=flag\n",
    b"\xff",
))
def test_invalid_unit_output_never_accepted(bad):
    rows = list(fixture())
    rows[0] = replace(rows[0], stdout=bad)
    assert assess(tuple(rows))["state"] == "unknown"


def test_duplicate_unit_unknown():
    rows = list(fixture())
    rows[-1] = rows[0]
    assert assess(tuple(rows))["state"] == "unknown"


@pytest.mark.parametrize("kw", (
    {"boot_id": "unknown"},
    {"previous_boot_id": BOOT},
    {"fenced_at": NOW - 30},
    {"restarted_at": NOW + 1},
    {"restarted_at": NOW - 301},
    {"restarted_at": 1e100},
    {"now": float("nan")},
    {"now": 10**1000},
))
def test_unverified_boot_fence_time_denied(kw):
    assert assess(fixture(), **kw)["state"] == "unknown"


def test_float_clock_accepted_without_promoting_authority():
    result = assess(fixture(), now=NOW + 0.5)
    assert result["state"] == "candidate"
    assert result["cutover_authorized"] is False


def test_no_host_access_and_all_known_source_units_are_fixed():
    from obsidian_automation import github_b3_old_host_unit_inventory as module
    assert "obsidian-core-promotion.timer" in REQUIRED_UNITS
    assert "obsidian-github-daily-apply.service" in REQUIRED_UNITS
    assert "obsidian-github-writer.service" in REQUIRED_UNITS
    assert len(REQUIRED_UNITS) == len(set(REQUIRED_UNITS))
    for name in ("run_systemctl", "subprocess", "restart", "stop",
                 "mask_unit", "connect_host", "cutover"):
        assert not hasattr(module, name)


def test_command_template_is_nonexecuting_allowlist():
    assert fixed_systemctl_show_args("obsidian-github-writer.service") == (
        "systemctl", "show",
        "--property=Id,LoadState,ActiveState,UnitFileState",
        "--", "obsidian-github-writer.service",
    )
    for unit in ("../writer.service", "--user", "arbitrary.service", ""):
        with pytest.raises(ValueError):
            fixed_systemctl_show_args(unit)
