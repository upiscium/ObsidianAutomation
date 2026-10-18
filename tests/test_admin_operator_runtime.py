"""#300 actual HTTP+Unix+worker+durable queue; FAKE effectful UnitAdapter."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import threading
import time

import pytest

from obsidian_automation.admin_dashboard import make_server
from obsidian_automation.admin_operation_broker import BrokerRejected, OperationBroker
from obsidian_automation.admin_operation_preview import preview_from_socket
from obsidian_automation.admin_operator_http import OperatorHTTPClient
from obsidian_automation.admin_operator_runtime import (
    OperatorRuntime, OperatorAuthority,
)
from obsidian_automation.admin_operation_queue import DurableOperationQueue
from obsidian_automation.admin_systemd_admission import RunOnceJobDispatcher
from test_admin_operation_broker import FakeUnits, SPEC, snapshot
from test_admin_operator_http import (
    call, issue_token, take_confirm, take_preview, TEST_INGRESS_KEY,
)

RID = "aaaaaaaa-1234-4234-8234-123456789abc"
RID2 = "bbbbbbbb-1234-4234-8234-123456789abc"
RID3 = "cccccccc-1234-4234-8234-123456789abc"
JOB = "/org/freedesktop/systemd1/job/174"
NOW = lambda: datetime.now(timezone.utc).replace(microsecond=0)


@pytest.fixture
def roots():
    with tempfile.TemporaryDirectory(prefix="oa300-live-worker-",dir="/tmp") as directory:
        base=Path(directory)
        base.chmod(0o700)
        for sub in ("queue","audit"):
            (base/sub).mkdir(mode=0o700)
        yield base


class FakeManager:
    def __init__(self):
        self.calls=[]

    def start_unit(self,spec):
        self.calls.append(spec)
        return JOB


@contextmanager
def fake_gate():
    # Explicit test double; not a verified production updater lock.
    yield


def make_runtime(base: Path, *, adapter=None, manager=None, enabled=True):
    if adapter is None:
        adapter=FakeUnits()
    if manager is None:
        manager=FakeManager()
    queue=DurableOperationQueue(
        queue_root=base/"queue",require_root=False,
        enabled=enabled,allow_run_once=enabled,clock=NOW,
    )
    broker=OperationBroker(
        audit_root=base/"audit",adapter=adapter,
        maintenance_gate=fake_gate,clock=NOW,
        require_root=False,effects_enabled=enabled,
    )
    dispatcher=RunOnceJobDispatcher(
        adapter=adapter,manager=manager,maintenance_gate=fake_gate,
        clock=NOW,require_root=False,enabled=enabled,
    )
    def provider():
        return snapshot(adapter,NOW())
    runtime=OperatorRuntime(
        queue=queue,broker=broker,run_once_dispatcher=dispatcher,
        observation_provider=provider,
        socket_path=base/"operator.sock", allowed_peer_uid=os.geteuid(),
        allowed_peer_gid=os.getegid(),
        ready=enabled,require_root=False,stop=threading.Event(),
    )
    return runtime,queue,adapter,manager


def launch(runtime):
    raised=[]
    def runner():
        try:
            runtime.serve()
        except BaseException as exc:
            raised.append(exc)
    thread=threading.Thread(target=runner,daemon=True)
    thread.start()
    deadline=time.monotonic()+3
    while not runtime.socket_path.exists() and time.monotonic()<deadline:
        if raised: raise raised[0]
        time.sleep(.01)
    assert runtime.socket_path.exists(),raised
    return thread,raised


def stop(runtime, thread):
    runtime.stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert not runtime.socket_path.exists()


def serve_http(runtime):
    path=runtime.socket_path
    web=OperatorHTTPClient(path)
    server=make_server(
        port=0,operator=web,operator_ui_enabled=True,
        observer=lambda: [],ai_observer=lambda: {},
        preview_observer=lambda req:preview_from_socket(req,socket_path=path),
    )
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    return server,thread


def stop_http(server,thread):
    server.shutdown()
    thread.join(timeout=3)
    server.server_close()


def issue(runtime_http, rid, action):
    preview=take_preview(runtime_http,request_id=rid,action=action)
    assert preview["status"]=="compatible",preview
    assert preview["can_execute"] is False
    ticket=preview["confirmation_ticket"]
    code, result=take_confirm(
        runtime_http,ticket,request_id=rid,action=action,
    )
    assert code==200,result
    assert result.get("request_id")==rid,result
    return result,ticket


def poll(server,rid,predicate=lambda state:state=="completed"):
    end=time.monotonic()+6
    while time.monotonic()<end:
        code,_,raw=call(server,"GET","/api/v1/operations/"+rid)
        assert code==200
        result=json.loads(raw)
        if predicate(result.get("state")):
            return result
        time.sleep(.04)
    raise AssertionError("worker_result_not_observed")


def test_real_http_unix_worker_pause_then_runonce_then_resume(roots):
    runtime,q,adapter,manager=make_runtime(roots)
    worker,raised=launch(runtime)
    web,webthread=serve_http(runtime)
    try:
        cap,_,raw=call(web,"GET","/api/v1/operator-ready")
        assert cap==200
        assert json.loads(raw)["operations"]=={
            "pause":True,"resume":True,"run_once":True,
        }
        first,_=issue(web,RID,"pause")
        assert first["state"] in ("accepted","processing","completed")
        done=poll(web,RID)
        assert done["state"]=="completed"
        assert adapter.timer_state=="paused"
        assert adapter.calls==[("systemctl","disable","--now",SPEC.timer)]
        assert q.receipt(RID)["state"]=="completed"

        second,_=issue(web,RID2,"run_once")
        assert second["state"] in ("accepted","processing","systemd_job_accepted")
        accepted=poll(web,RID2,lambda s:s=="systemd_job_accepted")
        assert accepted["systemd_job_path"]==JOB
        assert accepted["authority"]=="systemd_admission_only"
        assert accepted["can_execute"] is False
        assert len(manager.calls)==1
        assert adapter.started==100  # no fake business result/second effect

        third,_=issue(web,RID3,"resume")
        assert third["state"] in ("accepted","processing","completed")
        assert poll(web,RID3)["state"]=="completed"
        assert adapter.timer_state=="scheduled"
        assert len(adapter.calls)==2

        # Repeated status lookup or returned UUID never causes another OS
        # effect. Every earlier receipt is immutable and separately readable.
        for rid in (RID,RID2,RID3):
            code,_,raw=call(web,"GET","/api/v1/operations/"+rid)
            assert code==200 and json.loads(raw)["request_id"]==rid
        assert len(adapter.calls)==2 and len(manager.calls)==1
        assert not raised
    finally:
        stop_http(web,webthread)
        stop(runtime,worker)
    assert not raised


def test_duplicate_same_uuid_after_lost_http_reply_never_redispatches(roots):
    runtime,q,adapter,manager=make_runtime(roots)
    worker,raised=launch(runtime)
    web,webthread=serve_http(runtime)
    try:
        preview=take_preview(web,request_id=RID)
        ticket=preview["confirmation_ticket"]
        result,_=take_confirm(web,ticket,request_id=RID)
        assert result==200
        assert poll(web,RID)["state"]=="completed"
        again,replayed=take_confirm(web,ticket,request_id=RID)
        assert again==200
        assert replayed["request_id"]==RID
        assert replayed["state"]=="completed"
        assert len(adapter.calls)==1
    finally:
        stop_http(web,webthread)
        stop(runtime,worker)
    assert not raised


def test_worker_crash_conservative_claim_survives_restart_without_replay(roots):
    runtime,q,adapter,manager=make_runtime(roots)
    q.submit({"version":1,"request_id":RID,
              "service_id":SPEC.identifier,"action":"pause"})
    q.claim_for_worker()  # crashed after durable claim, before OS effect
    assert q.receipt(RID)["state"]=="processing"
    worker,raised=launch(runtime)
    try:
        time.sleep(.6)
        assert q.receipt(RID)["state"]=="processing"
        assert adapter.calls==[]
        assert runtime._worker_failure=="queue_recovery_required"
    finally:
        stop(runtime,worker)
    assert not raised


def test_ready_runtime_rejects_unqualified_defaults_without_opening_socket(roots):
    runtime,q,a,m=make_runtime(roots,enabled=False)
    with pytest.raises(BrokerRejected,match="operator_runtime_not_qualified"):
        runtime.serve()
    assert not runtime.socket_path.exists()


def test_capabilities_from_disabled_socket_are_strictly_no_authority(roots):
    from obsidian_automation.admin_control_queue_socket import (
        OperationQueueClient,OperationQueueSocket,
    )
    runtime,q,a,m=make_runtime(roots,enabled=False)
    # A caller may configure ready=True incorrectly; the server must
    # still emit false because durable queue intake was not enabled.
    with OperationQueueSocket(
        roots/"operator.sock",queue=q,
        allowed_peer_uid=os.geteuid(),allowed_peer_gid=os.getegid(),
        observation_provider=runtime.observation_provider,
        ready=True,require_root=False,
    ) as server:
        result=[]
        t=threading.Thread(target=lambda:result.append(
            OperationQueueClient(roots/"operator.sock").capabilities()
        ))
        t.start()
        assert server.serve_once()
        t.join(timeout=3)
        assert not t.is_alive()
    assert not any(result[0]["operations"].values())


def test_browser_backend_unavailable_is_not_executable(roots):
    runtime,q,a,m=make_runtime(roots)
    web,thread=serve_http(runtime)  # no root listener
    try:
        cap,_,raw=call(web,"GET","/api/v1/operator-ready")
        assert cap==200
        assert not any(json.loads(raw)["operations"].values())
        preview=take_preview(web,request_id=RID)
        assert preview["status"]=="rejected"
        assert "confirmation_ticket" not in preview
    finally:
        stop_http(web,thread)
