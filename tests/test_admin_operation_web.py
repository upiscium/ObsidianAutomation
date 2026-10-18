"""Loopback HTTP and CSRF operator preview; never touches production services."""
from __future__ import annotations

from contextlib import contextmanager
import http.client
import json
import os
import socket
import threading

import pytest

from obsidian_automation.admin_dashboard import HTML, JS, make_server
from obsidian_automation.admin_operation_web import (
    MAX_REQUEST_BYTES, OneTimePreviewCsrf,
    PreviewRequestRejected, parse_operation_preview_request,
    strict_browser_preview,
)

RID = "12345678-1234-4234-8234-123456789abc"
SAMPLE = {"version": 1, "request_id": RID, "service_id": "github-sync", "action": "pause"}


@contextmanager
def live_server(callback=None):
    called = []
    def default(request):
        called.append(request)
        return {
            "version": 1, "authority": "preview_only", "can_execute": False,
            "status": "compatible", "reason": "not_operational",
            "candidate": {
                "request_id": request["request_id"],
                "service_id": request["service_id"],
                "action": request["action"],
                "observed_at": "2026-10-16T08:25:31+00:00",
                "authority": "requires_broker_live_recheck",
            },
        }
    server = make_server(
        port=0,
        observer=lambda: [],
        ai_observer=lambda: {},
        preview_observer=callback or default,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, called
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def call(server, method, path, *, payload=None, token=None,
         origin=True, host=None, content_type="application/json", more_headers=None):
    port = server.server_address[1]
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
    headers = {
        "Host": host or f"127.0.0.1:{port}",
    }
    if origin is True:
        headers["Origin"] = f"http://127.0.0.1:{port}"
    elif origin is not None and origin is not False:
        headers["Origin"] = origin
    if token is not None:
        headers["X-Obsidian-CSRF"] = token
    if payload is not None:
        headers["Content-Type"] = content_type
        payload = json.dumps(payload).encode("utf-8")
    if more_headers:
        headers.update(more_headers)
    try:
        connection.request(method, path, body=payload, headers=headers)
        r = connection.getresponse()
        return r.status, dict(r.getheaders()), r.read()
    finally:
        connection.close()


def issue_token(server):
    status, headers, body = call(server, "GET", "/api/v1/operation-session")
    assert status == 200
    assert headers["Cache-Control"] == "no-store"
    value = json.loads(body)
    assert value["authority"] == "preview_only"
    assert value["can_execute"] is False
    assert value["expires_in_seconds"] == 120
    return value["csrf_token"]


def test_preview_csrf_issue_consume_once_and_expire():
    now = [100.]
    vault = OneTimePreviewCsrf(now=lambda: now[0])
    info = vault.issue()
    token = info["csrf_token"]
    assert len(token) == 112
    assert vault.consume(token)
    assert not vault.consume(token)
    other = vault.issue()["csrf_token"]
    now[0] += 120
    assert not vault.consume(other)


def test_invalid_tokens_and_capacity_are_bounded():
    sessions = OneTimePreviewCsrf()
    assert not sessions.consume(None)
    assert not sessions.consume("invalid")
    assert not sessions.consume("f" * 111)
    assert not sessions.consume("Z" * 112)
    generated = {sessions.issue()["csrf_token"] for _ in range(160)}
    assert len(generated) == 160
    # A cross-site GET cannot exhaust a token-issuance pool: issuance is
    # stateless, with no reserved slots for unread tokens.
    assert sessions.consume(next(iter(generated)))
    assert len(sessions.issue()["csrf_token"]) == 112


def test_valid_no_effect_preview_and_one_use_http_csrf():
    with live_server() as (srv, called):
        token = issue_token(srv)
        status, headers, body = call(
            srv, "POST", "/api/v1/operation-previews", payload=SAMPLE, token=token,
        )
        assert status == 200
        value = json.loads(body)
        assert value["status"] == "compatible"
        assert value["authority"] == "preview_only"
        assert value["can_execute"] is False
        assert value["candidate"]["service_id"] == "github-sync"
        assert len(called) == 1
        assert called[0]["op"] == "preview"
        assert called[0] == {"version": 1, "op": "preview", **SAMPLE}
        assert headers["Cache-Control"] == "no-store"
        assert "Access-Control-Allow-Origin" not in headers
        # Replay must fail *before* the preview callback is invoked again.
        again, _, raw = call(
            srv, "POST", "/api/v1/operation-previews", payload=SAMPLE, token=token,
        )
        assert again == 403
        assert json.loads(raw)["error"] == "csrf_rejected"
        assert len(called) == 1


@pytest.mark.parametrize("case", [
    ("missing_origin", False, None, None),
    ("foreign_origin", "https://evil.invalid", None, None),
    ("hostile_host", True, "evil.invalid", None),
    ("cross_site", True, None, {"Sec-Fetch-Site": "cross-site"}),
    ("invalid_token", True, None, {"X-Obsidian-CSRF": "invalid"}),
])
def test_http_off_origin_requests_blocked_before_preview(case):
    _, origin, host, extras = case
    with live_server() as (srv, called):
        token = issue_token(srv)
        status, _, body = call(
            srv, "POST", "/api/v1/operation-previews",
            payload=SAMPLE, token=token, origin=origin, host=host,
            more_headers=extras,
        )
        assert status == 403
        assert not called
        assert json.loads(body)["error"] in ("origin_rejected", "csrf_rejected")


def test_cross_origin_preview_token_cannot_be_minted_or_read():
    with live_server() as (srv, _):
        status, _, raw = call(
            srv, "GET", "/api/v1/operation-session",
            origin="https://evil.example",
        )
        assert status == 403
        assert b"csrf_token" not in raw


@pytest.mark.parametrize("bad_request", [
    {**SAMPLE, "action": "stop"},
    {**SAMPLE, "action": "run_once; rm -rf /"},
    {**SAMPLE, "service_id": "sshd"},
    {**SAMPLE, "service_id": "../../etc"},
    {**SAMPLE, "request_id": "not-a-uuid"},
    {**SAMPLE, "version": True},
    {**SAMPLE, "observed_at": "1970-01-01T00:00:00Z"},
    {**SAMPLE, "secret": "CREDENTIAL_NOT_FOR_OUTPUT"},
])
def test_browser_never_passes_invalid_preview_payload_to_backend(bad_request):
    with live_server() as (srv, called):
        status, _, raw = call(
            srv, "POST", "/api/v1/operation-previews",
            payload=bad_request, token=issue_token(srv),
        )
        assert status == 400
        assert json.loads(raw) == {"error": "invalid_preview_request"}
        assert b"CREDENTIAL" not in raw
        assert called == []


@pytest.mark.parametrize("headers", [
    {"Content-Type": "text/plain"},
    {"Transfer-Encoding": "chunked"},
    {"Content-Encoding": "gzip"},
    {"Expect": "100-continue"},
])
def test_invalid_content_type_and_transfer_encoding_rejected(headers):
    with live_server() as (srv, called):
        response = call(
            srv, "POST", "/api/v1/operation-previews",
            payload=SAMPLE, token=issue_token(srv), more_headers=headers,
        )
        assert response[0] == 400
        assert called == []


def test_large_preview_request_rejected_without_calling_backend():
    with live_server() as (srv, called):
        token = issue_token(srv)
        body = b" " * (MAX_REQUEST_BYTES + 1)
        conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1])
        port = srv.server_address[1]
        conn.request("POST", "/api/v1/operation-previews", body=body, headers={
            "Host": f"127.0.0.1:{port}",
            "Origin": f"http://127.0.0.1:{port}",
            "X-Obsidian-CSRF": token,
            "Content-Type": "application/json",
        })
        r = conn.getresponse()
        assert r.status == 400
        r.read()
        conn.close()
        assert not called


@pytest.mark.parametrize("raw_body", [
    b'{"version":1,"version":1,"request_id":"12345678-1234-4234-8234-123456789abc","service_id":"github-sync","action":"pause"}',
    b"\xff",
    b"[]",
    b"{}",
])
def test_duplicate_keys_invalid_utf8_or_wrong_shape_rejected(raw_body):
    with pytest.raises(PreviewRequestRejected, match="invalid_preview_payload"):
        parse_operation_preview_request(raw_body)


def test_browser_never_exposes_systemd_effect_even_if_backend_claims_true():
    fake = lambda req: {
        "version": 1, "authority": "privileged_operator",
        "can_execute": True, "status": "compatible", "reason": "approved",
        "password": "MUST_NOT_LEAK",
    }
    with live_server(fake) as (srv, called):
        code, _, raw = call(
            srv, "POST", "/api/v1/operation-previews",
            payload=SAMPLE, token=issue_token(srv),
        )
        assert code == 200
        assert json.loads(raw) == {
            "version": 1, "authority": "preview_only", "can_execute": False,
            "status": "rejected", "reason": "preview_unavailable",
        }
        assert b"MUST_NOT_LEAK" not in raw


def test_backend_exception_is_not_exposed():
    def broken(_):
        raise RuntimeError("Authorization Bearer confidential-canary")
    with live_server(broken) as (srv, _):
        code, _, raw = call(
            srv, "POST", "/api/v1/operation-previews",
            payload=SAMPLE, token=issue_token(srv),
        )
        assert code == 200
        assert json.loads(raw)["reason"] == "preview_unavailable"
        assert b"confidential-canary" not in raw


@pytest.mark.parametrize("path", [
    "/api/v1/operations",
    "/api/v1/operation-confirm",
    "/api/v1/status",
    "/api/v1/operation-capabilities",
    "/",
])
def test_all_real_control_paths_remain_unimplemented(path):
    with live_server() as (srv, called):
        status, _, raw = call(
            srv, "POST", path, payload=SAMPLE, token=issue_token(srv),
        )
        assert status == 405
        assert called == []


def test_origin_must_exactly_match_host_for_preview_even_if_both_are_local():
    with live_server() as (srv, called):
        port = srv.server_address[1]
        code, _, _ = call(
            srv, "POST", "/api/v1/operation-previews", payload=SAMPLE,
            token=issue_token(srv), host=f"localhost:{port}", origin=True,
        )
        assert code == 403
        assert called == []


def test_token_stale_across_web_server_restarts():
    with live_server() as (first, _):
        token = issue_token(first)
    with live_server() as (second, called):
        code, _, _ = call(
            second, "POST", "/api/v1/operation-previews",
            payload=SAMPLE, token=token,
        )
        assert code == 403
        assert called == []


def test_preview_html_is_explicitly_nonoperational_and_xss_safe():
    assert 'id="operation-preview"' in HTML
    assert 'id="preview-check"' in HTML
    assert 'aria-label="実際のサービス操作は未対応"' in HTML
    assert '実行不可' in HTML
    assert "previewOperation()" in JS
    assert '"X-Obsidian-CSRF":session.csrf_token' in JS
    assert '"/api/v1/operation-session"' in JS
    assert '"/api/v1/operation-previews"' in JS
    assert 'can_execute !== false' in JS
    assert "innerHTML" not in JS


def test_csrf_mac_tampering_and_restart_secret_are_denied():
    old = OneTimePreviewCsrf()
    token = old.issue()["csrf_token"]
    forged = token[:22] + ("0" if token[22] != "0" else "1") + token[23:]
    assert not old.consume(forged)
    assert not OneTimePreviewCsrf().consume(token)
    assert old.consume(token)
    assert not old.consume(token)


def test_many_unread_session_tokens_do_not_exhaust_browser_capacity():
    with live_server() as (srv, called):
        all_tokens = [issue_token(srv) for _ in range(60)]
        assert len(set(all_tokens)) == 60
        status, _, result = call(
            srv, "POST", "/api/v1/operation-previews",
            payload=SAMPLE, token=all_tokens[0],
        )
        assert status == 200
        assert json.loads(result)["authority"] == "preview_only"
        assert len(called) == 1


def test_ui_discards_inflight_preview_after_refresh_or_selection_change():
    from obsidian_automation.admin_dashboard import JS
    assert "let previewGeneration = 0" in JS
    assert "++previewGeneration; // Invalidate any in-flight operation preview." in JS
    assert "generation === previewGeneration" in JS
    assert '"preview-service").value === serviceId' in JS
    assert '"preview-action").value === action' in JS
    assert 'if (stillCurrent())' in JS
    assert '$(id).addEventListener("change"' in JS


def test_pre_header_partial_http_request_has_fixed_total_deadline():
    import time
    with live_server() as (srv, called):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(4)
        try:
            sock.connect(("127.0.0.1", srv.server_address[1]))
            sock.sendall(b"GET /api/v1/status HTTP/1.1\r\nHost: ")
            # Never terminate the header; a read timeout alone would be
            # reset by each byte, but the full header deadline expires.
            started = time.monotonic()
            closed = sock.recv(256)
            assert closed == b""
            assert time.monotonic() - started < 3.5
            assert not called
        finally:
            sock.close()


def test_handler_threads_are_bounded_before_header_parsing():
    from obsidian_automation.admin_dashboard import MAX_DASHBOARD_HANDLERS
    with live_server() as (srv, called):
        # Simulate occupied handler slots without creating noisy TCP clients.
        acquired = []
        try:
            for _ in range(MAX_DASHBOARD_HANDLERS):
                assert srv._slots.acquire(blocking=False)
                acquired.append(True)
            assert not srv._slots.acquire(blocking=False)
            # No extra handler may be admitted when all slots are held.
            conn = socket.create_connection(("127.0.0.1", srv.server_address[1]), timeout=2)
            conn.settimeout(2)
            try:
                conn.sendall(b"GET / HTTP/1.0\r\nHost: localhost\r\n\r\n")
                assert conn.recv(10) == b""
            except ConnectionResetError:
                pass
            finally:
                conn.close()
            assert not called
        finally:
            for _ in acquired:
                srv._slots.release()


def test_head_operator_session_is_not_a_valid_csrf_issuance():
    with live_server() as (srv, called):
        code, _, payload = call(srv, "HEAD", "/api/v1/operation-session")
        assert code == 405
        assert payload == b""
        assert not called


def test_systemd_status_observer_is_singleflight_and_short_cache():
    import time
    from obsidian_automation.admin_dashboard import make_server
    entered = threading.Event()
    finish = threading.Event()
    counts = []
    def slow_status():
        counts.append(True)
        if len(counts) == 1:
            entered.set()
            assert finish.wait(timeout=3)
        return [{"id": "github-sync", "schedule": "paused"}]

    srv = make_server(
        port=0, observer=slow_status, ai_observer=lambda: {},
    )
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    first = []
    def first_request():
        first.append(call(srv, "GET", "/api/v1/status", origin=False))
    client = threading.Thread(target=first_request)
    client.start()
    try:
        assert entered.wait(timeout=2)
        started = time.monotonic()
        code, _, raw = call(srv, "GET", "/api/v1/status", origin=False)
        assert code == 503
        assert json.loads(raw) == {"error": "status_busy"}
        assert time.monotonic() - started < .7
        assert len(counts) == 1
        finish.set()
        client.join(timeout=3)
        assert not client.is_alive()
        assert first[0][0] == 200
        assert len(counts) == 1
        # Fast repeat GET uses immutable, bounded-age source projection.
        cached = call(srv, "GET", "/api/v1/status", origin=False)
        assert cached[0] == 200 and cached[2] == first[0][2]
        assert len(counts) == 1
        time.sleep(1.1)
        again = call(srv, "GET", "/api/v1/status", origin=False)
        assert again[0] == 200
        assert len(counts) == 2
    finally:
        finish.set()
        client.join(timeout=3)
        srv.shutdown()
        thread.join(timeout=3)
        srv.server_close()


def test_status_failed_refresh_does_not_republish_expired_success():
    import time
    from obsidian_automation.admin_dashboard import make_server
    calls = [0]
    def fragile():
        calls[0] += 1
        if calls[0] > 1:
            raise RuntimeError("unexpected private auth error")
        return [{"id": "github-sync"}]
    srv = make_server(port=0, observer=fragile, ai_observer=lambda: {})
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        first = call(srv, "GET", "/api/v1/status", origin=False)
        assert first[0] == 200
        time.sleep(1.1)
        second = call(srv, "GET", "/api/v1/status", origin=False)
        assert second[0] == 503
        assert json.loads(second[2]) == {"error": "status_unavailable"}
        assert b"private" not in second[2]
        third = call(srv, "GET", "/api/v1/status", origin=False)
        assert third[0] == 503
        assert b"github-sync" not in third[2]
    finally:
        srv.shutdown()
        thread.join(timeout=3)
        srv.server_close()


def test_slow_local_capabilities_socket_cannot_occupy_every_web_handler():
    import time
    from obsidian_automation.admin_dashboard import make_server
    began = threading.Event()
    release = threading.Event()
    observations = []
    def slow_capabilities():
        observations.append("read")
        if len(observations) == 1:
            began.set()
            assert release.wait(timeout=3)
        return {
            "version": 1, "authority": "observation_only",
            "operations": {"pause": False, "resume": False, "run_once": False},
            "reason": "cross_updater_exclusion_unqualified",
        }
    srv = make_server(
        port=0, observer=lambda: [], ai_observer=lambda: {},
        capabilities_observer=slow_capabilities,
    )
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    first = []
    def first_request():
        first.append(call(srv, "GET", "/api/v1/operation-capabilities", origin=False))
    client = threading.Thread(target=first_request)
    client.start()
    try:
        assert began.wait(timeout=2)
        started = time.monotonic()
        code, _, raw = call(
            srv, "GET", "/api/v1/operation-capabilities", origin=False,
        )
        assert code == 200
        assert json.loads(raw)["reason"] == "broker_unavailable"
        assert time.monotonic() - started < .7
        assert len(observations) == 1
        release.set()
        client.join(timeout=3)
        assert not client.is_alive()
        assert json.loads(first[0][2])["reason"] == "cross_updater_exclusion_unqualified"
        cached = call(srv, "GET", "/api/v1/operation-capabilities")
        assert cached[2] == first[0][2]
        assert len(observations) == 1
    finally:
        release.set()
        client.join(timeout=3)
        srv.shutdown()
        thread.join(timeout=3)
        srv.server_close()
