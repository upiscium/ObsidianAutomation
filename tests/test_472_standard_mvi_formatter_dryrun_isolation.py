"""#472 Standard MVI: synthetic cross-workload Formatter control acceptance.

This test only uses a temporary local Vault fixture, local SQLite state, and
an ephemeral loopback HTTP server. No Nextcloud, credentials, Writer, Gitea,
host services, or deployment are involved.
"""
from __future__ import annotations

from contextlib import contextmanager
import http.client
import json
from pathlib import Path
import threading
import uuid

from obsidian_automation.admin_dashboard import make_server
from obsidian_automation.snapshot_job import SnapshotJob
from obsidian_automation.vault_format_job import FormatterJob
from obsidian_automation.vault_formatter import create_plan


@contextmanager
def dashboard(formatter: FormatterJob, snapshot: SnapshotJob):
    server = make_server(
        port=0, bind_address="127.0.0.1",
        formatter_job=formatter, snapshot_job=snapshot,
        observer=lambda: [], ai_observer=lambda: {},
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)
        assert not thread.is_alive()
        server.server_close()


def request(server, method: str, route: str, payload=None, csrf=None):
    port = server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
    headers = {"Host": f"127.0.0.1:{port}"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
        headers["Origin"] = f"http://127.0.0.1:{port}"
    if csrf is not None:
        headers["X-Obsidian-CSRF"] = csrf
    try:
        conn.request(method, route, body=json.dumps(payload) if payload is not None else None,
                     headers=headers)
        reply = conn.getresponse()
        return reply.status, json.loads(reply.read())
    finally:
        conn.close()



def token(server) -> str:
    code, data = request(server, "GET", "/api/v1/operation-session")
    assert code == 200
    return data["csrf_token"]


def test_formatter_off_manual_and_scheduled_dryrun_do_not_change_snapshot(tmp_path: Path):
    """AC-FMT-103-01/04/06: one app UI/API, independent job state, no Writer."""
    vault = tmp_path / "synthetic-vault"
    target = vault / "98-System" / "test.md"
    target.parent.mkdir(parents=True)
    original = b"# Preview\nparagraph\n  \ncontent\n"
    target.write_bytes(original)
    clock = [1000.0]
    scans: list[str] = []
    snapshot_calls: list[str] = []

    def read_local_only():
        scans.append("local-plan")
        return create_plan(vault)

    snap = SnapshotJob(
        tmp_path / "snapshot-state",
        lambda run_id: snapshot_calls.append(run_id) or ("no_changes", "verify", None),
        configured=True, clock=lambda: clock[0],
    )
    try:
        snap.update({"enabled": True, "interval_seconds": 300,
                     "source_name": "nextcloud", "destination_name": "gitea"})
        snapshot_baseline = snap.settings()
        assert snapshot_baseline["enabled"] is True
        with FormatterJob(
            tmp_path / "formatter-state",
            read_local_only,
            configured=lambda: True,
            source_generation=lambda: "a" * 64,
            clock=lambda: clock[0],
            # No Writer injection: neither UI mode nor schedule can perform a PUT.
        ) as formatter:
            with dashboard(formatter, snap) as server:
                session_code, session = request(server, "GET", "/api/v1/operation-session")
                assert session_code == 200
                assert isinstance(session["csrf_token"], str)
                fmt_code, fmt = request(server, "GET", "/api/v1/formatter")
                assert fmt_code == 200
                assert fmt["settings"]["mode"] == "off"
                assert fmt["settings"]["write_capable"] is False
                assert fmt["last_run"] is None
                assert formatter.tick() is False
                assert scans == [] and snapshot_calls == []

                manual_id = uuid.uuid4().hex
                code, started = request(server, "POST", "/api/v1/formatter/runs",
                                        {"request_id": manual_id}, csrf=token(server))
                assert code == 202
                manual_run_id = started["run_id"]
                assert started["outcome"] == "accepted"
                assert formatter._worker is not None
                formatter._worker.join(timeout=5)
                assert not formatter._worker.is_alive()
                code, completed = request(server, "GET",
                                          "/api/v1/formatter/runs/" + manual_run_id)
                assert code == 200
                assert completed["trigger"] == "manual"
                assert completed["run_mode"] == "dry-run"
                assert completed["status"] == "would_change"
                assert completed["would_change"] == 1
                assert target.read_bytes() == original

                code, duplicate = request(server, "POST", "/api/v1/formatter/runs",
                                          {"request_id": manual_id}, csrf=token(server))
                assert code == 202
                assert duplicate["outcome"] == "replayed"
                assert duplicate["run_id"] == manual_run_id
                assert len(scans) == 1

                code, auto_denied = request(
                    server, "POST", "/api/v1/formatter/settings",
                    {"mode": "auto-apply", "interval_seconds": 300}, csrf=token(server),
                )
                assert code == 400
                assert auto_denied["error"] == "invalid_formatter_request"

                code, changed = request(server, "POST", "/api/v1/formatter/settings",
                                        {"mode": "dry-run", "interval_seconds": 300},
                                        csrf=token(server))
                assert code == 200
                assert changed["mode"] == "dry-run"
                assert changed["next_due"] == 1300.0
                assert snap.settings() == snapshot_baseline
                assert snap.settings()["next_due"] == 1300.0
                clock[0] = 1299.0
                assert formatter.tick() is False
                clock[0] = 1300.0
                assert formatter.tick() is True
                assert formatter._worker is not None
                formatter._worker.join(timeout=5)
                assert not formatter._worker.is_alive()
                status_code, status = request(server, "GET", "/api/v1/formatter")
                assert status_code == 200
                assert status["last_run"]["trigger"] == "scheduled"
                scheduled_run_id = status["last_run"]["run_id"]
                assert status["last_run"]["run_mode"] == "dry-run"
                assert status["last_run"]["status"] == "would_change"
                assert status["settings"]["next_due"] == 1600.0
                assert "paragraph" not in json.dumps(status)
                assert len(scans) == 2
                assert target.read_bytes() == original
                assert snap.settings() == snapshot_baseline
                assert snap.settings()["next_due"] == 1300.0
                assert snapshot_calls == []

                code, off = request(server, "POST", "/api/v1/formatter/settings",
                                    {"mode": "off", "interval_seconds": 300},
                                    csrf=token(server))
                assert code == 200
                assert off["mode"] == "off" and off["next_due"] is None
                clock[0] = 1800.0
                assert formatter.tick() is False
                assert snap.settings() == snapshot_baseline
                assert len(scans) == 2 and snapshot_calls == []
                assert target.read_bytes() == original

        # Reopen Formatter's SQLite state in this same pytest process; this
        # is not a process restart or crash-recovery test.
        with FormatterJob(tmp_path / "formatter-state",
                          lambda: (_ for _ in ()).throw(AssertionError("unexpected scan")),
                          configured=lambda: True,
                          source_generation=lambda: "a" * 64,
                          clock=lambda: clock[0]) as reopened:
            assert reopened.settings()["mode"] == "off"
            assert reopened.settings()["write_capable"] is False
            assert reopened.status()["last_run"]["status"] == "would_change"
            assert reopened.run(manual_run_id)["status"] == "would_change"
            assert reopened.run(manual_run_id)["trigger"] == "manual"
            assert reopened.run(scheduled_run_id)["status"] == "would_change"
            assert reopened.run(scheduled_run_id)["trigger"] == "scheduled"
            assert reopened.tick() is False
        assert snap.settings() == snapshot_baseline
        assert snapshot_calls == []
        assert target.read_bytes() == original
    finally:
        snap.close()
