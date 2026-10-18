"""#300 browser-side explicit operator approval, no systemd access."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from obsidian_automation.admin_dashboard import HTML, JS, make_server
from obsidian_automation.admin_operator_http import (
    ConfirmationTickets, OperatorHTTPClient, parse_confirm_request,
    safe_capabilities, safe_operator_receipt,
)
from test_admin_operation_web import call as raw_call, issue_token, live_server, RID, SAMPLE

# LAN/VPN restriction is an external reverse-proxy/network requirement.
TEST_INGRESS_KEY = bytes(range(32))  # forged legacy-header canary


def call(server, method, path, *, ingress=True, more_headers=None, **kwargs):
    headers = dict(more_headers or {})
    return raw_call(
        server, method, path, more_headers=headers, **kwargs,
    )


def request(action="pause", rid=RID):
    return {
        "version": 1, "request_id": rid,
        "service_id": "github-sync", "action": action,
    }


def enabled():
    return {
        "version": 1, "authority": "operator_ready", "reason": "worker_ready",
        "operations": {"pause": True, "resume": True, "run_once": True},
    }


class FakeOperator:
    def __init__(self):
        self.calls = []
        self.receipts = {}

    def capabilities(self):
        return enabled()

    def submit(self, payload):
        self.calls.append(dict(payload))
        rid = payload["request_id"]
        result = {
            "version": 1, "request_id": rid,
            "service_id": payload["service_id"],
            "action": payload["action"], "state": "accepted",
            "authority": "durable_queue_only", "can_execute": False,
        }
        self.receipts[rid] = result
        return result

    def receipt(self, rid):
        return self.receipts.get(rid, {
            "version": 1, "request_id": rid,
            "status": "unavailable", "authority": "observation_only",
        })


def compatible(preview):
    return {
        "version": 1, "authority": "preview_only", "can_execute": False,
        "status": "compatible", "reason": "not_operational",
        "candidate": {
            "request_id": preview["request_id"],
            "service_id": preview["service_id"],
            "action": preview["action"],
            "observed_at": "2026-10-17T08:25:31+00:00",
            "authority": "requires_broker_live_recheck",
        },
    }


def start_server(*, operator=None, ready=False, preview=compatible):
    import threading
    server = make_server(
        port=0, observer=lambda: [], ai_observer=lambda: {},
        operator=operator, operator_ui_enabled=ready,
        preview_observer=preview,
    )
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, t


def stop_server(server, thread):
    server.shutdown()
    thread.join(timeout=3)
    server.server_close()


def take_preview(server, *, request_id=RID, action="pause"):
    payload = {
        "version": 1, "request_id": request_id,
        "service_id": "github-sync", "action": action,
    }
    status, _, data = call(
        server, "POST", "/api/v1/operation-previews",
        payload=payload, token=issue_token(server),
    )
    assert status == 200
    return json.loads(data)


def take_confirm(server, ticket, *, request_id=RID, action="pause",
                 confirmed=True, origin=True, token=None):
    req = {
        **request(action, request_id),
        "confirmation_ticket": ticket, "confirm": confirmed,
    }
    status, _, data = call(
        server, "POST", "/api/v1/operations",
        payload=req, token=issue_token(server) if token is None else token,
        origin=origin,
    )
    return status, json.loads(data) if data.startswith(b"{") else data


def test_ticket_is_bound_to_exact_request_service_action_and_expiry():
    now = [1234.0]
    signer = ConfirmationTickets(now=lambda: now[0], secret=b"t"*32)
    old = request()
    ticket = signer.issue(old)
    assert len(ticket) == 112
    assert signer.verify(old, ticket)
    assert not signer.verify(request("resume"), ticket)
    assert not signer.verify(request(rid="aaaaaaaa-1234-4234-8234-123456789abc"), ticket)
    assert not signer.verify({**old, "service_id": "core-promotion"}, ticket)
    # Tamper deterministically: replacing with "0" alone is a no-op
    # if the original random HMAC digest already ends with "0".
    flipped = "0" if ticket[-1] != "0" else "1"
    assert not signer.verify(old, ticket[:-1] + flipped)
    assert not ConfirmationTickets(now=lambda: now[0]).verify(old, ticket)
    now[0]+=91
    assert not signer.verify(old,ticket)
    assert signer.issue(old) != ticket


@pytest.mark.parametrize("payload", [
    {**request(), "confirmation_ticket": "x", "confirm": False},
    {**request(), "confirmation_ticket": "x", "confirm": 1},
    {**request(), "confirmation_ticket": "x", "confirm": True, "secret": "SHOULD_NOT_LEAK"},
    {**request(), "confirmation_ticket": 0, "confirm": True},
    {**request(), "confirmation_ticket": "x", "confirm": True, "version": True},
    {**request(action="start"), "confirmation_ticket": "x", "confirm": True},
    {**request(), "confirmation_ticket": "x", "confirm": True, "service_id": "sshd"},
])
def test_confirm_parser_fails_closed(payload):
    with pytest.raises(ValueError, match="invalid_confirm_request"):
        parse_confirm_request(json.dumps(payload).encode())


def test_duplicate_json_and_invalid_encoding_rejected():
    for body in [
        b'{"version":1,"version":1}', b'[]', b'\xff', b'{}',
        b' ' * 513,
    ]:
        with pytest.raises(ValueError, match="invalid_confirm_request"):
            parse_confirm_request(body)


def test_operator_ui_default_disabled_even_with_spoofed_positive_backend():
    f = FakeOperator()
    server,t = start_server(operator=f, ready=False)
    try:
        code,_,raw = call(server, "GET", "/api/v1/operator-ready")
        assert code == 200
        assert json.loads(raw)["operations"]["pause"] is False
        data = take_preview(server)
        assert "confirmation_ticket" not in data
        code, _, raw = call(server, "POST", "/api/v1/operations",
                           payload={**request(), "confirmation_ticket":"x","confirm":True},
                           token=issue_token(server))
        assert code == 405
        assert f.calls == []
    finally:
        stop_server(server,t)


def test_valid_preview_user_confirm_submits_exactly_once():
    f = FakeOperator()
    server,t = start_server(operator=f, ready=True)
    try:
        code,_,raw = call(server,"GET","/api/v1/operator-ready")
        assert code==200
        assert json.loads(raw)["authority"]=="operator_ready"
        candidate = take_preview(server)
        assert candidate["can_execute"] is False
        assert candidate["status"]=="compatible"
        token = candidate["confirmation_ticket"]
        assert len(token)==112
        code,body = take_confirm(server,token)
        assert code==200
        assert body["state"]=="accepted"
        assert body["can_execute"] is False
        assert f.calls==[request()]
        lookup,_,raw = call(server,"GET",f"/api/v1/operations/{RID}")
        assert lookup==200
        assert json.loads(raw)["state"]=="accepted"
        # Replay same ticket is only a duplicate exact UUID; root Queue MUST
        # return same receipt and never dispatch twice (tested separately).
        repeated_code, repeated = take_confirm(server,token)
        assert repeated_code==200 and repeated["request_id"]==RID
        assert f.calls==[request(),request()]
    finally:
        stop_server(server,t)


@pytest.mark.parametrize("change", [
    {"request_id":"bbbbbbbb-1234-4234-8234-123456789abc"},
    {"service_id":"core-promotion"},
    {"action":"resume"},
])
def test_confirmation_ticket_cannot_be_retargeted(change):
    f = FakeOperator()
    server,t = start_server(operator=f,ready=True)
    try:
        ticket=take_preview(server)["confirmation_ticket"]
        req={**request(),**change,"confirmation_ticket":ticket,"confirm":True}
        status,_,raw=call(server,"POST","/api/v1/operations",
                          payload=req,token=issue_token(server))
        assert status==403
        assert json.loads(raw)["error"]=="confirmation_required"
        assert f.calls == []
    finally:
        stop_server(server,t)


def test_cross_origin_missing_csrf_or_no_confirmation_never_calls_backend():
    f=FakeOperator()
    server,t=start_server(operator=f,ready=True)
    try:
        ticket=take_preview(server)["confirmation_ticket"]
        for override in (
            {"origin":"https://evil.invalid"},
            {"origin":False},
            {"token":"bogus"},
            {"confirmed":False},
        ):
            code,body = take_confirm(server,ticket,**override)
            assert code in (400,403)
        assert f.calls == []
    finally:
        stop_server(server,t)


def test_rejected_preview_does_not_issue_ticket_even_if_worker_ready():
    f=FakeOperator()
    reject=lambda _: {
        "version":1,"authority":"preview_only","can_execute":False,
        "status":"rejected","reason":"operation_state_mismatch",
    }
    server,t=start_server(operator=f,ready=True,preview=reject)
    try:
        response=take_preview(server)
        assert response["status"]=="rejected"
        assert "confirmation_ticket" not in response
        assert not f.calls
    finally:
        stop_server(server,t)


@pytest.mark.parametrize("malicious", [
    {"version":1,"authority":"operator_ready",
     "operations":{"pause":True,"resume":True,"run_once":True},
     "reason":"wrong"},
    {"version":1,"authority":"operator_ready",
     "operations":{"pause":1,"resume":True,"run_once":True},
     "reason":"worker_ready"},
    {"version":1,"authority":"operator_ready",
     "operations":{"pause":True,"resume":False,"run_once":True},
     "reason":"worker_ready"},
    {"version":1,"authority":"observation_only",
     "operations":{"pause":True,"resume":False,"run_once":False},
     "reason":"broker_unavailable"},
])
def test_capabilities_spoof_cannot_turn_on_operations(malicious):
    assert not any(safe_capabilities(malicious)["operations"].values())


def test_receipt_status_path_rejects_arbitrary_ids_and_never_runs_effects():
    f=FakeOperator()
    server,t=start_server(operator=f,ready=True)
    try:
        for tail in ("../../etc", "sshd", "x"*250):
            code,_,_=call(server,"GET","/api/v1/operations/"+tail)
            assert code==404
        assert not f.calls
    finally:
        stop_server(server,t)


def test_browser_script_uses_confirmation_and_reuses_request_identity():
    assert 'id="preview-confirm"' in HTML
    assert 'id="preview-receipt-check"' in HTML
    assert "window.confirm(description)" in JS
    assert 'latestOperationId = choice.request.request_id' in JS
    assert '"/api/v1/operations"' in JS
    assert '"/api/v1/operator-ready"' in JS
    assert '"/api/v1/operations/" + latestOperationId' in JS
    assert "innerHTML" not in JS



def test_trusted_lan_vpn_console_needs_no_extra_auth_header():
    f = FakeOperator()
    server,t = start_server(operator=f,ready=True)
    try:
        code,_,raw = call(server,"GET","/api/v1/operator-ready",ingress=False)
        assert code == 200
        assert json.loads(raw)["operations"]["pause"] is True
        preview=take_preview(server)
        assert "confirmation_ticket" in preview
        code,body=take_confirm(server,preview["confirmation_ticket"])
        assert code==200 and body["state"]=="accepted"
        code,_,raw=call(server,"GET",f"/api/v1/operations/{RID}",ingress=False)
        assert code==200 and json.loads(raw)["request_id"]==RID
        assert f.calls==[request()]
    finally:
        stop_server(server,t)


def test_forged_ingress_header_rejected():
    f=FakeOperator()
    server,t=start_server(operator=f,ready=True)
    try:
        good=take_preview(server)["confirmation_ticket"]
        for headers in (
            {"X-Obsidian-Private-Ingress":"0"*64},
            {"X-Obsidian-Private-Ingress":TEST_INGRESS_KEY.hex().upper()},
            {"X-Obsidian-Private-Ingress":TEST_INGRESS_KEY.hex()+",evil"},
        ):
            code,_,raw=raw_call(
                server,"POST","/api/v1/operations",
                payload={**request(),"confirmation_ticket":good,"confirm":True},
                token=issue_token(server),more_headers=headers,
            )
            assert code==403
        assert f.calls==[]
    finally:
        stop_server(server,t)


def test_operator_status_messaging_does_not_claim_read_only_when_available():
    from obsidian_automation.admin_dashboard import HTML, JS
    assert 'id="dashboard-mode-title"' in HTML
    assert 'id="dashboard-mode-description"' in HTML
    assert 'id="dashboard-mode-detail"' in HTML
    assert "function updateOperatorMode()" in JS
    assert "サービス操作が有効" in JS
    assert "現在は状態確認のみ可能" in HTML
    assert "権限付きWorkerが利用可能な場合" in JS
    assert "updateOperatorMode();" in JS
