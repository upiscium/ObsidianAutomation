"""Bounded, sanitized read model for the existing AI pre-review status projection.

This module is observation-only. It never reads the job database, provider
configuration, Vault, review artifacts, environment, or production credentials.
"""
from __future__ import annotations

import os
import stat
from datetime import datetime, timezone
from pathlib import Path

from .pre_review_status import PreReviewStatusError, parse_status


AI_STATUS_PATH = Path(
    "/var/lib/obsidian-ai/state/02-Orchestration/status/pre-review-status.json"
)
MAX_STATUS_BYTES = 64 * 1024
DEFAULT_STALE_AFTER_SECONDS = 15 * 60


def _empty(freshness: str = "unavailable") -> dict[str, object]:
    return {
        "freshness": freshness,
        "health": "unknown",
        "generated_at": None,
        "age_seconds": None,
        "review_count": None,
        "operational_failures": None,
        "deterministic_rejections": None,
        "backpressure": None,
        "review_reminder_due": None,
    }


def _read_bounded_nofollow(path: Path) -> bytes:
    """Open every path component with O_NOFOLLOW and read at most 64 KiB.

    The status writer atomically replaces a regular file. Reading through the
    acquired fd produces one consistent version, including across replacement.
    """
    if not path.is_absolute() or ".." in path.parts or len(path.parts) < 2:
        raise ValueError("unsupported status path")
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in path.parts[1:-1]:
            opened = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=directory,
            )
            os.close(directory)
            directory = opened
        leaf = os.open(
            path.name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory,
        )
        try:
            info = os.fstat(leaf)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_size > MAX_STATUS_BYTES
                or stat.S_IMODE(info.st_mode) & 0o022
            ):
                raise ValueError("unsafe status source")
            with os.fdopen(os.dup(leaf), "rb", closefd=True) as stream:
                data = stream.read(MAX_STATUS_BYTES + 1)
            if len(data) > MAX_STATUS_BYTES:
                raise ValueError("oversized status source")
            return data
        finally:
            os.close(leaf)
    finally:
        os.close(directory)


def load_ai_health(
    *,
    path: Path = AI_STATUS_PATH,
    now: datetime | None = None,
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
) -> dict[str, object]:
    """Return only approved counters and freshness, never source JSON or errors."""
    if type(stale_after_seconds) is not int or stale_after_seconds <= 0:
        raise ValueError("stale_after_seconds must be positive")
    current = now if now is not None else datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    current = current.astimezone(timezone.utc)
    try:
        raw = _read_bounded_nofollow(path)
        parsed = parse_status(raw)
    except (OSError, ValueError, UnicodeError, PreReviewStatusError):
        return _empty()
    # parse_status already validated an exact UTC Z timestamp.
    generated = datetime.fromisoformat(parsed.generated_at[:-1] + "+00:00")
    age = int((current - generated).total_seconds())
    if age < -60:
        return _empty("invalid")
    age = max(age, 0)
    if age >= stale_after_seconds:
        return {
            **_empty("stale"),
            "generated_at": parsed.generated_at,
            "age_seconds": age,
        }
    failures = sum(
        parsed.states[name]
        for name in ("retryable_failure", "retry_exhausted", "blocked")
    )
    return {
        "freshness": "fresh",
        "health": parsed.pipeline_health.lower(),
        "generated_at": parsed.generated_at,
        "age_seconds": age,
        "review_count": parsed.states["awaiting_human_review"],
        "operational_failures": failures,
        "deterministic_rejections": parsed.states["deterministic_reject"],
        "backpressure": parsed.backpressure_active,
        "review_reminder_due": parsed.review_reminder_due,
    }
