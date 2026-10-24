"""Explicit opt-in nonroot dashboard and embedded scheduler (no installer)."""
from __future__ import annotations

import argparse
import tomllib
from pathlib import Path

from .admin_dashboard import make_server
from .snapshot_adapter import Connections, SnapshotAdapter
from .snapshot_job import SnapshotJob
from .snapshot_setup import SetupStore


def main() -> int:
    parser = argparse.ArgumentParser(description="Nonroot snapshot dashboard; opt-in deployment only")
    parser.add_argument("--config", type=Path, help="Legacy provisioned connection file")
    parser.add_argument("--state", type=Path, help="Fresh service-owned state directory")
    parser.add_argument("--port", type=int, default=8370)
    args = parser.parse_args()
    if (args.config is None) == (args.state is None):
        parser.error("select exactly one of --config and --state")
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
    def runner(run_id):
        current = store.connections() if store else c
        return SnapshotAdapter(current, settle_seconds=10)(run_id) if current else ("failed", "sync", None)

    with make_server(port=args.port, snapshot_setup=store) as server, _job_context(
            state, runner, configured=c is not None,
            connection_generation=store.generation() if store else None) as job:
        server.snapshot_job = job
        job.start_scheduler()
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
