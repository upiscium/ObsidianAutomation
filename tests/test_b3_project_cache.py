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
                connection=http.client.HTTPConnection("127.0.0.1",server.server_address[1],timeout=10)
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
                "127.0.0.1", server.server_address[1], timeout=10,
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


def test_bounded_directory_enumeration_stops_before_unbounded_sort(
    tmp_path, monkeypatch,
):
    """Even a hypothetical million-entry directory is not fully consumed."""
    import obsidian_automation.b3_project_cache as cache
    mirror, watcher_db = setup_source(tmp_path)
    inspected = []

    class HugeSyntheticDirectory:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            for index in range(1_000_000):
                inspected.append(index)
                yield type("Entry", (), {
                    "path": str(mirror / "10-Project" / f"{index:07d}.md")
                })()

    with monkeypatch.context() as patch:
        patch.setattr(cache.os, "scandir", lambda _root: HugeSyntheticDirectory())
        with pytest.raises(CacheUnavailable):
            build_project_snapshot(mirror, watcher_db, now=now())
    assert len(inspected) == cache._MAX_DIR_ENTRIES + 1


def test_bounded_markdown_candidate_limit_has_exact_boundary(tmp_path):
    import obsidian_automation.b3_project_cache as cache
    mirror, watcher_db = setup_source(tmp_path)
    folder = mirror / "10-Project"
    # setup_source installs five Markdown candidates (four Projects).
    for index in range(cache._MAX_NOTES_SCAN - 5):
        (folder / f"Journal{index:04d}.md").write_text(
            "---\ntype: journal\n---\n"
        )
    assert build_project_snapshot(
        mirror, watcher_db, now=now()
    )["projectCount"] == 4
    # The old rglob("*.md") also counted a note literally named ".md".
    (folder / ".md").write_text("---\ntype: journal\n---\n")
    with pytest.raises(CacheUnavailable):
        build_project_snapshot(mirror, watcher_db, now=now())
    (folder / ".md").unlink()
    (folder / "Overflow.md").write_text("---\ntype: journal\n---\n")
    with pytest.raises(CacheUnavailable):
        build_project_snapshot(mirror, watcher_db, now=now())


def test_bounded_directory_count_and_depth_fail_closed(tmp_path):
    import obsidian_automation.b3_project_cache as cache
    mirror, watcher_db = setup_source(tmp_path)
    folder = mirror / "10-Project"
    deep = folder
    for index in range(cache._MAX_SCAN_DEPTH):
        deep = deep / f"level{index}"
        deep.mkdir()
    (deep / "Valid.md").write_text(
        "---\ntype: project\nstatus: running\ngithub_watch: false\n---\n"
    )
    assert build_project_snapshot(
        mirror, watcher_db, now=now()
    )["projectCount"] == 5
    (deep / "too-deep").mkdir()
    with pytest.raises(CacheUnavailable):
        build_project_snapshot(mirror, watcher_db, now=now())
    (deep / "too-deep").rmdir()

    # Root + the eight nested directories already consume nine slots.
    for index in range(cache._MAX_SCAN_DIRS - cache._MAX_SCAN_DEPTH - 1):
        (folder / f"subdir{index:03d}").mkdir()
    assert build_project_snapshot(
        mirror, watcher_db, now=now()
    )["projectCount"] == 5
    (folder / "OverflowDir").mkdir()
    with pytest.raises(CacheUnavailable):
        build_project_snapshot(mirror, watcher_db, now=now())


def test_total_directory_entries_bounded_even_when_not_markdown(tmp_path):
    import obsidian_automation.b3_project_cache as cache
    mirror, watcher_db = setup_source(tmp_path)
    folder = mirror / "10-Project"
    # Root has five Markdown entries and three added directories.
    per_dir = (cache._MAX_SCAN_ENTRIES - 8) // 3
    assert 3 * per_dir + 8 == cache._MAX_SCAN_ENTRIES
    assert per_dir < cache._MAX_DIR_ENTRIES
    for directory in range(3):
        nested = folder / f"nonnotes{directory}"
        nested.mkdir()
        for index in range(per_dir):
            (nested / f"{index:04d}.txt").touch()
    assert build_project_snapshot(
        mirror, watcher_db, now=now()
    )["projectCount"] == 4
    (nested / "Overflow.txt").touch()
    with pytest.raises(CacheUnavailable):
        build_project_snapshot(mirror, watcher_db, now=now())


@pytest.mark.parametrize("special_name, shadow_name", (
    ("watcher?copy.sqlite3", "watcher"),
    ("watcher#copy.sqlite3", "watcher"),
    ("watcher%23copy.sqlite3", "watcher#copy.sqlite3"),
))
def test_sqlite_readonly_uri_escapes_reserved_path_characters_without_creating_alias(
    tmp_path, special_name, shadow_name,
):
    import obsidian_automation.b3_project_cache as cache
    mirror, original_db = setup_source(tmp_path)
    special = tmp_path / special_name
    original_db.rename(special)
    shadow = tmp_path / shadow_name
    assert not shadow.exists()
    evidence = cache._read_watcher_evidence(
        special, ("10-Project/Watched.md",),
    )
    assert len(evidence) == 1
    assert evidence[0].repository == "upiscium/Repo"
    assert evidence[0].project_path == "10-Project/Watched.md"
    assert not shadow.exists()
    assert not original_db.exists()
    assert build_project_snapshot(mirror, special, now=now())["projectCount"] == 4
    assert not shadow.exists()


@pytest.mark.parametrize("special_name, shadow_name", (
    ("watcher?copy.sqlite3", "watcher"),
    ("watcher#copy.sqlite3", "watcher"),
    ("watcher%23copy.sqlite3", "watcher#copy.sqlite3"),
))
def test_sqlite_readonly_uri_reads_exact_database_not_valid_decoy(
    tmp_path, special_name, shadow_name,
):
    import obsidian_automation.b3_project_cache as cache
    _, original_db = setup_source(tmp_path)
    special = tmp_path / special_name
    original_db.rename(special)
    shadow = tmp_path / shadow_name
    with sqlite3.connect(shadow) as conn:
        conn.execute(
            "CREATE TABLE project_state (project_path TEXT PRIMARY KEY,"
            "repository TEXT, observed_at TEXT)"
        )
        conn.execute(
            "INSERT INTO project_state VALUES (?,?,?)",
            ("10-Project/Decoy.md", "evil/Decoy", "2026-11-08T00:00:00+00:00"),
        )
    evidence = cache._read_watcher_evidence(
        special, ("10-Project/Watched.md",),
    )
    assert [(row.project_path, row.repository) for row in evidence] == [
        ("10-Project/Watched.md", "upiscium/Repo"),
    ]
    with sqlite3.connect(shadow) as conn:
        assert conn.execute("SELECT repository FROM project_state").fetchone() == (
            "evil/Decoy",
        )


def test_per_directory_entry_limit_exact_boundary(tmp_path):
    import obsidian_automation.b3_project_cache as cache
    mirror, watcher_db = setup_source(tmp_path)
    folder = mirror / "10-Project"
    for index in range(cache._MAX_DIR_ENTRIES - 5):
        (folder / f"Ignore{index:04d}.txt").touch()
    assert build_project_snapshot(
        mirror, watcher_db, now=now()
    )["projectCount"] == 4
    (folder / "Overflow.txt").touch()
    with pytest.raises(CacheUnavailable):
        build_project_snapshot(mirror, watcher_db, now=now())


def test_bounded_nested_lexical_order_and_no_symlink_directory_traversal(tmp_path):
    import obsidian_automation.b3_project_cache as cache
    mirror, watcher_db = setup_source(tmp_path)
    folder = mirror / "10-Project"
    nested = folder / "a"
    nested.mkdir()
    (nested / "Inside.md").write_text("---\ntype: journal\n---\n")
    (folder / "a.md").write_text("---\ntype: journal\n---\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "Outside.md").write_text("---\ntype: project\n---\n")
    (folder / "outside-link").symlink_to(outside, target_is_directory=True)
    found = tuple(cache._bounded_markdown_files(folder))
    assert found.index(nested / "Inside.md") < found.index(folder / "a.md")
    assert (outside / "Outside.md") not in found
    assert build_project_snapshot(
        mirror, watcher_db, now=now()
    )["projectCount"] == 4


def test_bounded_scan_rejects_fifo_and_scandir_error(tmp_path, monkeypatch):
    import os
    import obsidian_automation.b3_project_cache as cache
    mirror, watcher_db = setup_source(tmp_path)
    fifo = mirror / "10-Project" / "NotRegular.md"
    os.mkfifo(fifo)
    with pytest.raises(CacheUnavailable):
        build_project_snapshot(mirror, watcher_db, now=now())
    fifo.unlink()
    with monkeypatch.context() as patch:
        def fail_listing(_path):
            raise OSError("synthetic no directory access")
        patch.setattr(cache.os, "scandir", fail_listing)
        with pytest.raises(CacheUnavailable):
            build_project_snapshot(mirror, watcher_db, now=now())


@pytest.mark.parametrize("directory_name", ("Empty.md", ".md"))
def test_markdown_named_directory_rejected_even_when_empty(
    tmp_path, directory_name, monkeypatch,
):
    """Preserve the old rglob('*.md') + fstat fail-closed contract."""
    import obsidian_automation.b3_project_cache as cache
    mirror, db = setup_source(tmp_path)
    root = mirror / "10-Project"
    bad = root / directory_name
    bad.mkdir()
    assert bad in tuple(root.rglob("*.md"))
    original = cache.os.scandir
    called = []

    def tracked(path):
        called.append(str(path))
        if path == bad:
            raise AssertionError("a Markdown-named directory must not be read")
        return original(path)

    with monkeypatch.context() as patch:
        patch.setattr(cache.os, "scandir", tracked)
        with pytest.raises(CacheUnavailable):
            build_project_snapshot(mirror, db, now=now())
    assert str(bad) not in called


def test_nested_markdown_named_directory_rejected_without_partial_success(
    tmp_path,
):
    mirror, db = setup_source(tmp_path)
    parent = mirror / "10-Project" / "nested"
    parent.mkdir()
    (parent / "ordinary.md").write_text("---\ntype: journal\n---\n")
    assert build_project_snapshot(mirror, db, now=now())["projectCount"] == 4
    bad = parent / "Folder.md"
    bad.mkdir()
    with pytest.raises(CacheUnavailable):
        build_project_snapshot(mirror, db, now=now())
    bad.rmdir()
    assert build_project_snapshot(mirror, db, now=now())["projectCount"] == 4


def test_nonmatching_directory_suffix_still_scanned(tmp_path):
    mirror, db = setup_source(tmp_path)
    folder = mirror / "10-Project" / "Folder.MD"
    folder.mkdir()
    (folder / "Nested.md").write_text(
        "---\ntype: project\nstatus: running\ngithub_watch: false\n---\n"
    )
    snap = build_project_snapshot(mirror, db, now=now())
    assert snap["projectCount"] == 5
    assert any(r["name"] == "Nested" for r in snap["projects"])
