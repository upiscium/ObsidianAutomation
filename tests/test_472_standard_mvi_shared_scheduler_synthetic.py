"""#472 synthetic E2E for the REAL shared Scheduler thread (no Provider I/O).

An OS thread runs SnapshotJob._schedule_loop with the same five tick slots as
snapshot_app: Snapshot.tick followed by Daily, Formatter, B3 and B3-phase.
All sources and persistent SQLite state are disposable; this is not a deployed
LXC, a process-restart test, or real Snapshot/Gitea/Vault acceptance.
"""
from __future__ import annotations

from contextlib import ExitStack
import threading
import time
from pathlib import Path

from obsidian_automation.b3_phase_runtime import B3PhaseRuntime
from obsidian_automation.github_b3_observer_job import B3ObserverJob
from obsidian_automation.github_b3_watcher_bridge import VERSION as BRIDGE_VERSION
from obsidian_automation.github_daily_job import DailyJob
from obsidian_automation.snapshot_job import SnapshotJob
from obsidian_automation.vault_format_job import FormatterJob
from obsidian_automation.vault_formatter import create_plan


class FakeClock:
    def __init__(self):
        self.value = 1000.0
        self.lock = threading.Lock()

    def __call__(self) -> float:
        with self.lock:
            return self.value

    def set(self, value: float) -> None:
        with self.lock:
            assert value >= self.value
            self.value = value


def await_observed(label: str, predicate, *, timeout: float = 7.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("shared_scheduler_did_not_observe_" + label)


def test_shared_scheduler_drives_isolated_synthetic_jobs_and_formatter_off_resume(
    tmp_path: Path,
) -> None:
    """Actual scheduler thread, four Job owners + B3 phase; all fake runners."""
    clock = FakeClock()
    fixture = tmp_path / "synthetic-vault" / "98-System" / "startup.md"
    fixture.parent.mkdir(parents=True)
    original = b"# Introduction\nparagraph\n  \ncontent\n"
    fixture.write_bytes(original)
    calls: dict[str, list] = {
        "snapshot": [], "daily": [], "formatter": [], "b3": [],
    }
    pulse = threading.Event()

    def snapshot_runner(run_id):
        calls["snapshot"].append(run_id)
        return "no_changes", "verify", None

    def daily_preview():
        calls["daily"].append("synthetic-preview")
        return {
            "version": 1, "status": "preview_ready",
            "model_used": False, "publication_authorized": False,
            "vault_modified": False, "date_jst": "2026-11-10",
            "capture_sha256": "a" * 64, "digest_sha256": "b" * 64,
            "original_event_count": 1, "rendered_entity_count": 1,
        }

    def local_format_plan():
        calls["formatter"].append("synthetic-local-plan")
        return create_plan(fixture.parents[1])

    def b3_observation():
        calls["b3"].append("synthetic-readonly")
        return {
            "schema_version": BRIDGE_VERSION, "state": "observed",
            "reason": "local_read_only_observation", "observations": 1,
            "pending_proposals": 0, "vault_write_authorized": False,
            "cutover_authorized": False,
        }

    def heartbeat():
        pulse.set()
        return False

    formatter_root = tmp_path / "formatter-state"
    snapshot_root = tmp_path / "snapshot-state"
    daily_root = tmp_path / "daily-state"
    b3_root = tmp_path / "b3-state"
    with ExitStack() as cleanup:
        formatter = cleanup.enter_context(FormatterJob(
            formatter_root, local_format_plan,
            configured=lambda: True, source_generation=lambda: "a" * 64,
            clock=clock,  # No Writer injected.
        ))
        daily = cleanup.enter_context(DailyJob(
            daily_root, daily_preview,
            configured=lambda: True, clock=clock,  # No Publisher injected.
        ))
        b3 = cleanup.enter_context(B3ObserverJob(
            b3_root, b3_observation,
            configured=lambda: True, generation=lambda: "c" * 64,
            clock=clock,  # Synthetic collector, no GitHub client.
        ))
        phase = B3PhaseRuntime(b3)
        snapshot = SnapshotJob(
            snapshot_root, snapshot_runner, configured=True, clock=clock,
        )
        # ExitStack is LIFO: stop/join the scheduler before closing the other
        # Job DBs and locks, including if an assertion fails.
        cleanup.callback(snapshot.close)
        snapshot.update({
            "enabled": True, "interval_seconds": 60,
            "source_name": "nextcloud", "destination_name": "gitea",
        })
        daily.update({"enabled": True, "interval_seconds": 3600})
        b3.update({"enabled": True, "interval_seconds": 3600})
        assert formatter.settings()["mode"] == "off"
        assert formatter.settings()["write_capable"] is False
        assert phase.status()["enabled"] is False
        assert not any(calls.values())

        # Match snapshot_app's four real tick hooks, then a passive test-only
        # heartbeat. No test calls any Job.tick() directly.
        snapshot.start_scheduler(tick_hooks=(
            daily.tick, formatter.tick, b3.tick, phase.tick, heartbeat,
        ))
        assert snapshot._scheduler is not None
        assert snapshot._scheduler.is_alive()
        assert pulse.wait(5), "actual scheduler thread did not tick"
        assert not any(calls.values())
        assert all(owner.status()["last_run"] is None for owner in
                   (snapshot, daily, formatter, b3))

        formatter.update({"mode": "dry-run", "interval_seconds": 300})
        assert formatter.settings()["next_due"] == 1300.0
        clock.set(4600.0)  # Four actual Job owners are now due.
        await_observed("first_batch", lambda:
            len(calls["snapshot"]) == len(calls["daily"]) ==
            len(calls["formatter"]) == len(calls["b3"]) == 1
            and snapshot.status()["last_run"]["status"] == "no_changes"
            and daily.status()["last_run"]["status"] == "preview_ready"
            and formatter.status()["last_run"]["status"] == "would_change"
            and b3.status()["last_run"]["status"] == "observed")
        first_formatter_run_id = formatter.status()["last_run"]["run_id"]
        first_snapshot_run_id = snapshot.status()["last_run"]["run_id"]
        first_daily_run_id = daily.status()["last_run"]["run_id"]
        first_b3_run_id = b3.status()["last_run"]["run_id"]
        assert formatter.status()["last_run"]["trigger"] == "scheduled"
        assert formatter.status()["last_run"]["run_mode"] == "dry-run"
        assert snapshot.settings()["next_due"] == 4660.0
        assert formatter.settings()["next_due"] == 4900.0
        assert daily.settings()["next_due"] == 8200.0
        assert b3.settings()["next_due"] == 8200.0
        assert phase.status()["enabled"] is False
        assert fixture.read_bytes() == original
        assert daily.status()["publication_enabled"] is False
        assert b3.status()["vault_write_authorized"] is False

        other_settings = (snapshot.settings(), daily.settings(),
                          b3.settings(), phase.status())
        formatter.update({"mode": "off", "interval_seconds": 300})
        assert formatter.settings()["next_due"] is None
        assert (snapshot.settings(), daily.settings(), b3.settings(),
                phase.status()) == other_settings
        clock.set(4660.0)
        await_observed("snapshot_continues_while_formatter_off",
                       lambda: len(calls["snapshot"]) == 2
                       and snapshot.status()["last_run"]["status"] == "no_changes")
        assert len(calls["formatter"]) == 1
        assert len(calls["daily"]) == 1
        assert len(calls["b3"]) == 1
        assert fixture.read_bytes() == original

        # An explicit operator Resume schedules a NEW dry-run, not replay.
        formatter.update({"mode": "dry-run", "interval_seconds": 300})
        assert formatter.settings()["next_due"] == 4960.0
        clock.set(4960.0)
        await_observed("resume_independently", lambda:
            len(calls["snapshot"]) == 3
            and len(calls["formatter"]) == 2
            and snapshot.status()["last_run"]["status"] == "no_changes"
            and formatter.status()["last_run"]["status"] == "would_change")
        second_formatter_run_id = formatter.status()["last_run"]["run_id"]
        assert second_formatter_run_id != first_formatter_run_id
        assert formatter.status()["last_run"]["run_mode"] == "dry-run"
        assert len(calls["daily"]) == len(calls["b3"]) == 1
        assert fixture.read_bytes() == original

        # Off remains local to Formatter; the other real Jobs still tick.
        formatter.update({"mode": "off", "interval_seconds": 300})
        settings_after_off = (daily.settings(), b3.settings(), phase.status())
        clock.set(5300.0)
        await_observed("other_jobs_remain_scheduled",
                       lambda: len(calls["snapshot"]) == 4
                       and snapshot.status()["last_run"]["status"] == "no_changes")
        assert len(calls["formatter"]) == 2
        assert len(calls["daily"]) == len(calls["b3"]) == 1
        assert (daily.settings(), b3.settings(), phase.status()) == settings_after_off
        assert formatter.settings()["mode"] == "off"
        assert snapshot._scheduler.is_alive()
        assert fixture.read_bytes() == original
        assert all(item["status"] == "would_change" for item in (
            formatter.run(first_formatter_run_id),
            formatter.run(second_formatter_run_id),
        ))

    assert snapshot._scheduler is not None and not snapshot._scheduler.is_alive()
    # Closing and reopening all persistent Jobs in this SAME test process is
    # not evidence of a crash, OS reboot, or an actual app process restart.
    with ExitStack() as recovery:
        fmt = recovery.enter_context(FormatterJob(
            formatter_root,
            lambda: (_ for _ in ()).throw(AssertionError("replayed formatter scan")),
            configured=lambda: True, source_generation=lambda: "a" * 64,
            clock=clock,
        ))
        new_daily = recovery.enter_context(DailyJob(
            daily_root,
            lambda: (_ for _ in ()).throw(AssertionError("replayed daily preview")),
            configured=lambda: True, clock=clock,
        ))
        new_b3 = recovery.enter_context(B3ObserverJob(
            b3_root,
            lambda: (_ for _ in ()).throw(AssertionError("replayed B3 collector")),
            configured=lambda: True, generation=lambda: "c" * 64,
            clock=clock,
        ))
        new_snapshot = SnapshotJob(
            snapshot_root,
            lambda _: (_ for _ in ()).throw(AssertionError("replayed snapshot")),
            configured=True, clock=clock,
        )
        recovery.callback(new_snapshot.close)
        assert fmt.settings()["mode"] == "off"
        assert fmt.settings()["write_capable"] is False
        assert fmt.run(first_formatter_run_id)["status"] == "would_change"
        assert fmt.run(second_formatter_run_id)["status"] == "would_change"
        assert new_snapshot.run(first_snapshot_run_id)["status"] == "no_changes"
        assert new_daily.run(first_daily_run_id)["status"] == "preview_ready"
        assert new_b3.run(first_b3_run_id)["status"] == "observed"
        assert new_snapshot._scheduler is None  # no implicit re-activation
        assert len(calls["snapshot"]) == 4
        assert len(calls["daily"]) == len(calls["b3"]) == 1
        assert len(calls["formatter"]) == 2
        assert fixture.read_bytes() == original
