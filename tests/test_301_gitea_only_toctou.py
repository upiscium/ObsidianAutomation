"""#301: deterministic Gitea-only vs shared Nextcloud rotation TOCTOU proof.

Every HTTP request targets an ephemeral 127.0.0.1 test listener. Provider
probes use synthetic in-memory predicates; no real remote I/O occurs.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import http.client
import json
from pathlib import Path
import threading
import time

import pytest

from obsidian_automation.admin_dashboard import Handler, make_server
from obsidian_automation.github_daily_job import DailyJob
from obsidian_automation.snapshot_job import SnapshotJob
from obsidian_automation.snapshot_setup import (
    GITEA_FIELDS, NEXTCLOUD_FIELDS, SetupStore,
)
from obsidian_automation.vault_format_job import FormatterJob
from test_snapshot_setup import sample


NC_PATH = "/api/v1/nextcloud/connections"
GITEA_PATH = "/api/v1/snapshot/connections"


def _http(server, method: str, path: str, payload=None, *, csrf=None):
    port = server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Host": f"127.0.0.1:{port}"}
    if method == "POST":
        headers["Origin"] = f"http://127.0.0.1:{port}"
        headers["Content-Type"] = "application/json"
        assert isinstance(csrf, str)
        headers["X-Obsidian-CSRF"] = csrf
    try:
        conn.request(method, path, json.dumps(payload) if payload is not None else None,
                     headers=headers)
        response = conn.getresponse()
        raw = response.read()
        if raw.startswith(b"{"):
            body = json.loads(raw)
        else:
            body = None
        return response.status, body
    finally:
        conn.close()


def _csrf(server):
    status, body = _http(server, "GET", "/api/v1/operation-session")
    assert status == 200
    return body["csrf_token"]


def _store(tmp_path):
    store = SetupStore(tmp_path / "shared", probe=lambda _: True,
                       obscure=lambda _: "syntheticEncoded")
    store.nextcloud_probe = lambda _: True
    nc = {key: sample()[key] for key in NEXTCLOUD_FIELDS}
    assert store.save_nextcloud(nc)
    gitea = {key: sample()[key] for key in GITEA_FIELDS}
    return store, nc, gitea


def _run_server(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


def _close_server(server, thread):
    server.shutdown()
    thread.join(timeout=5)
    assert not thread.is_alive()
    server.server_close()


class _ObservedWriterGate:
    """Preserve the actual Formatter RLock; observe Gitea's enter *attempt*."""

    def __init__(self, real_gate, gitea_thread, attempt_event):
        self.real_gate = real_gate
        self.gitea_thread = gitea_thread
        self.attempt_event = attempt_event

    def __enter__(self):
        if threading.get_ident() == self.gitea_thread[0]:
            self.attempt_event.set()
        self.real_gate.acquire()
        return self

    def __exit__(self, *_):
        self.real_gate.release()


def test_rotating_nextcloud_cannot_be_rolled_back_by_concurrent_gitea_only_http(
    tmp_path,
):
    """Force the Gitea request to reach the writer gate while rotation holds it.

    Old code snapshots the old Nextcloud credentials before the gate, then
    saves them after the rotation. Fixed code resolves after acquiring gate.
    No sleeps or external provider/network probes are used for ordering.
    """
    store, original, gitea = _store(tmp_path)
    rotated = dict(original, nextcloud_password="ROTATED-SYNTHETIC-PRIVATE")
    assert rotated != original

    probe_entered = threading.Event()
    permit_probe = threading.Event()
    gitea_at_gate = threading.Event()
    gitea_thread_id = [None]

    def blocked_probe(_payload):
        probe_entered.set()
        return permit_probe.wait(8)

    store.nextcloud_probe = blocked_probe

    def generation():
        current = store.load_nextcloud()
        return (hashlib.sha256(json.dumps(current, sort_keys=True).encode()).hexdigest()
                if current else None)

    with closing(SnapshotJob(tmp_path / "snapshot", lambda _: ("no_changes", "verify", None))) as snapshot:
        with DailyJob(tmp_path / "daily", lambda: {}, configured=lambda: True) as daily:
            with FormatterJob(
                tmp_path / "formatter", lambda: {},
                configured=lambda: store.source_connections() is not None,
                source_generation=generation,
            ) as formatter:
                # These are real local, never-executed schedules; the real
                # Nextcloud update should revoke them, Gitea-only must not.
                daily.update({"enabled": True, "interval_seconds": 86400})
                formatter.update({"mode": "dry-run", "interval_seconds": 3600})
                formatter._write_gate = _ObservedWriterGate(
                    formatter._write_gate, gitea_thread_id, gitea_at_gate,
                )

                class ObservedHandler(Handler):
                    def _read_bounded_json(self):
                        raw = super()._read_bounded_json()
                        if self.path == GITEA_PATH:
                            gitea_thread_id[0] = threading.get_ident()
                        return raw

                server = make_server(
                    port=0, snapshot_job=snapshot, snapshot_setup=store,
                    daily_job=daily, formatter_job=formatter,
                    observer=lambda: [], ai_observer=lambda: {},
                )
                server.RequestHandlerClass = ObservedHandler
                serving = _run_server(server)
                threads = []
                results = {}
                failures = []

                def post_as(name, path, payload, ticket):
                    try:
                        results[name] = _http(server, "POST", path, payload, csrf=ticket)
                    except BaseException as exc:
                        failures.append(type(exc).__name__)

                try:
                    nc_ticket = _csrf(server)
                    git_ticket = _csrf(server)
                    t_nc = threading.Thread(target=post_as,
                        args=("nextcloud", NC_PATH, rotated, nc_ticket), daemon=True)
                    t_nc.start()
                    threads.append(t_nc)
                    assert probe_entered.wait(5), "Nextcloud POST did not enter gated probe"

                    t_git = threading.Thread(target=post_as,
                        args=("gitea", GITEA_PATH, gitea, git_ticket), daemon=True)
                    t_git.start()
                    threads.append(t_git)
                    assert gitea_at_gate.wait(5), "Gitea POST did not reach writer gate"
                    # Rotation has not yet been admitted; old source is still
                    # canonical. We release only after observing Gitea's
                    # gate-acquire attempt, making this race deterministic.
                    assert store.load_nextcloud() == original
                    permit_probe.set()
                    for thread in threads:
                        thread.join(timeout=9)
                        assert not thread.is_alive(), "concurrent POST deadlocked"
                    assert failures == [], failures
                    assert results["nextcloud"][0] == 200
                    assert results["gitea"][0] == 200
                    assert store.load_nextcloud() == rotated, (
                        "Gitea-only request restored stale Nextcloud credentials"
                    )
                    assert store.load() == {**rotated, **gitea}
                    assert snapshot.settings()["enabled"] is False
                    assert daily.settings()["enabled"] is False
                    assert formatter.settings()["mode"] == "off"
                    for job in (snapshot, daily, formatter):
                        assert job.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
                    assert "ROTATED-SYNTHETIC" not in json.dumps(results)
                finally:
                    permit_probe.set()
                    for thread in threads:
                        thread.join(timeout=9)
                    _close_server(server, serving)


def test_missing_source_invalid_gitea_and_failed_probe_preserve_private_state(
    tmp_path,
):
    # Exact Gitea-only shape with no canonical Nextcloud source must fail
    # before remote provider probes, and may never create credentials.
    store = SetupStore(tmp_path / "shared", probe=lambda _: False,
                       obscure=lambda _: "syntheticEncoded")
    gitea = {key: sample()[key] for key in GITEA_FIELDS}
    with closing(SnapshotJob(tmp_path / "snapshot", lambda _: ("no_changes", "verify", None))) as snapshot:
        server = make_server(
            port=0, snapshot_job=snapshot, snapshot_setup=store,
            observer=lambda: [], ai_observer=lambda: {},
        )
        serving = _run_server(server)
        try:
            status, body = _http(server, "POST", GITEA_PATH, gitea, csrf=_csrf(server))
            assert status == 400 and body == {"error": "invalid_connection"}
            assert store.load_nextcloud() is None
            assert store.load() is None
            nc = {key: sample()[key] for key in NEXTCLOUD_FIELDS}
            store.nextcloud_probe = lambda _: True
            assert store.save_nextcloud(nc)
            invalid = {**gitea, "git_url": "https://gitea.invalid/bad"}
            status, body = _http(server, "POST", GITEA_PATH, invalid, csrf=_csrf(server))
            assert status == 400 and body == {"error": "invalid_connection"}
            assert store.load_nextcloud() == nc
            assert store.load() is None
            status, body = _http(server, "POST", GITEA_PATH, gitea, csrf=_csrf(server))
            assert status == 422 and body == {"error": "connection_probe_failed"}
            assert store.load_nextcloud() == nc
            assert store.load() is None
            assert snapshot.settings()["enabled"] is False
        finally:
            _close_server(server, serving)


def test_queued_snapshot_rejects_gitea_without_reopening_shared_source(tmp_path):
    store, original, gitea = _store(tmp_path)
    with closing(SnapshotJob(tmp_path / "snapshot", lambda _: ("no_changes", "verify", None))) as snapshot:
        with snapshot.db:
            snapshot.db.execute(
                "INSERT INTO runs (run_id,request_id,trigger,status,created,stage) "
                "VALUES (?,?,?,?,?,?)",
                ("a"*32, "b"*32, "manual", "queued", time.time(), "queued"),
            )
        source_reads = []
        original_reader = store.load_nextcloud

        def observed_reader():
            source_reads.append("called")
            return original_reader()

        store.load_nextcloud = observed_reader
        server = make_server(
            port=0, snapshot_job=snapshot, snapshot_setup=store,
            observer=lambda: [], ai_observer=lambda: {},
        )
        serving = _run_server(server)
        try:
            code, body = _http(server, "POST", GITEA_PATH, gitea, csrf=_csrf(server))
            assert code == 409 and body == {"error": "snapshot_busy"}
            assert source_reads == [], "Busy request read private Nextcloud outside gate"
            assert store.load() is None
            assert snapshot.settings()["enabled"] is False
        finally:
            _close_server(server, serving)


@pytest.mark.parametrize(
    ("queued_kind", "expected_error"),
    [
        ("daily", "daily_busy"),
        ("formatter", "formatter_busy"),
    ],
)
def test_other_active_jobs_reject_gitea_before_reading_shared_nextcloud(
    tmp_path, queued_kind, expected_error,
):
    """Busy B4/Formatter checks keep the current writer-admission priority."""
    store, original, gitea = _store(tmp_path)
    with closing(SnapshotJob(tmp_path / "snapshot", lambda _: ("no_changes", "verify", None))) as snapshot:
        with DailyJob(tmp_path / "daily", lambda: {}, configured=lambda: True) as daily:
            with FormatterJob(
                tmp_path / "formatter", lambda: {},
                configured=lambda: True,
                source_generation=lambda: None,
            ) as formatter:
                if queued_kind == "daily":
                    with daily.db:
                        daily.db.execute(
                            "INSERT INTO runs (run_id,request_id,trigger,status,created) "
                            "VALUES (?,?,?,?,?)",
                            ("a"*32, "b"*32, "manual", "queued", time.time()),
                        )
                else:
                    with formatter.db:
                        formatter.db.execute(
                            "INSERT INTO runs (run_id,request_id,trigger,run_mode,"
                            "status,created) VALUES (?,?,?,?,?,?)",
                            ("a"*32, "b"*32, "manual", "dry-run", "queued", time.time()),
                        )
                source_reads = []
                real_load = store.load_nextcloud

                def observed_load():
                    source_reads.append("called")
                    return real_load()

                store.load_nextcloud = observed_load
                server = make_server(
                    port=0, snapshot_job=snapshot, snapshot_setup=store,
                    daily_job=daily, formatter_job=formatter,
                    observer=lambda: [], ai_observer=lambda: {},
                )
                serving = _run_server(server)
                try:
                    code, body = _http(
                        server, "POST", GITEA_PATH, gitea, csrf=_csrf(server),
                    )
                    assert code == 409 and body == {"error": expected_error}
                    assert source_reads == []
                    assert store.load() is None
                    assert snapshot.settings()["enabled"] is False
                finally:
                    _close_server(server, serving)
                assert real_load() == original


def test_full_eight_field_post_preserves_existing_direct_validation(tmp_path):
    """Non-Gitea-only clients remain on the existing explicit full-payload path."""
    store, original, gitea = _store(tmp_path)
    with closing(SnapshotJob(tmp_path / "snapshot", lambda _: ("no_changes", "verify", None))) as job:
        server = make_server(
            port=0, snapshot_job=job, snapshot_setup=store,
            observer=lambda: [], ai_observer=lambda: {},
        )
        serving = _run_server(server)
        try:
            full = {**original, **gitea}
            status, body = _http(
                server, "POST", GITEA_PATH, full, csrf=_csrf(server),
            )
            assert status == 200 and body["configured"] is True
            assert store.load() == full
            assert job.settings()["enabled"] is False
            assert job.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        finally:
            _close_server(server, serving)
