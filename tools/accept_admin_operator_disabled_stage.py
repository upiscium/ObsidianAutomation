#!/usr/bin/env python3
"""#403: bounded, disabled-only operator stage onto an existing single LXC.

Deploys ONLY the reviewed root launcher, its systemd Unit, the one changed
privileged source module and a disabled authority manifest. It never explicitly
starts/stops/enables/disables managed timers or re-runs global ACL provisioning.
systemctl daemon-reload is manager-wide and may re-run normal unit generators.

This is a one-time, human-supervised root maintenance utility. Immutable
backup/receipt is preserved for manual recovery. It is NOT the final Cutover.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile

TARGET = "977f29bf7a7f69172d894f6cf98785a8b4258f2c"
OLD = "2b860f2fdec954ed363f6b23ed1df5f1597bbaf3"
SOURCE_PREFIX = "/dev/null/obsidian-admin-source-only"
APP = Path("/opt/obsidian-automation/app")
PKG = Path("/opt/obsidian-automation/venv/lib/python3.13/site-packages/obsidian_automation")
AUTHORITY = Path("/etc/obsidian-admin/authority.json")
OPERATOR = "obsidian-admin-operator.service"
WEB = "obsidian-admin-dashboard.service"
STAGE_PARENT = Path("/var/lib/obsidian-admin/operator-stages")
HOST_LOCK = Path("/var/lib/obsidian-automation/deployments/host-update.lock")
REFRESH_LOCK = Path("/run/obsidian-automation/semantic-refresh-inhibited.lock")
HOST_PENDING = HOST_LOCK.parent / "pending-runtime.json"
REFRESH_PENDING = REFRESH_LOCK.parent / "semantic-refresh-inhibited.json"
SOCKET = Path("/run/obsidian-admin/operation.sock")
TIMERS = (
    "obsidian-ai-vault-pull.timer",
    "obsidian-pre-review.timer",
    "obsidian-github-sync.timer",
    "obsidian-github-daily-progress.timer",
    "obsidian-core-promotion.timer",
)
BLOBS = {
    "launcher": (
        "examples/admin/obsidian-admin-root-launch.py",
        Path("/usr/local/libexec/obsidian-admin-root-launch.py"), 0o755,
        "6ca1c6480384043fc9e349220fead57526d7ff188b5acc97416385edf91d7b9b",
    ),
    "unit": (
        "examples/admin/obsidian-admin-operator.service",
        Path("/etc/systemd/system/obsidian-admin-operator.service"), 0o644,
        "a4d4e1796840aab062fe1942f5aeb6a2b3209d4c2af39c85623ee75ee4d2a0dc",
    ),
    "runtime": (
        "src/obsidian_automation/admin_operator_runtime.py",
        PKG/"admin_operator_runtime.py", 0o644,
        "5a98f8f41ea43c867bf080b3a6fb643b2a318b11f7480a9c74354babcb3a60ac",
    ),
    "stager": (
        "tools/prepare_admin_operator_authority.py", None, 0o600,
        "cb7f35bdccdff964b14652523e1c8bccccbf332b92566b42b2cf43b10b5f99bc",
    ),
}

class StageBlocked(RuntimeError):
    pass

def require(condition: bool, reason: str) -> None:
    if not condition:
        raise StageBlocked(reason)

def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def run(args: list[str], *, timeout: int = 35) -> bytes:
    try:
        p = subprocess.run(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StageBlocked("subprocess_unavailable") from exc
    if p.returncode != 0:
        raise StageBlocked("command_failed:" + Path(args[0]).name)
    return p.stdout

def parent_safe(path: Path) -> None:
    require(path.is_absolute() and ".." not in path.parts,
            "invalid_absolute_path")
    fd = os.open("/", os.O_DIRECTORY | os.O_RDONLY | os.O_CLOEXEC)
    try:
        for part in path.parts[1:-1]:
            nxt = os.open(part, os.O_DIRECTORY | os.O_RDONLY |
                          os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            info = os.fstat(nxt)
            require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0
                    and not stat.S_IMODE(info.st_mode) & 0o022,
                    "unsafe_parent_directory")
            os.close(fd)
            fd = nxt
    finally:
        os.close(fd)

def root_file(path: Path, *, max_bytes: int = 2 * 1024 * 1024) -> bytes:
    parent_safe(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW |
                 os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == 0
                and info.st_nlink == 1
                and not stat.S_IMODE(info.st_mode) & 0o022
                and 0 < info.st_size <= max_bytes,
                "unsafe_root_owned_file:" + path.name)
        data = os.read(fd, max_bytes + 1)
        require(len(data) == info.st_size, "short_read:" + path.name)
        return data
    finally:
        os.close(fd)

def qualified_git(*arguments: str) -> bytes:
    """Only read Git objects from a root-owned checkout with inert hooks.

    Host source does not come from a writable CWD, environment-selected Git
    configuration, or textconv/filter callbacks. The tree is NOT reset.
    """
    parent_safe(APP / ".git")
    app_stat = APP.lstat()
    gitdir = APP / ".git"
    git_stat = gitdir.lstat()
    require(
        stat.S_ISDIR(app_stat.st_mode) and app_stat.st_uid == 0
        and not stat.S_IMODE(app_stat.st_mode) & 0o022
        and stat.S_ISDIR(git_stat.st_mode) and git_stat.st_uid == 0
        and not stat.S_IMODE(git_stat.st_mode) & 0o022,
        "untrusted_git_checkout",
    )
    root_file(gitdir / "config", max_bytes=65536)
    # Local read-only commands only. Any Git subprocess receives an explicitly
    # safe config/environment; no configured hooks, fsmonitor, or inherited
    # writable user/system Git configuration.
    allowed = {"rev-parse", "cat-file", "show", "merge-base"}
    require(arguments and arguments[0] in allowed,
            "disallowed_git_operation")
    command = [
        "/usr/bin/git", "--no-optional-locks",
        "-c", "core.fsmonitor=false",
        "-c", "core.hooksPath=/dev/null",
        "-c", "core.untrackedCache=false",
        "-C", str(APP), *arguments,
    ]
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/root",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "LANG": "C",
    }
    try:
        result = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=environment, check=False, timeout=35,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StageBlocked("trusted_git_unavailable") from exc
    require(result.returncode == 0, "trusted_git_read_failed")
    return result.stdout


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_DIRECTORY | os.O_RDONLY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

def durable_new(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                 os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.fchown(fd, 0, 0)
        with os.fdopen(fd, "wb", closefd=False) as f:
            f.write(data)
            f.flush()
        os.fsync(fd)
    finally:
        os.close(fd)
    sync_directory(path.parent)

def atomic_replace(path: Path, data: bytes, mode: int) -> None:
    parent_safe(path)
    temporary_fd, name = tempfile.mkstemp(prefix=".oa403-stage-",dir=path.parent)
    tmp = Path(name)
    try:
        os.fchown(temporary_fd, 0, 0)
        os.fchmod(temporary_fd, mode)
        with os.fdopen(temporary_fd, "wb", closefd=False) as f:
            f.write(data)
            f.flush()
        os.fsync(temporary_fd)
    finally:
        os.close(temporary_fd)
    try:
        os.replace(tmp, path)
        sync_directory(path.parent)
    finally:
        if tmp.exists():
            tmp.unlink()

def actual_lock(lock: Path) -> int:
    parent_safe(lock)
    fd = os.open(lock, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        path_info = os.stat(lock, follow_symlinks=False)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == 0
                and stat.S_IMODE(info.st_mode) == 0o600
                and info.st_nlink == 1
                and (info.st_dev, info.st_ino) ==
                (path_info.st_dev, path_info.st_ino), "unsafe_update_lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise StageBlocked("concurrent_updater_busy") from exc
        after = os.stat(lock, follow_symlinks=False)
        require((after.st_dev, after.st_ino) ==
                (info.st_dev, info.st_ino), "replaced_update_lock")
        result, fd = fd, None
        return result
    finally:
        if fd is not None:
            os.close(fd)

@contextmanager
def updater_gate():
    host = refresh = None
    try:
        host = actual_lock(HOST_LOCK)
        require(not os.path.lexists(HOST_PENDING), "host_update_pending")
        refresh = actual_lock(REFRESH_LOCK)
        require(not os.path.lexists(REFRESH_PENDING), "refresh_update_pending")
        require(not os.path.lexists(HOST_PENDING), "host_update_pending")
        yield
    finally:
        if refresh is not None:
            os.close(refresh)
        if host is not None:
            os.close(host)

def get_systemd(name: str) -> dict[str,str]:
    output = run([
        "/usr/bin/systemctl", "show", name,
        "-p", "ActiveState", "-p", "UnitFileState",
        "-p", "SubState", "-p", "MainPID", "-p", "Job",
        "--no-pager",
    ]).decode("utf-8")
    fields = dict(line.split("=",1) for line in output.splitlines()
                  if "=" in line)
    require("ActiveState" in fields and "UnitFileState" in fields,
            "systemd_state_incomplete")
    return fields

def observe_timers() -> dict[str,dict[str,str]]:
    return {
        name: {k:v for k,v in get_systemd(name).items()
               if k in ("ActiveState","UnitFileState")}
        for name in TIMERS
    }

def check_inert() -> None:
    op = get_systemd(OPERATOR)
    web = get_systemd(WEB)
    require(op["UnitFileState"] == "disabled"
            and op["ActiveState"] == "inactive"
            and op["MainPID"] == "0" and not op["Job"],
            "root_operator_not_inert")
    require(web["ActiveState"] == "active" and web["MainPID"] != "0",
            "web_dashboard_not_active")
    require(not os.path.lexists(SOCKET), "root_worker_socket_present")

def git_blob(path: str, max_bytes: int = 2 * 1024 * 1024) -> bytes:
    value = qualified_git("show",f"{TARGET}:{path}")
    require(0 < len(value) <= max_bytes, "invalid_target_git_blob")
    return value

def load_target() -> dict[str, bytes]:
    require(os.geteuid() == 0, "root_required")
    require(sys.flags.isolated == sys.flags.no_site ==
            sys.flags.dont_write_bytecode == 1,
            "isolated_source_only_python_required")
    require(sys.pycache_prefix == SOURCE_PREFIX
            and stat.S_ISCHR(Path("/dev/null").lstat().st_mode),
            "unreachable_cache_prefix_required")
    require(run(["/usr/bin/hostname"]).strip() == b"obsidian-automation",
            "wrong_production_host")
    require(qualified_git("rev-parse","HEAD").strip().decode()
            == OLD, "global_application_revision_changed")
    require(qualified_git("rev-parse","origin/main").strip().decode()
            == TARGET, "reviewed_remote_revision_unavailable")
    require(qualified_git("cat-file","-t",TARGET).strip()
            == b"commit", "target_commit_unavailable")
    qualified_git("merge-base","--is-ancestor",OLD,TARGET)
    result={}
    for key,(rel,_,__,expected) in BLOBS.items():
        data=git_blob(rel)
        require(sha(data) == expected, "unapproved_target_blob:" + key)
        result[key]=data
    return result

def validate_layout(blobs: dict[str,bytes]) -> dict:
    check_inert()
    require(not os.path.lexists(HOST_PENDING)
            and not os.path.lexists(REFRESH_PENDING),
            "pending_updater_effect")
    current=json.loads(root_file(AUTHORITY,max_bytes=6500))
    require(current["revision"] in (OLD,TARGET), "unexpected_authority_revision")
    for flag in ("enabled","coordinated_updaters_verified",
                 "uncoordinated_launchers_retired"):
        require(current.get(flag) is False, "effect_authority_not_disabled")
    require(current.get("single_active_lxc") is True,
            "single_lxc_unqualified")
    require(len(current["unit_sha256"]) == 10
            and len(current["module_sha256"]) == 14,
            "unexpected_manifest_closure")
    for name,digest in current["unit_sha256"].items():
        require(sha(root_file(Path("/etc/systemd/system")/name,
                              max_bytes=16384)) == digest,
                "installed_unit_drift:" + name)
    for name,digest in current["module_sha256"].items():
        require(sha(root_file(PKG/name)) == digest,
                "installed_module_drift:" + name)
        if name != "admin_operator_runtime.py":
            require(sha(root_file(PKG/name)) ==
                    sha(git_blob("src/obsidian_automation/"+name)),
                    "unrelated_privileged_module_changed:" + name)
    # The old installed launcher is not listed in authority.json; retain a
    # precise backup and enforce exact target bytes before staging success.
    assert blobs["launcher"] and blobs["unit"] and blobs["runtime"]
    parent_safe(AUTHORITY)
    info=AUTHORITY.parent.stat()
    require(info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o710,
            "unsafe_authority_directory")
    for _,dest,mode,_ in BLOBS.values():
        if dest is not None:
            info=dest.lstat()
            require(info.st_uid == 0 and stat.S_ISREG(info.st_mode)
                    and stat.S_IMODE(info.st_mode) == mode,
                    "unexpected_installed_mode")
    return current

def reject_unresolved_operator_stages() -> None:
    if not STAGE_PARENT.exists():
        return
    parent_safe(STAGE_PARENT / "sentinel")
    state=STAGE_PARENT.lstat()
    require(stat.S_ISDIR(state.st_mode) and state.st_uid == 0
            and stat.S_IMODE(state.st_mode) == 0o700,
            "unsafe_existing_stage_parent")
    for directory in STAGE_PARENT.iterdir():
        require(directory.is_dir() and not directory.is_symlink(),
                "untrusted_stage_entry")
        if os.path.lexists(directory / "pending.json"):
            raise StageBlocked("operator_stage_recovery_required")


def create_stage_dir() -> Path:
    base=STAGE_PARENT.parent
    require(base.is_dir() and not base.is_symlink()
            and base.stat().st_uid == 0
            and stat.S_IMODE(base.stat().st_mode)==0o700,
            "unsafe_operator_state_directory")
    if not STAGE_PARENT.exists():
        os.mkdir(STAGE_PARENT,0o700)
        sync_directory(base)
    require(STAGE_PARENT.is_dir() and not STAGE_PARENT.is_symlink()
            and STAGE_PARENT.stat().st_uid == 0
            and stat.S_IMODE(STAGE_PARENT.stat().st_mode)==0o700,
            "unsafe_staging_parent")
    result=Path(tempfile.mkdtemp(
        prefix="stage-"+TARGET[:12]+"-",dir=STAGE_PARENT))
    os.chmod(result,0o700)
    sync_directory(STAGE_PARENT)
    return result

def verify_staged(blobs:dict[str,bytes], before:dict[str,dict[str,str]]) -> dict:
    check_inert()
    require(observe_timers()==before, "managed_timer_intent_changed")
    actual=json.loads(root_file(AUTHORITY,max_bytes=6500))
    require(actual["revision"]==TARGET and actual["single_active_lxc"] is True,
            "staged_authority_revision_mismatch")
    for flag in ("enabled","coordinated_updaters_verified",
                 "uncoordinated_launchers_retired"):
        require(actual.get(flag) is False,
                "staged_authority_unsafe:"+flag)
    require(set(actual["module_sha256"]) ==
            set(json.loads(root_file(AUTHORITY,max_bytes=6500))["module_sha256"]),
            "missing_privileged_modules")
    for name,digest in actual["module_sha256"].items():
        require(sha(root_file(PKG/name))==digest,
                "module_sha_mismatch:"+name)
    for name,digest in actual["unit_sha256"].items():
        require(sha(root_file(Path("/etc/systemd/system")/name,
                              max_bytes=16384))==digest,
                "unit_sha_mismatch:"+name)
    for k in ("launcher","unit","runtime"):
        _,dest,_,_ = BLOBS[k]
        require(sha(root_file(dest))==sha(blobs[k]),
                "installed_blob_drift:"+k)
    run(["/usr/bin/systemd-analyze","verify",
         str(BLOBS["unit"][1])])
    require(not os.path.lexists(HOST_PENDING)
            and not os.path.lexists(REFRESH_PENDING),
            "updater_started_during_stage")
    return actual

def stage(blobs:dict[str,bytes], old:dict) -> dict:
    before=observe_timers()
    if old["revision"]==TARGET:
        state=verify_staged(blobs,before)
        return {"status":"already_staged","target_sha":TARGET,
                "flags_disabled":True,"module_count":len(state["module_sha256"]),
                "unit_count":len(state["unit_sha256"])}
    stage_dir=create_stage_dir()
    old_files={
        key:root_file(dest,max_bytes=6500 if key=="authority" else 2*1024*1024)
        for key,dest in {
            "launcher": BLOBS["launcher"][1],
            "unit": BLOBS["unit"][1],
            "runtime": BLOBS["runtime"][1],
            "authority":AUTHORITY,
        }.items()
    }
    paths={key:BLOBS[key][1] for key in ("launcher","unit","runtime")}
    paths["authority"]=AUTHORITY
    modes={key:BLOBS[key][2] for key in ("launcher","unit","runtime")}
    modes["authority"]=0o600
    touched=[]
    for key,content in old_files.items():
        durable_new(stage_dir/("before-"+key+".bin"),content)
    durable_new(stage_dir/"reviewed-stage-helper.py",blobs["stager"])
    pending={"version":1,"phase":"prepared","target_sha":TARGET,
             "global_app_sha":OLD,"backup_files":{
                 k:sha(v) for k,v in old_files.items()
             },"operator_effects_enabled":False,
             "timers_before":before}
    durable_new(stage_dir/"pending.json",
                (json.dumps(pending,sort_keys=True)+"\n").encode())
    try:
        for key in ("runtime","launcher","unit"):
            touched.append(key)
            atomic_replace(paths[key],blobs[key],modes[key])
        run(["/usr/bin/systemctl","daemon-reload"])
        # Execute exact reviewed stdlib helper from private root staging.
        # Its fixed authority path is redirected ONLY to a random candidate
        # in the SAME protected /etc/obsidian-admin directory. The live
        # off-by-default authority remains available until atomic replace.
        source=stage_dir/"reviewed-stage-helper.py"
        spec=importlib.util.spec_from_file_location(
            "_oa403_exact_disabled_stage",source)
        require(spec is not None and spec.loader is not None,
                "invalid_reviewed_stage_helper")
        helper=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        candidate=AUTHORITY.parent/(".authority-oa403-"+stage_dir.name+".json")
        require(not os.path.lexists(candidate), "candidate_collision")
        helper.DEFAULT_AUTHORITY_PATH=candidate
        try:
            helper.stage(TARGET)
            candidate_bytes=root_file(candidate,max_bytes=6500)
            manifest=json.loads(candidate_bytes)
            require(manifest["revision"]==TARGET and
                    all(manifest[k] is False for k in
                        ("enabled","coordinated_updaters_verified",
                         "uncoordinated_launchers_retired")),
                    "reviewed_stager_not_disabled")
            # Publish the ONE expected candidate authority SHA before
            # replacing the live file. Recovery may accept only this exact
            # generated manifest or the backed-up previous authority bytes.
            # A third-party disabled manifest is never silently overwritten.
            durable_new(stage_dir/"candidate-authority.sha256",
                        (sha(candidate_bytes)+"\n").encode("ascii"))
            touched.append("authority")
            os.replace(candidate,AUTHORITY)
            sync_directory(AUTHORITY.parent)
        finally:
            if os.path.lexists(candidate):
                candidate.unlink()
        actual=verify_staged(blobs,before)
        receipt={"version":1,"phase":"completed",
                 "status":"disabled_operator_staged",
                 "target_sha":TARGET,"global_app_sha":OLD,
                 "root_launcher_sha256":sha(blobs["launcher"]),
                 "root_unit_sha256":sha(blobs["unit"]),
                 "root_runtime_sha256":sha(blobs["runtime"]),
                 "manifest_sha256":sha(root_file(AUTHORITY,max_bytes=6500)),
                 "module_count":len(actual["module_sha256"]),
                 "unit_count":len(actual["unit_sha256"]),
                 "timers_before":before,"timers_after":observe_timers(),
                 "operator_effects_enabled":False,
                 "recovery_dir":str(stage_dir),
                 "note":"Operator-only overlay; global app checkout unchanged"}
        durable_new(stage_dir/"completed.json",
                    (json.dumps(receipt,sort_keys=True)+"\n").encode())
        (stage_dir/"pending.json").unlink()
        sync_directory(stage_dir)
        return receipt
    except BaseException:
        # A completion artifact is not valid if the final durable commit
        # fails. Revoke it before attempting any rollback of installed files.
        complete=stage_dir/"completed.json"
        if os.path.lexists(complete):
            try:
                complete.unlink()
                sync_directory(stage_dir)
            except OSError:
                # Never publish an apparent rollback success while a stale
                # completed.json still exists. Require manual reconciliation.
                failure_marker=stage_dir/"completion_revoke_failed"
                if not os.path.lexists(failure_marker):
                    durable_new(failure_marker,b"manual_recovery_required\n")
                raise
        # An interrupted/failed stage must restore every potentially changed
        # installed file and the disabled authority. No timer restoration is
        # attempted because this utility NEVER changes any timer.
        failure=[]
        for key in reversed(touched):
            try:
                atomic_replace(paths[key],old_files[key],modes[key])
            except Exception as exc:
                failure.append(key+":"+type(exc).__name__)
        try:
            run(["/usr/bin/systemctl","daemon-reload"])
        except Exception as exc:
            failure.append("daemon_reload:"+type(exc).__name__)
        document={"version":1,"phase":"rolled_back" if not failure else
                  "manual_recovery_required","target_sha":TARGET,
                  "changed_paths":touched,"rollback_failures":failure,
                  "recovery_dir":str(stage_dir)}
        try:
            durable_new(stage_dir/"failure.json",
                        (json.dumps(document,sort_keys=True)+"\n").encode())
            if not failure:
                (stage_dir/"pending.json").unlink()
                sync_directory(stage_dir)
        except Exception:
            pass
        raise

def recover_pending(directory: Path, blobs: dict[str,bytes]) -> dict:
    """Manual exact-backup recovery after an interrupted disabled-only stage.

    This requires the same two updater locks, an inert root Worker and four
    root-private backup digests bound to a durable pending.json. Never restore
    paths or arbitrary files selected by HTTP or from an untrusted directory.
    """
    require(directory.is_absolute() and directory.parent==STAGE_PARENT
            and directory.name.startswith("stage-"+TARGET[:12]+"-"),
            "invalid_recovery_directory")
    parent_safe(directory/"pending.json")
    info=directory.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid==0
            and stat.S_IMODE(info.st_mode)==0o700,
            "untrusted_recovery_directory")
    pending=json.loads(root_file(directory/"pending.json",max_bytes=65536))
    require(pending.get("version")==1 and pending.get("phase")=="prepared"
            and pending.get("target_sha")==TARGET
            and pending.get("global_app_sha")==OLD
            and pending.get("operator_effects_enabled") is False,
            "untrusted_pending_stage")
    before=pending.get("timers_before")
    require(isinstance(before,dict) and set(before)==set(TIMERS),
            "untrusted_timer_intent")
    require(observe_timers()==before, "timer_intent_drift_before_recovery")
    check_inert()
    paths={k:BLOBS[k][1] for k in ("launcher","unit","runtime")}
    paths["authority"]=AUTHORITY
    modes={k:BLOBS[k][2] for k in ("launcher","unit","runtime")}
    modes["authority"]=0o600
    backup_digests=pending.get("backup_files")
    require(isinstance(backup_digests,dict)
            and set(backup_digests)==set(paths), "missing_backup_digest")
    backups={}
    for name in paths:
        value=root_file(directory/("before-"+name+".bin"),
                        max_bytes=6500 if name=="authority" else 2*1024*1024)
        require(sha(value)==backup_digests[name],
                "recovery_backup_mismatch:"+name)
        backups[name]=value
    previous=json.loads(backups["authority"])
    require(previous.get("revision")==OLD
            and all(previous.get(x) is False for x in
                    ("enabled","coordinated_updaters_verified",
                     "uncoordinated_launchers_retired")),
            "recovery_original_authority_unsafe")
    # The current files must be either our exact target or their backed-up
    # previous bytes; reject unrelated changes instead of clobbering them.
    candidate_digest=directory/"candidate-authority.sha256"
    allowed_authorities={sha(backups["authority"])}
    if os.path.lexists(candidate_digest):
        expected=root_file(candidate_digest,max_bytes=128).decode("ascii")
        require(len(expected.strip())==64 and
                all(c in "0123456789abcdef" for c in expected.strip()),
                "invalid_candidate_authority_digest")
        allowed_authorities.add(expected.strip())
    for name,path in paths.items():
        current=root_file(path,max_bytes=6500 if name=="authority" else 2*1024*1024)
        if name=="authority":
            state=json.loads(current)
            require(state.get("revision") in (OLD,TARGET)
                    and all(state.get(x) is False for x in
                            ("enabled","coordinated_updaters_verified",
                             "uncoordinated_launchers_retired")),
                    "recovery_authority_current_unsafe")
            require(sha(current) in allowed_authorities,
                    "recovery_authority_unrelated_change")
        else:
            require(sha(current) in {sha(backups[name]),sha(blobs[name])},
                    "recovery_file_unexpected:"+name)
    complete=directory/"completed.json"
    if os.path.lexists(complete):
        complete.unlink()
        sync_directory(directory)
    for name in ("authority","unit","launcher","runtime"):
        atomic_replace(paths[name],backups[name],modes[name])
    run(["/usr/bin/systemctl","daemon-reload"])
    for name,path in paths.items():
        require(sha(root_file(path,max_bytes=6500 if name=="authority" else
                              2*1024*1024))==backup_digests[name],
                "recovery_restore_not_durable:"+name)
    check_inert()
    require(observe_timers()==before, "timer_intent_drift_after_recovery")
    result={"status":"recovered_previous_disabled_operator",
            "target_sha":TARGET,"global_app_sha":OLD,
            "operator_effects_enabled":False,
            "recovery_dir":str(directory)}
    durable_new(directory/"recovered.json",
                (json.dumps(result,sort_keys=True)+"\n").encode())
    (directory/"pending.json").unlink()
    sync_directory(directory)
    return result


def main():
    parser=argparse.ArgumentParser()
    options=parser.add_mutually_exclusive_group(required=True)
    options.add_argument("--inspect",action="store_true")
    options.add_argument("--apply-disabled",action="store_true")
    options.add_argument("--recover-dir",type=Path)
    args=parser.parse_args()
    try:
        blobs=load_target()
        with updater_gate():
            if args.recover_dir is not None:
                result=recover_pending(args.recover_dir,blobs)
                print(json.dumps(result,sort_keys=True))
                return 0
            reject_unresolved_operator_stages()
            old=validate_layout(blobs)
            timers=observe_timers()
            if args.inspect:
                print(json.dumps({
                    "status":"PRECHECK_PASS",
                    "target_sha":TARGET,
                    "installed_global_sha":OLD,
                    "authority_revision":old["revision"],
                    "operator_state":get_systemd(OPERATOR),
                    "module_count":len(old["module_sha256"]),
                    "unit_count":len(old["unit_sha256"]),
                    "target_blobs":{k:sha(v) for k,v in blobs.items()},
                    "timer_intent":timers,"operator_effects_enabled":False,
                },sort_keys=True))
                return 0
            result=stage(blobs,old)
            print(json.dumps(result,sort_keys=True))
            return 0
    except (StageBlocked, OSError, ValueError, KeyError, TypeError,
            subprocess.SubprocessError) as exc:
        print(json.dumps({"status":"BLOCKED","error_type":type(exc).__name__,
                          "reason":str(exc)[:160]},sort_keys=True),
              file=sys.stderr)
        return 2

if __name__=="__main__":
    raise SystemExit(main())
