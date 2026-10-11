from __future__ import annotations

import http.client
import json
import threading
from contextlib import contextmanager

import pytest

from obsidian_automation.admin_dashboard import Handler, JS, make_server


@contextmanager
def serving(observer):
    server = make_server(port=0, observer=observer)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def response(server, method="GET", path="/api/v1/status", *, host=None, origin=None):
    assert server.server_address[0] == "127.0.0.1"
    port = server.server_address[1]
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    headers = {"Host": host if host is not None else f"127.0.0.1:{port}"}
    if origin is not None:
        headers["Origin"] = origin
    try:
        connection.request(method, path, headers=headers)
        reply = connection.getresponse()
        body = reply.read()
        return reply.status, dict(reply.getheaders()), body
    finally:
        connection.close()


def fixture_observer():
    return [
        {
            "id": "ai-vault-pull", "label": "Vault Mirror",
            "timer": "obsidian-ai-vault-pull.timer",
            "service": "obsidian-ai-vault-pull.service",
            "schedule": "scheduled", "latest_execution": "succeeded",
            "health": "observed_success",
        }
    ]


def test_is_bound_to_loopback_and_json_observation_only() -> None:
    with serving(fixture_observer) as server:
        code, headers, body = response(server)
        assert code == 200
        value = json.loads(body)
        assert value["version"] == 1
        assert value["authority"] == "observation_only"
        assert value["actions"] == {"start": False, "stop": False, "trigger": False}
        assert len(value["services"]) == 1
        assert value["services"][0]["health"] == "observed_success"
        assert value["ai_pipeline"]["health"] == "unknown"
        assert value["ai_pipeline"]["review_pending"] is None
        assert value["ai_pipeline"]["normal_rejections"] is None
        assert value["ai_pipeline"]["incidents"] == []
        assert "default-src 'none'" in headers["Content-Security-Policy"]
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
        assert headers["Cache-Control"] == "no-store"
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert "Access-Control-Allow-Origin" not in headers


def test_browsable_document_and_external_assets() -> None:
    with serving(fixture_observer) as server:
        assert response(server, path="/")[0] == 200
        assert b"ObsidianAutomation" in response(server, path="/")[2]
        css = response(server, path="/static/style.css")
        js = response(server, path="/static/app.js")
        assert css[0] == 200 and css[1]["Content-Type"].startswith("text/css")
        assert js[0] == 200 and js[1]["Content-Type"].startswith("text/javascript")
        assert b"innerHTML" not in js[2]  # use textContent for untrusted data
        assert "textContent" in JS
        assert response(server, path="/../../../etc/passwd")[0] == 404


@pytest.mark.parametrize("verb", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def test_no_state_changing_routes(verb: str) -> None:
    with serving(fixture_observer) as server:
        code, _, body = response(server, method=verb)
        assert code == 405
        assert body == b"Method not allowed"


def test_host_validation_and_origin_validation() -> None:
    with serving(fixture_observer) as server:
        port = server.server_address[1]
        assert response(server, host="attacker.example")[0] == 403
        assert response(server, host="127.0.0.1.attacker.invalid")[0] == 403
        assert response(server, origin="https://attacker.example")[0] == 403
        assert response(server, origin=f"http://127.0.0.1:{port}")[0] == 200
        assert response(server, host=f"localhost:{port}")[0] == 200


def test_duplicate_host_header_is_rejected() -> None:
    with serving(fixture_observer) as server:
        port = server.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            conn.putrequest("GET", "/", skip_host=True)
            conn.putheader("Host", f"127.0.0.1:{port}")
            conn.putheader("Host", "attacker.invalid")
            conn.endheaders()
            r = conn.getresponse()
            assert r.status == 403
            r.read()
        finally:
            conn.close()


def test_observer_failure_is_redacted_and_not_a_200() -> None:
    def fail():
        raise RuntimeError("PASSWORD=not-for-client")
    with serving(fail) as server:
        status, headers, body = response(server)
        assert status == 503
        assert json.loads(body) == {"error": "status_unavailable"}
        assert b"PASSWORD" not in body


def test_head_no_response_body() -> None:
    with serving(fixture_observer) as server:
        code, headers, body = response(server, method="HEAD")
        assert code == 200
        assert body == b""
        assert int(headers["Content-Length"]) > 0


def test_invalid_port_rejected() -> None:
    with pytest.raises(ValueError):
        make_server(port=-1)
    with pytest.raises(ValueError):
        make_server(port=65536)
