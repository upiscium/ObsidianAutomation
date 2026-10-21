"""#403 isolated root-only test of the disabled Operator stage and recovery.

Requires euid=0, uses ONLY a freshly created /run/oa403-recovery-fixture-*
directory. Every systemd action is fake; source loading from Git, real /etc,
real /opt, live Unix sockets, timers and external providers are never called.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
TARGET="977f29bf7a7f69172d894f6cf98785a8b4258f2c"
OLD="2b860f2fdec954ed363f6b23ed1df5f1597bbaf3"
assert os.geteuid()==0, "disposable fixture root required"
assert Path("/run").is_dir() and not Path("/run").is_symlink()


def stager_fixture_bytes() -> bytes:
    return (
        "import json,os\n"
        "from pathlib import Path\n"
        "DEFAULT_AUTHORITY_PATH=None\n"
        "def stage(revision):\n"
        " document={'revision':revision,'enabled':False,"
        "'coordinated_updaters_verified':False,"
        "'uncoordinated_launchers_retired':False}\n"
        " path=DEFAULT_AUTHORITY_PATH\n"
        " fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)\n"
        " try:\n"
        "  body=(json.dumps(document,sort_keys=True)+'\\n').encode()\n"
        "  os.write(fd,body)\n"
        "  os.fsync(fd)\n"
        " finally: os.close(fd)\n"
    ).encode()

def load():
    p=ROOT/"tools/accept_admin_operator_disabled_stage.py"
    spec=importlib.util.spec_from_file_location("_oa403_root_fixture",p)
    assert spec and spec.loader
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def fresh_state():
    base=Path(tempfile.mkdtemp(prefix="oa403-recovery-fixture-",dir="/run"))
    os.chmod(base,0o700)
    mod=load()
    destination={
        "launcher":base/"launcher.py",
        "unit":base/"operator.service",
        "runtime":base/"admin_operator_runtime.py",
        "authority":base/"authority.json",
    }
    backup={
        "launcher":b"OLD_LAUNCHER",
        "unit":b"OLD_UNIT",
        "runtime":b"OLD_ROOT_MODULE",
        "authority":(json.dumps({
            "revision":OLD,"enabled":False,
            "coordinated_updaters_verified":False,
            "uncoordinated_launchers_retired":False,
        },sort_keys=True)+"\n").encode(),
    }
    blobs={
        "launcher":b"NEW_LAUNCHER","unit":b"NEW_UNIT",
        "runtime":b"NEW_ROOT_MODULE","stager":stager_fixture_bytes(),
    }
    mod.AUTHORITY=destination["authority"]
    mod.STAGE_PARENT=base/"operator-stages"
    mod.BLOBS={
        key:(f"fixture-{key}",destination.get(key),0o755 if key=="launcher" else 0o644,
             "0"*64)
        for key in ("launcher","unit","runtime","stager")
    }
    mod.BLOBS["stager"]=("fixture-stager",None,0o600,"0"*64)
    mod.get_systemd=lambda name:{
        "ActiveState":"inactive" if name==mod.OPERATOR else "active",
        "UnitFileState":"disabled" if name==mod.OPERATOR else "enabled",
        "MainPID":"0" if name==mod.OPERATOR else "987",
        "Job":"",
    }
    mod.check_inert=lambda:None
    mod.observe_timers=lambda:{
        unit:{"ActiveState":"active","UnitFileState":"enabled"}
        for unit in mod.TIMERS
    }
    mod.run=lambda *a,**kw:b""
    mod.verify_staged=lambda blobs, before:{
        "module_sha256":{str(i):"0"*64 for i in range(14)},
        "unit_sha256":{str(i):"0"*64 for i in range(10)},
    }
    for name,file in destination.items():
        mode=0o755 if name=="launcher" else 0o600 if name=="authority" else 0o644
        fd=os.open(file,os.O_CREAT|os.O_EXCL|os.O_WRONLY,mode)
        try:
            os.fchmod(fd,mode)
            os.write(fd,backup[name])
        finally: os.close(fd)
    return mod,base,destination,backup,blobs

def private_pending(mod, base, destination, backup, blobs):
    stage_dir=mod.create_stage_dir()
    for name,value in backup.items():
        mod.durable_new(stage_dir/f"before-{name}.bin",value)
    pending={
        "version":1,"phase":"prepared","target_sha":TARGET,
        "global_app_sha":OLD,"operator_effects_enabled":False,
        "backup_files":{k:mod.sha(v) for k,v in backup.items()},
        "timers_before":mod.observe_timers(),
    }
    mod.durable_new(stage_dir/"pending.json",
                    (json.dumps(pending,sort_keys=True)+"\n").encode())
    return stage_dir

def verify_old(mod,destination,backup):
    for name,file in destination.items():
        assert mod.root_file(file)==backup[name], f"restored {name}"
    assert json.loads(mod.root_file(destination["authority"]))["enabled"] is False

def verify_new(mod,destination,blobs):
    for name in ("launcher","unit","runtime"):
        assert mod.root_file(destination[name])==blobs[name]
    authority=json.loads(mod.root_file(destination["authority"]))
    assert authority["revision"]==TARGET
    assert all(authority[k] is False for k in (
        "enabled","coordinated_updaters_verified",
        "uncoordinated_launchers_retired"
    ))

def test_completed():
    mod,root,dest,before,blobs=fresh_state()
    try:
        result=mod.stage(blobs,{"revision":OLD})
        assert result["status"]=="disabled_operator_staged"
        verify_new(mod,dest,blobs)
        stage_dir=Path(result["recovery_dir"])
        assert (stage_dir/"completed.json").is_file()
        assert not (stage_dir/"pending.json").exists()
        assert all((stage_dir/f"before-{k}.bin").is_file() for k in dest)
        print("TEST_COMPLETED_DISABLED_STAGE_PASS")
    finally: shutil.rmtree(root)

def test_failed_receipt_cleanup_and_rollback():
    mod,root,dest,before,blobs=fresh_state()
    original=mod.sync_directory
    tripped=[False]
    def failure_on_complete(directory):
        if (
            not tripped[0]
            and directory.name.startswith("stage-"+TARGET[:12])
            and (directory/"completed.json").exists()
            and (directory/"pending.json").exists()
        ):
            tripped[0]=True
            raise OSError("DISPOSABLE_FIXTURE_SIMULATED_FSYNC_FAILURE")
        return original(directory)
    mod.sync_directory=failure_on_complete
    try:
        try:
            mod.stage(blobs,{"revision":OLD})
            raise AssertionError("failure injection was not reached")
        except OSError as exc:
            assert "SIMULATED_FSYNC_FAILURE" in str(exc)
        assert tripped[0]
        verify_old(mod,dest,before)
        stage_dirs=list(mod.STAGE_PARENT.iterdir())
        assert len(stage_dirs)==1
        stage_dir=stage_dirs[0]
        assert not (stage_dir/"completed.json").exists()
        assert (stage_dir/"failure.json").exists()
        assert not (stage_dir/"pending.json").exists()
        print("TEST_ROLLBACK_NO_STALE_COMPLETION_PASS")
    finally: shutil.rmtree(root)

def test_interrupted_recovery_exact_manifest_only():
    mod,root,dest,before,blobs=fresh_state()
    try:
        stage_dir=private_pending(mod,root,dest,before,blobs)
        for name in ("launcher","unit","runtime"):
            mode=0o755 if name=="launcher" else 0o644
            mod.atomic_replace(dest[name],blobs[name],mode)
        expected_authority=(json.dumps({
            "revision":TARGET,"enabled":False,
            "coordinated_updaters_verified":False,
            "uncoordinated_launchers_retired":False,
        },sort_keys=True)+"\n").encode()
        mod.durable_new(stage_dir/"candidate-authority.sha256",
                        (mod.sha(expected_authority)+"\n").encode())
        mod.atomic_replace(dest["authority"],expected_authority,0o600)
        unknown=(json.dumps({
            "revision":TARGET,"enabled":False,
            "coordinated_updaters_verified":False,
            "uncoordinated_launchers_retired":False,
            "unrelated_authority":True,
        },sort_keys=True)+"\n").encode()
        mod.atomic_replace(dest["authority"],unknown,0o600)
        try:
            mod.recover_pending(stage_dir,blobs)
            raise AssertionError("unrelated authority should be refused")
        except mod.StageBlocked as exc:
            assert "recovery_authority_unrelated_change" in str(exc)
        assert (stage_dir/"pending.json").exists()
        assert mod.root_file(dest["authority"])==unknown
        mod.atomic_replace(dest["authority"],expected_authority,0o600)
        result=mod.recover_pending(stage_dir,blobs)
        assert result["status"]=="recovered_previous_disabled_operator"
        verify_old(mod,dest,before)
        assert not (stage_dir/"pending.json").exists()
        assert (stage_dir/"recovered.json").is_file()
        print("TEST_SIGKILL_RECOVERY_AND_UNRELATED_CHANGE_REFUSAL_PASS")
    finally: shutil.rmtree(root)

if __name__=="__main__":
    test_completed()
    test_failed_receipt_cleanup_and_rollback()
    test_interrupted_recovery_exact_manifest_only()
    print("DISPOSABLE_FIXTURE_ALL_PASS")
