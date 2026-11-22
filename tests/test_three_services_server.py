"""No live credentials, provider calls or Vault data in this server test."""
import http.client
import json
import threading

from obsidian_automation.three_services_server import make_server


class StubJob:
    def __init__(self, name):
        self.name = name
        self.settings_value = {"enabled": False, "configured": True,
                               "interval_seconds": 3600, "next_due": None,
                               "source_bound": True}
        self.receipt = None

    def status(self):
        return {"version": 1, "job": self.name,
                "settings": self.settings_value, "last_run": self.receipt}

    def update(self, payload):
        self.settings_value["enabled"] = payload["enabled"]
        self.settings_value["interval_seconds"] = payload["interval_seconds"]
        return self.settings_value

    def submit(self, request_id):
        self.receipt = {"run_id": "f" * 32, "request_id": request_id,
                        "status": "queued"}
        return "accepted", self.receipt

    def run(self, run_id):
        return self.receipt if run_id == "f" * 32 else None


def request(port, method, path, *, payload=None, csrf=None, origin=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    headers = {}
    if origin is not None:
        headers["Origin"] = origin
    if csrf is not None:
        headers["X-Obsidian-CSRF"] = csrf
    body = None
    if payload is not None:
        body = json.dumps(payload)
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=body, headers=headers)
    result = conn.getresponse()
    code, data = result.status, result.read()
    conn.close()
    return code, data


def test_only_three_services_exposed_and_origin_csrf_required():
    with make_server(port=0, bind_address="127.0.0.1") as server:
        server.snapshot_job = StubJob("vault_snapshot")
        server.daily_job = StubJob("github_daily_preview")
        server.core_promotion_job = StubJob("core_promotion")
        port = server.server_port
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            code, body = request(port, "GET", "/")
            assert code == 200
            assert body.count(b' class="service"') == 3
            assert b"Vault Formatter" not in body
            assert b"ChatGPT" not in body
            assert b"AI Knowledge" not in body
            for path in ("/api/v1/formatter", "/api/v1/b3/observer",
                         "/api/v1/workflows", "/api/v1/status",
                         "/api/v1/operation-capabilities",
                         "/workflow-control"):
                assert request(port, "GET", path)[0] == 404
            for path in ("/api/v1/snapshot", "/api/v1/daily", "/api/v1/core-promotion"):
                assert request(port, "GET", path)[0] == 200
            host = "http://127.0.0.1:" + str(port)
            change = {"enabled": True, "interval_seconds": 3600,
                      "source_name": "nextcloud", "destination_name": "gitea"}
            assert request(port, "POST", "/api/v1/snapshot/settings",
                           payload=change, origin=host)[0] == 403
            session = json.loads(request(port, "GET", "/api/v1/session")[1])
            assert request(port, "POST", "/api/v1/snapshot/settings",
                           payload=change, csrf=session["csrf"],
                           origin="http://evil.test")[0] == 403
            # Wrong Origin did not consume token; correct request does.
            code, body = request(port, "POST", "/api/v1/snapshot/settings",
                                 payload=change, csrf=session["csrf"], origin=host)
            assert code == 200
            assert json.loads(body)["enabled"] is True
            assert request(port, "POST", "/api/v1/snapshot/settings",
                           payload=change, csrf=session["csrf"], origin=host)[0] == 403
            assert request(port, "HEAD", "/api/v1/status")[0] == 405
            assert request(port, "POST", "/api/v1/formatter/settings",
                           payload=change, origin=host)[0] == 405
        finally:
            server.shutdown()
            thread.join(timeout=3)


def test_core_promotion_run_admission_is_not_false_success():
    with make_server(port=0, bind_address="127.0.0.1") as server:
        server.core_promotion_job = StubJob("core_promotion")
        port = server.server_port
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            session = json.loads(request(port, "GET", "/api/v1/session")[1])
            code, body = request(
                port, "POST", "/api/v1/core-promotion/runs",
                payload={"request_id": "a" * 32}, csrf=session["csrf"],
                origin="http://127.0.0.1:" + str(port))
            assert code == 202
            response = json.loads(body)
            assert response["outcome"] == "accepted"
            assert "status" not in response
            run = json.loads(request(
                port, "GET", "/api/v1/core-promotion/runs/" + response["run_id"])[1])
            assert run["status"] == "queued"
        finally:
            server.shutdown()
            thread.join(timeout=3)


def test_wildcard_and_non_rfc1918_bind_addresses_refused():
    import pytest

    for address in ("0.0.0.0", "169.254.1.1", "100.64.0.1",
                    "224.0.0.1", "255.255.255.255", "::", "::1"):
        with pytest.raises(ValueError):
            make_server(port=0, bind_address=address)


def test_incomplete_headers_release_all_handler_slots_after_absolute_deadline():
    """16 slow connections must not indefinitely deny a new operator request."""
    import socket
    import time

    with make_server(port=0, bind_address="127.0.0.1") as server:
        port = server.server_port
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        clients = []
        try:
            for _ in range(16):
                sock = socket.create_connection(("127.0.0.1", port), timeout=3)
                sock.sendall(b"GET / HTTP/1.1\r\nHost: 127.0.0.1")
                clients.append(sock)
            # Total monotonic header timeout, not reset by each byte.
            for _ in range(5):
                time.sleep(0.5)
                for sock in clients:
                    try:
                        sock.send(b"x")
                    except OSError:
                        pass
            # The stalled requests have expired; a new client can enter.
            code, data = request(port, "GET", "/api/v1/session")
            assert code == 200
            assert isinstance(json.loads(data)["csrf"], str)
        finally:
            for sock in clients:
                sock.close()
            server.shutdown()
            worker.join(timeout=3)
