"""Explicit opt-in nonroot dashboard and embedded scheduler (no installer)."""
from __future__ import annotations

import argparse
from contextlib import nullcontext
import tomllib
from pathlib import Path

from .admin_dashboard import make_server
from .snapshot_adapter import Connections, SnapshotAdapter
from .snapshot_job import SnapshotJob
from .snapshot_setup import SetupStore
from .github_daily_job import DailyJob, DailyStageFailure
from .github_b3_observer_job import B3ObserverJob
from .b3_phase_runtime import B3PhaseRuntime
from .github_b3_watcher_bridge import collect_b3_read_only
from .github_daily_setup import DailySetup, DailySetupError
from .github_daily_runonce import run_once
from .github_daily_deterministic_writer import publish_verified_daily
from .github_project_watcher import GitHubClient
from .vault_format_job import FormatterJob
from .vault_format_source import FormatterSource, FormatterSourceError
from .vault_root_activation import (
    JobRootGuard, RootPinRefused, https_transport_factory,
)
from .vault_bindings import RoleGuard, physical_folder, record_generation, run_verified
from .vault_root_webui import build_vault_root_ui


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
        # B4 reads Project notes (P2-2) and writes the Daily note (P2-3) through
        # the mapping; its Daily body links only to GitHub, not to Project paths.
        "daily": RoleGuard(root_guard, ("project", "daily")),
        "b3": RoleGuard(root_guard, ("project",)),
        "formatter": RoleGuard(root_guard, ("system",)),
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


def guard_formatter_runner(guard, source):
    def guarded():
        try:
            verified = guard()
        except RootPinRefused:
            raise FormatterSourceError("formatter_root_pin_refused") from None
        return run_verified(verified, source)
    return guarded


def main() -> int:
    parser = argparse.ArgumentParser(description="Nonroot snapshot dashboard; opt-in deployment only")
    parser.add_argument("--config", type=Path, help="Legacy provisioned connection file")
    parser.add_argument("--state", type=Path, help="Fresh service-owned state directory")
    parser.add_argument("--port", type=int, default=8370)
    parser.add_argument("--bind", default="127.0.0.1",
                        help="Explicit RFC1918/LAN IPv4 for a trusted private dashboard; "
                             "default is loopback")
    parser.add_argument(
        "--daily-publish", action="store_true",
        help="Explicitly opt into B4 canonical Daily Note delivery after verified Preview; "
             "normal/default service starts remain Preview-only",
    )
    parser.add_argument(
        "--vault-root-ui", action="store_true",
        help="#303: enable the WebUI to confirm the Nextcloud account, preview and "
             "explicitly switch the Vault root (restricted contract); default OFF",
    )
    args = parser.parse_args()
    if args.vault_root_ui and args.state is None:
        parser.error("--vault-root-ui requires the fresh shared --state app")
    if (args.config is None) == (args.state is None):
        parser.error("select exactly one of --config and --state")
    if args.daily_publish and args.state is None:
        parser.error("--daily-publish requires the fresh shared --state app")
    store = None
    if args.state is not None:
        if not args.state.is_absolute():
            parser.error("state must be absolute")
        store = SetupStore(args.state)
        c = store.connections()
        state = args.state
    else:
        with args.config.open("rb") as stream:
            data = tomllib.load(stream)
        if set(data) != {"state", "rclone_remote", "git_remote", "branch", "policy", "rclone_config"}:
            parser.error("expected six provisioned connection keys")
        c = Connections(state=Path(data["state"]), rclone_remote=data["rclone_remote"],
                        git_remote=data["git_remote"], branch=data["branch"],
                        policy=Path(data["policy"]), rclone_config=Path(data["rclone_config"]))
        c.validate()
        state = c.state
    # #303 V1/D: every Job checks the pinned Vault root before it reads or
    # writes. With no activation ever made this allows without provider I/O.
    root_guard = (JobRootGuard(store, transport_factory=https_transport_factory)
                  if store is not None else None)

    def unguarded_runner(run_id):
        current = store.connections() if store else c
        return SnapshotAdapter(current, settle_seconds=10)(run_id) if current else ("failed", "sync", None)

    runner = guard_snapshot_runner(root_guard, unguarded_runner)

    # The fresh one-user app owns both B2 and B4. B4 is opt-in and Off until
    # GitHub credentials and a pull-only local Project mirror are configured.
    daily_state = state / "github-daily" if store else None

    layout = (lambda role: physical_folder(store.state, role)) if store is not None else None
    layout_generation = (lambda: record_generation(store.state)) if store is not None else None
    daily_setup = (DailySetup(daily_state, store, layout=layout,
                              layout_generation=layout_generation)
                   if store is not None else None)

    def unguarded_daily_runner():
        # Still uses the same B2 read-only Nextcloud mirror and GitHub GET
        # collection; only bounded diagnosis of failure stages is new.
        return collect_b4_preview(daily_setup, daily_state)

    guards = job_guards(root_guard) if root_guard is not None else {}
    daily_guard = guards.get("daily")
    project_guard = guards.get("b3")
    system_guard = guards.get("formatter")
    daily_runner = guard_daily_runner(daily_guard, unguarded_daily_runner)

    daily_publisher = make_daily_publisher(daily_setup, daily_state, store,
                                           daily_guard, layout)

    # B2/B4 and Formatter share one timer thread. No Writer is injected:
    # Source-side deployment is always read-only, irrespective of UI state.
    formatter_state = state / "vault-formatter" if store else None
    formatter_source = (FormatterSource(formatter_state, store, layout=layout,
                                        layout_generation=layout_generation)
                        if store else None)

    formatter_runner = (guard_formatter_runner(system_guard, formatter_source)
                        if formatter_source is not None else None)
    formatter_context = (
        FormatterJob(formatter_state, formatter_runner,
                     configured=formatter_source.configured,
                     source_generation=formatter_source.generation)
        if formatter_source is not None else nullcontext(None)
    )

    # The single in-app scheduler ticks B4. Publication requires explicit
    # --daily-publish regardless of whether --bind exposes a private LAN UI.
    daily_context = (
        DailyJob(daily_state, daily_runner, configured=daily_setup.configured,
                 publisher=daily_publisher if args.daily_publish else None)
        if daily_setup is not None else nullcontext(None)
    )

    # B3 uses B4's configured read-only Project source. There is NO B3
    # canonical Writer, Promotion, or publishing callback. OFF by default;
    # its independent observer Job is ticked only by the existing scheduler.
    b3_state = state / "github-b3" if store is not None else None
    b3_context = (
        B3ObserverJob(
            b3_state,
            project_guard.wrap(
                lambda: collect_b3_read_only(daily_setup, state_root=b3_state)),
            configured=daily_setup.configured,
            generation=daily_setup.source_fingerprint,
            phase_generation=daily_setup.phase_source_fingerprint,
            phase_collector=project_guard.wrap(lambda expected: collect_b3_read_only(
                daily_setup, state_root=b3_state,
                expected_generation=expected,
            )),
            source_lock=daily_setup.phase_receipt_guard,
        )
        if daily_setup is not None else nullcontext(None)
    )
    with make_server(port=args.port, bind_address=args.bind,
                     snapshot_setup=store) as server, _job_context(
            state, runner, configured=c is not None,
            connection_generation=store.generation() if store else None) as job, daily_context as daily_job, formatter_context as formatter_job, b3_context as b3_job:
        server.snapshot_job = job
        server.daily_job = daily_job
        server.daily_setup = daily_setup
        server.formatter_job = formatter_job
        server.b3_observer_job = b3_job
        if args.vault_root_ui:
            # Opt-in only. Without the flag the routes stay 404/405.
            server.vault_root_selection = build_vault_root_ui(store, server)
        # Phase scheduling is an independent, durable, OFF-by-default B3
        # observer-only admission. There is deliberately no public control
        # route or UI activation in this source slice.
        phase_job = B3PhaseRuntime(b3_job) if b3_job is not None else None
        hooks = tuple(item.tick for item in (daily_job, formatter_job, b3_job, phase_job)
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
