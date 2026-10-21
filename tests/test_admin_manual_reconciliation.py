"""#442 root-only manual acknowledgement: old unknown stays unknown forever.

All fixtures use disposable 0700 directories and fake operations; no systemd,
real root authority, provider calls or live Queue modifications are performed.
"""
from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

import pytest

from obsidian_automation.admin_operation_broker import BrokerRejected
from obsidian_automation.admin_operation_queue import (
    DurableOperationQueue, _record_sha256,
)
from obsidian_automation.admin_operation_broker import _store_new

RID = "c3b97d52-c988-47b9-addb-31ea49960b71"
RID2 = "ffffffff-1234-4234-8234-123456789abc"
BOOT = "11111111-2222-4333-8444-555555555555"
EVIDENCE = "a" * 64
APPROVAL = f"acknowledge_indeterminate_without_replay:{RID}"
REFERENCE = "https://github.com/upiscium/ObsidianAutomation/issues/442#issuecomment-123456"
CLOCK = datetime(2026,10,21,10,40,tzinfo=timezone.utc)


@pytest.fixture
def root():
    with tempfile.TemporaryDirectory(prefix="oa442-",dir="/tmp") as folder:
        p=Path(folder)
        p.chmod(0o700)
        yield p


def q(root,enabled=True):
    return DurableOperationQueue(
        queue_root=root,require_root=False,enabled=enabled,
        allow_run_once=True,clock=lambda:CLOCK,
    )


def request(rid=RID):
    return {"version":1,"request_id":rid,"service_id":"github-sync",
            "action":"run_once"}


def unknown(root):
    queue=q(root)
    queue.submit(request())
    called=[]
    with pytest.raises(BrokerRejected,match="indeterminate_effect"):
        queue.work_once(lambda item: (called.append(item),False)[1])
    assert len(called)==1
    assert queue.receipt(RID)["state"]=="indeterminate"
    return queue


def store_manual_evidence(queue):
    """Test-only root actor: fake fixed safe sysinfo with immutable journal."""
    fd, lock = queue._open()
    try:
        name=f"manual-evidence-{RID}.json"
        if not (queue.queue_root/name).exists():
            record = {
                "record_version":1, "request_id":RID,
                "service_id":"github-sync", "action":"run_once",
                "state":"root_verified_manual_evidence",
                "decision":"acknowledge_unknown_no_replay",
                "approval_reference":REFERENCE,
                "observed_at":CLOCK.isoformat(timespec="seconds"),
                "boot_id":BOOT,
                "operator_inactive":True,
                "authority_disabled":True,
                "socket_absent":True,
                "role_services_idle":True,
                "timer_intents_restored":True,
                "business_outcome":"unknown_no_success_claim",
                "request_sha256":_record_sha256(fd,f"request-{RID}.json"),
                "processing_sha256":_record_sha256(fd,f"processing-{RID}.json"),
                "result_sha256":_record_sha256(fd,f"result-{RID}.json"),
                "active_sha256":_record_sha256(fd,"active.json"),
            }
            _store_new(fd,name,record)
        return _record_sha256(fd,name)
    finally:
        queue._close(fd,lock)


def approve(queue, **overrides):
    evidence=store_manual_evidence(queue)
    args=dict(human_accept=APPROVAL,
              approval_reference=REFERENCE,
              evidence_sha256=evidence,
              boot_id=BOOT)
    args.update(overrides)
    return queue.manually_reconcile_unknown(RID,**args)


def test_manual_proof_preserves_unknown_and_all_original_files(root):
    queue=unknown(root)
    originals={k:(root/k).read_bytes() for k in (
        "active.json",f"request-{RID}.json",
        f"processing-{RID}.json",f"result-{RID}.json",
    )}
    result=approve(queue)
    assert result["state"]=="manually_reconciled_unknown"
    assert result["business_success_asserted"] is False
    assert result["can_execute"] is False
    assert not (root/"active.json").exists()
    assert queue.receipt(RID)["state"]=="indeterminate"
    assert queue.submit(request())["state"]=="indeterminate"
    assert queue.submit(request(RID2))["state"]=="accepted"
    # The new accepted request is now the active.json, but original ID
    # still remains visible as UNKNOWN and must never be re-admitted.
    assert queue.receipt(RID)["state"]=="indeterminate"
    assert queue.submit(request())["state"]=="indeterminate"
    for k in (f"request-{RID}.json",f"processing-{RID}.json",f"result-{RID}.json"):
        assert (root/k).read_bytes()==originals[k]
    marker=json.loads((root/f"manual-reconcile-{RID}.json").read_bytes())
    assert marker["decision"]=="acknowledge_unknown_no_replay"
    assert marker["active_sha256"]==hashlib.sha256(originals["active.json"]).hexdigest()
    assert marker["request_sha256"]==hashlib.sha256(originals[f"request-{RID}.json"]).hexdigest()
    assert marker["result_sha256"]==hashlib.sha256(originals[f"result-{RID}.json"]).hexdigest()
    assert marker["evidence_sha256"]==hashlib.sha256(
        (root/f"manual-evidence-{RID}.json").read_bytes()
    ).hexdigest()
    assert marker["approval_reference"]==REFERENCE
    assert stat.S_IMODE((root/f"manual-reconcile-{RID}.json").stat().st_mode)==0o600


def test_disabled_queue_still_allows_root_supervised_reconciliation(root):
    unknown(root)
    off=q(root,enabled=False)
    assert approve(off)["state"]=="manually_reconciled_unknown"
    enabled=q(root,enabled=True)
    assert enabled.submit(request(RID2))["state"]=="accepted"


def test_unknown_remains_blocking_before_manual_reconcile(root):
    queue=unknown(root)
    with pytest.raises(BrokerRejected,match="queue_busy"):
        queue.submit(request(RID2))
    (root/"active.json").unlink()
    with pytest.raises(BrokerRejected,match="queue_recovery_required"):
        queue.submit(request(RID2))
    with pytest.raises(BrokerRejected,match="queue_recovery_required"):
        approve(queue)


@pytest.mark.parametrize("wrong",[
    {"human_accept":"yes"},
    {"human_accept":f"acknowledge_indeterminate_without_replay:{RID2}"},
    {"approval_reference":"unreviewed"},
    {"approval_reference":"https://github.com/upiscium/ObsidianAutomation/issues/442"},
    {"evidence_sha256":"0"*65},
    {"evidence_sha256":"-"*64},
    {"boot_id":"not-a-boot-id"},
    {"boot_id":None},
])
def test_missing_or_wrong_human_evidence_fails_before_mutation(root,wrong):
    queue=unknown(root)
    with pytest.raises(BrokerRejected,match="queue_recovery_required"):
        approve(queue,**wrong)
    assert (root/"active.json").exists()
    assert not list(root.glob("manual-reconcile-*"))


def test_only_indeterminate_run_once_may_be_acknowledged(root):
    queue=q(root)
    queue.submit(request())
    with pytest.raises(BrokerRejected,match="queue_recovery_required"):
        approve(queue)
    assert not list(root.glob("manual-reconcile-*"))


def test_wrong_request_id_cannot_reconcile_original(root):
    queue=unknown(root)
    with pytest.raises(BrokerRejected,match="queue_recovery_required"):
        queue.manually_reconcile_unknown(
            RID2, human_accept=APPROVAL,
            approval_reference=REFERENCE,evidence_sha256=EVIDENCE,boot_id=BOOT,
        )
    assert (root/"active.json").exists()


def test_interrupted_fsync_after_manual_proof_keeps_pending_until_resume(root,monkeypatch):
    queue=unknown(root)
    unlink=os.unlink
    def deny_active(name,**kwargs):
        if name=="active.json":
            raise OSError("simulated filesystem interruption")
        return unlink(name,**kwargs)
    with monkeypatch.context() as m:
        m.setattr(os,"unlink",deny_active)
        with pytest.raises(BrokerRejected,match="queue_state_invalid"):
            approve(queue)
    marker=root/f"manual-reconcile-{RID}.json"
    assert marker.is_file() and (root/"active.json").is_file()
    with pytest.raises(BrokerRejected,match="queue_busy"):
        queue.submit(request(RID2))
    assert queue.receipt(RID)["state"]=="indeterminate"
    assert approve(q(root,enabled=False))["state"]=="manually_reconciled_unknown"
    assert not (root/"active.json").exists()
    assert queue.submit(request(RID2))["state"]=="accepted"


def test_recovered_marker_needs_same_evidence_and_approval(root,monkeypatch):
    queue=unknown(root)
    unlink=os.unlink
    with monkeypatch.context() as m:
        m.setattr(os,"unlink",lambda name,**kwargs: (
            (_ for _ in ()).throw(OSError("interrupted"))
            if name=="active.json" else unlink(name,**kwargs)
        ))
        with pytest.raises(BrokerRejected):
            approve(queue)
    with pytest.raises(BrokerRejected,match="queue_recovery_required"):
        approve(queue,evidence_sha256="b"*64)
    assert (root/"active.json").exists()
    assert approve(queue)["state"]=="manually_reconciled_unknown"
    assert approve(queue)["state"]=="manually_reconciled_unknown"


def test_tampered_original_receipt_breaks_manual_attestation(root):
    queue=unknown(root)
    approve(queue)
    doc=root/f"result-{RID}.json"
    doc.write_text(json.dumps({
        "record_version":1,"request_id":RID,"service_id":"github-sync",
        "action":"run_once","state":"completed",
    })+"\n")
    doc.chmod(0o600)
    with pytest.raises(BrokerRejected,match="queue_recovery_required"):
        queue.submit(request(RID2))
    with pytest.raises(BrokerRejected):
        queue.receipt(RID)


def test_tampered_manual_evidence_or_marker_fails_closed(root):
    queue=unknown(root)
    approve(queue)
    marker=root/f"manual-reconcile-{RID}.json"
    evidence=root/f"manual-evidence-{RID}.json"
    original=evidence.read_bytes()
    data=json.loads(evidence.read_text())
    data["role_services_idle"]=False
    evidence.write_text(json.dumps(data)+"\n")
    evidence.chmod(0o600)
    with pytest.raises(BrokerRejected,match="queue_recovery_required"):
        q(root).submit(request(RID2))
    evidence.write_bytes(original)
    evidence.chmod(0o600)
    proof=json.loads(marker.read_text())
    proof["evidence_sha256"]="b"*64
    marker.write_text(json.dumps(proof)+"\n")
    marker.chmod(0o600)
    with pytest.raises(BrokerRejected,match="queue_recovery_required"):
        q(root).submit(request(RID2))


def test_symlinked_or_wrong_mode_manual_record_fails_closed(root):
    queue=unknown(root)
    approve(queue)
    marker=root/f"manual-reconcile-{RID}.json"
    marker.unlink()
    marker.symlink_to("/etc/passwd")
    with pytest.raises(BrokerRejected):
        queue.submit(request(RID2))
    marker.unlink()
    marker.write_text("{}\n")
    marker.chmod(0o644)
    with pytest.raises(BrokerRejected):
        queue.submit(request(RID2))


def test_orphan_manual_record_is_not_a_valid_history(root):
    queue=q(root)
    marker=root/f"manual-reconcile-{RID}.json"
    marker.write_text("{}\n")
    marker.chmod(0o600)
    with pytest.raises(BrokerRejected,match="queue_recovery_required"):
        queue.submit(request(RID2))


def test_queue_lock_contention_prevents_reconciliation(root):
    queue=unknown(root)
    fd=os.open(root/"admission.lock",os.O_RDWR)
    fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    try:
        with pytest.raises(BrokerRejected,match="admission_lock_busy"):
            approve(queue)
    finally:
        os.close(fd)
    assert (root/"active.json").is_file()


def test_bad_directory_owner_or_nonroot_authority_refuses_reconciliation(root,monkeypatch):
    queue=unknown(root)
    privileged=DurableOperationQueue(queue_root=root,require_root=True,enabled=False)
    with monkeypatch.context() as m:
        m.setattr(os,"geteuid",lambda:12345)
        with pytest.raises(BrokerRejected,match="privileged_worker_required"):
            approve(privileged)
    root.chmod(0o750)
    try:
        with pytest.raises(BrokerRejected,match="unsafe_audit_root"):
            approve(queue)
    finally:
        root.chmod(0o700)


def test_invalid_manual_marker_cannot_leak_details(root):
    queue=unknown(root)
    approve(queue)
    marker=root/f"manual-reconcile-{RID}.json"
    doc=json.loads(marker.read_text())
    doc["request_sha256"]="0"*64
    marker.write_text(json.dumps(doc)+"\n")
    marker.chmod(0o600)
    with pytest.raises(BrokerRejected,match="queue_recovery_required"):
        queue.submit(request(RID2))
    assert (root/f"result-{RID}.json").is_file()
