"""#442 test-only checks for root manual reconciliation CLI (no host effects)."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

from obsidian_automation.admin_status import SERVICES
from obsidian_automation.admin_workflow_quiescence import (
    GROUP_UNITS, WorkflowObservation, TIMER_READ_FIELDS, WORKFLOW_READ_FIELDS,
)
from test_admin_workflow_quiescence import fixture_for

ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/"tools/reconcile_admin_operator_queue.py"


def load():
    spec=importlib.util.spec_from_file_location("_oa442_tool_unit",SCRIPT)
    assert spec and spec.loader
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def install_synthetic_idle_host(module,monkeypatch):
    original={n:{
        "ActiveState":"inactive" if n=="obsidian-github-daily-progress.timer" else "active",
        "UnitFileState":"enabled",
    } for n in module.TIMERS}
    baseline=(json.dumps(original,sort_keys=True)+"\n").encode()
    cutover=b'{"version":1,"phase":"rolled_back"}\n'
    module.BASELINE_SHA=module.sha(baseline)
    module.FAILURE_SHA=module.sha(cutover)
    source= {
        str(module.BASELINE):baseline,
        str(module.FAILURE_RECEIPT):cutover,
    }
    monkeypatch.setattr(module,"root_file",lambda path,**kw:source[str(path)])
    monkeypatch.setattr(module,"strict_unchanged_authority",lambda:{"revision":"reviewed-test-source"})
    monkeypatch.setattr(module,"snapshot_timer_intents",lambda:original)
    states={
        module.ROOT_OPERATOR:{"ActiveState":"inactive","UnitFileState":"disabled","MainPID":"0","Job":""},
        module.NGINX:{"ActiveState":"inactive","UnitFileState":"disabled"},
        module.DASHBOARD:{"ActiveState":"active","MainPID":"23"},
    }
    monkeypatch.setattr(module,"service_fields",lambda name,properties:states[name])
    monkeypatch.setattr(module.os,"geteuid",lambda:0)
    monkeypatch.setattr(module.os,"uname",lambda:SimpleNamespace(nodename="obsidian-automation"))
    monkeypatch.setattr(module,"sys",SimpleNamespace(
        flags=SimpleNamespace(isolated=1,no_site=1,dont_write_bytecode=1),
        pycache_prefix="/dev/null/obsidian-admin-source-only",
    ))
    snap=fixture_for(SERVICES[2])
    return snap,original,states


def test_cli_fixed_single_incident_and_no_arbitrary_target():
    mod=load()
    assert mod.UUID=="c3b97d52-c988-47b9-addb-31ea49960b71"
    assert mod.TARGET_ROLE=="github-sync"
    assert mod.QUEUE==Path("/var/lib/obsidian-admin/queue")
    assert mod.ACCEPT=="acknowledge_indeterminate_without_replay:"+mod.UUID
    assert mod.PREVIOUS_MODULE_SHA!="SOURCE_SHA_TO_BE_PINNED"
    src=SCRIPT.read_text()
    assert "/usr/bin/systemctl" in src and "show" in src
    for bad in (
        '"start",', '"stop",','"restart",','"enable",','"disable",',
        '"StartUnit",','"SetUnitProperties",',
    ):
        assert bad not in src
    assert "manually_reconcile_unknown" in src


def test_inspector_accepts_fully_idle_role_and_restored_timer_states(monkeypatch):
    mod=load()
    snap,_,_=install_synthetic_idle_host(mod,monkeypatch)
    adapter=SimpleNamespace(observe_workflow=lambda spec:snap)
    state=mod.check_environment(
        adapter,None,GROUP_UNITS,TIMER_READ_FIELDS,WORKFLOW_READ_FIELDS,
    )
    assert state["role_services_idle"] is True
    assert state["timer_intents_restored"] is True


@pytest.mark.parametrize("unit,state",[
    ("obsidian-github-writer.service",{"Job":"333"}),
    ("obsidian-github-compactor.service",{"ActiveState":"activating"}),
    ("obsidian-github-daily-apply.service",{"MainPID":"929"}),
])
def test_any_running_or_queued_workflow_dependency_blocks(monkeypatch,unit,state):
    mod=load()
    snap,_,_=install_synthetic_idle_host(mod,monkeypatch)
    snap.services[unit].update(state)
    with pytest.raises(mod.ReconcileBlocked,match="related_service_job_or_process_active"):
        mod.check_environment(
            SimpleNamespace(observe_workflow=lambda spec:snap),None,
            GROUP_UNITS,TIMER_READ_FIELDS,WORKFLOW_READ_FIELDS,
        )


@pytest.mark.parametrize("which",[
    "worker_running", "ingress_open", "dashboard_stopped", "timer_drift",
    "wrong_role_fragment", "unexpected_role_job",
])
def test_host_invariants_are_fail_closed(monkeypatch,which):
    mod=load()
    snap,original,states=install_synthetic_idle_host(mod,monkeypatch)
    if which=="worker_running":
        states[mod.ROOT_OPERATOR]["ActiveState"]="active"
    elif which=="ingress_open":
        states[mod.NGINX]["ActiveState"]="active"
    elif which=="dashboard_stopped":
        states[mod.DASHBOARD]["ActiveState"]="inactive"
    elif which=="timer_drift":
        change={k:dict(v) for k,v in original.items()}
        change["obsidian-github-sync.timer"]["UnitFileState"]="disabled"
        monkeypatch.setattr(mod,"snapshot_timer_intents",lambda:change)
    elif which=="wrong_role_fragment":
        snap.services["obsidian-github-writer.service"]["FragmentPath"]="/tmp/evil.service"
    else:
        snap.timers["obsidian-github-daily-progress.timer"]["Job"]="123"
    with pytest.raises(mod.ReconcileBlocked):
        mod.check_environment(
            SimpleNamespace(observe_workflow=lambda spec:snap),None,
            GROUP_UNITS,TIMER_READ_FIELDS,WORKFLOW_READ_FIELDS,
        )


def test_wrong_bootstrap_flags_denied_even_without_any_writes(monkeypatch):
    mod=load()
    snap,_,_=install_synthetic_idle_host(mod,monkeypatch)
    mod.sys.pycache_prefix=None
    with pytest.raises(mod.ReconcileBlocked,match="root_isolated_source_only_required"):
        mod.check_environment(
            SimpleNamespace(observe_workflow=lambda spec:snap),None,
            GROUP_UNITS,TIMER_READ_FIELDS,WORKFLOW_READ_FIELDS,
        )


def test_cli_rejects_unauthorized_approval_reference():
    mod=load()
    assert mod.REF_PATTERN.fullmatch(
        "https://github.com/upiscium/ObsidianAutomation/issues/442#issuecomment-1234"
    )
    assert not mod.REF_PATTERN.fullmatch(
        "https://github.com/evil/ObsidianAutomation/issues/442#issuecomment-1234"
    )
    assert not mod.REF_PATTERN.fullmatch(
        "https://github.com/upiscium/ObsidianAutomation/issues/442"
    )


def test_cli_inspects_original_unknown_after_completed_reconcile_and_new_active(
    tmp_path, monkeypatch,
):
    """The historical UUID remains inspectable while a fresh UUID is active."""
    from obsidian_automation.admin_operation_queue import (
        DurableOperationQueue, _read_private,
    )
    from obsidian_automation.admin_operation_broker import BrokerRejected
    from test_admin_manual_reconciliation import (
        RID, RID2, q, unknown, approve, request,
    )
    root=tmp_path/"audit"
    root.mkdir(mode=0o700)
    queue=unknown(root)
    mod=load()
    monkeypatch.setattr(mod,"QUEUE",root)
    # Original active marker exists; no proof needed for lookup.
    action,record=mod.require_record_evidence(queue,_read_private)
    assert action.request_id==RID
    assert record["action"]=="run_once"
    assert approve(queue)["state"]=="manually_reconciled_unknown"
    action,record=mod.require_record_evidence(queue,_read_private)
    assert action.request_id==RID
    assert queue.submit(request(RID2))["state"]=="accepted"
    action,record=mod.require_record_evidence(queue,_read_private)
    assert action.request_id==RID
    assert queue.receipt(RID)["state"]=="indeterminate"
    assert queue.submit(request())["state"]=="indeterminate"


def test_cli_does_not_accept_missing_active_marker_without_proof(tmp_path,monkeypatch):
    from obsidian_automation.admin_operation_queue import _read_private
    from test_admin_manual_reconciliation import RID,unknown
    root=tmp_path/"audit"
    root.mkdir(mode=0o700)
    queue=unknown(root)
    (root/"active.json").unlink()
    mod=load()
    monkeypatch.setattr(mod,"QUEUE",root)
    with pytest.raises(mod.ReconcileBlocked,match="historical_queue_identity_mismatch"):
        mod.require_record_evidence(queue,_read_private)


def test_reviewed_authority_digest_pins_all_fields_not_just_counts(monkeypatch):
    mod=load()
    baseline={
        "revision":mod.PREVIOUS_AUTHORITY_REVISION,
        "enabled":False,"coordinated_updaters_verified":False,
        "uncoordinated_launchers_retired":False,"single_active_lxc":True,
        "module_sha256":{f"module{i}.py":"a"*64 for i in range(13)}
        | {"admin_operation_queue.py":mod.PREVIOUS_MODULE_SHA},
        "unit_sha256":{f"unit{i}.service":"b"*64 for i in range(10)},
    }
    reviewed=(json.dumps(baseline,sort_keys=True,separators=(",",":"))+"\n").encode()
    monkeypatch.setattr(mod,"PREVIOUS_AUTHORITY_CANONICAL_SHA",mod.sha(reviewed))
    current={
        **baseline,"revision":"f"*40,
        "module_sha256":{
            **baseline["module_sha256"],
            "admin_operation_queue.py":mod.EXPECTED_QUEUE_SHA,
        },
    }
    installed={}
    for name,digest in current["module_sha256"].items():
        installed[str(mod.SOURCE_PACKAGE/"obsidian_automation"/name)]=b"same"
    for name in current["unit_sha256"]:
        installed[str(Path("/etc/systemd/system")/name)]=b"unit"
    # Pin the local fake fixture hashes independently of the authority
    # reconstruction; exact pinned manifest must be reviewed and unchanged.
    current["module_sha256"]={
        k:(mod.sha(installed[str(mod.SOURCE_PACKAGE/"obsidian_automation"/k)])
           if k!="admin_operation_queue.py" else mod.EXPECTED_QUEUE_SHA)
        for k in current["module_sha256"]
    }
    current["unit_sha256"]={
        k:mod.sha(installed[str(Path("/etc/systemd/system")/k)])
        for k in current["unit_sha256"]
    }
    # Recalculate the fake-reviewed baseline for this precise unit closure.
    expected_old={**current,"revision":mod.PREVIOUS_AUTHORITY_REVISION,
                  "module_sha256":{
                      **current["module_sha256"],
                      "admin_operation_queue.py":mod.PREVIOUS_MODULE_SHA,
                  }}
    expected=(json.dumps(expected_old,sort_keys=True,separators=(",",":"))+"\n").encode()
    monkeypatch.setattr(mod,"PREVIOUS_AUTHORITY_CANONICAL_SHA",mod.sha(expected))
    monkeypatch.setattr(mod,"root_file",lambda path,**kw:(
        (json.dumps(current,sort_keys=True,separators=(",",":"))+"\n").encode()
        if path==mod.AUTHORITY else installed[str(path)]
    ))
    # Unreviewed content cannot be substituted, even if the replacement
    # supplies matching self-declared names/counts/hashes.
    attacker=json.loads(json.dumps(current))
    attacker["unit_sha256"]["unit0.service"]="0"*64
    monkeypatch.setattr(mod,"root_file",lambda path,**kw:(
        (json.dumps(attacker,sort_keys=True,separators=(",",":"))+"\n").encode()
        if path==mod.AUTHORITY else installed[str(path)]
    ))
    with pytest.raises(mod.ReconcileBlocked,match="source_authority_not_reviewed_baseline"):
        mod.strict_unchanged_authority()


def test_reboot_drift_requires_explicit_approval_and_keeps_evidence_identity():
    mod=load()
    prior={"boot_id":"11111111-2222-4333-8444-555555555555"}
    current="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    assert mod.previously_recorded_boot(
        prior,prior["boot_id"],resume_across_reboot=False,
    )==prior["boot_id"]
    with pytest.raises(
        mod.ReconcileBlocked,
        match="manual_evidence_reboot_requires_explicit_renewed_acceptance",
    ):
        mod.previously_recorded_boot(
            prior,current,resume_across_reboot=False,
        )
    assert mod.previously_recorded_boot(
        prior,current,resume_across_reboot=True,
    )==prior["boot_id"]


@pytest.mark.parametrize("value",[
    None,0,42,True,"untrusted","not-an-uuid",
])
def test_incorrect_pre_reboot_evidence_boot_never_permitted(value):
    mod=load()
    with pytest.raises(mod.ReconcileBlocked,match="manual_evidence_boot_invalid"):
        mod.previously_recorded_boot(
            {"boot_id":value},"aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            resume_across_reboot=True,
        )


def test_resumed_queue_finalization_after_reboot_uses_original_boot(tmp_path,monkeypatch):
    """A true proof+queue marker survive reboot; no original is replayed."""
    from obsidian_automation.admin_operation_broker import BrokerRejected
    from test_admin_manual_reconciliation import (
        RID,BOOT,REFERENCE,APPROVAL,unknown,q,store_manual_evidence,
    )
    root=tmp_path/"audit"
    root.mkdir(mode=0o700)
    queued=unknown(root)
    proof_digest=store_manual_evidence(queued)
    unlink=os.unlink
    with monkeypatch.context() as patch:
        patch.setattr(os,"unlink",lambda name,**kw:(
            (_ for _ in ()).throw(OSError("simulated hard crash"))
            if name=="active.json" else unlink(name,**kw)
        ))
        with pytest.raises(BrokerRejected):
            queued.manually_reconcile_unknown(
                RID,human_accept=APPROVAL,
                approval_reference=REFERENCE,
                evidence_sha256=proof_digest,boot_id=BOOT,
            )
    assert (root/"active.json").exists()
    assert (root/f"manual-reconcile-{RID}.json").is_file()
    mod=load()
    changed="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    with pytest.raises(mod.ReconcileBlocked):
        mod.previously_recorded_boot(
            {"boot_id":BOOT},changed,resume_across_reboot=False,
        )
    old=mod.previously_recorded_boot(
        {"boot_id":BOOT},changed,resume_across_reboot=True,
    )
    assert queued.manually_reconcile_unknown(
        RID,human_accept=APPROVAL,approval_reference=REFERENCE,
        evidence_sha256=proof_digest,boot_id=old,
    )["state"]=="manually_reconciled_unknown"
    assert queued.receipt(RID)["state"]=="indeterminate"
    assert not (root/"active.json").exists()


def test_root_owned_evidence_path_refuses_symlinks(tmp_path):
    mod=load()
    root=tmp_path/"root"
    root.mkdir(mode=0o700)
    file=root/"report.json"
    file.write_bytes(b'{"probe":true}\n')
    file.chmod(0o600)
    bad=root/"linked.json"
    bad.symlink_to(file)
    with pytest.raises((mod.ReconcileBlocked,OSError)):
        mod.root_file(bad)


def test_protected_file_refuses_wrong_owner_or_world_writable_parent(tmp_path):
    mod=load()
    root=tmp_path/"root"
    root.mkdir(mode=0o777)
    file=root/"receipt.json"
    file.write_bytes(b'{"value":1}\n')
    file.chmod(0o600)
    with pytest.raises((mod.ReconcileBlocked,OSError)):
        mod.root_file(file)
