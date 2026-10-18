"""Synthetic fail-closed run-once admission for fixed single-LXC workflows."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import re
import sys

import pytest

from obsidian_automation.admin_status import SERVICES
from obsidian_automation.admin_workflow_quiescence import (
    AI_UNITS, CORE_UNITS, GITHUB_UNITS, UNIT_NAMES,
    GROUP_UNITS, TIMER_TARGETS, TIMER_READ_FIELDS, WORKFLOW_READ_FIELDS,
    WorkflowObservation, WorkflowNotQuiescent, require_run_once_quiescent,
)

ROOT = Path(__file__).resolve().parents[1]


def fixture_for(spec):
    timers = {
        timer: {
            "LoadState": "loaded", "UnitFileState": "disabled",
            "ActiveState": "inactive", "SubState": "dead",
            "Unit": TIMER_TARGETS[timer], "Job": "0",
            "FragmentPath": f"/etc/systemd/system/{timer}",
        }
        for timer in (SERVICES[0].timer, SERVICES[1].timer)
        if spec.identifier in ("ai-vault-pull", "pre-review")
    }
    if spec.identifier in ("github-sync", "github-daily"):
        timers = {
            timer: {
                "LoadState": "loaded", "UnitFileState": "disabled",
                "ActiveState": "inactive", "SubState": "dead",
                "Unit": TIMER_TARGETS[timer], "Job": "0",
                "FragmentPath": f"/etc/systemd/system/{timer}",
            }
            for timer in (SERVICES[2].timer, SERVICES[3].timer)
        }
    if spec.identifier == "core-promotion":
        timers = {
            SERVICES[4].timer: {
                "LoadState": "loaded", "UnitFileState": "disabled",
                "ActiveState": "inactive", "SubState": "dead",
                "Unit": SERVICES[4].service, "Job": "0",
                "FragmentPath": f"/etc/systemd/system/{SERVICES[4].timer}",
            }
        }
    services = {
        unit: {
            "LoadState": "loaded", "ActiveState": "inactive", "SubState": "dead",
            "Type": "oneshot", "Job": "0", "MainPID": "0", "ControlPID": "0",
            "FragmentPath": f"/etc/systemd/system/{unit}",
        }
        for unit in GROUP_UNITS[spec.identifier]
    }
    return WorkflowObservation(timers=timers, services=services)


@pytest.mark.parametrize("spec", SERVICES, ids=[s.identifier for s in SERVICES])
def test_all_five_roles_quiescent_when_expected(spec):
    assert require_run_once_quiescent(spec, fixture_for(spec)) is None


@pytest.mark.parametrize("spec", SERVICES, ids=[s.identifier for s in SERVICES])
def test_each_group_has_a_nonempty_fixed_service_set_and_pinned_timer(spec):
    obs = fixture_for(spec)
    assert len(obs.timers) >= 1
    assert spec.service in obs.services
    assert spec.timer in obs.timers
    assert all(unit in UNIT_NAMES for unit in obs.services)


@pytest.mark.parametrize("field,value", [
    ("LoadState", "not-found"),
    ("UnitFileState", "masked"),
    ("UnitFileState", "enabled"),
    ("ActiveState", "active"),
    ("SubState", "waiting"),
    ("SubState", "running"),
    ("Unit", "sshd.service"),
    ("Job", "3"),
    ("FragmentPath", "/run/systemd/generator/evil.timer"),
])
def test_each_related_timer_must_be_paused_and_authenticated(field, value):
    spec = SERVICES[2]
    snap = fixture_for(spec)
    sibling = SERVICES[3].timer
    snap.timers[sibling][field] = value
    with pytest.raises(WorkflowNotQuiescent, match="workflow_timer_not_quiescent"):
        require_run_once_quiescent(spec, snap)


@pytest.mark.parametrize("field,value", [
    ("LoadState", "not-found"),
    ("ActiveState", "activating"),
    ("ActiveState", "failed"),
    ("SubState", "running"),
    ("Type", "notify"),
    ("Job", "24"),
    ("MainPID", "4"),
    ("ControlPID", "5"),
    ("FragmentPath", "/tmp/evaded.service"),
])
def test_any_related_service_pending_or_running_fails_closed(field, value):
    spec = SERVICES[1]
    snap = fixture_for(spec)
    # The last service is an AI post-review dependency, not the visible final
    # timer target. One active dependency must block the entire run_once.
    member = "obsidian-ai-post-review-transport.service"
    snap.services[member][field] = value
    with pytest.raises(WorkflowNotQuiescent, match="workflow_service_not_quiescent"):
        require_run_once_quiescent(spec, snap)


def test_semantic_on_success_dependency_also_blocks_pre_review():
    spec = SERVICES[1]
    snap = fixture_for(spec)
    assert "obsidian-semantic-index-refresh-embed.service" in snap.services
    snap.services["obsidian-semantic-index-refresh-embed.service"]["Job"] = "9"
    with pytest.raises(WorkflowNotQuiescent, match="workflow_service_not_quiescent"):
        require_run_once_quiescent(spec, snap)


def test_github_daily_and_project_sync_share_single_domain():
    assert GROUP_UNITS["github-daily"] is GROUP_UNITS["github-sync"]
    snap = fixture_for(SERVICES[3])
    snap.services["obsidian-github-writer.service"]["ActiveState"] = "activating"
    with pytest.raises(WorkflowNotQuiescent):
        require_run_once_quiescent(SERVICES[3], snap)


@pytest.mark.parametrize("bad", [
    None, {}, [], True, "loaded",
    WorkflowObservation(timers={}, services={}),
    WorkflowObservation(timers={"bad.timer": {}}, services={}),
])
def test_missing_or_malformed_snapshot_fails_closed(bad):
    with pytest.raises(WorkflowNotQuiescent):
        require_run_once_quiescent(SERVICES[0], bad)


def test_extra_service_or_unexpected_field_fails_closed():
    snap = fixture_for(SERVICES[4])
    snap.services["sshd.service"] = {}
    with pytest.raises(WorkflowNotQuiescent, match="workflow_inventory_unverified"):
        require_run_once_quiescent(SERVICES[4], snap)
    snap = fixture_for(SERVICES[4])
    snap.services[SERVICES[4].service]["ExecStart"] = "private-secret"
    with pytest.raises(WorkflowNotQuiescent, match="workflow_service_unverified"):
        require_run_once_quiescent(SERVICES[4], snap)


@pytest.mark.parametrize("field,table", [
    ("Job", "services"),
    ("Job", "timers"),
    ("MainPID", "services"),
    ("FragmentPath", "services"),
])
def test_no_untrusted_or_oversize_output_can_pass(field, table):
    snap = fixture_for(SERVICES[4])
    records = getattr(snap, table)
    records[next(iter(records))][field] = "very-long-" * 60
    with pytest.raises(WorkflowNotQuiescent):
        require_run_once_quiescent(SERVICES[4], snap)


def test_role_registry_matches_runtime_installed_service_inventory():
    # Static cross-check against the systemd installer. Prevents forgetting
    # newly added pipeline dependencies when changing the runtime manifest.
    module_path = ROOT / "tools/host_runtime_lifecycle.py"
    spec = importlib.util.spec_from_file_location("_test_host_runtime_services", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        assert len(UNIT_NAMES) == len(AI_UNITS) + len(GITHUB_UNITS) + len(CORE_UNITS)
        assert set(module.SERVICES) == UNIT_NAMES
        assert set(module.TIMERS) == set(TIMER_TARGETS)
    finally:
        del sys.modules[spec.name]


def test_fixture_links_static_requirements_and_on_success_chains():
    # Parse only *reviewed source fixtures* at test time, never dynamic
    # installed systemd unit files at runtime or arbitrary machine paths.
    domains = {
        "ai": AI_UNITS,
        "github-sync": GITHUB_UNITS,
        "promotion": CORE_UNITS,
    }
    for folder, members in domains.items():
        for name in members:
            source = ROOT / "examples" / folder / name
            assert source.is_file(), name
            text = source.read_text(encoding="utf-8")
            for label in ("Requires", "OnSuccess"):
                values = re.findall(rf"(?m)^{label}=([^\n]+)$", text)
                for value in values:
                    for dependency in value.split():
                        if dependency.endswith(".service"):
                            assert dependency in UNIT_NAMES, (name, dependency)


def test_unknown_static_spec_is_not_an_operator_entrypoint():
    from obsidian_automation.admin_status import ServiceSpec
    rogue = ServiceSpec("sshd", "Rogue", "sshd.timer", "sshd.service")
    with pytest.raises(WorkflowNotQuiescent, match="workflow_identity_unverified"):
        require_run_once_quiescent(rogue, fixture_for(SERVICES[0]))


def test_subclassed_mapping_cannot_inject_custom_iter_behavior():
    class MaliciousMapping(dict):
        def __iter__(self):
            raise RuntimeError("secret from controlled map")
    good = fixture_for(SERVICES[2])
    bad = WorkflowObservation(
        timers=MaliciousMapping(good.timers), services=good.services,
    )
    with pytest.raises(WorkflowNotQuiescent, match="workflow_inventory_unverified"):
        require_run_once_quiescent(SERVICES[2], bad)


def test_string_subclass_keys_are_rejected_without_calling_custom_hash():
    class Key(str):
        reject = False
        def __hash__(self):
            if self.reject:
                raise RuntimeError("untrusted hash side effect")
            return super().__hash__()
    good = fixture_for(SERVICES[4])
    first = SERVICES[4].timer
    malicious = Key(first)
    timers = dict(good.timers)
    previous = timers.pop(first)
    timers[malicious] = previous
    malicious.reject = True
    with pytest.raises(WorkflowNotQuiescent, match="workflow_inventory_unverified"):
        require_run_once_quiescent(
            SERVICES[4],
            WorkflowObservation(timers=timers, services=good.services),
        )


@pytest.mark.parametrize("spec", SERVICES, ids=[s.identifier for s in SERVICES])
def test_real_systemctl_empty_job_properties_are_idle(spec):
    obs = fixture_for(spec)
    for timer in obs.timers.values():
        timer["Job"] = ""
    for service in obs.services.values():
        service["Job"] = ""
    assert require_run_once_quiescent(spec, obs) is None


@pytest.mark.parametrize("value", ["7", "4 /org/freedesktop/systemd1/job/4", "  "])
def test_unknown_nonzero_job_still_rejected_with_empty_job_support(value):
    spec = SERVICES[2]
    obs = fixture_for(spec)
    for service in obs.services.values():
        service["Job"] = ""
    obs.services["obsidian-github-sync.service"]["Job"] = value
    with pytest.raises(WorkflowNotQuiescent, match="workflow_service_not_quiescent"):
        require_run_once_quiescent(spec, obs)
