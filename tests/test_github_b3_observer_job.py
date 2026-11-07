"""Disposable tests for read-only B3 scheduling; never use real remote I/O."""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from obsidian_automation.github_b3_observer_job import (
    B3ObserverJob, Busy, Invalid, VERSION,
)
from obsidian_automation.github_b3_watcher_bridge import VERSION as BRIDGE_VERSION


class Source:
    def __init__(self):
        self.ready = True
        self.generation = "a" * 64
        self.calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.value = {
            "schema_version": BRIDGE_VERSION,
            "state": "observed",
            "reason": "local_read_only_observation",
            "observations": 9,
            "pending_proposals": 2,
            "vault_write_authorized": False,
            "cutover_authorized": False,
        }

    def configured(self):
        return self.ready

    def fingerprint(self):
        return self.generation

    def collect(self):
        self.calls += 1
        self.entered.set()
        if not self.release.wait(3):
            raise RuntimeError("test collector timeout")
        return dict(self.value)


class Clock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value


def job(root: Path, source: Source, clock: Clock):
    return B3ObserverJob(root, source.collect,
                         configured=source.configured,
                         generation=source.fingerprint,
                         clock=clock)


def await_terminal(owner: B3ObserverJob, run_id: str):
    for _ in range(150):
        row = owner.run(run_id)
        if row["status"] not in ("queued", "running"):
            return row
        time.sleep(.01)
    raise AssertionError("worker did not finish")


def test_default_off_has_zero_provider_calls_and_explicit_read_only_flags(tmp_path):
    source, clock = Source(), Clock()
    root = tmp_path / "b3"
    with job(root, source, clock) as owner:
        settings = owner.settings()
        assert settings["enabled"] is False
        assert settings["effective_enabled"] is False
        assert settings["interval_seconds"] == 3600
        assert settings["configured"] is True
        assert owner.tick() is False
        status = owner.status()
        assert status["schema_version"] == VERSION
        assert status["authority"] == "observation_only"
        assert status["last_run"] is None
        assert status["vault_write_authorized"] is False
        assert status["cutover_authorized"] is False
        assert owner.run("fake") is None
        assert source.calls == 0


def test_manual_run_persists_only_read_only_observation_and_replays_id(tmp_path):
    source, clock = Source(), Clock()
    with job(tmp_path / "b3", source, clock) as owner:
        decision, queued = owner.submit("a" * 32)
        assert decision == "accepted" and queued["trigger"] == "manual"
        outcome = await_terminal(owner, queued["run_id"])
        assert outcome["status"] == "observed"
        assert outcome["observations"] == 9
        assert outcome["pending_proposals"] == 2
        assert outcome["reason"] == "local_read_only_observation"
        assert owner.submit("a" * 32)[0] == "replayed"
        assert source.calls == 1
    with job(tmp_path / "b3", source, clock) as reopened:
        assert reopened.submit("a" * 32)[0] == "replayed"
        assert reopened.status()["last_run"]["status"] == "observed"
        assert source.calls == 1


def test_busy_rejection_does_not_queue_second_effect(tmp_path):
    source, clock = Source(), Clock()
    source.release.clear()
    with job(tmp_path / "b3", source, clock) as owner:
        _, first = owner.submit("a" * 32)
        assert source.entered.wait(2)
        assert owner.submit("b" * 32) == ("busy", None)
        assert owner.submit("a" * 32)[0] == "replayed"
        source.release.set()
        assert await_terminal(owner, first["run_id"])["status"] == "observed"
        assert source.calls == 1


def test_schedule_never_runs_until_explicit_enable_and_due(tmp_path):
    source, clock = Source(), Clock()
    with job(tmp_path / "b3", source, clock) as owner:
        assert owner.update({"enabled": True, "interval_seconds": 3600})[
            "next_due"
        ] == 4600.0
        assert owner.tick() is False
        clock.value = 4600.0
        assert owner.tick() is True
        run_id = owner.status()["last_run"]["run_id"]
        assert await_terminal(owner, run_id)["trigger"] == "scheduled"
        assert source.calls == 1
        assert owner.status()["settings"]["next_due"] == 8200.0
        assert owner.update({"enabled": False, "interval_seconds": 3600})[
            "effective_enabled"
        ] is False
        clock.value = 9000.0
        assert owner.tick() is False
        assert source.calls == 1


def test_source_rotation_invalidates_schedule_without_network_calls(tmp_path):
    source, clock = Source(), Clock()
    with job(tmp_path / "b3", source, clock) as owner:
        owner.update({"enabled": True, "interval_seconds": 3600})
        source.generation = "b" * 64
        state = owner.settings()
        assert state["enabled"] is True
        assert state["effective_enabled"] is False
        assert state["reason"] == "source_changed"
        clock.value = 5000.0
        assert owner.tick() is False
        assert owner.settings()["enabled"] is False
        assert source.calls == 0
        # Re-enable requires a new operator action with fresh binding.
        refreshed = owner.update({"enabled": True, "interval_seconds": 3600})
        assert refreshed["effective_enabled"] is True
        assert refreshed["next_due"] == 8600.0


def test_unconfigured_does_not_enable_or_submit(tmp_path):
    source, clock = Source(), Clock()
    source.ready = False
    with job(tmp_path / "b3", source, clock) as owner:
        assert owner.submit("a" * 32) == ("unconfigured", None)
        with pytest.raises(Invalid):
            owner.update({"enabled": True, "interval_seconds": 3600})
        assert owner.update({"enabled": False, "interval_seconds": 3600})[
            "configured"
        ] is False
        assert source.calls == 0


@pytest.mark.parametrize("bad", [
    {"enabled": "yes", "interval_seconds": 3600},
    {"enabled": True, "interval_seconds": True},
    {"enabled": True, "interval_seconds": 300},
    {"enabled": True, "interval_seconds": 86401},
    {"enabled": True},
    {"enabled": False, "interval_seconds": 3600, "shell": "rm -rf"},
])
def test_invalid_settings_refuse_without_changes(tmp_path, bad):
    source, clock = Source(), Clock()
    with job(tmp_path / "b3", source, clock) as owner:
        with pytest.raises(Invalid):
            owner.update(bad)
        assert owner.settings()["enabled"] is False
        assert source.calls == 0


@pytest.mark.parametrize("request_id", [None, "A"*32, "abc", True, "a"*33])
def test_untrusted_request_ids_refused(tmp_path, request_id):
    source, clock = Source(), Clock()
    with job(tmp_path / "b3", source, clock) as owner:
        with pytest.raises(Invalid):
            owner.submit(request_id)
        assert source.calls == 0


def test_reader_does_not_claim_unknown_or_partial_success(tmp_path):
    source, clock = Source(), Clock()
    source.value["vault_write_authorized"] = True
    with job(tmp_path / "b3", source, clock) as owner:
        _, row = owner.submit("a"*32)
        observed = await_terminal(owner, row["run_id"])
        assert observed["status"] == "unknown"
        assert observed["observations"] == 0
        assert observed["pending_proposals"] == 0
        assert owner.status()["vault_write_authorized"] is False


def test_collector_exception_is_nonsecret_unknown(tmp_path):
    source, clock = Source(), Clock()
    def fail():
        raise RuntimeError("credential: super-secret")
    with B3ObserverJob(tmp_path/"b3", fail,
                       configured=source.configured,
                       generation=source.fingerprint, clock=clock) as owner:
        _, row = owner.submit("a"*32)
        observed = await_terminal(owner, row["run_id"])
        assert observed["status"] == "unknown"
        assert "super-secret" not in str(observed)


def test_cross_owner_is_guarded_by_file_lock(tmp_path):
    source, clock = Source(), Clock()
    with job(tmp_path/"b3", source, clock):
        with pytest.raises(BlockingIOError):
            job(tmp_path/"b3", source, clock)


def test_restart_marks_stale_queued_unknown_and_does_not_replay(tmp_path):
    source, clock = Source(), Clock()
    root = tmp_path/"b3"
    with job(root, source, clock):
        pass
    db = sqlite3.connect(root/"observer-job.sqlite3")
    with db:
        db.execute(
            "INSERT INTO runs (run_id,request_id,trigger,status,created,reason)"
            " VALUES (?,?,?,'queued',?,'pending')",
            ("a"*32, "b"*32, "manual", 900.0)
        )
    db.close()
    with job(root, source, clock) as owner:
        row = owner.run("a"*32)
        assert row["status"] == "unknown"
        assert row["reason"] == "interrupted"
        assert owner.submit("b"*32)[0] == "replayed"
        assert source.calls == 0


def test_private_state_mode_and_symlink_database_refused(tmp_path):
    source, clock = Source(), Clock()
    root = tmp_path/"b3"
    root.mkdir(mode=0o700)
    root.chmod(0o755)
    with pytest.raises(Invalid):
        job(root, source, clock)
    root.chmod(0o700)
    (root/"observer-job.sqlite3").symlink_to(tmp_path/"somewhere")
    with pytest.raises(Invalid):
        job(root, source, clock)
    assert source.calls == 0


def test_read_only_module_exposes_no_canonical_writer_interface():
    module = (Path(__file__).resolve().parents[1]
              / "src/obsidian_automation/github_b3_observer_job.py").read_text()
    assert "webdav_create" not in module
    assert "apply_project_status" not in module
    assert "apply_project_overview" not in module
