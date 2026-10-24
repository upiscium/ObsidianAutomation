"""Explicit opt-in nonroot dashboard and embedded scheduler (no installer)."""
from __future__ import annotations

import argparse
import tomllib
from pathlib import Path

from .admin_dashboard import make_server
from .snapshot_adapter import Connections, SnapshotAdapter
from .snapshot_job import SnapshotJob


def main() -> int:
    parser = argparse.ArgumentParser(description="Nonroot snapshot dashboard; opt-in deployment only")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--port", type=int, default=8370)
    args = parser.parse_args()
    with args.config.open("rb") as stream:
        data = tomllib.load(stream)
    if set(data) != {"state", "rclone_remote", "git_remote", "branch", "policy", "rclone_config"}:
        parser.error("expected six provisioned connection keys")
    c = Connections(state=Path(data["state"]), rclone_remote=data["rclone_remote"],
                    git_remote=data["git_remote"], branch=data["branch"],
                    policy=Path(data["policy"]), rclone_config=Path(data["rclone_config"]))
    adapter = SnapshotAdapter(c, settle_seconds=10)
    with make_server(port=args.port) as server, _job_context(c.state, adapter) as job:
        server.snapshot_job = job
        job.start_scheduler()
        server.serve_forever(poll_interval=0.25)
    return 0


class _job_context:
    def __init__(self, state, runner):
        self.job = SnapshotJob(state, runner)

    def __enter__(self):
        return self.job

    def __exit__(self, *_):
        self.job.close()


if __name__ == "__main__":
    raise SystemExit(main())
