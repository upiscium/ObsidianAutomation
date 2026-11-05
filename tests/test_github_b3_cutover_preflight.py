"""Synthetic old/new systemd snapshots; never shell out or affect LXC."""
from __future__ import annotations

from dataclasses import replace

import pytest

from obsidian_automation.github_b3_cutover_preflight import (
    HostUnitSnapshot, SERVICES, TIMERS, VERSION,
    assess_b3_cutover_preflight,
)


def snapshot(host, when=1000.0, *, new=False):
    v = ("not-found", "inactive", "dead") if new else ("loaded", "inactive", "dead")
    timer = (
        ("not-found", "not-found", "inactive", "dead")
        if new else ("loaded", "disabled", "inactive", "dead")
    )
    return HostUnitSnapshot(
        host, when,
        {name: v for name in SERVICES},
        {name: timer for name in TIMERS},
    )


def assess(old=None, new=None, now=1001.0):
    return assess_b3_cutover_preflight(
        snapshot("obsidian-automation") if old is None else old,
        snapshot("obsidian-control", new=True) if new is None else new,
        now=now,
    )


def test_all_idle_is_only_candidate_not_authority():
    value = assess()
    assert value == {
        "schema_version": VERSION, "state": "candidate",
        "reason": "durable_fencing_unverified", "cutover_authorized": False,
    }


def test_active_legacy_github_sync_timer_blocks_even_if_writer_inactive():
    old = snapshot("obsidian-automation")
    old.timers["obsidian-github-sync.timer"] = ("loaded", "enabled", "active", "elapsed")
    value = assess(old)
    assert value["state"] == "blocked"
    assert value["reason"] == "legacy_writer_may_run"
    assert value["cutover_authorized"] is False


@pytest.mark.parametrize("name", SERVICES)
def test_each_legacy_service_active_or_failed_blocks(name):
    for state in (("loaded", "active", "running"),
                  ("loaded", "failed", "failed")):
        old = snapshot("obsidian-automation")
        old.services[name] = state
        assert assess(old)["state"] == "blocked"


@pytest.mark.parametrize("name", TIMERS)
def test_each_legacy_timer_active_blocks(name):
    old = snapshot("obsidian-automation")
    old.timers[name] = ("loaded", "enabled", "active", "waiting")
    assert assess(old)["state"] == "blocked"


@pytest.mark.parametrize("name", SERVICES)
def test_each_new_service_running_blocks(name):
    new = snapshot("obsidian-control", new=True)
    new.services[name] = ("loaded", "active", "running")
    value = assess(new=new)
    assert value["reason"] == "new_writer_not_inert"
    assert value["cutover_authorized"] is False


@pytest.mark.parametrize("name", TIMERS)
def test_each_new_timer_running_blocks(name):
    new = snapshot("obsidian-control", new=True)
    new.timers[name] = ("loaded", "enabled", "active", "waiting")
    assert assess(new=new)["state"] == "blocked"


@pytest.mark.parametrize("old_change", [
    {"host": "different-host"},
    {"captured_at": 900.0},
    {"captured_at": float("nan")},
    {"captured_at": 1002.0},
    {"services": {}},
    {"timers": {}},
])
def test_invalid_or_stale_observation_is_unknown(old_change):
    old = replace(snapshot("obsidian-automation"), **old_change)
    value = assess(old)
    assert value["state"] == "unknown"
    assert value["cutover_authorized"] is False


def test_missing_units_and_forged_wrong_types_fail_closed():
    old = snapshot("obsidian-automation")
    old.services.pop(next(iter(SERVICES)))
    assert assess(old)["state"] == "unknown"
    bad = snapshot("obsidian-automation")
    bad.timers["obsidian-github-sync.timer"] = [
        "loaded", "disabled", "inactive", "dead",
    ]
    assert assess(bad)["state"] == "unknown"
    assert assess(new=snapshot("fake-target", new=True))["state"] == "unknown"


@pytest.mark.parametrize(
    "bad_now", [float("nan"), float("inf"), -1, "now", True, 10 ** 1000, 1e308],
)
def test_bad_clock_does_not_authorize(bad_now):
    value = assess(now=bad_now)
    assert value["state"] == "unknown"
    assert value["cutover_authorized"] is False


def test_new_host_inertly_staged_units_are_not_old_fencing():
    new = snapshot("obsidian-control", new=True)
    new.services["obsidian-github-writer.service"] = ("loaded", "inactive", "dead")
    new.timers["obsidian-github-sync.timer"] = (
        "loaded", "disabled", "inactive", "dead",
    )
    value = assess(new=new)
    assert value["state"] == "candidate"
    assert value["cutover_authorized"] is False


def test_snapshot_has_no_secret_or_effect_surface():
    value = assess()
    assert set(value) == {"schema_version", "state", "reason", "cutover_authorized"}
    assert all(type(v) in (str, bool) for v in value.values())
    from obsidian_automation import github_b3_cutover_preflight as source
    assert not hasattr(source, "sync_project")
    assert not hasattr(source, "cutover")



def test_old_timer_disabled_at_runtime_but_enabled_at_boot_is_blocked():
    old = snapshot("obsidian-automation")
    old.timers["obsidian-github-sync.timer"] = (
        "loaded", "enabled", "inactive", "dead",
    )
    value = assess(old)
    assert value["state"] == "blocked"
    assert value["reason"] == "legacy_writer_may_run"


def test_new_timer_enabled_at_boot_is_not_inert():
    new = snapshot("obsidian-control", new=True)
    new.timers["obsidian-github-sync.timer"] = (
        "loaded", "enabled", "inactive", "dead",
    )
    assert assess(new=new)["reason"] == "new_writer_not_inert"
