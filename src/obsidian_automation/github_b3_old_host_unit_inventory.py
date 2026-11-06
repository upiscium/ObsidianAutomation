"""Offline old-LXC systemd show parser for *known* legacy writer units.

This code never calls systemctl or accesses the old host. It does not
enumerate unknown services, containers, cron jobs, non-systemd processes,
credentials, provider network routes or actual WebDAV authorization.
Even a fully masked unit inventory is NOT cutover authorization.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import re

VERSION = "github-b3-old-host-unit-inventory/v1"
# Source-derived from examples/github-sync and examples/promotion.
REQUIRED_UNITS = (
    "obsidian-core-promotion.service",
    "obsidian-core-promotion.timer",
    "obsidian-github-compactor.service",
    "obsidian-github-daily-apply.service",
    "obsidian-github-daily-progress.timer",
    "obsidian-github-sync.service",
    "obsidian-github-sync.timer",
    "obsidian-github-writer.service",
)
_BOOT = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_MAX_RAW = 1024
_MAX_TIME = 253402300799
_FRESH = 60
_PROPERTIES = ("Id", "LoadState", "ActiveState", "UnitFileState")
_EXPECTED = {"LoadState": "masked", "ActiveState": "inactive",
             "UnitFileState": "masked"}


@dataclass(frozen=True, slots=True)
class B3OldHostUnitCapture:
    """Untrusted systemctl show output; no proof of who ran the command."""

    unit: str
    command: tuple[str, ...]
    exit_code: int
    stdout: bytes
    stderr: bytes
    observed_at: int
    boot_id: str


def fixed_systemctl_show_args(unit: str) -> tuple[str, ...]:
    """Build only one allowlisted read-only command *argument vector*."""
    if type(unit) is not str or unit not in REQUIRED_UNITS:
        raise ValueError("not_a_known_legacy_unit")
    return (
        "systemctl", "show",
        "--property=Id,LoadState,ActiveState,UnitFileState", "--", unit,
    )


def _time(t: object, now: object, *, fresh: bool) -> bool:
    try:
        return (
            type(t) is int and type(now) in (int, float)
            and math.isfinite(now)
            and 0 <= t <= now <= _MAX_TIME
            and (not fresh or now - t <= _FRESH)
        )
    except (ValueError, TypeError, OverflowError):
        return False


def _valid_now(now: object) -> bool:
    try:
        return (
            type(now) in (int, float)
            and math.isfinite(now) and 0 <= now <= _MAX_TIME
        )
    except (TypeError, ValueError, OverflowError):
        return False


def _decode_show(raw: object, unit: str) -> dict[str, str]:
    if type(raw) is not bytes or not 0 < len(raw) <= _MAX_RAW:
        raise ValueError("invalid_systemctl_size")
    data = raw.decode("utf-8")
    if not data.endswith("\n") or "\r" in data or "\x00" in data:
        raise ValueError("invalid_systemctl_format")
    rows = data[:-1].split("\n")
    if len(rows) != len(_PROPERTIES):
        raise ValueError("invalid_systemctl_fields")
    result: dict[str, str] = {}
    for row in rows:
        if row.count("=") != 1:
            raise ValueError("invalid_systemctl_field")
        name, val = row.split("=", 1)
        if name not in _PROPERTIES or name in result or not val:
            raise ValueError("invalid_systemctl_field")
        result[name] = val
    if set(result) != set(_PROPERTIES) or result["Id"] != unit:
        raise ValueError("systemctl_identity_mismatch")
    return result


def _result(state: str, reason: str) -> dict[str, object]:
    return {
        "schema_version": VERSION,
        "state": state,
        "reason": reason,
        "cutover_authorized": False,
    }


def assess_legacy_old_host_units(
    captures: tuple[B3OldHostUnitCapture, ...],
    *,
    boot_id: str,
    previous_boot_id: str,
    fenced_at: int,
    restarted_at: int,
    now: float,
) -> dict[str, object]:
    """Classify a complete *claimed* fixed-unit inventory; never authorize.

    Checking a caller-supplied boot ID is not authenticated reboot proof.
    A masked unit can be bypassed by another process or another credential.
    """
    try:
        if (
            type(captures) is not tuple
            or len(captures) != len(REQUIRED_UNITS)
            or type(boot_id) is not str or _BOOT.fullmatch(boot_id) is None
            or type(previous_boot_id) is not str
            or _BOOT.fullmatch(previous_boot_id) is None
            or boot_id == previous_boot_id
            or not _valid_now(now)
            or not _time(fenced_at, now, fresh=False)
            or not _time(restarted_at, now, fresh=False)
            or not fenced_at < restarted_at
        ):
            return _result("unknown", "invalid_old_host_scope")
        names: set[str] = set()
        for capture in captures:
            if (
                type(capture) is not B3OldHostUnitCapture
                or capture.unit not in REQUIRED_UNITS
                or capture.unit in names
                or capture.command != fixed_systemctl_show_args(capture.unit)
                or type(capture.exit_code) is not int or capture.exit_code != 0
                or type(capture.stderr) is not bytes or capture.stderr != b""
                or capture.boot_id != boot_id
                or not _time(capture.observed_at, now, fresh=True)
                or capture.observed_at < restarted_at
            ):
                return _result("unknown", "unverified_old_host_capture")
            names.add(capture.unit)
            properties = _decode_show(capture.stdout, capture.unit)
            if any(properties[k] != v for k, v in _EXPECTED.items()):
                return _result("blocked", "legacy_unit_not_masked_and_inactive")
        if names != set(REQUIRED_UNITS):
            return _result("unknown", "incomplete_known_unit_inventory")
        return _result("candidate", "old_host_origin_and_nonunit_writers_unverified")
    except (TypeError, ValueError, OverflowError, KeyError,
            AttributeError, UnicodeError, RecursionError):
        return _result("unknown", "unverified_old_host_capture")
