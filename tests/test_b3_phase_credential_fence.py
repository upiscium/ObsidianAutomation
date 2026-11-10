"""B3 phase GitHub credential fencing: fully synthetic, no Provider I/O."""
from __future__ import annotations

import sqlite3
import threading

import pytest

from obsidian_automation.b3_phase_runtime import B3PhaseRuntime
from obsidian_automation.github_b3_observer_job import B3ObserverJob, Invalid
from obsidian_automation.github_b3_watcher_bridge import VERSION as BRIDGE_VERSION
from test_github_daily_setup import TOKEN, make_setup, project_copy
from test_b3_phase_runtime import Clock, stamp

TOKEN_B = "NEW_GITHUB_FIXTURE_TOKEN_9876543210"


def observed() -> dict[str, object]:
    return {
        "schema_version": BRIDGE_VERSION,
        "state": "observed",
        "reason": "local_read_only_observation",
        "observations": 1,
        "pending_proposals": 0,
        "vault_write_authorized": False,
        "cutover_authorized": False,
    }


def setup_phase(tmp_path, collector=None):
    shared, source = make_setup(tmp_path)
    shared._lock = threading.RLock()  # Real SetupStore uses this same RLock.
    assert source.save({"github_token": TOKEN})
    clock = Clock(stamp(11, 59))
    attempts = []

    def fake_bound(expected):
        with source.project_source(expected_generation=expected):
            attempts.append("read_only_synthetic")
        return observed()

    job = B3ObserverJob(
        tmp_path / "b3", lambda: observed(),
        configured=source.configured,
        generation=source.source_fingerprint,  # unchanged B4/legacy binding
        phase_generation=source.phase_source_fingerprint,
        phase_collector=collector or fake_bound,
        source_lock=source.phase_receipt_guard, clock=clock,
    )
    return source, clock, attempts, job, B3PhaseRuntime(job)


def enable_phase(controller):
    return controller.configure({
        "enabled": True,
        "timezone": "Asia/Tokyo",
        "anchorLocalTime": "12:00",
        "intervalSeconds": 3600,
        "expectedRevision": 0,
    })


def admit_without_thread(job, phase, clock):
    admission = []
    job._launch = admission.append  # No thread or Provider side effects.
    clock.value = stamp(12)
    assert phase.tick() is True
    assert len(admission) == 1
    assert job.run(admission[0])["status"] == "queued"
    return admission[0]


def test_phase_missing_distinct_generation_callback_is_rejected(tmp_path):
    source, _, _, job, _ = setup_phase(tmp_path)
    job.close()
    with pytest.raises(Invalid, match="invalid_b3_observer_config"):
        B3ObserverJob(
            tmp_path / "fail-closed", lambda: observed(),
            configured=source.configured,
            generation=source.source_fingerprint,
            phase_collector=lambda _: observed(),
            source_lock=source.phase_receipt_guard,
        )


def test_token_rotation_before_phase_worker_revokes_durable_slot(tmp_path):
    source, clock, attempts, job, phase = setup_phase(tmp_path)
    with job:
        assert enable_phase(phase)["effectiveEnabled"] is True
        b4_generation = source.source_fingerprint()
        phase_generation = source.phase_source_fingerprint()
        run_id = admit_without_thread(job, phase, clock)
        assert source.save({"github_token": TOKEN_B})
        assert source.source_fingerprint() == b4_generation
        assert source.phase_source_fingerprint() != phase_generation

        job._execute(run_id)
        receipt = job.run(run_id)
        assert receipt["status"] == "unknown"
        assert receipt["reason"] == "source_changed"
        assert receipt["observations"] == 0
        assert attempts == []
        assert phase.tick() is False
        assert phase.status()["enabled"] is False
        assert phase.status()["effectiveEnabled"] is False
        assert job.settings()["configured"] is True


def test_token_rotation_during_phase_collection_never_claims_observed(tmp_path):
    holder = {}

    def rotate_during_collection(expected):
        source = holder["source"]
        with source.project_source(expected_generation=expected) as (_, token):
            assert token == TOKEN
            assert source.save({"github_token": TOKEN_B})
        return observed()  # Must be unreachable after source was rotated.

    source, clock, _, job, phase = setup_phase(tmp_path, rotate_during_collection)
    holder["source"] = source
    with job:
        enable_phase(phase)
        run_id = admit_without_thread(job, phase, clock)
        job._execute(run_id)
        receipt = job.run(run_id)
        assert receipt["status"] == "unknown"
        assert receipt["observations"] == 0
        assert receipt["pending_proposals"] == 0
        assert receipt["reason"] != "local_read_only_observation"
        assert phase.tick() is False
        assert phase.status()["enabled"] is False


def test_token_rotation_just_before_terminal_receipt_cannot_commit_success(tmp_path):
    source, clock, attempts, job, phase = setup_phase(tmp_path)
    with job:
        enable_phase(phase)
        run_id = admit_without_thread(job, phase, clock)
        original = job._finish
        invoked = []

        def rotate_before_commit(run_id_, **kwargs):
            invoked.append(run_id_)
            assert source.save({"github_token": TOKEN_B})
            return original(run_id_, **kwargs)

        job._finish = rotate_before_commit
        job._execute(run_id)
        assert invoked == [run_id]
        assert attempts == ["read_only_synthetic"]
        receipt = job.run(run_id)
        assert receipt["status"] == "unknown"
        assert receipt["reason"] == "source_changed"
        assert receipt["observations"] == 0


def test_actual_token_save_waits_until_phase_positive_sqlite_commit(tmp_path):
    source, clock, _, job, phase = setup_phase(tmp_path)
    with job:
        enable_phase(phase)
        previous = source.phase_source_fingerprint()
        assert previous is not None
        with job.db:
            run = job._admit("c" * 32, "scheduled", stamp(12))
        entered_commit = threading.Event()
        release_commit = threading.Event()
        rotate_started = threading.Event()
        rotate_completed = threading.Event()
        outcomes = {}
        original = job.db

        class InstrumentedDB:
            def execute(self, statement, arguments=()):
                return original.execute(statement, arguments)

            def __enter__(self):
                original.__enter__()
                return self

            def __exit__(self, *args):
                entered_commit.set()
                assert release_commit.wait(4), "release SQLite COMMIT fence"
                return original.__exit__(*args)

            def close(self):
                return original.close()

        job.db = InstrumentedDB()

        def finish():
            outcomes["finished"] = job._finish(
                run["run_id"], status="observed",
                reason="local_read_only_observation",
                count=1, expected_generation=previous,
            )

        def rotate():
            rotate_started.set()
            outcomes["rotation"] = source.save({"github_token": TOKEN_B})
            rotate_completed.set()

        t1 = threading.Thread(target=finish, daemon=True)
        t2 = threading.Thread(target=rotate, daemon=True)
        t1.start()
        assert entered_commit.wait(2)
        # Directly prove both required locks are held by the receipt thread
        # through the SQLite COMMIT boundary (not just scheduler timing).
        unexpected_daily = source._lock.acquire(blocking=False)
        if unexpected_daily:
            source._lock.release()
        unexpected_shared = source.shared._lock.acquire(blocking=False)
        if unexpected_shared:
            source.shared._lock.release()
        t2.start()
        try:
            assert not unexpected_daily and not unexpected_shared
            assert rotate_started.wait(2)
            # Save must wait until the actual DB COMMIT has completed.
            assert not rotate_completed.wait(.15)
        finally:
            release_commit.set()
            t1.join(4)
            t2.join(4)
        assert not t1.is_alive() and not t2.is_alive()
        assert outcomes == {"finished": True, "rotation": True}
        assert job.run(run["run_id"])["status"] == "observed"
        assert source.phase_source_fingerprint() != previous


def test_legacy_relative_b3_and_b4_source_binding_unchanged(tmp_path):
    source, clock, _, job, phase = setup_phase(tmp_path)
    with job:
        assert phase.status()["enabled"] is False
        job.update({"enabled": True, "interval_seconds": 3600})
        legacy = source.source_fingerprint()
        before = job.settings()
        assert before["enabled"] is True
        assert before["effective_enabled"] is True
        assert source.save({"github_token": TOKEN_B})
        assert source.source_fingerprint() == legacy
        assert job.settings()["enabled"] is True
        assert job.settings()["effective_enabled"] is True
        with source.project_source() as (config, token):
            assert config.vault_root.is_dir() and token == TOKEN_B
        assert job.status()["last_run"] is None


def test_preexisting_v1_phase_binding_and_queued_slot_cannot_observe(tmp_path):
    """Upgrade: an old Nextcloud-only durable slot is no longer sufficient."""
    source, clock, attempts, job, phase = setup_phase(tmp_path)
    with job:
        enable_phase(phase)
        run_id = admit_without_thread(job, phase, clock)
        assert job._phase_generation() != source.source_fingerprint()
        # Reproduce the persisted values of a v1 phase slot on disk, without
        # modifying real historical data or initiating a Provider operation.
        legacy = source.source_fingerprint()
        with job.db:
            job.db.execute(
                "UPDATE phase_settings SET generation=? WHERE id=1", (legacy,),
            )
            job.db.execute(
                "UPDATE phase_slots SET generation=?",
                (legacy,),
            )
        assert phase.status()["enabled"] is True
        assert phase.status()["effectiveEnabled"] is False
        job._execute(run_id)
        receipt = job.run(run_id)
        assert receipt["status"] == "unknown"
        assert receipt["reason"] == "source_changed"
        assert receipt["observations"] == 0
        assert attempts == []
        assert phase.tick() is False
        assert phase.status()["enabled"] is False


def test_preexisting_v1_queued_slot_is_never_replayed_on_restart(tmp_path):
    """A v1 durable admission must not bypass the new phase callback."""
    source, clock, attempts, job, phase = setup_phase(tmp_path)
    with job:
        enable_phase(phase)
        run_id = admit_without_thread(job, phase, clock)
        legacy = source.source_fingerprint()
        with job.db:
            job.db.execute(
                "UPDATE phase_settings SET generation=? WHERE id=1", (legacy,),
            )
            job.db.execute(
                "UPDATE phase_slots SET generation=?", (legacy,),
            )

    restarted = B3ObserverJob(
        tmp_path / "b3", lambda: observed(),
        configured=source.configured,
        generation=source.source_fingerprint,
        phase_generation=source.phase_source_fingerprint,
        phase_collector=lambda expected: pytest.fail("no replay of v1 source"),
        source_lock=source.phase_receipt_guard,
        clock=clock,
    )
    with restarted:
        restored_phase = B3PhaseRuntime(restarted)
        receipt = restarted.run(run_id)
        assert receipt["status"] == "unknown"
        assert receipt["observations"] == 0
        assert restored_phase.status()["enabled"] is True
        assert restored_phase.status()["effectiveEnabled"] is False
        assert restored_phase.tick() is False
        assert restored_phase.status()["enabled"] is False
        assert attempts == []
        with sqlite3.connect(tmp_path / "b3/observer-job.sqlite3") as db:
            assert db.execute("SELECT count(*) FROM runs").fetchone() == (1,)
            assert db.execute("SELECT count(*) FROM phase_slots").fetchone() == (1,)
