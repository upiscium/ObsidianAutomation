"""Consecutive-failure health of a Job, read from its own ``runs`` table.

A hidden failure is worse than a visible one: the Snapshot froze for 17 hours
without anyone seeing it. Each Job reports how many of its latest finished runs
failed in a row; the panel shows a banner once a per-service threshold is
reached. Counts and times only: no paths, errors or private text.
"""
from __future__ import annotations

import sqlite3
import threading
from typing import Callable, Iterable

# Looking back further than this is pointless: the banner only needs the streak.
LOOKBACK_RUNS = 200


def job_health(
    db: sqlite3.Connection,
    lock: threading.RLock | threading.Lock,
    *,
    service: str,
    success: Iterable[str],
    failure: Iterable[str],
    threshold: int,
    neutral: Iterable[str] = (),
    active: Iterable[str] = ("queued", "running"),
) -> dict:
    """``{service, consecutive_failures, failing_since, last_success, threshold, alert}``.

    Runs still active and ``neutral`` runs (for example "skipped") neither extend
    nor break a streak. A run in any status not listed counts as neither success
    nor failure and also does not break the streak.
    """
    success, failure = frozenset(success), frozenset(failure)
    skip = frozenset(neutral) | frozenset(active)
    if type(threshold) is not int or threshold < 1:
        raise ValueError("threshold must be a positive integer")
    with lock:
        rows = db.execute(
            "SELECT status,created,completed FROM runs ORDER BY created DESC,rowid DESC LIMIT ?",
            (LOOKBACK_RUNS,),
        ).fetchall()
    streak = 0
    failing_since = None
    last_success = None
    for status, created, completed in rows:  # newest first
        if status in skip:
            continue
        if status in success:
            last_success = completed if completed is not None else created
            break  # the first success ends the streak
        if status in failure:
            streak += 1
            failing_since = created
    return {
        "service": service,
        "consecutive_failures": streak,
        "failing_since": failing_since if streak else None,
        "last_success": last_success,
        "threshold": threshold,
        "alert": streak >= threshold,
    }


def aggregate(sources: Iterable[tuple[str, Callable[[], dict] | None]]) -> dict:
    """The health of every enabled service; one broken source never hides the others."""
    services = []
    for name, source in sources:
        if source is None:
            continue
        try:
            services.append(source())
        except Exception:
            services.append({"service": name, "consecutive_failures": 0, "failing_since": None,
                             "last_success": None, "threshold": 1, "alert": False,
                             "unavailable": True})
    return {"version": 1, "services": services,
            "alert": any(item.get("alert") for item in services)}
