from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from obsidian_automation.planner_cadence import (
    HARD_MINIMUM_INTERVAL_SECONDS,
    MULTI_REVIEW_INTERVAL_SECONDS,
    NORMAL_INTERVAL_SECONDS,
    ONE_REVIEW_INTERVAL_SECONDS,
    PlannerCadenceError,
    cadence_interval_seconds,
    cadence_snapshot,
    load_cadence_state,
    record_novelty_skip,
    record_submission,
)


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "state"
    (root / "02-Orchestration").mkdir(parents=True)
    return root


def _z(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def test_backlog_switches_generation_interval() -> None:
    assert HARD_MINIMUM_INTERVAL_SECONDS == 15 * 60
    assert cadence_interval_seconds(0) == (
        NORMAL_INTERVAL_SECONDS,
        "normal_interval",
    )
    assert cadence_interval_seconds(1) == (
        ONE_REVIEW_INTERVAL_SECONDS,
        "human_review_backlog_1",
    )
    assert cadence_interval_seconds(2) == (
        MULTI_REVIEW_INTERVAL_SECONDS,
        "human_review_backlog_2_plus",
    )
    assert cadence_interval_seconds(20) == (
        MULTI_REVIEW_INTERVAL_SECONDS,
        "human_review_backlog_2_plus",
    )
    assert NORMAL_INTERVAL_SECONDS == 60 * 60
    assert ONE_REVIEW_INTERVAL_SECONDS == 90 * 60
    assert MULTI_REVIEW_INTERVAL_SECONDS == 180 * 60


def test_first_submission_is_ready_and_state_survives_reload(tmp_path: Path) -> None:
    root = _root(tmp_path)
    now = datetime(2026, 9, 29, 1, 0, tzinfo=timezone.utc)

    before = cadence_snapshot(
        root,
        awaiting_human_review=0,
        now=now,
    )
    assert before.eligible is True
    assert before.reason == "first_submission_ready"
    assert before.next_eligible_at == _z(now)

    record_submission(
        root,
        submitted_at=_z(now),
        selection_policy="coverage-shuffle-v0",
        objective_policy="synthesize-v0",
    )

    # Reload from the durable file rather than reusing any process-local state.
    persisted = load_cadence_state(root)
    assert persisted.last_submission_at == _z(now)
    assert persisted.last_selection_policy == "coverage-shuffle-v0"
    assert persisted.last_objective_policy == "synthesize-v0"

    after = cadence_snapshot(
        root,
        awaiting_human_review=0,
        now=now + timedelta(minutes=10),
    )
    assert after.eligible is False
    assert after.interval_seconds == 60 * 60
    assert after.next_eligible_at == _z(now + timedelta(minutes=60))

    ready = cadence_snapshot(
        root,
        awaiting_human_review=0,
        now=now + timedelta(minutes=60),
    )
    assert ready.eligible is True


def test_backlog_recomputes_next_eligible_from_last_submission(tmp_path: Path) -> None:
    root = _root(tmp_path)
    submitted = datetime(2026, 9, 29, 1, 0, tzinfo=timezone.utc)
    record_submission(
        root,
        submitted_at=_z(submitted),
        selection_policy="coverage-shuffle-v0",
        objective_policy="synthesize-v0",
    )

    one = cadence_snapshot(
        root,
        awaiting_human_review=1,
        now=submitted + timedelta(minutes=70),
    )
    assert one.eligible is False
    assert one.reason == "human_review_backlog_1"
    assert one.next_eligible_at == _z(submitted + timedelta(minutes=90))

    two = cadence_snapshot(
        root,
        awaiting_human_review=2,
        now=submitted + timedelta(minutes=100),
    )
    assert two.eligible is False
    assert two.reason == "human_review_backlog_2_plus"
    assert two.next_eligible_at == _z(submitted + timedelta(minutes=180))

    ready = cadence_snapshot(
        root,
        awaiting_human_review=2,
        now=submitted + timedelta(minutes=181),
    )
    assert ready.eligible is True


def test_novelty_skip_observability_does_not_rewrite_submission_clock(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    submitted = datetime(2026, 9, 29, 1, 0, tzinfo=timezone.utc)
    skipped = submitted + timedelta(minutes=65)
    record_submission(
        root,
        submitted_at=_z(submitted),
        selection_policy="coverage-shuffle-v0",
        objective_policy="synthesize-v0",
    )
    record_novelty_skip(
        root,
        skipped_at=_z(skipped),
        reason="insufficient_semantic_novelty",
    )

    state = load_cadence_state(root)
    assert state.last_submission_at == _z(submitted)
    assert state.last_novelty_skip_at == _z(skipped)
    assert state.last_novelty_skip_reason == "insufficient_semantic_novelty"


def test_future_submission_timestamp_fails_closed(tmp_path: Path) -> None:
    root = _root(tmp_path)
    submitted = datetime(2026, 9, 29, 2, 0, tzinfo=timezone.utc)
    record_submission(
        root,
        submitted_at=_z(submitted),
        selection_policy="coverage-shuffle-v0",
        objective_policy="synthesize-v0",
    )

    with pytest.raises(PlannerCadenceError, match="in the future"):
        cadence_snapshot(
            root,
            awaiting_human_review=0,
            now=submitted - timedelta(seconds=1),
        )
