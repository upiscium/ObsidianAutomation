"""Explicit opt-in nonroot dashboard and embedded scheduler (no installer)."""
from __future__ import annotations

import argparse
from pathlib import Path

from .three_services_server import make_server
from .snapshot_adapter import SnapshotAdapter
from .snapshot_job import SnapshotJob
from .snapshot_setup import SetupStore
from .github_daily_job import DailyJob, DailyStageFailure
from .github_daily_setup import DailySetup, DailySetupError
from .github_daily_runonce import run_once
from .github_daily_deterministic_writer import publish_verified_daily
from .github_project_watcher import GitHubClient
from .core_promotion_job import CorePromotionJob
from .core_promotion_runtime import CorePromotionRuntime
from .vault_root_activation import (
    JobRootGuard, RootPinRefused, https_transport_factory,
)
from .vault_bindings import RoleGuard, physical_folder, record_generation, run_verified
from .vault_root_webui import build_vault_bindings_ui, build_vault_root_ui


# Only trusted static source errors may appear in B4 durable diagnostics.
_B4_SOURCE_ERRORS = {
    "github_daily_not_configured": ("source_admission", "not_configured"),
    "project_mirror_pull_failed": ("source_pull", "pull_failed"),
    "no_watched_github_projects": ("project_selection", "no_watched_projects"),
    "source_connections_changed": ("source_verification", "source_changed"),
}


def collect_b4_preview(daily_setup: DailySetup, daily_state: Path) -> dict:
    """Project mirror plus GitHub Preview, with bounded error stage markers.

    GitHub observations and local Preview/CAS processing are both owned by
    run_once. They cannot reliably be separated here, so the stage name
    intentionally describes both rather than claiming a specific cause.
    """
    try:
        with daily_setup.project_source() as (config, token):
            try:
                client = GitHubClient(
                    token=token, timeout_seconds=config.request_timeout_seconds,
                )
                result = run_once(
                    config, client, cas_root=daily_state / "cas",
                    preview_root=daily_state / "preview",
                )
            except Exception:
                raise DailyStageFailure(
                    "collection_preview", "collection_or_preview_failed",
                ) from None
        return result
    except DailyStageFailure:
        raise
    except DailySetupError as exc:
        reason = exc.args[0] if len(exc.args) == 1 and type(exc.args[0]) is str else None
        stage, code = _B4_SOURCE_ERRORS.get(
            reason, ("source_admission", "source_unavailable"),
        )
        raise DailyStageFailure(stage, code) from None
    except Exception:
        raise DailyStageFailure("source_admission", "source_unavailable") from None


def job_guards(root_guard) -> dict:
    """#584: each Job also pins the role folders it uses (P2-2, P2-3).

    B3 and the Formatter only read on this host. B4 reads Project notes and
    writes the Daily note, both through the mapping; its publisher runs the
    same guard again right before the PUT (``make_daily_publisher``).
    """
    return {
        "daily": RoleGuard(root_guard, ("project", "daily")),
        "promotion": RoleGuard(root_guard, ("system",)),
    }


def make_daily_publisher(daily_setup, daily_state, store, guard, layout):
    """B4 publisher. #584: re-check the root and role pins right before the
    PUT and write to the Daily folder that check verified. A refusal there is
    reported as "nothing sent", not as an unconfirmed outcome."""
    def daily_publisher(preview_receipt):
        if daily_setup is None or not daily_setup.configured():
            raise ValueError("Daily delivery source no longer configured")
        try:
            verified = guard()
        except RootPinRefused:
            raise DailyStageFailure("publication", "vault_root_refused") from None
        return run_verified(verified, lambda: publish_verified_daily(
            preview_receipt, cas_root=daily_state / "cas", shared=store,
            layout=layout,
        ))
    return daily_publisher


def guard_snapshot_runner(guard, runner):
    """#303: refuse a Snapshot run before any Vault read when the pin fails."""
    def guarded(run_id):
        if guard is not None and guard.status() not in ("ok", "legacy_unpinned"):
            return ("failed", "sync", None)
        return runner(run_id)
    return guarded


def guard_daily_runner(guard, runner):
    """B4 Preview and its publication (same run) are both behind this check."""
    def guarded():
        try:
            verified = guard()
        except RootPinRefused:
            raise DailyStageFailure("source_admission", "source_unavailable") from None
        # Only a RoleGuard returns the mapping it checked; a plain root guard
        # returns None and the pull then reads the record itself.
        return run_verified(verified, runner)
    return guarded


def main() -> int:
    parser = argparse.ArgumentParser(description="Nonroot Snapshot / Core Promotion / GitHub Daily manager")
    parser.add_argument("--state", type=Path, required=True,
                        help="Absolute service-owned shared state directory")
    parser.add_argument("--port", type=int, default=8370)
    parser.add_argument("--bind", default="127.0.0.1",
                        help="Explicit RFC1918/LAN IPv4 for a trusted private dashboard; "
                             "default is loopback")
    parser.add_argument(
        "--daily-publish", action="store_true",
        help="Explicitly opt into B4 canonical Daily Note delivery after verified Preview; "
             "normal/default service starts remain Preview-only",
    )
    args = parser.parse_args()
    if not args.state.is_absolute():
        parser.error("state must be absolute")
    state = args.state
    store = SetupStore(state)
    c = store.connections()
    # Every source/Writer action verifies the previously pinned Vault root.
    root_guard = JobRootGuard(store, transport_factory=https_transport_factory)

    def unguarded_runner(run_id):
        current = store.connections()
        return SnapshotAdapter(current, settle_seconds=10)(run_id) if current else ("failed", "sync", None)

    runner = guard_snapshot_runner(root_guard, unguarded_runner)

    # The fresh one-user app owns both B2 and B4. B4 is opt-in and Off until
    # GitHub credentials and a pull-only local Project mirror are configured.
    daily_state = state / "github-daily"

    layout = lambda role: physical_folder(store.state, role)
    layout_generation = lambda: record_generation(store.state)
    daily_setup = DailySetup(daily_state, store, layout=layout,
                             layout_generation=layout_generation)

    def unguarded_daily_runner():
        # Still uses the same B2 read-only Nextcloud mirror and GitHub GET
        # collection; only bounded diagnosis of failure stages is new.
        return collect_b4_preview(daily_setup, daily_state)

    guards = job_guards(root_guard)
    daily_guard = guards.get("daily")
    promotion_guard = guards.get("promotion")
    daily_runner = guard_daily_runner(daily_guard, unguarded_daily_runner)
    daily_publisher = make_daily_publisher(
        daily_setup, daily_state, store, daily_guard, layout,
    )
    # Only these three independent Jobs are scheduled. Retired B3, Formatter,
    # AI and External Import have no active tick hook or public action route.
    daily_context = DailyJob(
        daily_state, daily_runner, configured=daily_setup.configured,
        source_generation=daily_setup.source_fingerprint,
        publisher=daily_publisher if args.daily_publish else None,
    )
    promotion_runtime = CorePromotionRuntime(state, store, promotion_guard)
    promotion_context = CorePromotionJob(
        state / "core-promotion-job", promotion_runtime.run,
        configured=promotion_runtime.configured,
        source_generation=promotion_runtime.source_generation,
    )
    with make_server(
        port=args.port, bind_address=args.bind, snapshot_setup=store,
        minimal_mode=True,
    ) as server, _job_context(
        state, runner, configured=c is not None,
        connection_generation=store.generation(),
    ) as job, daily_context as daily_job, promotion_context as promotion_job:
        server.snapshot_job = job
        server.daily_job = daily_job
        server.daily_setup = daily_setup
        server.core_promotion_job = promotion_job
        server.core_promotion_runtime = promotion_runtime
        # Vault Settings are common configuration, not a fourth Job.
        # Existing verified source/pin/role editors are gated with *all*
        # three Job locks and the Core Promotion effect gate.
        server.vault_root_selection = build_vault_root_ui(store, server)
        server.vault_bindings_editor = build_vault_bindings_ui(store, server)
        hooks = tuple(item.tick for item in (daily_job, promotion_job)
                      if item is not None)
        job.start_scheduler(tick_hooks=hooks)
        server.serve_forever(poll_interval=0.25)
    return 0


class _job_context:
    def __init__(self, state, runner, configured=True, connection_generation=None):
        self.job = SnapshotJob(state, runner, configured=configured, connection_generation=connection_generation)

    def __enter__(self):
        return self.job

    def __exit__(self, *_):
        self.job.close()


if __name__ == "__main__":
    raise SystemExit(main())
