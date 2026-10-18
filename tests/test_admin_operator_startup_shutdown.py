"""Exact pre-import launcher closure and runtime health/shutdown behavior."""
from __future__ import annotations

import ast
import importlib.util
import os
from pathlib import Path
import sys
import threading
import time

import pytest

from obsidian_automation import admin_operator_runtime as runtime_module
from obsidian_automation.admin_control_queue_socket import OperationQueueClient
from obsidian_automation.admin_operation_broker import BrokerRejected
from test_admin_operator_runtime import (
    RID, make_runtime, roots, launch, stop, serve_http, stop_http,
)
from test_admin_operator_http import call

ROOT=Path(__file__).resolve().parents[1]


def _static_privileged_closure():
    packages=ROOT/"src"/"obsidian_automation"
    registry={f.stem:f for f in packages.glob("*.py")}
    stack=["admin_operator_runtime"]
    result=set()
    while stack:
        name=stack.pop()
        if name not in registry or name in result: continue
        result.add(name)
        module=ast.parse(registry[name].read_text(encoding="utf-8"))
        for node in ast.walk(module):
            if isinstance(node,ast.ImportFrom):
                if node.level and node.module:
                    part=node.module.split(".")[0]
                    if part in registry: stack.append(part)
                elif node.level and not node.module:
                    for a in node.names:
                        if a.name in registry: stack.append(a.name)
            elif isinstance(node,ast.Import):
                for a in node.names:
                    if a.name.startswith("obsidian_automation."):
                        part=a.name.split(".")[1]
                        if part in registry: stack.append(part)
    return {name+".py" for name in result}|{"__init__.py"}


def test_all_transitive_root_imports_are_hashed_before_exec():
    source=ROOT/"examples/admin/obsidian-admin-root-launch.py"
    namespace={}
    spec=importlib.util.spec_from_file_location("_isolated_operator_bootstrap",source)
    assert spec is not None and spec.loader is not None
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    closure=_static_privileged_closure()
    assert closure == set(runtime_module._AUTHORITY_MODULES)
    assert closure == set(module.MODULES)
    assert len(closure) >= 14
    source_code=source.read_text()
    assert "os.execve" in source_code
    assert '"-I", "-S", "-B", "-c", _RUNTIME_CODE' in source_code
    assert "sys.path.insert(0,sys.argv[1])" in source_code
    assert "sys.flags.no_site" in source_code
    assert 'PROVENANCE_ENV: str(fd)' in source_code
    assert 'os.memfd_create(' in source_code
    assert 'F_ADD_SEALS' in source_code
    # The bootstrap's frozen second-stage code intentionally imports
    # the VERIFIED root package after disabling site; the bootstrap's own
    # module import tree must be stdlib only.
    parsed=ast.parse(source_code)
    assert not any(
        isinstance(node,(ast.Import,ast.ImportFrom))
        and (
            (isinstance(node,ast.ImportFrom)
             and (node.module or "").startswith("obsidian_automation"))
            or (isinstance(node,ast.Import)
                and any(n.name.startswith("obsidian_automation")
                        for n in node.names))
        )
        for node in ast.walk(parsed)
    )


def test_root_console_entrypoint_cannot_bypass_preflight():
    project=(ROOT/"pyproject.toml").read_text()
    assert 'obsidian-admin-operator = ' not in project
    assert "admin_operator_runtime:main" not in project
    with pytest.raises(SystemExit,match="operator_trusted_bootstrap_required"):
        runtime_module.main([])


def test_root_launcher_rejects_untrusted_source_and_symlinks(tmp_path):
    source=ROOT/"examples/admin/obsidian-admin-root-launch.py"
    spec=importlib.util.spec_from_file_location("_operator_launcher_test",source)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fake=tmp_path/"untrusted.py"
    fake.write_text("pass\n")
    with pytest.raises(RuntimeError,match="untrusted_source_parent"):
        module.rooted_file(fake,100)
    indirect=tmp_path/"symlink.py"
    indirect.symlink_to(fake)
    with pytest.raises((OSError,RuntimeError)):
        module.rooted_file(indirect,100)


def test_processing_recovery_removes_worker_ready_capabilities(roots):
    rt,queue,adapter,manager=make_runtime(roots)
    queue.submit({
        "version":1,"request_id":RID,"service_id":"github-sync","action":"pause",
    })
    queue.claim_for_worker()  # persisted intent but effect may be unknown
    root_thread,raised=launch(rt)
    try:
        deadline=time.monotonic()+3
        while rt._worker_failure != "queue_recovery_required" and time.monotonic()<deadline:
            time.sleep(.05)
        assert rt._worker_failure=="queue_recovery_required"
        assert rt._available() is False
        cap=OperationQueueClient(rt.socket_path,timeout=1).capabilities()
        assert cap["authority"]=="observation_only"
        assert not any(cap["operations"].values())
        web,t=serve_http(rt)
        try:
            code,_,body=call(web,"GET","/api/v1/operator-ready")
            assert code==200
            assert b'"worker_ready"' not in body
        finally:
            stop_http(web,t)
        assert queue.receipt(RID)["state"]=="processing"
        assert adapter.calls==[]
    finally:
        stop(rt,root_thread)
    assert not raised


def test_sigterm_gracefully_waits_for_inflight_worker(roots,monkeypatch):
    entered=threading.Event()
    release=threading.Event()
    rt,queue,adapter,manager=make_runtime(roots)
    def blocked(*,queue,broker,trusted_snapshot,run_once_dispatcher):
        entered.set()
        if not release.wait(timeout=5):
            raise RuntimeError("test_barrier_timeout")
        raise BrokerRejected("queue_missing_receipt")
    monkeypatch.setattr(runtime_module,"run_accepted_control_once",blocked)
    holder,raised=launch(rt)
    try:
        assert entered.wait(timeout=3)
        rt.stop.set()
        time.sleep(.3)  # allow listener 0.2-second accept bound to release
        assert holder.is_alive()  # do not abandon accepted effects on stop
        assert rt._worker_thread is not None
        assert rt._worker_thread.is_alive()
        release.set()
        holder.join(timeout=3)
        assert not holder.is_alive()
        assert not rt.socket_path.exists()
        assert not raised
    finally:
        release.set()
        rt.stop.set()
        holder.join(timeout=3)


def test_root_cutover_attestations_required_before_any_effects(monkeypatch):
    """A staged exact-SHA file alone is never permission to start services."""
    import hashlib
    import json
    from obsidian_automation.admin_operator_runtime import (
        OperatorAuthority, _UNITS, _AUTHORITY_MODULES,
    )
    base=Path("/mock-reviewed-root")
    data={}
    units={}
    modules={}
    for name in _UNITS:
        body=("FIXED_UNIT="+name).encode()
        data[base/"units"/name]=body
        units[name]=hashlib.sha256(body).hexdigest()
    for name in _AUTHORITY_MODULES:
        body=("FIXED_MODULE="+name).encode()
        data[base/"modules"/name]=body
        modules[name]=hashlib.sha256(body).hexdigest()
    authority={
        "version":1,"enabled":True,"revision":"a"*40,
        "single_active_lxc":True,"web_user":"obsidian-admin-web",
        "coordinated_updaters_verified":False,
        "uncoordinated_launchers_retired":False,
        "unit_sha256":units,"module_sha256":modules,
    }
    data[base/"authority.json"]=json.dumps(authority).encode()
    monkeypatch.setattr(os,"geteuid",lambda:0)
    monkeypatch.setattr(
        runtime_module,"_pinned_root_regular",
        lambda path,**kw:data[path],
    )
    checker=OperatorAuthority(
        config_path=base/"authority.json",
        systemd_dir=base/"units",module_dir=base/"modules",
    )
    with pytest.raises(BrokerRejected,match="operator_authority_unavailable"):
        checker.verify()
    authority["coordinated_updaters_verified"]=True
    data[base/"authority.json"]=json.dumps(authority).encode()
    with pytest.raises(BrokerRejected,match="operator_authority_unavailable"):
        checker.verify()
    authority["uncoordinated_launchers_retired"]=True
    data[base/"authority.json"]=json.dumps(authority).encode()
    assert checker.verify()["revision"]=="a"*40
    # Changing one pinned dependency or unit causes another fail closed.
    data[base/"modules"/"admin_status.py"]=b"UNVERIFIED_AFTER_STAGING"
    with pytest.raises(BrokerRejected,match="operator_authority_unavailable"):
        checker.verify()


def test_staging_helper_does_not_preenable_root_runtime():
    tool=(ROOT/"tools/prepare_admin_operator_authority.py").read_text()
    assert '"enabled": False' in tool
    assert '"coordinated_updaters_verified": False' in tool
    assert '"uncoordinated_launchers_retired": False' in tool


def test_installed_root_unit_paths_match_socket_and_updater_lock_requirements():
    from obsidian_automation.admin_control_queue_socket import DEFAULT_OPERATION_QUEUE_SOCKET
    from obsidian_automation.admin_local_maintenance_gate import (
        HOST_RECEIPT_DIR, REFRESH_DIR,
    )
    unit=(ROOT/"examples/admin/obsidian-admin-operator.service").read_text()
    assert DEFAULT_OPERATION_QUEUE_SOCKET == Path("/run/obsidian-admin/operation.sock")
    assert "RuntimeDirectory=obsidian-admin" in unit
    assert "RuntimeDirectoryMode=0710" in unit
    assert "StateDirectory=obsidian-admin/queue obsidian-admin/broker" in unit
    # Stable flock inodes are opened O_RDWR. They must not reside under a
    # readonly mount inside the privileged worker's systemd namespace.
    rw=next(line for line in unit.splitlines()
            if line.startswith("ReadWritePaths=")).partition("=")[2].split()
    ro=next(line for line in unit.splitlines()
            if line.startswith("ReadOnlyPaths=")).partition("=")[2].split()
    assert str(DEFAULT_OPERATION_QUEUE_SOCKET.parent) in rw
    assert str(HOST_RECEIPT_DIR) in rw
    assert str(REFRESH_DIR) in rw
    assert str(HOST_RECEIPT_DIR) not in ro
    assert str(REFRESH_DIR) not in ro
    assert "ExecStart=/usr/bin/python3 -I -S -B /usr/local/libexec/obsidian-admin-root-launch.py" in unit


def test_root_module_cannot_start_with_plain_forged_marker(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(os,"geteuid",lambda:0)
    monkeypatch.setattr(
        runtime_module,"sys",SimpleNamespace(
            flags=SimpleNamespace(isolated=1,no_site=1,dont_write_bytecode=1),
        ),
    )
    monkeypatch.setenv("OBSIDIAN_ADMIN_VERIFIED_BOOTSTRAP","1")
    monkeypatch.delenv("OBSIDIAN_ADMIN_BOOTSTRAP_FD",raising=False)
    with pytest.raises(BrokerRejected,match="operator_trusted_bootstrap_required"):
        runtime_module._require_verified_bootstrap()


def test_sealed_bootstrap_descriptor_is_bound_to_actual_authority(monkeypatch):
    import fcntl
    import hashlib
    from types import SimpleNamespace
    frozen_manifest=b"exact_root_authority_bytes_for_test"
    descriptor=os.memfd_create("oa300-bootstrap-test",os.MFD_ALLOW_SEALING)
    try:
        token=(runtime_module._BOOTSTRAP_MAGIC
               +hashlib.sha256(frozen_manifest).digest())
        assert os.write(descriptor,token)==len(token)
        fcntl.fcntl(
            descriptor,fcntl.F_ADD_SEALS,runtime_module._BOOTSTRAP_SEALS,
        )
        info=os.fstat(descriptor)
        actual_fstat=os.fstat
        def stat_root(fd):
            value=actual_fstat(fd)
            if fd!=descriptor: return value
            return SimpleNamespace(
                st_uid=0,st_mode=value.st_mode,st_size=value.st_size,
            )
        monkeypatch.setattr(os,"geteuid",lambda:0)
        monkeypatch.setattr(
            runtime_module,"sys",SimpleNamespace(
                flags=SimpleNamespace(isolated=1,no_site=1,dont_write_bytecode=1),
            ),
        )
        monkeypatch.setattr(os,"fstat",stat_root)
        monkeypatch.setattr(
            runtime_module,"_pinned_root_regular",
            lambda *_args,**_kwargs:frozen_manifest,
        )
        monkeypatch.setenv(runtime_module._BOOTSTRAP_FD_ENV,str(descriptor))
        runtime_module._require_verified_bootstrap()
        assert runtime_module._BOOTSTRAP_FD_ENV not in os.environ
        with pytest.raises(BrokerRejected,match="operator_trusted_bootstrap_required"):
            runtime_module._require_verified_bootstrap()
    finally:
        try: os.close(descriptor)
        except OSError: pass


def test_unsealed_or_mismatched_fd_is_rejected(monkeypatch):
    import hashlib
    from types import SimpleNamespace
    descriptor=os.memfd_create("oa300-unsealed",os.MFD_ALLOW_SEALING)
    try:
        value=runtime_module._BOOTSTRAP_MAGIC+hashlib.sha256(b"wrong").digest()
        os.write(descriptor,value)
        original=os.fstat
        def root_owner(fd):
            info=original(fd)
            if fd!=descriptor:return info
            return SimpleNamespace(
                st_uid=0,st_mode=info.st_mode,st_size=info.st_size,
            )
        monkeypatch.setattr(os,"geteuid",lambda:0)
        monkeypatch.setattr(os,"fstat",root_owner)
        monkeypatch.setattr(
            runtime_module,"sys",SimpleNamespace(
                flags=SimpleNamespace(isolated=1,no_site=1,dont_write_bytecode=1),
            ),
        )
        monkeypatch.setattr(runtime_module,"_pinned_root_regular",
                            lambda *a,**kw:b"correct")
        monkeypatch.setenv(runtime_module._BOOTSTRAP_FD_ENV,str(descriptor))
        with pytest.raises(BrokerRejected,match="operator_trusted_bootstrap_required"):
            runtime_module._require_verified_bootstrap()
    finally:
        try: os.close(descriptor)
        except OSError: pass


def test_staging_helper_privileged_import_closure_equals_bootstrap():
    import importlib.util
    path=ROOT/"tools/prepare_admin_operator_authority.py"
    spec=importlib.util.spec_from_file_location("_disabled_operator_stage",path)
    assert spec is not None
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert set(module._AUTHORITY_MODULES)==_static_privileged_closure()
    assert set(module._UNITS)==set(runtime_module._UNITS)
    assert "from obsidian_automation" not in path.read_text()


def test_real_isolated_no_site_second_stage_ignores_executable_pth(tmp_path):
    """-I -S never executes .pth startup code even in an injected import root."""
    import subprocess
    site_root=tmp_path/"test-site-packages"
    site_root.mkdir(mode=0o700)
    hook=site_root/"unsafe.pth"
    marker=tmp_path/"UNTRUSTED_BOOTSTRAP_EXECUTED"
    hook.write_text(
        "import pathlib; pathlib.Path("+repr(str(marker))+").write_text('bad')\n",
        encoding="utf-8",
    )
    # Import our exact 14-file source closure from a separate explicitly
    # supplied root. Plain site-packages hooks from an extra sys.path path
    # must still NEVER be interpreted by Python.
    path=ROOT/"src"
    code=(
        "import sys;"
        "assert sys.flags.isolated==1 and sys.flags.no_site==1;"
        "assert 'site' not in sys.modules;"
        "sys.path.insert(0,sys.argv[1]);"
        "sys.path.insert(0,sys.argv[2]);"
        "from obsidian_automation import admin_operator_runtime;"
        "print('NO_SITE_HOOK_EXECUTED')"
    )
    done=subprocess.run(
        [sys.executable,"-I","-S","-c",code,str(path),str(site_root)],
        check=False,capture_output=True,text=True,timeout=5,
    )
    assert done.returncode==0,done.stderr[:400]
    assert done.stdout.strip()=="NO_SITE_HOOK_EXECUTED"
    assert not marker.exists()


def test_bootstrap_source_and_runtime_deny_site_initialization():
    launcher=(ROOT/"examples/admin/obsidian-admin-root-launch.py").read_text()
    runner=(ROOT/"src/obsidian_automation/admin_operator_runtime.py").read_text()
    unit=(ROOT/"examples/admin/obsidian-admin-operator.service").read_text()
    assert 'sys.flags.no_site != 1' in launcher
    assert 'sys.flags.no_site!=1' in runner
    assert '"-I", "-S", "-B", "-c"' in launcher
    assert "/usr/bin/python3 -I -S -B /usr/local/libexec/" in unit
    # No fallback to an unverified module entrypoint.
    assert ' "-m", "obsidian_automation.admin_operator_runtime"' not in launcher


@pytest.mark.parametrize("name", [
    "__pycache__", "admin_operator_runtime.pyc",
    "admin_operator_runtime.cpython-311-x86_64-linux-gnu.so",
    "admin_operator_runtime.pyd", "admin_operator_runtime.pyo",
])
def test_root_preflight_refuses_unverified_package_import_artifacts(tmp_path,name):
    import importlib.util
    source=ROOT/"examples/admin/obsidian-admin-root-launch.py"
    spec=importlib.util.spec_from_file_location("_verified_import_inventory",source)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    directory=tmp_path/"obsidian_automation"
    directory.mkdir(mode=0o700)
    (directory/"admin_operator_runtime.py").write_text("pass\n")
    if name=="__pycache__":
        (directory/name).mkdir(mode=0o700)
    else:
        (directory/name).write_bytes(b"SHADOWED-CODE")
    with pytest.raises(RuntimeError,match="unreviewed_executable_import_artifact"):
        module.reject_unverified_import_artifacts(directory)


def test_root_preflight_accepts_only_source_importable_files(tmp_path):
    import importlib.util
    source=ROOT/"examples/admin/obsidian-admin-root-launch.py"
    spec=importlib.util.spec_from_file_location("_approved_import_inventory",source)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    directory=tmp_path/"obsidian_automation"
    directory.mkdir(mode=0o700)
    (directory/"admin_operator_runtime.py").write_text("pass\n")
    (directory/"__init__.py").write_text("")
    assert module.reject_unverified_import_artifacts(directory) is None


def test_no_site_no_bytecode_python_imports_verified_package(tmp_path):
    import subprocess
    marker=tmp_path/"UNTRUSTED_SITE_HOOK_EXECUTED"
    fake_site=tmp_path/"site-packages"
    fake_site.mkdir(mode=0o700)
    (fake_site/"bad.pth").write_text(
        "import pathlib; pathlib.Path("+repr(str(marker))+").write_text('BAD')\n"
    )
    snippet=(
        "import sys;"
        "assert sys.flags.isolated==1;"
        "assert sys.flags.no_site==1;"
        "assert sys.flags.dont_write_bytecode==1;"
        "assert 'site' not in sys.modules;"
        "sys.path.insert(0,sys.argv[1]);"
        "sys.path.insert(0,sys.argv[2]);"
        "from obsidian_automation import admin_operator_runtime as runtime;"
        "assert runtime._BOOTSTRAP_MAGIC.endswith(b'\\x00');"
        "print('NO_UNVERIFIED_STARTUP_EXECUTED')"
    )
    completed=subprocess.run(
        [sys.executable,"-I","-S","-B","-c",
         snippet,str(ROOT/"src"),str(fake_site)],
        capture_output=True,text=True,timeout=5,check=False,
    )
    assert completed.returncode==0,completed.stderr
    assert completed.stdout.strip()=="NO_UNVERIFIED_STARTUP_EXECUTED"
    assert not marker.exists()
