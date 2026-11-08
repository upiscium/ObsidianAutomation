"""Synthetic all-Project snapshot, never remote Provider or Vault write."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from obsidian_automation.b3_project_cache import (
    CacheUnavailable, build_project_snapshot, persist_project_snapshot,
    read_project_snapshot,
)
from obsidian_automation.github_b3_observer_job import B3ObserverJob
from obsidian_automation.admin_dashboard import make_server
import threading
import http.client


def setup_source(tmp_path):
    mirror = tmp_path / "mirror"
    folder = mirror / "10-Project"
    folder.mkdir(parents=True)
    def note(name, meta):
        p = folder / name
        p.parent.mkdir(parents=True,exist_ok=True)
        p.write_text("---\n" + meta + "\n---\nsecret note body must never leak\n")
    note("Watched.md", "type: project\nstatus: running\ngithub_watch: true\ngithub_repo: upiscium/Repo")
    note("Disabled.md", "type: project\nstatus: planning\ngithub_watch: false\ngithub_repo: upiscium/Disabled")
    note("Unconfigured.md", "type: project\nstatus: running\ngithub_watch: true")
    note("Terminal.md", "type: project\nstatus: done\ngithub_watch: true\ngithub_repo: upiscium/Done")
    note("NotProject.md", "type: journal\nstatus: running\ngithub_watch: true")
    db = tmp_path / "watcher.sqlite3"
    with sqlite3.connect(db) as c:
        c.execute(
            "CREATE TABLE project_state (project_path TEXT PRIMARY KEY,"
            "repository TEXT, observed_at TEXT)"
        )
        c.execute(
            "INSERT INTO project_state VALUES (?,?,?)",
            ("10-Project/Watched.md","upiscium/Repo","2026-11-08T00:00:00+00:00"),
        )
    return mirror, db


def now():
    return datetime(2026,11,8,1,0,tzinfo=timezone.utc)


def test_cache_all_projects_including_disabled_and_misconfigured(tmp_path):
    mirror, db = setup_source(tmp_path)
    snap = build_project_snapshot(mirror, db, now=now())
    assert snap["schemaVersion"]=="b3-project-watch-list/v1"
    assert snap["projectCount"]==4
    rows={row["name"]:row for row in snap["projects"]}
    assert rows["Disabled"]["monitoringState"]=="disabled_by_project"
    assert rows["Unconfigured"]["monitoringState"]=="misconfigured"
    assert rows["Terminal"]["monitoringState"]=="ineligible_status"
    assert rows["Watched"]["monitoringState"]=="global_control_unverified"
    assert all(row["lastVerifiedVaultWrite"] is None for row in rows.values())
    assert "secret note body" not in json.dumps(snap)
    assert "10-Project/" not in json.dumps(snap)
    root=tmp_path/"private"
    root.mkdir(mode=0o700)
    persist_project_snapshot(root,"a"*64,snap,now=now().timestamp())
    result=read_project_snapshot(root,"a"*64,now=now().timestamp()+30)
    assert result["projectCount"]==4 and result["projectedAt"]==now().isoformat()
    assert (root/"project-watch-cache.json").stat().st_mode & 0o077 == 0


def test_get_refuses_rotated_stale_and_missing_cache(tmp_path):
    mirror, db = setup_source(tmp_path)
    root=tmp_path/"private"
    root.mkdir(mode=0o700)
    with pytest.raises(CacheUnavailable):
        read_project_snapshot(root,"a"*64)
    snap=build_project_snapshot(mirror,db,now=now())
    persist_project_snapshot(root,"a"*64,snap,now=now().timestamp())
    with pytest.raises(CacheUnavailable):
        read_project_snapshot(root,"b"*64,now=now().timestamp())
    with pytest.raises(CacheUnavailable):
        read_project_snapshot(root,None,now=now().timestamp())
    with pytest.raises(CacheUnavailable):
        read_project_snapshot(root,"a"*64,now=now().timestamp()+86401)
    with pytest.raises(CacheUnavailable):
        read_project_snapshot(root,"a"*64,now=now().timestamp()-10)


def test_symlink_and_large_note_refused_without_provider_io(tmp_path):
    mirror, db=setup_source(tmp_path)
    p=mirror/"10-Project"/"Injected.md"
    p.symlink_to(tmp_path/"secret.md")
    with pytest.raises(CacheUnavailable):
        build_project_snapshot(mirror, db, now=now())
    p.unlink()
    p.write_text("x"*65537)
    with pytest.raises(CacheUnavailable):
        build_project_snapshot(mirror, db, now=now())


def test_invalid_watcher_evidence_refused_not_project_success(tmp_path):
    mirror, db=setup_source(tmp_path)
    with sqlite3.connect(db) as c:
        c.execute("UPDATE project_state SET observed_at='not-a-time'")
    with pytest.raises(CacheUnavailable):
        build_project_snapshot(mirror,db,now=now())


def test_no_untrusted_cache_replacement_or_insecure_state(tmp_path):
    mirror, db=setup_source(tmp_path)
    snap=build_project_snapshot(mirror,db,now=now())
    root=tmp_path/"private"
    root.mkdir(mode=0o700)
    target=root/"project-watch-cache.json"
    target.symlink_to(tmp_path/"outside")
    with pytest.raises(CacheUnavailable):
        persist_project_snapshot(root,"a"*64,snap,now=now().timestamp())
    with pytest.raises(CacheUnavailable):
        read_project_snapshot(root,"a"*64,now=now().timestamp())
    target.unlink()
    root.chmod(0o755)
    with pytest.raises(CacheUnavailable):
        persist_project_snapshot(root,"a"*64,snap,now=now().timestamp())


def test_get_http_cache_is_only_local_never_collects_or_writes(tmp_path):
    mirror, db=setup_source(tmp_path)
    # The HTTP GET uses the real wall clock; its synthetic cache must also
    # carry an authentic current projection timestamp, not a stale fixture.
    snap=build_project_snapshot(mirror,db,now=datetime.now(timezone.utc))
    generation="a"*64
    calls=[]
    root=tmp_path/"b3"
    with B3ObserverJob(root, lambda: calls.append("provider_called") or {},
                       configured=lambda: True,generation=lambda:generation) as job:
        # Real-time cache timestamp, so it is fresh at HTTP GET.
        import time
        persist_project_snapshot(root,generation,snap,now=time.time())
        server=make_server(port=0,b3_observer_job=job)
        thread=threading.Thread(target=server.serve_forever,kwargs={"poll_interval":.01})
        thread.start()
        try:
            def get():
                connection=http.client.HTTPConnection("127.0.0.1",server.server_address[1],timeout=4)
                try:
                    connection.request("GET","/api/v1/b3/projects")
                    response=connection.getresponse()
                    return response.status,response.read()
                finally:connection.close()
            code,raw=get()
            assert code==200
            result=json.loads(raw)
            assert result["projectCount"]==4
            assert calls==[]
            assert job.status()["last_run"] is None
            generation="b"*64
            code,raw=get()
            assert code==503
            assert b"cache_unavailable" in raw
            assert calls==[]
        finally:
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()


@pytest.mark.parametrize("corruption", (
    ("top_secret", "password"),
    ("nested_secret", "token"),
    ("writer_claim", True),
    ("invalid_history", "not-a-date"),
))
def test_tampered_cache_never_echoes_unknown_or_unverified_data(tmp_path, corruption):
    mirror, db = setup_source(tmp_path)
    snap = build_project_snapshot(mirror, db, now=now())
    root = tmp_path/"private"
    root.mkdir(mode=0o700)
    persist_project_snapshot(root,"a"*64,snap,now=now().timestamp())
    path = root/"project-watch-cache.json"
    raw = json.loads(path.read_text())
    if corruption[0] == "top_secret":
        raw["projectList"]["privateToken"] = corruption[1]
    elif corruption[0] == "nested_secret":
        raw["projectList"]["projects"][0]["privateToken"] = corruption[1]
    elif corruption[0] == "writer_claim":
        raw["projectList"]["projects"][0]["lastVerifiedVaultWrite"] = corruption[1]
    else:
        raw["projectList"]["projects"][0]["lastWatcherObservedAt"] = corruption[1]
    path.write_text(json.dumps(raw))
    path.chmod(0o600)
    with pytest.raises(CacheUnavailable):
        read_project_snapshot(root,"a"*64,now=now().timestamp())


def test_cache_persist_rejects_unknown_fields_before_publication(tmp_path):
    mirror, db = setup_source(tmp_path)
    snap = build_project_snapshot(mirror, db, now=now())
    snap["privateToken"] = "never expose"
    root = tmp_path/"private"
    root.mkdir(mode=0o700)
    with pytest.raises(CacheUnavailable):
        persist_project_snapshot(root,"a"*64,snap,now=now().timestamp())
    assert not (root/"project-watch-cache.json").exists()


@pytest.mark.parametrize("mutated_field,value", (
    ("globalDesiredEnabled", True),
    ("globalDesiredEnabled", False),
    ("globalControlVerified", True),
))
def test_cached_local_projection_cannot_claim_verified_global_authority(
    tmp_path, mutated_field, value,
):
    mirror, watcher_db = setup_source(tmp_path)
    snapshot = build_project_snapshot(mirror, watcher_db, now=now())
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    persist_project_snapshot(root, "a" * 64, snapshot, now=now().timestamp())
    target = root / "project-watch-cache.json"
    poisoned = json.loads(target.read_text())
    poisoned["projectList"][mutated_field] = value
    target.write_text(json.dumps(poisoned))
    target.chmod(0o600)
    with pytest.raises(CacheUnavailable):
        read_project_snapshot(root, "a" * 64, now=now().timestamp())


@pytest.mark.parametrize("corruption", (
    "recently_observed",
    "not_observed",
    "paused_globally",
    "stale",
    "observation_invalid",
    "ineligible_status",
))
def test_tampered_cache_cannot_fake_observed_or_effective_project_state(tmp_path, corruption):
    mirror, watcher_db = setup_source(tmp_path)
    original = build_project_snapshot(mirror, watcher_db, now=now())
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    persist_project_snapshot(root, "a" * 64, original, now=now().timestamp())
    path = root / "project-watch-cache.json"
    data = json.loads(path.read_text())
    target = next(
        p for p in data["projectList"]["projects"] if p["name"] == "Watched"
    )
    target["monitoringState"] = corruption
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    with pytest.raises(CacheUnavailable):
        read_project_snapshot(root, "a" * 64, now=now().timestamp())


def test_tampered_cache_cannot_forge_watcher_timestamp_under_unverified_control(tmp_path):
    mirror, watcher_db = setup_source(tmp_path)
    original = build_project_snapshot(mirror, watcher_db, now=now())
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    persist_project_snapshot(root, "a" * 64, original, now=now().timestamp())
    path = root / "project-watch-cache.json"
    data = json.loads(path.read_text())
    target = next(
        p for p in data["projectList"]["projects"] if p["name"] == "Watched"
    )
    target["lastWatcherObservedAt"] = now().isoformat()
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    with pytest.raises(CacheUnavailable):
        read_project_snapshot(root, "a" * 64, now=now().timestamp())


@pytest.mark.parametrize("seconds", (-1, 601, 3600))
def test_persist_rejects_projection_time_outside_bounded_publication_window(
    tmp_path, seconds,
):
    mirror, watcher_db = setup_source(tmp_path)
    original = build_project_snapshot(mirror, watcher_db, now=now())
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    with pytest.raises(CacheUnavailable):
        persist_project_snapshot(
            root, "a" * 64, original, now=now().timestamp() + seconds,
        )
    assert not (root / "project-watch-cache.json").exists()


@pytest.mark.parametrize("seconds", (1, -601, 3600))
def test_tampered_cache_cannot_forge_projection_age(tmp_path, seconds):
    mirror, watcher_db = setup_source(tmp_path)
    original = build_project_snapshot(mirror, watcher_db, now=now())
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    persist_project_snapshot(root, "a" * 64, original, now=now().timestamp())
    path = root / "project-watch-cache.json"
    data = json.loads(path.read_text())
    data["projectList"]["projectedAt"] = (
        __import__("datetime").datetime.fromtimestamp(
            now().timestamp() + seconds, timezone.utc
        ).isoformat()
    )
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    with pytest.raises(CacheUnavailable):
        read_project_snapshot(root, "a" * 64, now=now().timestamp())



def test_get_rechecks_source_generation_after_bounded_local_cache_read(
    tmp_path, monkeypatch,
):
    import obsidian_automation.admin_dashboard as dashboard
    mirror, watcher_db = setup_source(tmp_path)
    projected = build_project_snapshot(
        mirror, watcher_db, now=datetime.now(timezone.utc),
    )
    root = tmp_path / "b3"
    current = ["a" * 64]
    calls = []
    with B3ObserverJob(
        root,
        lambda: calls.append("provider_call") or {},
        configured=lambda: True,
        generation=lambda: current[0],
    ) as job:
        persist_project_snapshot(root, current[0], projected)
        original = dashboard.read_project_snapshot

        def rotate_after_local_read(*args, **kwargs):
            data = original(*args, **kwargs)
            current[0] = "b" * 64
            return data

        monkeypatch.setattr(
            dashboard, "read_project_snapshot", rotate_after_local_read,
        )
        server = make_server(port=0, b3_observer_job=job)
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": .01},
        )
        thread.start()
        try:
            connection = http.client.HTTPConnection(
                "127.0.0.1", server.server_address[1], timeout=4,
            )
            try:
                connection.request("GET", "/api/v1/b3/projects")
                response = connection.getresponse()
                body = response.read()
                assert response.status == 503
                assert b"cache_unavailable" in body
            finally:
                connection.close()
            assert calls == []
            assert job.status()["last_run"] is None
        finally:
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()
