#!/usr/bin/env python3
"""#442: root-owned, HUMAN-APPROVED single-incident Queue reconciliation.

Only fixes the durable admission state AFTER independent historical audit.
Never calls StartUnit, systemctl start/stop, patches original receipts or
automatically resubmits the indeterminate Run once. Requires explicit per-UUID
approval. Production invocation requires an independently reviewed, pinned,
root-owned copy of THIS script and matching installed root-owned source.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

UUID = "c3b97d52-c988-47b9-addb-31ea49960b71"
SOURCE_PACKAGE = Path("/opt/obsidian-automation/venv/lib/python3.13/site-packages")
AUTHORITY = Path("/etc/obsidian-admin/authority.json")
QUEUE = Path("/var/lib/obsidian-admin/queue")
SOCKET = Path("/run/obsidian-admin/operation.sock")
BASELINE = Path(
    "/var/lib/obsidian-admin/operator-cutovers/"
    "cutover-1994763e9454-e1l_z2vc/before-timers.json"
)
FAILURE_RECEIPT = Path(
    "/var/lib/obsidian-admin/operator-cutovers/"
    "cutover-1994763e9454-e1l_z2vc/failure.json"
)
BASELINE_SHA = "ee98edc24d404aa7c895921e7aa7de883b5da9af6810a4bb858eae331d0cc271"
FAILURE_SHA = "83c579b21564c0a68fc42c83648b75672ae6bd24949fd9d4a73497711cb50dc2"
PREVIOUS_MODULE_SHA = "8807440468a8db8daa1320baec43ec8305e43c4769866743b8d911f4202f7b91"
PREVIOUS_AUTHORITY_REVISION = "1994763e9454928bb1d19084ca29dec97fdbdf84"
PREVIOUS_AUTHORITY_CANONICAL_SHA = "46bda7259a59a3a1e00b57e0c375b1f4114b59892ea8506f40d6e22c12c85ac1"
EXPECTED_QUEUE_SHA = "feb9692b1eacdaa05ff02c87993824f2db2741d34189396597ce15a47ec121be"
ACCEPT = "acknowledge_indeterminate_without_replay:" + UUID
REF_PATTERN = re.compile(
    r"https://github\.com/upiscium/ObsidianAutomation/issues/"
    r"[1-9][0-9]*#issuecomment-[1-9][0-9]*\Z"
)
TIMERS = (
    "obsidian-ai-vault-pull.timer",
    "obsidian-pre-review.timer",
    "obsidian-github-sync.timer",
    "obsidian-github-daily-progress.timer",
    "obsidian-core-promotion.timer",
)
TARGET_ROLE = "github-sync"
ROOT_OPERATOR = "obsidian-admin-operator.service"
DASHBOARD = "obsidian-admin-dashboard.service"
NGINX = "nginx.service"
MAX_FILE_BYTES = 65536

class ReconcileBlocked(RuntimeError):
    """Stable, unprivileged-safe failure reason. No secret fields."""

def need(ok: bool, code: str) -> None:
    if not ok:
        raise ReconcileBlocked(code)

def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def root_file(path: Path, *, bound: int = MAX_FILE_BYTES) -> bytes:
    """Open a root-owned path through checked dirfds without symlink hops."""
    need(type(path) is type(Path("/")) and path.is_absolute()
         and ".." not in path.parts,"invalid_root_evidence_path")
    fd=os.open("/",os.O_RDONLY|os.O_DIRECTORY|os.O_CLOEXEC)
    try:
        for component in path.parts[1:-1]:
            nxt=os.open(component,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW
                        |os.O_CLOEXEC,dir_fd=fd)
            os.close(fd);fd=nxt
            st=os.fstat(fd)
            need(stat.S_ISDIR(st.st_mode) and st.st_uid==0
                 and (stat.S_IMODE(st.st_mode)&0o022)==0,
                 "unsafe_evidence_parent")
        leaf=os.open(path.name,os.O_RDONLY|os.O_NONBLOCK|os.O_NOFOLLOW
                     |os.O_CLOEXEC,dir_fd=fd)
        try:
            st=os.fstat(leaf)
            need(stat.S_ISREG(st.st_mode) and st.st_uid==0 and st.st_nlink==1
                 and (stat.S_IMODE(st.st_mode)&0o022)==0
                 and 0<st.st_size<=bound,"unsafe_evidence_file")
            data=os.read(leaf,bound+1)
            need(len(data)==st.st_size,"evidence_length_drift")
            return data
        finally:
            os.close(leaf)
    finally:
        os.close(fd)

def service_fields(unit: str, properties: tuple[str,...]) -> dict[str,str]:
    need(unit in {*TIMERS,ROOT_OPERATOR,DASHBOARD,NGINX},
         "unreviewed_systemd_unit")
    result=subprocess.run(
        ["/usr/bin/systemctl","show",unit,"--no-pager",
         *(f"--property={key}" for key in properties)],
        stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,timeout=5,check=False,
        env={"PATH":"/usr/bin:/bin","LC_ALL":"C"},
    )
    need(result.returncode==0 and len(result.stdout)<=4096,
         "systemd_observation_unavailable")
    try:
        lines=[line.split("=",1) for line in result.stdout.decode().splitlines()]
        fields=dict(lines)
    except (ValueError,UnicodeError):
        raise ReconcileBlocked("systemd_observation_unavailable") from None
    need(len(lines)==len(fields) and set(fields)==set(properties),
         "systemd_observation_unverifiable")
    return fields

def qualify_root_entrypoint() -> None:
    """Reject executing privileged recovery directly from a user worktree."""
    path=Path(__file__)
    release_root=Path("/var/lib/obsidian-admin/operator-releases")
    need(path.parent.parent==release_root
         and path.name=="reconcile-admin-queue.py"
         and re.fullmatch(r"[0-9a-f]{40}",path.parent.name) is not None,
         "unreviewed_root_script_location")
    st=path.lstat()
    need(stat.S_ISREG(st.st_mode) and st.st_uid==0
         and st.st_nlink==1 and stat.S_IMODE(st.st_mode)==0o400,
         "unqualified_root_script")
    root_file(path,bound=MAX_FILE_BYTES)


def qualify_package_entry(entry: Path) -> None:
    """Check the whole installed package for unsafe import shadow artifacts.

    The manifest intentionally pins the privileged *subset* of this normal
    production package, rather than all unrelated application modules.
    Those unrelated .py files are permitted only as root-owned, non-writable,
    single-link regular files; the privileged subset is separately SHA-pinned.
    """
    try:
        info=entry.lstat()
    except OSError:
        raise ReconcileBlocked("unreviewed_package_entry") from None
    need(not stat.S_ISLNK(info.st_mode),
         "unreviewed_import_symlink")
    need(info.st_uid==0
         and (stat.S_IMODE(info.st_mode)&0o022)==0,
         "unsafe_package_entry_ownership")
    if stat.S_ISREG(info.st_mode):
        need(info.st_nlink==1,"unreviewed_package_hardlink")
        need(entry.suffix not in (".pyc",".pyo",".so",".pyd"),
             "unreviewed_executable_import_artifact")
    elif stat.S_ISDIR(info.st_mode):
        need(entry.name=="__pycache__",
             "unexpected_privileged_import_directory")
    else:
        raise ReconcileBlocked("unexpected_privileged_import_inode")


def strict_unchanged_authority() -> dict:
    raw=root_file(AUTHORITY,bound=6500)
    def pairs(value):
        obj={}
        for k,v in value:
            need(k not in obj,"duplicate_authority_field")
            obj[k]=v
        return obj
    doc=json.loads(raw,object_pairs_hook=pairs)
    need(type(doc) is dict
         and doc.get("enabled") is False
         and doc.get("coordinated_updaters_verified") is False
         and doc.get("uncoordinated_launchers_retired") is False
         and doc.get("single_active_lxc") is True,
         "authority_not_disabled")
    need(len(doc["module_sha256"])==14 and len(doc["unit_sha256"])==10,
         "authority_inventory_changed")
    need(type(doc.get("revision")) is str
         and re.fullmatch(r"[0-9a-f]{40}",doc["revision"]) is not None
         and doc["revision"] != PREVIOUS_AUTHORITY_REVISION,
         "expected_new_source_revision_unavailable")
    # Reconstruct the previously independently accepted authority by
    # reversing ONLY the new commit revision and one Queue module digest.
    # The SHA of every other field, principal, module and Unit is thereby
    # independently pinned instead of trusting the live manifest's own list.
    baseline={**doc,
              "revision":PREVIOUS_AUTHORITY_REVISION,
              "module_sha256":{
                  **doc["module_sha256"],
                  "admin_operation_queue.py":PREVIOUS_MODULE_SHA,
              }}
    canonical=(json.dumps(baseline,sort_keys=True,separators=(",",":"))+"\n").encode()
    need(sha(canonical)==PREVIOUS_AUTHORITY_CANONICAL_SHA,
         "source_authority_not_reviewed_baseline")
    queue_path=SOURCE_PACKAGE/"obsidian_automation/admin_operation_queue.py"
    digest=sha(root_file(queue_path,bound=256000))
    need(doc["module_sha256"]["admin_operation_queue.py"]==digest
         and digest==EXPECTED_QUEUE_SHA and digest!=PREVIOUS_MODULE_SHA,
         "reviewed_reconcile_queue_not_installed")
    pinned_dir=SOURCE_PACKAGE/"obsidian_automation"
    # On the deployed host this package contains 14 authority-pinned modules
    # and 88 unrelated root-owned application .py files. They are NOT part
    # of the privileged import closure. Their mere presence must not block
    # recovery; keep unsafe symlinks/writable entries/native/bytecode shadows
    # fail-closed, and fully verify every module in the pinned import closure.
    for entry in pinned_dir.iterdir():
        qualify_package_entry(entry)
    for name,want in doc["module_sha256"].items():
        need(sha(root_file(pinned_dir/name,bound=256000))==want,
             "privileged_module_drift")
    for name,want in doc["unit_sha256"].items():
        need(sha(root_file(Path("/etc/systemd/system")/name,bound=16000))==want,
             "managed_unit_drift")
    return doc

def load_qualified_imports():
    need(str(SOURCE_PACKAGE) not in sys.path,
         "unexpected_import_path")
    sys.path.insert(0,str(SOURCE_PACKAGE))
    from obsidian_automation.admin_local_maintenance_gate import cooperating_updater_gate
    from obsidian_automation.admin_operation_broker import SystemctlAdapter, BrokerRejected
    from obsidian_automation.admin_operation_queue import (
        DurableOperationQueue, _record_sha256, _read_private, _store_new,
    )
    from obsidian_automation.admin_status import SERVICES
    from obsidian_automation.admin_workflow_quiescence import (
        GROUP_UNITS, TIMER_READ_FIELDS, WORKFLOW_READ_FIELDS,
    )
    return (
        cooperating_updater_gate, SystemctlAdapter, BrokerRejected,
        DurableOperationQueue,_record_sha256,_read_private,_store_new,
        SERVICES,GROUP_UNITS,TIMER_READ_FIELDS,WORKFLOW_READ_FIELDS,
    )

def snapshot_timer_intents() -> dict[str,dict[str,str]]:
    return {
        timer:service_fields(timer,("ActiveState","UnitFileState"))
        for timer in TIMERS
    }

def check_environment(adapter, queue, group_units, timer_fields, service_fields_keys):
    need(os.geteuid()==0 and sys.flags.isolated==sys.flags.no_site==
         sys.flags.dont_write_bytecode==1
         and sys.pycache_prefix=="/dev/null/obsidian-admin-source-only",
         "root_isolated_source_only_required")
    need(os.uname().nodename=="obsidian-automation","wrong_host")
    need(not os.path.lexists(SOCKET),"worker_socket_present")
    operator=service_fields(ROOT_OPERATOR,("ActiveState","UnitFileState","MainPID","Job"))
    need(operator=={
        "ActiveState":"inactive","UnitFileState":"disabled",
        "MainPID":"0","Job":""
    },"root_worker_must_be_inert")
    ng=service_fields(NGINX,("ActiveState","UnitFileState"))
    need(ng=={"ActiveState":"inactive","UnitFileState":"disabled"},
         "external_ingress_not_isolated")
    db=service_fields(DASHBOARD,("ActiveState","MainPID"))
    need(db["ActiveState"]=="active" and db["MainPID"] not in ("","0"),
         "dashboard_not_healthy")
    auth=strict_unchanged_authority()
    raw=root_file(BASELINE,bound=65536)
    need(sha(raw)==BASELINE_SHA,"original_timer_baseline_changed")
    original=json.loads(raw)
    need(type(original) is dict and set(original)==set(TIMERS),
         "timer_baseline_invalid")
    timers=snapshot_timer_intents()
    need(timers==original,"timer_intent_not_restored")
    need(sha(root_file(FAILURE_RECEIPT,bound=MAX_FILE_BYTES))==FAILURE_SHA,
         "cutover_rollback_evidence_changed")
    from obsidian_automation.admin_status import SERVICES
    spec=next(x for x in SERVICES if x.identifier==TARGET_ROLE)
    observation=adapter.observe_workflow(spec)
    need(set(observation.services)==set(group_units[TARGET_ROLE]),
         "role_service_inventory_changed")
    for name,props in observation.services.items():
        need(set(props)==set(service_fields_keys)
             and props["LoadState"]=="loaded"
             and props["ActiveState"]=="inactive"
             and props["SubState"]=="dead"
             and props["Type"]=="oneshot"
             and props["Job"] in ("","0")
             and props["MainPID"]=="0"
             and props["ControlPID"]=="0"
             and props["FragmentPath"]=="/etc/systemd/system/"+name,
             "related_service_job_or_process_active")
    for name,props in observation.timers.items():
        need(set(props)==set(timer_fields)
             and props["LoadState"]=="loaded"
             and props["Job"] in ("","0")
             and props["Unit"] in {
                 "obsidian-github-compactor.service",
                 "obsidian-github-daily-apply.service",
             }
             and props["FragmentPath"]=="/etc/systemd/system/"+name,
             "related_timer_unverified")
    return {"authority_revision":auth["revision"],
            "original_timer_intents":original,
            "role_services_idle":True,
            "timer_intents_restored":True}

def require_record_evidence(queue, recorder):
    from obsidian_automation.admin_operation_queue import _safe_identity
    # Inspection is READ ONLY: the existing queue lock must be present.
    # Queue._open normally creates that lock for first-time provisioning.
    prior=QUEUE/"admission.lock"
    info=prior.lstat()
    need(stat.S_ISREG(info.st_mode) and info.st_uid==os.geteuid()
         and stat.S_IMODE(info.st_mode)==0o600 and info.st_nlink==1,
         "existing_queue_lock_unavailable")
    fd,lock=queue._open()
    try:
        original=recorder(fd,"request-"+UUID+".json")
        claim=recorder(fd,"processing-"+UUID+".json")
        outcome=recorder(fd,"result-"+UUID+".json")
        from obsidian_automation.admin_operation_queue import _exists
        active=(recorder(fd,"active.json")
                if _exists(fd,"active.json") else None)
        action=_safe_identity(original)
        need(action.request_id==UUID and action.service_id==TARGET_ROLE
             and action.action=="run_once",
             "historical_queue_identity_mismatch")
        if active != original:
            # After finalization, another new request may be active; this old
            # UUID remains UNKNOWN and can be inspected without replay.
            need(queue._manual_proof(fd,action) is not None,
                 "historical_queue_identity_mismatch")
        need(claim=={
          "record_version":1,"request_id":UUID,"service_id":TARGET_ROLE,
          "action":"run_once","state":"processing",
        },"historical_queue_claim_invalid")
        need(outcome=={
          "record_version":1,"request_id":UUID,"service_id":TARGET_ROLE,
          "action":"run_once","state":"indeterminate",
        },"not_an_indeterminate_history")
        # A completed reconciliation is never silently repeated here. For a
        # partial proof, explicit continuation is handled by Queue's
        # idempotent guarded finalizer with same approval and evidence bytes.
        return action, original
    finally:
        queue._close(fd,lock)

def previously_recorded_boot(
    evidence: dict, current_boot: str, *, resume_across_reboot: bool,
) -> str:
    """Explicit post-reboot continuation keeps the original evidence boot.

    All current root-owned source, systemd units, authority, updater locks,
    original receipts and idle services have been requalified before this
    function is called. No historical evidence is rewritten on boot drift.
    """
    old=evidence.get("boot_id")
    need(type(old) is str and re.fullmatch(
        r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}", old
    ) is not None,"manual_evidence_boot_invalid")
    need(
        old==current_boot or resume_across_reboot is True,
        "manual_evidence_reboot_requires_explicit_renewed_acceptance",
    )
    return old


def main() -> int:
    parser=argparse.ArgumentParser(description="#442 no-replay root forensic")
    modes=parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--inspect",action="store_true")
    modes.add_argument("--reconcile",action="store_true")
    parser.add_argument("--request-id",required=True)
    parser.add_argument("--approval-reference",default="")
    parser.add_argument("--human-accept",default="")
    parser.add_argument("--resume-across-reboot",action="store_true")
    args=parser.parse_args()
    try:
        need(args.request_id==UUID,"unreviewed_incident_id")
        need(os.geteuid()==0 and sys.flags.isolated==sys.flags.no_site==
             sys.flags.dont_write_bytecode==1
             and sys.pycache_prefix=="/dev/null/obsidian-admin-source-only",
             "root_isolated_source_only_required")
        qualify_root_entrypoint()
        strict_unchanged_authority()
        (gate,adapter_type,_,queue_type,raw_sha,read,_store,
         _,groups,timer_fields,service_props)=load_qualified_imports()
        queue=queue_type(queue_root=QUEUE,require_root=True,enabled=False,
                         allow_run_once=False)
        with gate():
            state=check_environment(
                adapter_type(),queue,groups,timer_fields,service_props
            )
            action,_=require_record_evidence(queue,read)
            raw_fd=os.open("/proc/sys/kernel/random/boot_id",
                           os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
            try: boot=os.read(raw_fd,64).decode("ascii").strip()
            finally:os.close(raw_fd)
            need(re.fullmatch(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}",boot)
                 is not None,"boot_id_unavailable")
            if args.inspect:
                print(json.dumps({
                    "status":"READ_ONLY_RECONCILIATION_PRECHECK_PASS",
                    "request_id":UUID,"old_result":"indeterminate",
                    "business_outcome":"UNKNOWN",
                    "boot_id":boot,
                    "authority_revision":state["authority_revision"],
                    "all_role_services_idle":True,
                    "timer_intents_restored":True,
                    "would_resubmit":False,
                    "would_modify_queue":False,
                },sort_keys=True))
                return 0
            need(args.human_accept==ACCEPT,"specific_human_approval_missing")
            need(REF_PATTERN.fullmatch(args.approval_reference) is not None,
                 "specific_approval_reference_missing")
            evidence_boot=boot
            fd,lock=queue._open()
            try:
                filename=f"manual-evidence-{UUID}.json"
                # Never overwrite or reinterpret an existing root-owned
                # evidence file. _read_private verifies symlinks, length,
                # owner, mode and duplicate keys, and Queue later verifies
                # all original-record hashes again before finalization.
                recorded=read(fd,filename)
                need(recorded["approval_reference"]==args.approval_reference,
                     "manual_evidence_reference_mismatch")
                evidence_boot=previously_recorded_boot(
                    recorded,boot,
                    resume_across_reboot=args.resume_across_reboot,
                )
            except FileNotFoundError:
                need(not args.resume_across_reboot,
                     "reboot_continuation_without_prior_evidence")
                evidence={
                    "record_version":1,"request_id":UUID,
                    "service_id":TARGET_ROLE,"action":"run_once",
                    "state":"root_verified_manual_evidence",
                    "decision":"acknowledge_unknown_no_replay",
                    "approval_reference":args.approval_reference,
                    "observed_at":datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "boot_id":boot,
                    "operator_inactive":True,
                    "authority_disabled":True,
                    "socket_absent":True,
                    "role_services_idle":True,
                    "timer_intents_restored":True,
                    "business_outcome":"unknown_no_success_claim",
                    "request_sha256":raw_sha(fd,"request-"+UUID+".json"),
                    "processing_sha256":raw_sha(fd,"processing-"+UUID+".json"),
                    "result_sha256":raw_sha(fd,"result-"+UUID+".json"),
                    "active_sha256":raw_sha(fd,"active.json"),
                }
                _store(fd,filename,evidence)
            finally:
                queue._close(fd,lock)
            fd,lock=queue._open()
            try: digest=raw_sha(fd,f"manual-evidence-{UUID}.json")
            finally: queue._close(fd,lock)
            result=queue.manually_reconcile_unknown(
                UUID,human_accept=args.human_accept,
                approval_reference=args.approval_reference,
                evidence_sha256=digest,boot_id=evidence_boot,
            )
            need(result["state"]=="manually_reconciled_unknown",
                 "manual_finalization_failed")
            print(json.dumps({
                "status":"MANUALLY_RECONCILED_WITHOUT_REPLAY",
                "request_id":UUID,
                "original_result":"indeterminate",
                "evidence_sha256":digest,
                "job_submissions":0,
                "business_success_asserted":False,
                "queue_remaining_on_hold":False,
                "recorded_evidence_boot_id":evidence_boot,
                "actual_boot_id_at_reconcile":boot,
                "reboot_continuation":evidence_boot!=boot,
            },sort_keys=True))
            return 0
    except Exception as exc:
        # This standalone root tool must not print a filesystem exception,
        # production record content, arbitrary subprocess output or secrets.
        # BaseException (KeyboardInterrupt/SystemExit) is not caught.
        print(json.dumps({
            "status":"RECONCILIATION_BLOCKED",
            "reason":type(exc).__name__ if type(exc) is not ReconcileBlocked
                     else str(exc),
        },sort_keys=True),file=sys.stderr)
        return 2

if __name__=="__main__":
    raise SystemExit(main())
