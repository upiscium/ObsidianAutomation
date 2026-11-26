import json
import os
import sqlite3
import uuid

import pytest

from obsidian_automation import snapshot_retention as retention
from obsidian_automation.snapshot_job import SnapshotJob
from obsidian_automation.snapshot_retention import (
    apply_prune, main, plan_prune, prune_runs,
)


def _db():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY, request_id TEXT, trigger TEXT, "
               "status TEXT, created REAL, completed REAL, stage TEXT, remote_ref TEXT)")
    return db


def _add(db, state, status, created, *, directory=True):
    run_id = uuid.uuid4().hex
    db.execute("INSERT INTO runs VALUES (?,?,?,?,?,NULL,'verify',NULL)",
               (run_id, uuid.uuid4().hex, "scheduled", status, created))
    if directory:
        path = state / "runs" / run_id
        (path / "mirror").mkdir(parents=True)
        (path / "mirror" / "note.md").write_text("x")
    return run_id


def test_deletes_only_runs_older_than_keep_and_leaves_newer_ones(tmp_path):
    db = _db()
    ids = [_add(db, tmp_path, "failed", float(n)) for n in range(10)]  # created 0..9
    plan = plan_prune(tmp_path, db, 4)
    assert set(plan.delete) == set(ids[:6])  # the six oldest
    assert plan.kept == 4
    result = apply_prune(tmp_path, plan)
    assert (result.deleted, result.failed) == (6, 0)
    assert all((tmp_path / "runs" / i).is_dir() for i in ids[6:])
    assert not any((tmp_path / "runs" / i).exists() for i in ids[:6])


def test_never_deletes_active_unconfirmed_or_the_latest_success(tmp_path):
    db = _db()
    unconfirmed = _add(db, tmp_path, "unconfirmed", 1.0)
    running = _add(db, tmp_path, "running", 2.0)
    queued = _add(db, tmp_path, "queued", 3.0)
    old_success = _add(db, tmp_path, "saved", 4.0)  # the latest success
    old_failed = _add(db, tmp_path, "failed", 5.0)
    older_success = _add(db, tmp_path, "no_changes", 0.5)  # an older success is not protected
    for n in range(10, 14):
        _add(db, tmp_path, "failed", float(n))
    plan = plan_prune(tmp_path, db, 4)
    assert set(plan.delete) == {old_failed, older_success}
    assert plan.protected == 4
    apply_prune(tmp_path, plan)
    for protected in (unconfirmed, running, queued, old_success):
        assert (tmp_path / "runs" / protected).is_dir()


def test_directories_without_a_row_and_odd_entries_are_never_touched(tmp_path):
    db = _db()
    ids = [_add(db, tmp_path, "failed", float(n)) for n in range(6)]
    stray = uuid.uuid4().hex
    (tmp_path / "runs" / stray).mkdir()
    (tmp_path / "runs" / "not-a-run-id").mkdir()
    (tmp_path / "runs" / "keep.me").write_text("x")
    plan = plan_prune(tmp_path, db, 2)
    assert plan.unknown_directories == 1 and plan.ignored_entries == 2
    apply_prune(tmp_path, plan)
    assert (tmp_path / "runs" / stray).is_dir()
    assert (tmp_path / "runs" / "not-a-run-id").is_dir()
    assert (tmp_path / "runs" / "keep.me").is_file()
    assert not any((tmp_path / "runs" / i).exists() for i in ids[:4])


def test_a_row_whose_directory_is_gone_is_counted_not_an_error(tmp_path):
    db = _db()
    _add(db, tmp_path, "failed", 1.0, directory=False)
    for n in range(2, 6):
        _add(db, tmp_path, "failed", float(n))
    plan = plan_prune(tmp_path, db, 4)
    assert plan.delete == () and plan.missing_directories == 1


def test_keep_must_be_a_sane_integer(tmp_path):
    db = _db()
    for bad in (0, 1, -3, True, "5", None):
        with pytest.raises(ValueError):
            plan_prune(tmp_path, db, bad)


def test_a_symlink_or_foreign_entry_is_refused_and_the_others_still_go(tmp_path):
    db = _db()
    victim = tmp_path / "precious"
    victim.mkdir()
    (victim / "data").write_text("must survive")
    linked = _add(db, tmp_path, "failed", 1.0, directory=False)
    (tmp_path / "runs").mkdir(exist_ok=True)
    os.symlink(victim, tmp_path / "runs" / linked)
    plain = _add(db, tmp_path, "failed", 2.0)
    for n in range(3, 6):
        _add(db, tmp_path, "failed", float(n))
    plan = plan_prune(tmp_path, db, 3)
    assert set(plan.delete) == {linked, plain}
    result = apply_prune(tmp_path, plan)
    assert (result.deleted, result.failed) == (1, 1)
    assert (victim / "data").read_text() == "must survive"
    assert (tmp_path / "runs" / linked).is_symlink()
    assert not (tmp_path / "runs" / plain).exists()


def test_one_unremovable_directory_does_not_stop_the_rest(tmp_path, monkeypatch):
    db = _db()
    ids = [_add(db, tmp_path, "failed", float(n)) for n in range(6)]
    real = retention._remove_one

    def flaky(runs_dir, run_id):
        if run_id == ids[1]:
            raise PermissionError("denied")
        real(runs_dir, run_id)

    monkeypatch.setattr(retention, "_remove_one", flaky)
    result = apply_prune(tmp_path, plan_prune(tmp_path, db, 2))
    assert (result.deleted, result.failed) == (3, 1)
    assert result.failures == [f"{ids[1]}:PermissionError"]


def test_cli_is_a_dry_run_unless_apply_is_given(tmp_path, capsys):
    db_path = tmp_path / "snapshot.sqlite3"
    db = sqlite3.connect(db_path)
    db.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY, request_id TEXT, trigger TEXT, "
               "status TEXT, created REAL, completed REAL, stage TEXT, remote_ref TEXT)")
    ids = [_add(db, tmp_path, "failed", float(n)) for n in range(6)]
    db.commit()
    db.close()
    assert main(["--state", str(tmp_path), "--keep", "2", "--list"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "dry-run" and report["would_delete"] == 4
    assert set(report["delete"]) == set(ids[:4])
    assert all((tmp_path / "runs" / i).is_dir() for i in ids)  # nothing was deleted
    assert main(["--state", str(tmp_path), "--keep", "2", "--apply"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "apply" and report["deleted"] == 4 and report["failed"] == 0
    assert not any((tmp_path / "runs" / i).exists() for i in ids[:4])
    with pytest.raises(SystemExit):
        main(["--state", str(tmp_path), "--keep", "1"])
    with pytest.raises(SystemExit):
        main(["--state", "relative", "--keep", "5"])


def test_service_prunes_after_each_run_only_when_opted_in(tmp_path):
    def make_runner(state):
        def runner(run_id):
            (state / "runs" / run_id / "mirror").mkdir(parents=True)
            return "saved", "verify", "a" * 40
        return runner

    def run_three(state, keep):
        job = SnapshotJob(state, make_runner(state), retention_keep=keep)
        try:
            for _ in range(3):
                kind, _receipt = job.submit(uuid.uuid4().hex)
                assert kind == "accepted"
                job._worker.join(5)
        finally:
            job.close()
        return sorted(os.listdir(state / "runs"))

    assert len(run_three(tmp_path / "off", None)) == 3  # default keeps everything
    assert len(run_three(tmp_path / "on", 2)) == 2  # newest two survive
