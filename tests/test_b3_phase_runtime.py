"""Synthetic integration for phase-locked B3 observer: never any provider I/O."""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from obsidian_automation.b3_phase_runtime import B3PhaseRuntime
from obsidian_automation.github_b3_observer_job import B3ObserverJob, Invalid
from obsidian_automation.github_b3_watcher_bridge import VERSION as BRIDGE

JST = ZoneInfo("Asia/Tokyo")


def stamp(hour: int, minute: int = 0):
    return datetime(2026, 11, 8, hour, minute, tzinfo=JST).timestamp()


class Source:
    def __init__(self):
        self.ready = True
        self.generation = "a" * 64
        self.lock = threading.RLock()
        self.calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def configured(self):
        return self.ready

    def fingerprint(self):
        return self.generation

    def collect_bound(self, expected_generation):
        if expected_generation != self.generation:
            raise RuntimeError("synthetic source generation rotated")
        return self.collect()

    def collect(self):
        self.calls += 1
        self.entered.set()
        if not self.release.wait(2):
            raise RuntimeError("synthetic collector timeout")
        return {
            "schema_version": BRIDGE, "state": "observed",
            "reason": "local_read_only_observation", "observations": 9,
            "pending_proposals": 0, "vault_write_authorized": False,
            "cutover_authorized": False,
        }


class Clock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


def make(tmp_path, source, clock):
    job = B3ObserverJob(
        tmp_path / "b3", source.collect,
        configured=source.configured, generation=source.fingerprint,
        clock=clock, phase_generation=source.fingerprint,
        phase_collector=source.collect_bound,
        source_lock=lambda: source.lock,
    )
    return job, B3PhaseRuntime(job)


def configure(phase, *, enabled=True, revision=0, anchor="12:00", interval=3600):
    return phase.configure({
        "enabled": enabled, "timezone": "Asia/Tokyo",
        "anchorLocalTime": anchor, "intervalSeconds": interval,
        "expectedRevision": revision,
    })


def await_terminal(job, run_id):
    for _ in range(150):
        row = job.run(run_id)
        if row["status"] not in ("queued", "running"):
            return row
        time.sleep(.01)
    raise AssertionError("phase worker did not finish")


def test_default_off_startup_does_not_admit_job_or_call_provider(tmp_path):
    source, clock = Source(), Clock(stamp(11, 59))
    with make(tmp_path, source, clock)[0] as job:
        controller = B3PhaseRuntime(job)
        s = controller.status()
        assert s["enabled"] is False and s["effectiveEnabled"] is False
        assert s["nextDueAt"] is None and s["revision"] == 0
        assert s["vaultWriteAuthorized"] is False
        assert controller.tick() is False
        assert job.tick() is False
        assert source.calls == 0
        assert job.status()["last_run"] is None


def test_noon_phase_next_slot_and_only_one_read_only_admission(tmp_path):
    source, clock = Source(), Clock(stamp(11, 59))
    job, phase = make(tmp_path, source, clock)
    with job:
        s = configure(phase)
        assert s["revision"] == 1
        assert datetime.fromisoformat(s["nextDueAt"]).astimezone(JST).strftime("%H:%M") == "12:00"
        assert phase.tick() is False
        clock.value = stamp(12)
        assert phase.tick() is True
        receipt = job.status()["last_run"]
        assert receipt["trigger"] == "scheduled"
        assert await_terminal(job, receipt["run_id"])["status"] == "observed"
        assert phase.tick() is False
        assert source.calls == 1
        assert datetime.fromisoformat(phase.status()["nextDueAt"]).astimezone(JST).strftime("%H:%M") == "13:00"
        with sqlite3.connect(tmp_path/"b3/observer-job.sqlite3") as c:
            assert c.execute("SELECT count(*) FROM phase_slots").fetchone()[0] == 1
            assert c.execute("SELECT count(*) FROM runs").fetchone()[0] == 1


def test_save_at_1237_enters_next_phase_at_1300_not_1337(tmp_path):
    source, clock = Source(), Clock(stamp(12, 37))
    job, phase = make(tmp_path, source, clock)
    with job:
        s = configure(phase)
        assert datetime.fromisoformat(s["nextDueAt"]).astimezone(JST).strftime("%H:%M") == "13:00"
        assert phase.tick() is False
        clock.value = stamp(13)
        assert phase.tick() is True
        assert await_terminal(job, job.status()["last_run"]["run_id"])["status"] == "observed"
        assert source.calls == 1


def test_overdue_slot_is_skipped_no_burst_or_replay(tmp_path):
    source, clock = Source(), Clock(stamp(11, 59))
    job, phase = make(tmp_path, source, clock)
    with job:
        configure(phase)
        clock.value = stamp(12, 30)
        assert phase.tick() is False
        assert source.calls == 0
        assert phase.tick() is False
        assert datetime.fromisoformat(phase.status()["nextDueAt"]).astimezone(JST).strftime("%H:%M") == "13:00"
        with sqlite3.connect(tmp_path/"b3/observer-job.sqlite3") as c:
            assert c.execute("SELECT outcome FROM phase_slots").fetchone()[0] == "skipped_late"


def test_off_during_live_worker_does_not_duplicate_or_cancel(tmp_path):
    source, clock = Source(), Clock(stamp(11, 59))
    source.release.clear()
    job, phase = make(tmp_path, source, clock)
    with job:
        configure(phase)
        clock.value = stamp(12)
        assert phase.tick() is True
        assert source.entered.wait(2)
        assert configure(phase, enabled=False, revision=1)["effectiveEnabled"] is False
        assert phase.tick() is False
        source.release.set()
        assert await_terminal(job, job.status()["last_run"]["run_id"])["status"] == "observed"
        assert source.calls == 1


def test_rotation_revokes_but_transient_outage_pauses(tmp_path):
    source, clock = Source(), Clock(stamp(11, 59))
    job, phase = make(tmp_path, source, clock)
    with job:
        configure(phase)
        source.ready = False
        clock.value = stamp(12)
        assert phase.tick() is False
        assert phase.status()["enabled"] is True
        source.ready = True
        source.generation = "b" * 64
        assert phase.tick() is False
        assert phase.status()["enabled"] is False
        assert phase.status()["revision"] == 2
        assert source.calls == 0


def test_revision_cas_and_old_relative_schedule_conflict(tmp_path):
    source, clock = Source(), Clock(stamp(11, 59))
    job, phase = make(tmp_path, source, clock)
    with job:
        configure(phase)
        with pytest.raises(Invalid, match="stale_b3_phase_revision"):
            configure(phase, revision=0)
        configure(phase, enabled=False, revision=1)
        job.update({"enabled": True, "interval_seconds": 3600})
        with pytest.raises(Invalid, match="b3_relative_schedule_already_enabled"):
            configure(phase, revision=2)
        assert phase.status()["enabled"] is False


def test_restart_preserves_noon_slot_and_revision(tmp_path):
    source, clock = Source(), Clock(stamp(11, 59))
    root = tmp_path / "b3"
    job, phase = make(tmp_path, source, clock)
    with job:
        configure(phase)
        assert phase.status()["revision"] == 1
    job2 = B3ObserverJob(root, source.collect, configured=source.configured,
                         generation=source.fingerprint, clock=clock,
                         phase_generation=source.fingerprint,
                         phase_collector=source.collect_bound,
                         source_lock=lambda: source.lock)
    with job2:
        resumed = B3PhaseRuntime(job2)
        assert resumed.status()["revision"] == 1
        assert resumed.status()["enabled"] is True
        clock.value = stamp(12)
        assert resumed.tick() is True
        assert await_terminal(job2, job2.status()["last_run"]["run_id"])["status"] == "observed"
        assert source.calls == 1


def test_configuring_off_does_not_touch_legacy_schedule(tmp_path):
    source, clock = Source(), Clock(stamp(11, 59))
    job, phase = make(tmp_path, source, clock)
    with job:
        job.update({"enabled": True, "interval_seconds": 3600})
        s = configure(phase, enabled=False)
        assert s["enabled"] is False
        assert job.settings()["enabled"] is True
        assert source.calls == 0


@pytest.mark.parametrize("payload", [
    {"enabled": True, "timezone": "right/Asia/Tokyo", "anchorLocalTime": "12:00", "intervalSeconds": 3600, "expectedRevision": 0},
    {"enabled": 1, "timezone": "Asia/Tokyo", "anchorLocalTime": "12:00", "intervalSeconds": 3600, "expectedRevision": 0},
    {"enabled": True, "timezone": "Asia/Tokyo", "anchorLocalTime": "12:00", "intervalSeconds": 3600, "expectedRevision": True},
    {"enabled": True, "timezone": "Asia/Tokyo", "anchorLocalTime": "12:00", "intervalSeconds": 3600, "expectedRevision": 0, "shell": "arbitrary"},
])
def test_invalid_phase_settings_do_not_modify_db(tmp_path, payload):
    source, clock = Source(), Clock(stamp(11, 59))
    job, phase = make(tmp_path, source, clock)
    with job:
        with pytest.raises(Invalid):
            phase.configure(payload)
        assert phase.status()["revision"] == 0
        assert phase.status()["enabled"] is False


def test_relative_schedule_cannot_be_enabled_after_phase_on(tmp_path):
    source, clock = Source(), Clock(stamp(11, 59))
    job, phase = make(tmp_path, source, clock)
    with job:
        configure(phase)
        with pytest.raises(Invalid, match="b3_phase_schedule_already_enabled"):
            job.update({"enabled": True, "interval_seconds": 3600})
        assert phase.status()["enabled"] is True
        assert job.settings()["enabled"] is False
        assert source.calls == 0



def test_source_rotation_after_slot_admission_before_worker_blocks_collection(tmp_path):
    source, clock = Source(), Clock(stamp(11, 59))
    job, phase = make(tmp_path, source, clock)
    with job:
        configure(phase)
        # Replace only thread launch with a deterministic test barrier.
        # Phase admission and its durable slot/run transaction still execute.
        admitted = []
        job._launch = admitted.append
        clock.value = stamp(12)
        assert phase.tick() is True
        assert len(admitted) == 1
        run_id = admitted[0]
        assert job.run(run_id)["status"] == "queued"
        source.generation = "b" * 64
        job._execute(run_id)
        outcome = job.run(run_id)
        assert outcome["status"] == "unknown"
        assert outcome["reason"] == "source_changed"
        assert outcome["observations"] == 0
        assert source.calls == 0
        with sqlite3.connect(tmp_path/"b3/observer-job.sqlite3") as c:
            assert c.execute("SELECT count(*) FROM phase_slots").fetchone()[0] == 1
            assert c.execute("SELECT count(*) FROM runs").fetchone()[0] == 1


def test_source_rotation_during_phase_collector_never_becomes_verified(tmp_path):
    source, clock = Source(), Clock(stamp(11, 59))
    source.release.clear()
    job, phase = make(tmp_path, source, clock)
    with job:
        configure(phase)
        clock.value = stamp(12)
        assert phase.tick() is True
        assert source.entered.wait(2)
        source.generation = "b" * 64
        source.release.set()
        run_id = job.status()["last_run"]["run_id"]
        outcome = await_terminal(job, run_id)
        assert outcome["status"] == "unknown"
        assert outcome["reason"] == "source_changed"
        assert outcome["observations"] == 0
        assert source.calls == 1
        assert phase.tick() is False
        assert phase.status()["enabled"] is False



def test_rotation_at_terminal_commit_cannot_persist_positive_phase_receipt(tmp_path):
    source, clock = Source(), Clock(stamp(11, 59))
    job, phase = make(tmp_path, source, clock)
    with job:
        configure(phase)
        original_finish = job._finish
        invoked = []

        def rotate_just_before_terminal_commit(run_id, **kwargs):
            invoked.append(run_id)
            with source.lock:
                source.generation = "b" * 64
            return original_finish(run_id, **kwargs)

        job._finish = rotate_just_before_terminal_commit
        clock.value = stamp(12)
        assert phase.tick() is True
        receipt = await_terminal(job, job.status()["last_run"]["run_id"])
        assert len(invoked) == 1
        assert receipt["status"] == "unknown"
        assert receipt["reason"] == "source_changed"
        assert receipt["observations"] == 0
        assert source.calls == 1


def test_phase_collector_requires_source_update_lock(tmp_path):
    source, clock = Source(), Clock(stamp(11, 59))
    with pytest.raises(Invalid, match="invalid_b3_observer_config"):
        B3ObserverJob(
            tmp_path / "b3", source.collect,
            configured=source.configured,
            generation=source.fingerprint, clock=clock,
            phase_generation=source.fingerprint,
            phase_collector=source.collect_bound,
            # Missing source_lock must fail closed instead of allowing
            # a positive receipt to race credential/source updates.
        )



def test_phase_terminal_lock_order_prevents_daily_store_deadlock(tmp_path):
    """The published #564 store->daily inversion is now forbidden.

    Hold the B4 DailySetup lock and start a B3 positive terminal receipt.
    B4 must still be able to acquire the shared SetupStore rotation lock
    while it holds DailySetup, even while the B3 receipt is waiting.
    Neither thread performs Provider I/O or any canonical Vault write.
    """
    from obsidian_automation.github_daily_setup import DailySetup

    b3_lock_attempted = threading.Event()
    b3_lock_order = []

    class ProbedRLock:
        """Record the actual __enter__ acquisition boundary, not a hint."""
        def __init__(self, name):
            self.name = name
            self.lock = threading.RLock()

        def __enter__(self):
            if threading.current_thread().name == "b3-receipt":
                b3_lock_order.append(self.name)
                b3_lock_attempted.set()
            self.lock.acquire()
            return self

        def __exit__(self, *_):
            self.lock.release()

        def acquire(self, *args, **kwargs):
            return self.lock.acquire(*args, **kwargs)

        def release(self):
            return self.lock.release()

    class Shared:
        def __init__(self):
            self._lock = ProbedRLock("shared")

    shared = Shared()
    daily = DailySetup(tmp_path / "daily", shared)
    daily._lock = ProbedRLock("daily")
    b4_has_daily = threading.Event()
    b4_checked_shared = threading.Event()
    b4_may_release = threading.Event()
    result = {}

    def configured():
        with daily._lock:
            with shared._lock:
                return True

    def generation():
        with shared._lock:
            return "a" * 64

    def guard_factory():
        return daily.phase_receipt_guard()

    with B3ObserverJob(
        tmp_path / "b3", lambda: pytest.fail("no remote collector"),
        configured=configured, generation=generation,
        phase_generation=generation,
        phase_collector=lambda _: pytest.fail("no phase collector"),
        source_lock=guard_factory, clock=lambda: 1000.0,
    ) as owner:
        with owner.db:
            run = owner._admit("b" * 32, "scheduled", 1000.0)

        def run_b4_acquisition():
            with daily._lock:
                b4_has_daily.set()
                if not b3_lock_attempted.wait(3):
                    result["b4_error"] = "b3_never_attempted_any_lock"
                    return
                # This acquisition was blocked by the original inversion:
                # B3 held the shared lock while waiting for DailySetup.
                acquired = shared._lock.acquire(timeout=.75)
                result["b4_got_shared"] = acquired
                if acquired:
                    shared._lock.release()
                b4_checked_shared.set()
                # Keep DailySetup held until the assertion phase, ensuring
                # a B3 that took the reverse lock order would be exposed.
                b4_may_release.wait(3)

        def run_b3_terminal():
            result["finished"] = owner._finish(
                run["run_id"], status="observed", reason="synthetic",
                count=1, expected_generation="a" * 64,
            )

        b4 = threading.Thread(target=run_b4_acquisition, daemon=True)
        b3 = threading.Thread(
            target=run_b3_terminal, daemon=True, name="b3-receipt",
        )
        b4.start()
        assert b4_has_daily.wait(2)
        b3.start()
        try:
            assert b4_checked_shared.wait(2), result
            # A reverted Store->Daily guard could pass if the B4 contender
            # wins the Store lock first. Record the actual first B3 lock
            # attempt so that inversion fails deterministically.
            assert b3_lock_order[0] == "daily", b3_lock_order
            assert result.get("b4_got_shared") is True, result
        finally:
            b4_may_release.set()
            b4.join(timeout=3)
            b3.join(timeout=3)
        assert not b4.is_alive() and not b3.is_alive(), "lock inversion"
        assert result["finished"] is True
        assert owner.run(run["run_id"])["status"] == "observed"


def test_phase_receipt_keeps_shared_rotation_lock_through_sqlite_write(tmp_path):
    """A concurrent source mutation cannot interleave an observed COMMIT."""
    from obsidian_automation.github_daily_setup import DailySetup

    class Shared:
        def __init__(self):
            self._lock = threading.RLock()

    shared = Shared()
    daily = DailySetup(tmp_path / "daily", shared)
    source = Source()
    at_commit_boundary = threading.Event()
    release_commit = threading.Event()
    rotator_attempted = threading.Event()
    source_rotated = threading.Event()
    results = {}

    def generation():
        with shared._lock:
            return source.generation

    def configured():
        with daily._lock:
            with shared._lock:
                return source.ready

    with B3ObserverJob(
        tmp_path / "b3", lambda: pytest.fail("no provider I/O"),
        configured=configured, generation=generation,
        phase_generation=generation,
        phase_collector=lambda _: pytest.fail("no provider I/O"),
        source_lock=daily.phase_receipt_guard,
        clock=lambda: 1000.0,
    ) as owner:
        with owner.db:
            row = owner._admit("c" * 32, "scheduled", 1000.0)
        original = owner.db

        class InstrumentedDB:
            def execute(self, statement, args=()):
                return original.execute(statement, args)

            def __enter__(self):
                original.__enter__()
                return self

            def __exit__(self, *args):
                # The transaction's COMMIT is performed by the original
                # sqlite3.Connection.__exit__ below. Pause at that precise
                # boundary, while the positive receipt must still hold
                # its shared source-rotation lock.
                at_commit_boundary.set()
                assert release_commit.wait(3), "test commit release unavailable"
                return original.__exit__(*args)

            def close(self):
                return original.close()

        owner.db = InstrumentedDB()

        def write_receipt():
            results["written"] = owner._finish(
                row["run_id"], status="observed",
                reason="local_read_only_observation", count=1,
                expected_generation="a" * 64,
            )

        def rotate_source():
            rotator_attempted.set()
            with shared._lock:
                source.generation = "b" * 64
                source_rotated.set()

        finish_thread = threading.Thread(target=write_receipt, daemon=True)
        rotate_thread = threading.Thread(target=rotate_source, daemon=True)
        finish_thread.start()
        assert at_commit_boundary.wait(2)
        # This checks the held lock itself instead of relying only on a
        # rotator thread receiving CPU time during a short wait.
        unexpected = shared._lock.acquire(blocking=False)
        if unexpected:
            shared._lock.release()
        rotate_thread.start()
        try:
            assert not unexpected, "shared lock was released before COMMIT"
            assert rotator_attempted.wait(2), "rotator did not start"
            assert not source_rotated.wait(.20), (
                "source rotated inside receipt transaction"
            )
        finally:
            release_commit.set()
            finish_thread.join(timeout=3)
            rotate_thread.join(timeout=3)
        assert not finish_thread.is_alive() and not rotate_thread.is_alive()
        assert results.get("written") is True
        assert source_rotated.is_set()
        assert owner.run(row["run_id"])["status"] == "observed"
        assert source.generation == "b" * 64


def test_source_guard_factory_rejects_context_instance(tmp_path):
    source, clock = Source(), Clock(stamp(11, 59))
    with pytest.raises(Invalid, match="invalid_b3_observer_config"):
        B3ObserverJob(
            tmp_path / "b3", source.collect,
            configured=source.configured, generation=source.fingerprint,
            phase_generation=source.fingerprint,
            phase_collector=source.collect_bound, clock=clock,
            source_lock=source.lock,
        )
