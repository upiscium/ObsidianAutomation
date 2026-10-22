"""#442: first real CLI reconcile path with actual durable Queue functions.

All tests use a disposable 0700 test Queue. No production paths, root-only
service, timer, live socket, network or systemd job is accessed.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from obsidian_automation.admin_operation_broker import (
    BrokerRejected, _read_private, _store_new,
)
from obsidian_automation.admin_operation_queue import _record_sha256
from test_admin_manual_reconciliation import (
    APPROVAL, REFERENCE, RID, RID2, q, request, unknown,
)
from test_admin_manual_reconcile_cli import load


@contextmanager
def mock_approved_non_effectful_gate():
    yield


class CliOs:
    """Root identity ONLY in this imported CLI module, not in real Queue."""
    def __getattr__(self, name):
        return getattr(os, name)

    def geteuid(self):
        return 0


def prepare_first_call(tmp_path, monkeypatch, *, human_accept=APPROVAL,
                       reference=REFERENCE, mode="--reconcile"):
    root=tmp_path/"synthetic-unknown"
    root.mkdir(mode=0o700)
    unknown(root)
    queue=q(root,enabled=False)
    module=load()

    # No root code paths or real service observation can run in this test.
    monkeypatch.setattr(module,"qualify_root_entrypoint",lambda:None)
    monkeypatch.setattr(module,"strict_unchanged_authority",
                        lambda:{"revision":"synthetic-approved-disabled"})
    monkeypatch.setattr(module,"load_qualified_imports",
                        lambda:(
                            mock_approved_non_effectful_gate,
                            lambda:object(),BrokerRejected,
                            lambda **_:queue,_record_sha256,_read_private,_store_new,
                            (),{},(),(),
                        ))
    monkeypatch.setattr(module,"check_environment",
                        lambda *args:{"authority_revision":"synthetic-disabled"})
    monkeypatch.setattr(module,"QUEUE",root)
    # Substitute sys flags in CLI only (real Queue still checks caller owner).
    monkeypatch.setattr(module,"sys",SimpleNamespace(
        flags=SimpleNamespace(isolated=1,no_site=1,dont_write_bytecode=1),
        pycache_prefix="/dev/null/obsidian-admin-source-only",
        stderr=sys.stderr,
    ))
    monkeypatch.setattr(module,"os",CliOs())
    # Existing synthetic records are genuinely owned by the test user;
    # only the root CLI's top-level root-identity check needs a stand-in.
    # Preserve the real require_record_evidence() implementation, using
    # the real uid inside its file ownership validation.
    original_require=module.require_record_evidence
    def require_real_synthetic_records(*args):
        proxy=module.os
        module.os=os
        try:
            return original_require(*args)
        finally:
            module.os=proxy
    monkeypatch.setattr(module,"require_record_evidence",
                        require_real_synthetic_records)
    monkeypatch.setattr(sys,"argv",[
        "isolated-test-cli",mode,"--request-id",RID,
        "--human-accept",human_accept,
        "--approval-reference",reference,
    ])
    return module,queue,root


def test_actual_first_call_creates_evidence_and_proof_without_replay(
    tmp_path,monkeypatch,capsys,
):
    mod,queue,root=prepare_first_call(tmp_path,monkeypatch)
    originals={k:(root/k).read_bytes() for k in (
        "active.json",f"request-{RID}.json",
        f"processing-{RID}.json",f"result-{RID}.json",
    )}
    evidence=root/f"manual-evidence-{RID}.json"
    proof=root/f"manual-reconcile-{RID}.json"

    # Reproduce the production failure root cause: broker read normalizes
    # ENOENT into BrokerRejected, NOT FileNotFoundError.
    fd,lock=queue._open()
    try:
        with pytest.raises(BrokerRejected,match="unsafe_audit_record"):
            _read_private(fd,evidence.name)
    finally:
        queue._close(fd,lock)
    assert not evidence.exists()
    assert not proof.exists()

    assert mod.main()==0
    out=json.loads(capsys.readouterr().out)
    assert out["status"]=="MANUALLY_RECONCILED_WITHOUT_REPLAY"
    assert out["original_result"]=="indeterminate"
    assert out["job_submissions"]==0
    assert out["business_success_asserted"] is False
    assert out["queue_remaining_on_hold"] is False
    assert not (root/"active.json").exists()
    assert evidence.is_file() and proof.is_file()
    assert json.loads(evidence.read_bytes())["approval_reference"]==REFERENCE
    assert json.loads(proof.read_bytes())["evidence_sha256"]==out["evidence_sha256"]
    assert queue.receipt(RID)["state"]=="indeterminate"

    for k in originals:
        if k!="active.json":
            assert (root/k).read_bytes()==originals[k]

    # The same approved root-only operation is idempotent, and cannot create
    # another evidence, proof, or original business receipt on re-run.
    original_evidence=evidence.read_bytes()
    original_proof=proof.read_bytes()
    assert mod.main()==0
    again=json.loads(capsys.readouterr().out)
    assert again["status"]=="MANUALLY_RECONCILED_WITHOUT_REPLAY"
    assert evidence.read_bytes()==original_evidence
    assert proof.read_bytes()==original_proof

    # Separate future work may use another UUID, but no replay of the first.
    fresh=q(root,enabled=True)
    assert fresh.submit(request(RID2))["state"]=="accepted"
    assert fresh.receipt(RID)["state"]=="indeterminate"
    assert fresh.submit(request())["state"]=="indeterminate"
    assert mod.main()==2
    assert json.loads(capsys.readouterr().err)["status"]=="RECONCILIATION_BLOCKED"
    assert (root/"active.json").exists()
    assert evidence.read_bytes()==original_evidence
    assert proof.read_bytes()==original_proof


def test_first_call_inspect_does_not_create_any_evidence(
    tmp_path,monkeypatch,capsys,
):
    mod,queue,root=prepare_first_call(tmp_path,monkeypatch,mode="--inspect")
    before=(root/"active.json").read_bytes()
    assert mod.main()==0
    out=json.loads(capsys.readouterr().out)
    assert out["status"]=="READ_ONLY_RECONCILIATION_PRECHECK_PASS"
    assert out["would_modify_queue"] is False
    assert (root/"active.json").read_bytes()==before
    assert not list(root.glob("manual-*.json"))


@pytest.mark.parametrize("contamination",[
    "symlink_to_valid","dangling_symlink","malformed_json",
])
def test_occupied_evidence_never_treated_as_missing(
    tmp_path,monkeypatch,capsys,contamination,
):
    mod,_,root=prepare_first_call(tmp_path,monkeypatch)
    original_active=(root/"active.json").read_bytes()
    evidence=root/f"manual-evidence-{RID}.json"
    if contamination=="symlink_to_valid":
        evidence.symlink_to(root/f"request-{RID}.json")
    elif contamination=="dangling_symlink":
        evidence.symlink_to(root/"absent-in-disposable-test.json")
    else:
        evidence.write_bytes(b"{malformed json\n")
        evidence.chmod(0o600)

    assert mod.main()==2
    result=json.loads(capsys.readouterr().err)
    assert result["status"]=="RECONCILIATION_BLOCKED"
    assert (root/"active.json").read_bytes()==original_active
    assert not (root/f"manual-reconcile-{RID}.json").exists()


def test_missing_evidence_reboot_continuation_is_blocked_without_publication(
    tmp_path,monkeypatch,capsys,
):
    mod,_,root=prepare_first_call(tmp_path,monkeypatch)
    monkeypatch.setattr(sys,"argv",sys.argv+["--resume-across-reboot"])
    assert mod.main()==2
    result=json.loads(capsys.readouterr().err)
    assert result["reason"]=="reboot_continuation_without_prior_evidence"
    assert (root/"active.json").is_file()
    assert not (root/f"manual-evidence-{RID}.json").exists()
    assert not (root/f"manual-reconcile-{RID}.json").exists()


def test_rejected_specific_human_token_does_not_create_any_evidence(
    tmp_path,monkeypatch,capsys,
):
    mod,_,root=prepare_first_call(
        tmp_path,monkeypatch,human_accept="unspecified-general-approval",
    )
    assert mod.main()==2
    result=json.loads(capsys.readouterr().err)
    assert result["reason"]=="specific_human_approval_missing"
    assert (root/"active.json").is_file()
    assert not list(root.glob("manual-*.json"))


def test_existing_valid_evidence_must_use_same_approval_reference(
    tmp_path,monkeypatch,capsys,
):
    from test_admin_manual_reconciliation import store_manual_evidence
    mod,queue,root=prepare_first_call(tmp_path,monkeypatch)
    digest=store_manual_evidence(queue)
    assert len(digest)==64
    old_bytes=(root/f"manual-evidence-{RID}.json").read_bytes()
    altered="https://github.com/upiscium/ObsidianAutomation/issues/442#issuecomment-765432"
    monkeypatch.setattr(sys,"argv",[
        "isolated-test-cli","--reconcile","--request-id",RID,
        "--human-accept",APPROVAL,"--approval-reference",altered,
    ])
    assert mod.main()==2
    result=json.loads(capsys.readouterr().err)
    assert result["reason"]=="manual_evidence_reference_mismatch"
    assert (root/"active.json").exists()
    assert (root/f"manual-evidence-{RID}.json").read_bytes()==old_bytes
    assert not (root/f"manual-reconcile-{RID}.json").exists()
