from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from typing import Sequence

from obsidian_automation import vault_snapshot


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _format_metrics(
    *,
    result: str,
    started_at: datetime,
    finished_at: datetime,
    duration_seconds: float,
    changed: bool | None,
    dry_run: bool,
    commit_sha: str | None,
    manifest_sha256: str | None,
    change_count: int | None,
) -> str:
    changed_value = "-" if changed is None else str(changed).lower()
    change_count_value = "-" if change_count is None else str(change_count)
    return (
        "Snapshot metrics: "
        f"result={result} "
        f"started_at={_format_timestamp(started_at)} "
        f"finished_at={_format_timestamp(finished_at)} "
        f"duration_seconds={duration_seconds:.3f} "
        f"changed={changed_value} "
        f"dry_run={str(dry_run).lower()} "
        f"commit_sha={commit_sha or '-'} "
        f"manifest_sha256={manifest_sha256 or '-'} "
        f"changes={change_count_value}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = vault_snapshot._parser().parse_args(argv)
    started_at = _utc_now()
    started_monotonic = time.monotonic()

    try:
        result = vault_snapshot.snapshot_vault(
            source=args.source,
            destination=args.destination,
            config_path=args.config,
            commit_message=args.commit_message,
            author_name=args.author_name,
            author_email=args.author_email,
            dry_run=args.dry_run,
        )
    except vault_snapshot.SnapshotError as exc:
        finished_at = _utc_now()
        duration_seconds = time.monotonic() - started_monotonic
        print(f"error: {exc}", file=sys.stderr)
        print(
            _format_metrics(
                result="failure",
                started_at=started_at,
                finished_at=finished_at,
                duration_seconds=duration_seconds,
                changed=None,
                dry_run=args.dry_run,
                commit_sha=None,
                manifest_sha256=None,
                change_count=None,
            ),
            file=sys.stderr,
        )
        return 2

    print(f"Source manifest: {result.manifest_sha256}")
    print(vault_snapshot._format_changes(result.changes))
    if result.commit_sha:
        print(f"Created snapshot commit {result.commit_sha}")
        result_label = "success"
    elif result.changed and args.dry_run:
        print("Dry run; no commit created.")
        result_label = "dry-run"
    else:
        print("No snapshot commit created.")
        result_label = "dry-run" if args.dry_run else "no-op"

    finished_at = _utc_now()
    duration_seconds = time.monotonic() - started_monotonic
    print(
        _format_metrics(
            result=result_label,
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=duration_seconds,
            changed=result.changed,
            dry_run=args.dry_run,
            commit_sha=result.commit_sha,
            manifest_sha256=result.manifest_sha256,
            change_count=len(result.changes),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
