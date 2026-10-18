"""Real disposable AF_UNIX peer-checked preview; never executes systemd."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
import os
from pathlib import Path
import socket
import struct
import tempfile
import threading

import pytest

from obsidian_automation.admin_broker_socket import BrokerSocketClient
from obsidian_automation.admin_operation_preview import (
    OperationPreviewSocket, preview_from_socket, _response_for_request,
)
from obsidian_automation.admin_status import SERVICES

NOW = datetime(2026, 10, 16, 8, 25, 31, tzinfo=timezone.utc)
RID = "12345678-1234-4234-8234-123456789abc"
CANDIDATE = {"version": 1, "op": "preview", "request_id": RID,
             "service_id": "github-sync", "action": "pause"}


def snapshot(*, schedule="scheduled", execution="succeeded", health="observed_success"):
    return {
        "generated_at": NOW.isoformat(timespec="seconds"),
        "services": [{
            "id": "github-sync", "schedule": schedule,
            "latest_execution": execution, "health": health,
        }],
    }


@pytest.fixture
def socket_path():
    with tempfile.TemporaryDirectory(prefix="oa-prev-", dir="/tmp") as folder:
        os.chmod(folder, 0o700)
        yield Path(folder) / "preview.sock"


def listener(path, provider=snapshot, *, uid=None):
    return OperationPreviewSocket(
        path, allowed_peer_uid=os.geteuid() if uid is None else uid,
        allowed_peer_gid=os.getegid(),
        observation_provider=provider,
        clock=lambda: NOW, require_root=False,
    )


def call_once(srv, request):
    output = []
    def task():
        output.append(BrokerSocketClient(srv.path).query(request))
    with srv:
        t = threading.Thread(target=task)
        t.start()
        assert srv.serve_once()
        t.join(timeout=3)
        assert not t.is_alive()
    return output[0]


def test_preview_reports_compatibility_but_no_execution_authority(socket_path):
    response = call_once(listener(socket_path), CANDIDATE)
    assert response == {
        "version": 1, "authority": "preview_only", "can_execute": False,
        "status": "compatible", "reason": "not_operational",
        "candidate": {
            "request_id": RID, "service_id": "github-sync", "action": "pause",
            "observed_at": NOW.isoformat(timespec="seconds"),
            "authority": "requires_broker_live_recheck",
        },
    }


def test_paused_role_accepts_resume_preview_without_effect(socket_path):
    req = {**CANDIDATE, "action": "resume"}
    response = call_once(listener(socket_path, lambda: snapshot(
        schedule="paused", execution="succeeded", health="paused",
    )), req)
    assert response["status"] == "compatible"
    assert response["can_execute"] is False


@pytest.mark.parametrize("edit", [
    {"action": "run_once"}, {"action": "resume"}, {"service_id": "sshd"},
    {"service_id": "github-sync; rm /"}, {"action": "stop"},
    {"version": True}, {"version": 2}, {"request_id": "../../etc"},
    {"secret": "should not be accepted"}, {"op": "execute"},
])
def test_malformed_or_incompatible_preview_never_calls_an_effect(socket_path, edit):
    candidate = {**CANDIDATE, **edit}
    result = call_once(listener(socket_path), candidate)
    if result.get("error"):
        assert result == {"version": 1, "error": "read_only_request_rejected"}
    else:
        assert result["can_execute"] is False
        assert result["status"] == "rejected" or result["status"] == "compatible"
        # A compatible status could only be an ordinary no-effect preview.
        assert result["authority"] == "preview_only"


def test_reported_observation_is_locally_owned_not_browser_timestamp(socket_path):
    request = {**CANDIDATE}
    assert "observed_at" not in request
    assert call_once(listener(socket_path), request)["candidate"]["observed_at"] == (
        NOW.isoformat(timespec="seconds")
    )


def test_unavailable_or_throwing_status_is_redacted(socket_path):
    def broken():
        raise RuntimeError("CREDENTIAL=PRIVATE_DATA_TO_HIDE")
    result = call_once(listener(socket_path, broken), CANDIDATE)
    assert result == {
        "version": 1, "authority": "preview_only", "can_execute": False,
        "status": "rejected", "reason": "preview_unavailable",
    }
    assert "PRIVATE" not in str(result)


@pytest.mark.parametrize("bad", [
    lambda: snapshot(schedule="triggering"),
    lambda: snapshot(schedule="scheduled", execution="running"),
    lambda: snapshot(schedule="scheduled", execution="succeeded", health="paused"),
    lambda: {"generated_at": NOW.isoformat(timespec="seconds"), "services": []},
    lambda: {"generated_at": "not-a-date", "services": []},
])
def test_invalid_live_snapshot_never_claims_operational_authority(socket_path, bad):
    result = call_once(listener(socket_path, bad), CANDIDATE)
    assert result["can_execute"] is False
    assert result["status"] == "rejected"


def test_old_or_future_snapshot_rejected_with_expiry(socket_path):
    old = snapshot()
    old["generated_at"] = (NOW-timedelta(minutes=2)).isoformat(timespec="seconds")
    assert call_once(listener(socket_path, lambda: old), CANDIDATE)["reason"] == "stale_observation"
    future = snapshot()
    future["generated_at"] = (NOW+timedelta(minutes=1)).isoformat(timespec="seconds")
    assert call_once(listener(socket_path, lambda: future), CANDIDATE)["reason"] == "stale_observation"


def test_default_root_required(socket_path, monkeypatch):
    inst = OperationPreviewSocket(
        socket_path, allowed_peer_uid=1000, observation_provider=snapshot,
    )
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    with pytest.raises(Exception, match="privileged_socket_required"):
        inst.open()
    assert not socket_path.exists()


def test_unauthorized_peer_gets_no_observation_bytes(socket_path):
    with listener(socket_path, uid=os.geteuid()+1) as instance:
        raw = []
        def client():
            try:
                raw.append(BrokerSocketClient(socket_path).query(CANDIDATE))
            except Exception:
                raw.append(None)
        thread = threading.Thread(target=client)
        thread.start()
        assert instance.serve_once()
        thread.join(timeout=3)
    assert raw == [None]


def test_client_rejects_unavailable_socket_and_forged_capabilities(socket_path):
    assert preview_from_socket(CANDIDATE, socket_path=socket_path) == {
        "version": 1, "authority": "preview_only", "can_execute": False,
        "status": "rejected", "reason": "preview_unavailable",
    }


def test_client_roundtrip_accepts_only_bounded_preview_fields(socket_path):
    with listener(socket_path) as instance:
        received = []
        def web_peer():
            received.append(preview_from_socket(CANDIDATE, socket_path=socket_path))
        t = threading.Thread(target=web_peer)
        t.start()
        assert instance.serve_once()
        t.join(timeout=3)
    assert received[0]["status"] == "compatible"
    assert received[0]["can_execute"] is False


@pytest.mark.parametrize("spoofed", [
    {"version": 1, "authority": "local_operator", "can_execute": True,
     "status": "compatible", "reason": "not_operational"},
    {"version": 1, "authority": "preview_only", "can_execute": True,
     "status": "compatible", "reason": "not_operational"},
    {"version": True, "authority": "preview_only", "can_execute": False,
     "status": "rejected", "reason": "preview_unavailable"},
])
def test_client_fails_closed_on_spoofed_positive_projection(monkeypatch, socket_path, spoofed):
    monkeypatch.setattr(BrokerSocketClient, "query", lambda *a, **kw: spoofed)
    assert preview_from_socket(CANDIDATE, socket_path=socket_path)["status"] == "rejected"
