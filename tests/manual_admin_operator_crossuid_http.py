"""Disposable real root/nonroot/HTTP/Unix/Worker E2E with FAKE systemd.

This is an intentionally manual, test-only source acceptance. A root-owned
private /run socket and journal are created; the peer WebUI stays non-root.
All UnitAdapter effects are FAKE. No real systemd, Vault or network provider
mutation is permitted. The helper root process is stopped and cleaned up.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import threading
import time

NAME="oa300-crossuid-" + secrets.token_hex(6)
RID="aaaaaaaa-1234-4234-8234-123456789abc"
RID2="bbbbbbbb-1234-4234-8234-123456789abc"
RID3="cccccccc-1234-4234-8234-123456789abc"
JOB="/org/freedesktop/systemd1/job/825"


def now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def worker(root_path: Path, uid: int, gid: int):
    # Root test-only process: FAKE adapter/method, no production code/unit.
    from obsidian_automation.admin_operation_queue import DurableOperationQueue
    from obsidian_automation.admin_operation_broker import OperationBroker
    from obsidian_automation.admin_operator_runtime import OperatorRuntime
    from obsidian_automation.admin_systemd_admission import RunOnceJobDispatcher
    from test_admin_operation_broker import FakeUnits, snapshot
    from test_admin_workflow_quiescence import fixture_for

    if (
        os.geteuid()!=0 or root_path.parent!=Path("/run")
        or not root_path.name.startswith("oa300-crossuid-")
        or root_path.exists()
        or uid<=0 or gid<=0
    ):
        raise RuntimeError("unsafe_crossuid_test_setup")
    root_path.mkdir(mode=0o700)
    os.chown(root_path,0,gid)
    root_path.chmod(0o710)
    for sub in ("queue","audit"):
        path=root_path/sub
        path.mkdir(mode=0o700)
    sock=root_path/"operator.sock"
    adapter=FakeUnits()
    class FakeManager:
        def __init__(self): self.calls=[]
        def start_unit(self,spec):
            self.calls.append(spec)
            return JOB
    manager=FakeManager()
    @contextmanager
    def fake_gate():
        yield
    queue=DurableOperationQueue(
        queue_root=root_path/"queue", enabled=True, allow_run_once=True,
        require_root=True, clock=now,
    )
    broker=OperationBroker(
        audit_root=root_path/"audit",adapter=adapter,
        maintenance_gate=fake_gate,clock=now,
        require_root=True,effects_enabled=True,
    )
    dispatcher=RunOnceJobDispatcher(
        adapter=adapter,manager=manager,
        maintenance_gate=fake_gate,clock=now,enabled=True,
        require_root=True,workflow_observer=fixture_for,
    )
    def obs():
        return snapshot(adapter,now())
    stop=threading.Event()
    runtime=OperatorRuntime(
        queue=queue,broker=broker,run_once_dispatcher=dispatcher,
        observation_provider=obs,
        selected_observation=lambda sid:obs(),
        socket_path=sock,allowed_peer_uid=uid,allowed_peer_gid=gid,
        require_root=True,ready=True,stop=stop,
    )
    def halt(*_):
        stop.set()
    signal.signal(signal.SIGTERM,halt)
    signal.signal(signal.SIGINT,halt)
    guardian=threading.Timer(15.0,stop.set)
    guardian.daemon=True
    guardian.start()
    print("ROOT_TEST_READY_PID="+str(os.getpid()),flush=True)
    try:
        runtime.serve()
        print("ROOT_FAKE_EFFECTS="+str(len(adapter.calls)),flush=True)
        print("ROOT_FAKE_JOBS="+str(len(manager.calls)),flush=True)
    finally:
        stop.set()
        guardian.cancel()
        if runtime.socket_path.exists():
            raise RuntimeError("root_socket_not_closed")
        # Test-only fixture requires exactly this root-owned newly created dir.
        import shutil
        if (root_path.parent!=Path("/run")
            or not root_path.name.startswith("oa300-crossuid-")
            or root_path.is_symlink()):
            raise RuntimeError("unsafe_crossuid_test_cleanup")
        shutil.rmtree(root_path)


def source_test(root_path: Path, proc):
    from obsidian_automation.admin_dashboard import make_server
    from obsidian_automation.admin_operation_preview import preview_from_socket
    from obsidian_automation.admin_operator_http import OperatorHTTPClient
    from test_admin_operator_http import (
        call, issue_token, take_preview, take_confirm,
    )
    import threading
    web=OperatorHTTPClient(root_path/"operator.sock")
    server=make_server(
        port=0,observer=lambda: [],ai_observer=lambda:{},
        operator=web,operator_ui_enabled=True,
        preview_observer=lambda req:preview_from_socket(
            req,socket_path=root_path/"operator.sock",
        ),
    )
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    def wait_ready():
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            code,_,raw=call(server,"GET","/api/v1/operator-ready")
            if code==200 and json.loads(raw)["operations"]["pause"] is True:
                return
            time.sleep(.05)
        raise RuntimeError("root_socket_did_not_advertise_ready")
    def submit(rid,action,terminal):
        preview=take_preview(server,request_id=rid,action=action)
        assert preview["status"]=="compatible"
        assert "confirmation_ticket" in preview
        code,response=take_confirm(
            server,preview["confirmation_ticket"],
            request_id=rid,action=action,
        )
        if code!=200 or response.get("request_id")!=rid:
            raise RuntimeError("crossuid_submit_failed")
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            code,_,raw=call(server,"GET","/api/v1/operations/"+rid)
            result=json.loads(raw)
            if code==200 and result.get("state")==terminal:
                return result
            time.sleep(.05)
        raise RuntimeError("crossuid_receipt_timeout")
    try:
        wait_ready()
        pause=submit(RID,"pause","completed")
        job=submit(RID2,"run_once","systemd_job_accepted")
        resume=submit(RID3,"resume","completed")
        assert "systemd_job_path" not in pause
        assert job["systemd_job_path"]==JOB
        assert job["authority"]=="systemd_admission_only"
        assert resume["state"]=="completed"
        # No re-execution for same request ID even if lost HTTP reply.
        code,_,raw=call(server,"GET","/api/v1/operations/"+RID2)
        assert json.loads(raw)["state"]=="systemd_job_accepted"
        print("REAL_ROOT_NONROOT_HTTP_UNIX_FAKE_WORKER_PASS")
        print("3_ACTIONS=PAUSE_RUN_ONCE_RESUME")
        print("JOB_ADMISSION_NOT_BUSINESS_SUCCESS=true")
        print("PRODUCTION_SYSTEMD_EFFECTS=0")
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def main():
    if os.geteuid()==0:
        raise RuntimeError("parent_must_be_nonroot")
    user_uid,user_gid=os.geteuid(),os.getegid()
    path=Path("/run")/NAME
    if path.exists():
        raise RuntimeError("random_fixture_already_exists")
    src=Path(__file__).resolve().parents[1]/"src"
    py=sys.executable
    env={
        "PATH":"/run/wrappers/bin:/run/current-system/sw/bin:/usr/bin:/bin",
        "PYTHONPATH":str(src)+":"+str(Path(__file__).resolve().parent),
        "LANG":"C",
    }
    child=subprocess.Popen(
        ["sudo","-n","env",*(f"{k}={v}" for k,v in env.items()),
         py,"-u",str(Path(__file__).resolve()),
         "--root-fixture",str(path),str(user_uid),str(user_gid)],
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,
        text=True,env=env,
    )
    pid=None
    try:
        first=child.stdout.readline().strip()
        if not first.startswith("ROOT_TEST_READY_PID="):
            raise RuntimeError("root_fixture_did_not_start")
        pid=int(first.split("=",1)[1])
        source_test(path,child)
    finally:
        if pid is not None:
            subprocess.run(
                ["sudo","-n","kill","-TERM",str(pid)],
                check=False,timeout=5,env=env,
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            )
        try:
            out,err=child.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            child.kill()
            out,err=child.communicate(timeout=5)
            raise RuntimeError("root_fixture_failed_to_stop")
        if child.returncode!=0:
            print("ROOT_TEST_ERROR",err[-500:],file=sys.stderr)
            raise RuntimeError("root_fixture_exit_failure")
        if path.exists():
            raise RuntimeError("root_fixture_left_behind")
        print("ROOT_TEST_TERMINATED_WITH_NO_SOCKET_OR_QUEUE_LEAK")


if __name__=="__main__":
    if len(sys.argv)==5 and sys.argv[1]=="--root-fixture":
        worker(Path(sys.argv[2]),int(sys.argv[3]),int(sys.argv[4]))
    else:
        main()
