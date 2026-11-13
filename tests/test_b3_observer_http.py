"""B3 GET-only observer HTTP routes: synthetic, no Vault or GitHub I/O."""
from __future__ import annotations

from contextlib import contextmanager
import http.client
import json
import threading
import time

from obsidian_automation.admin_dashboard import make_server
from obsidian_automation.github_b3_observer_job import B3ObserverJob
from obsidian_automation.github_b3_watcher_bridge import VERSION as BRIDGE_VERSION


@contextmanager
def serving(job=None):
    server = make_server(port=0, b3_observer_job=job)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01})
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def send(server, method="GET", path="/api/v1/b3/observer", *,
         payload=None, origin=None, csrf=None, host=None):
    port = server.server_address[1]
    data = (payload if type(payload) is bytes else
            json.dumps(payload).encode() if payload is not None else None)
    headers = {"Host": host if host is not None else f"127.0.0.1:{port}"}
    if method == "POST":
        headers["Origin"] = (
            f"http://127.0.0.1:{port}" if origin is None else origin
        )
        headers["Content-Type"] = "application/json"
        if csrf is not None:
            headers["X-Obsidian-CSRF"] = csrf
    elif origin is not None:
        headers["Origin"] = origin
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request(method, path, body=data, headers=headers)
        reply = connection.getresponse()
        return reply.status, dict(reply.getheaders()), reply.read()
    finally:
        connection.close()


def session(server):
    status, _, data = send(server, path="/api/v1/operation-session")
    assert status == 200
    return json.loads(data)["csrf_token"]


class Synthetic:
    def __init__(self):
        self.calls = 0
        self.ready = True
        self.generation = "a" * 64

    def collect(self):
        self.calls += 1
        return {
            "schema_version": BRIDGE_VERSION,
            "state": "observed", "reason": "local_read_only_observation",
            "observations": 2, "pending_proposals": 1,
            "vault_write_authorized": False, "cutover_authorized": False,
        }


def test_unattached_routes_fail_closed_without_b3_job():
    with serving() as server:
        assert send(server)[0] == 503
        assert send(server, "HEAD")[0] == 503
        assert send(server, path="/api/v1/b3/observer/runs/" + "a"*32)[0] == 503
        assert send(server, "POST", "/api/v1/b3/observer/runs",
                    payload={"request_id": "a"*32}, csrf=session(server))[0] == 503


def test_read_only_get_head_and_run_once_are_separately_guarded(tmp_path):
    source = Synthetic()
    with B3ObserverJob(tmp_path/"b3", source.collect,
                       configured=lambda: source.ready,
                       generation=lambda: source.generation) as owner:
        with serving(owner) as server:
            code, headers, body = send(server)
            assert code == 200
            data = json.loads(body)
            assert data["job"] == "b3_read_only_watcher"
            assert data["settings"]["enabled"] is False
            assert data["authority"] == "observation_only"
            assert data["vault_write_authorized"] is False
            assert source.calls == 0
            assert send(server, "HEAD")[0] == 200
            assert source.calls == 0
            assert headers["Cache-Control"] == "no-store"
            assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]

            # No CSRF; no effect/credential/reader state change.
            assert send(server, "POST", "/api/v1/b3/observer/runs",
                        payload={"request_id": "a"*32})[0] == 403
            assert send(server, "POST", "/api/v1/b3/observer/runs",
                        payload={"request_id": "a"*32},
                        origin="http://evil.invalid", csrf=session(server))[0] == 403
            assert send(server, "POST", "/api/v1/b3/observer/runs",
                        payload={"request_id": "a"*32},
                        host="evil.invalid", csrf=session(server))[0] == 403
            assert source.calls == 0

            code, _, body = send(
                server, "POST", "/api/v1/b3/observer/runs",
                payload={"request_id": "a"*32}, csrf=session(server)
            )
            assert code == 202
            receipt = json.loads(body)
            assert receipt["decision"] == "accepted"
            assert receipt["vault_write_authorized"] is False
            run_id = receipt["run_id"]
            for _ in range(100):
                code, _, body = send(server, path="/api/v1/b3/observer/runs/" + run_id)
                if json.loads(body)["status"] == "observed":
                    break
                time.sleep(.01)
            assert code == 200
            assert json.loads(body)["status"] == "observed"
            assert source.calls == 1
            code, _, body = send(server, "POST", "/api/v1/b3/observer/runs",
                                 payload={"request_id": "a"*32}, csrf=session(server))
            assert code == 202 and json.loads(body)["decision"] == "replayed"
            assert source.calls == 1

            code, _, body = send(server, "POST", "/api/v1/b3/observer/settings",
                                 payload={"enabled": True, "interval_seconds": 3600},
                                 csrf=session(server))
            assert code == 200 and json.loads(body)["effective_enabled"] is True
            assert source.calls == 1


def test_malformed_paths_inputs_and_source_errors_fail_closed(tmp_path):
    source = Synthetic()
    with B3ObserverJob(tmp_path/"b3", source.collect,
                       configured=lambda: source.ready,
                       generation=lambda: source.generation) as owner:
        with serving(owner) as server:
            for path in (
                "/api/v1/b3/observer/runs/x",
                "/api/v1/b3/observer/settings",
                "/api/v1/b3/observer?x=1",
                "/api/v1/b3/observer/runs/" + "a"*32 + "/x",
            ):
                assert send(server, path=path)[0] == 404
            assert send(server, path="/api/v1/b3/observer/runs/" + "c"*32)[0] == 404
            code, _, body = send(server, "POST", "/api/v1/b3/observer/runs",
                                 payload={"request_id": True}, csrf=session(server))
            assert code == 400 and b"invalid_b3_observer_request" in body
            code, _, body = send(server, "POST", "/api/v1/b3/observer/settings",
                                 payload={"enabled": True, "interval_seconds": True},
                                 csrf=session(server))
            assert code == 400
            assert source.calls == 0
            source.ready = False
            code, _, body = send(server, "POST", "/api/v1/b3/observer/runs",
                                 payload={"request_id": "a"*32}, csrf=session(server))
            assert code == 409
            assert json.loads(body)["decision"] == "unconfigured"
            assert source.calls == 0


def test_disallow_b3_publishing_and_unknown_methods(tmp_path):
    source = Synthetic()
    with B3ObserverJob(tmp_path/"b3", source.collect,
                       configured=lambda: source.ready,
                       generation=lambda: source.generation) as owner:
        with serving(owner) as server:
            for verb in ("PUT","PATCH","DELETE","OPTIONS"):
                assert send(server, verb, "/api/v1/b3/observer")[0] == 405
            assert send(server, "POST", "/api/v1/b3/observer",
                        payload={}, csrf=session(server))[0] == 405
            assert source.calls == 0


def test_reject_duplicate_json_fields_and_nonfinite_before_job_access(tmp_path):
    source = Synthetic()
    with B3ObserverJob(tmp_path/"b3", source.collect,
                       configured=lambda: source.ready,
                       generation=lambda: source.generation) as owner:
        with serving(owner) as server:
            for raw in (
                b'{"request_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
                b'"request_id":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}',
                b'{"enabled":true,"enabled":false,"interval_seconds":3600}',
                b'{"enabled":true,"interval_seconds":NaN}',
                b'{"enabled":true,"interval_seconds":Infinity}',
                b'["not a request"]',
            ):
                path = ("/api/v1/b3/observer/runs" if b"request_id" in raw
                        else "/api/v1/b3/observer/settings")
                code, _, body = send(server, "POST", path,
                                     payload=raw, csrf=session(server))
                assert code == 400
                assert b"invalid_b3_observer_request" in body
            assert source.calls == 0
            assert owner.settings()["enabled"] is False
