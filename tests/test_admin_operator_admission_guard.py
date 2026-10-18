"""Scoped Run once lock+hidden workflow checks; no real product service effect."""
from __future__ import annotations

from contextlib import contextmanager
import os
import threading
import time

import pytest

from obsidian_automation.admin_operation_broker import BrokerRejected
from obsidian_automation.admin_systemd_admission import RunOnceJobDispatcher
from obsidian_automation.admin_workflow_quiescence import (
    GROUP_UNITS, WorkflowNotQuiescent, require_run_once_nonoverlap,
)
from test_admin_operation_broker import FakeUnits, NOW, SPEC, SERVICES, snapshot
from test_admin_workflow_quiescence import fixture_for

RID = "aaaaaaab-1234-4234-8234-123456789abc"
JOB = "/org/freedesktop/systemd1/job/51"


class Manager:
    def __init__(self):
        self.calls = []
    def start_unit(self, spec):
        self.calls.append(spec)
        return JOB


def test_sibling_scheduled_timer_does_not_block_idle_manual_job():
    selected=SERVICES[2]
    record=fixture_for(selected)
    sibling=SERVICES[3].timer
    record.timers[sibling].update({
        "UnitFileState":"enabled","ActiveState":"active","SubState":"waiting",
    })
    assert require_run_once_nonoverlap(selected,record) is None


@pytest.mark.parametrize("field,val", [
    ("ActiveState","activating"), ("Job","18"), ("MainPID","55"),
    ("Type","simple"), ("FragmentPath","/tmp/forged.service"),
])
def test_hidden_active_dependency_blocks_without_startunit(field,val):
    selected=SERVICES[2]
    obs=fixture_for(selected)
    obs.services["obsidian-github-writer.service"][field]=val
    with pytest.raises(WorkflowNotQuiescent,match="workflow_service_not_quiescent"):
        require_run_once_nonoverlap(selected,obs)


@pytest.mark.parametrize("field,val", [
    ("UnitFileState","enabled"), ("ActiveState","active"),
    ("Job","47"), ("FragmentPath","/tmp/forged.timer"),
])
def test_selected_timer_is_paused_and_pinned(field,val):
    selected=SERVICES[2]
    obs=fixture_for(selected)
    obs.timers[selected.timer][field]=val
    with pytest.raises(WorkflowNotQuiescent,match="workflow_timer_not_quiescent"):
        require_run_once_nonoverlap(selected,obs)


@pytest.mark.parametrize("field,val", [
    ("ActiveState","activating"),("Job","13"),("LoadState","not-found"),
])
def test_sibling_timer_busy_or_missing_blocks(field,val):
    selected=SERVICES[2]
    obs=fixture_for(selected)
    obs.timers[SERVICES[3].timer][field]=val
    with pytest.raises(WorkflowNotQuiescent,match="workflow_timer_not_quiescent"):
        require_run_once_nonoverlap(selected,obs)


def test_production_root_dispatcher_must_supply_group_observer(monkeypatch):
    a=FakeUnits(timer_state="paused")
    mgr=Manager()
    monkeypatch.setattr(os,"geteuid",lambda: 0)
    @contextmanager
    def test_gate():
        yield
    dispatcher=RunOnceJobDispatcher(
        adapter=a,manager=mgr,maintenance_gate=test_gate,
        clock=lambda: NOW,require_root=True,enabled=True,
        workflow_observer=None,
    )
    with pytest.raises(BrokerRejected,match="workflow_observer_unavailable"):
        dispatcher.dispatch(
            request_id=RID,service_id=SPEC.identifier,
            trusted_snapshot=snapshot(a,NOW),
        )
    assert mgr.calls==[]


def test_live_group_observation_occurs_under_same_updater_gate():
    a=FakeUnits(timer_state="paused")
    mgr=Manager()
    lock={"held":False}
    @contextmanager
    def gate():
        lock["held"]=True
        try: yield
        finally: lock["held"]=False
    def source(spec):
        assert lock["held"]
        return fixture_for(spec)
    dispatcher=RunOnceJobDispatcher(
        adapter=a,manager=mgr,maintenance_gate=gate,
        clock=lambda:NOW,require_root=False,enabled=True,
        workflow_observer=source,
    )
    result=dispatcher.dispatch(
        request_id=RID,service_id=SPEC.identifier,
        trusted_snapshot=snapshot(a,NOW),
    )
    assert result.job_path==JOB
    assert mgr.calls==[SPEC]
    assert lock["held"] is False


def test_live_group_dependency_race_fails_before_systemd_job():
    a=FakeUnits(timer_state="paused")
    mgr=Manager()
    @contextmanager
    def gate(): yield
    def raced(spec):
        obs=fixture_for(spec)
        dependency=GROUP_UNITS[spec.identifier][-1]
        obs.services[dependency]["Job"]="81"
        return obs
    dispatcher=RunOnceJobDispatcher(
        adapter=a,manager=mgr,maintenance_gate=gate,
        clock=lambda:NOW,require_root=False,enabled=True,
        workflow_observer=raced,
    )
    with pytest.raises(BrokerRejected,match="workflow_service_not_quiescent"):
        dispatcher.dispatch(
            request_id=RID,service_id=SPEC.identifier,
            trusted_snapshot=snapshot(a,NOW),
        )
    assert mgr.calls==[]
