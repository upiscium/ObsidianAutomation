from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .artifact_lifecycle import ArtifactLifecycleError, _decode_json_object
from .planner_cadence import (
    NORMAL_INTERVAL_SECONDS,
    PlannerCadenceSnapshot,
    cadence_snapshot,
)
from .pre_review_job import _connect_ro

STATUS_RECORD_VERSION = 2
EXTERNAL_STATUS_RECORD_VERSION = 3
LEGACY_STATUS_RECORD_VERSION = 1
STATUS_AUTHORITY = "orchestration_status_projection_only"
DEFAULT_REVIEW_REMINDER_SECONDS = 24 * 60 * 60
DEFAULT_BACKPRESSURE_THRESHOLD = 8
LEGACY_STATES = (
    "queued",
    "generating",
    "validating",
    "building_evaluation_context",
    "evaluating",
    "awaiting_human_review",
    "approved_pending_execution",
    "human_rejected",
    "human_kept_as_idea",
    "completed",
    "retryable_failure",
    "retry_exhausted",
    "blocked",
    "deterministic_reject",
    "superseded",
)
EXTERNAL_STATES = (
    "external_admitted",
    "external_generating",
    "external_validating",
    "external_building_evaluation_context",
    "external_evaluating",
    "external_review_recorded",
)
ALL_STATES = LEGACY_STATES + EXTERNAL_STATES


class PreReviewStatusError(ArtifactLifecycleError):
    """Raised when the limited pre-review status projection is invalid."""


@dataclass(frozen=True)
class PreReviewStatus:
    generated_at: str
    pipeline_health: str
    reasons: tuple[str, ...]
    current_jobs: int
    states: Mapping[str, int]
    oldest_review_wait_seconds: int | None
    review_reminder_due: bool
    review_reminder_seconds: int
    backpressure_active: bool
    backpressure_threshold: int
    planner_cadence: PlannerCadenceSnapshot
    record_version: int = STATUS_RECORD_VERSION
    external_jobs: int = 0
    external_review_waiting: int = 0

    def to_json_bytes(self) -> bytes:
        if self.record_version not in {
            STATUS_RECORD_VERSION, EXTERNAL_STATUS_RECORD_VERSION,
        }:
            raise PreReviewStatusError("unsupported status record_version")
        emitted_states = (
            ALL_STATES if self.record_version == EXTERNAL_STATUS_RECORD_VERSION
            else LEGACY_STATES
        )
        if self.record_version == STATUS_RECORD_VERSION and (
            self.external_jobs or self.external_review_waiting or
            any(self.states.get(name, 0) for name in EXTERNAL_STATES)
        ):
            raise PreReviewStatusError("legacy status cannot encode External jobs")
        data = {
            "record_version": self.record_version,
            "authority": STATUS_AUTHORITY,
            "generated_at": self.generated_at,
            "pipeline_health": self.pipeline_health,
            "reasons": list(self.reasons),
            "current_jobs": self.current_jobs,
            "states": {name: self.states[name] for name in emitted_states},
            "review_wait": {
                "count": self.states["awaiting_human_review"],
                "oldest_age_seconds": self.oldest_review_wait_seconds,
                "reminder_after_seconds": self.review_reminder_seconds,
                "reminder_due": self.review_reminder_due,
            },
            "backpressure": {
                "threshold": self.backpressure_threshold,
                "active": self.backpressure_active,
            },
            "planner_cadence": self.planner_cadence.payload(),
        }
        if self.record_version == EXTERNAL_STATUS_RECORD_VERSION:
            data["external_source"] = {
                "jobs": self.external_jobs,
                "review_waiting": self.external_review_waiting,
            }
        return (
            json.dumps(
                data, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ) + "\n"
        ).encode("utf-8")


def _parse_timestamp(value: object, *, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise PreReviewStatusError(f"{label} must be a UTC Z timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise PreReviewStatusError(f"{label} is not a valid timestamp") from exc
    if parsed.tzinfo is None:
        raise PreReviewStatusError(f"{label} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def build_status(
    ai_root: Path,
    *,
    now: datetime | None = None,
    review_reminder_seconds: int = DEFAULT_REVIEW_REMINDER_SECONDS,
    backpressure_threshold: int = DEFAULT_BACKPRESSURE_THRESHOLD,
) -> PreReviewStatus:
    if (
        type(review_reminder_seconds) is not int
        or review_reminder_seconds <= 0
        or type(backpressure_threshold) is not int
        or backpressure_threshold <= 0
    ):
        raise PreReviewStatusError("status thresholds must be positive integers")

    observed_now = now or datetime.now(timezone.utc)
    if observed_now.tzinfo is None:
        raise PreReviewStatusError("status clock must be timezone-aware")
    observed_now = observed_now.astimezone(timezone.utc)

    conn = _connect_ro(ai_root)
    try:
        rows = conn.execute(
            """
            SELECT g.state, g.updated_at,
                   (
                     EXISTS (
                       SELECT 1 FROM stage_outputs s
                       WHERE s.generation_id=g.generation_id
                         AND s.stage IN (
                           'external_generation', 'external_validation',
                           'external_evaluation_context', 'external_evaluation'
                         )
                     ) OR EXISTS (
                       SELECT 1 FROM attempts a
                       WHERE a.generation_id=g.generation_id
                         AND a.stage IN (
                           'external_generation', 'external_validation',
                           'external_evaluation_context', 'external_evaluation'
                         )
                     )
                   ) AS external_stage_selected
            FROM generations g
            WHERE NOT EXISTS (
                SELECT 1
                FROM generations newer
                WHERE newer.job_id = g.job_id
                  AND newer.generation_index > g.generation_index
            )
            ORDER BY g.job_id
            """
        ).fetchall()
    finally:
        conn.close()

    counts = {state: 0 for state in ALL_STATES}
    oldest_review: datetime | None = None
    external_jobs = 0
    external_review_waiting = 0
    for row in rows:
        state = row["state"]
        if state not in counts:
            raise PreReviewStatusError("orchestration database contains unknown state")
        counts[state] += 1
        if state in EXTERNAL_STATES or row["external_stage_selected"] == 1:
            external_jobs += 1
            if state == "awaiting_human_review":
                external_review_waiting += 1
        if state == "awaiting_human_review":
            updated = _parse_timestamp(row["updated_at"], label="generation.updated_at")
            if updated > observed_now:
                raise PreReviewStatusError("generation.updated_at is in the future")
            if oldest_review is None or updated < oldest_review:
                oldest_review = updated

    reasons: list[str] = []
    if counts["blocked"]:
        reasons.append("blocked_generations")
    if counts["retry_exhausted"]:
        reasons.append("retry_exhausted_generations")
    if reasons:
        health = "CRITICAL"
    elif counts["retryable_failure"]:
        health = "WARNING"
        reasons.append("retryable_failures")
    else:
        health = "OK"

    oldest_age: int | None = None
    if oldest_review is not None:
        oldest_age = int((observed_now - oldest_review).total_seconds())

    awaiting = counts["awaiting_human_review"]
    return PreReviewStatus(
        generated_at=observed_now.isoformat().replace("+00:00", "Z"),
        pipeline_health=health,
        reasons=tuple(reasons),
        current_jobs=len(rows),
        states=counts,
        oldest_review_wait_seconds=oldest_age,
        review_reminder_due=(
            oldest_age is not None and oldest_age >= review_reminder_seconds
        ),
        review_reminder_seconds=review_reminder_seconds,
        backpressure_active=awaiting >= backpressure_threshold,
        backpressure_threshold=backpressure_threshold,
        planner_cadence=cadence_snapshot(
            ai_root,
            awaiting_human_review=awaiting,
            now=observed_now,
        ),
        record_version=(
            EXTERNAL_STATUS_RECORD_VERSION if external_jobs
            else STATUS_RECORD_VERSION
        ),
        external_jobs=external_jobs,
        external_review_waiting=external_review_waiting,
    )


def parse_status(data: bytes) -> PreReviewStatus:
    if len(data) > 64 * 1024:
        raise PreReviewStatusError("status projection exceeds maximum size")
    value = _decode_json_object(data, label="pre-review status projection")
    record_version = value.get("record_version")
    legacy_required = {
        "record_version",
        "authority",
        "generated_at",
        "pipeline_health",
        "reasons",
        "current_jobs",
        "states",
        "review_wait",
        "backpressure",
    }
    current_required = legacy_required | {"planner_cadence"}
    external_required = current_required | {"external_source"}
    if type(record_version) is not int:
        raise PreReviewStatusError("unsupported status record_version")
    if record_version == LEGACY_STATUS_RECORD_VERSION:
        if set(value) != legacy_required:
            raise PreReviewStatusError(
                "legacy status projection properties do not match contract"
            )
    elif record_version == STATUS_RECORD_VERSION:
        if set(value) != current_required:
            raise PreReviewStatusError(
                "status projection properties do not match contract"
            )
    elif record_version == EXTERNAL_STATUS_RECORD_VERSION:
        if set(value) != external_required:
            raise PreReviewStatusError(
                "external status projection properties do not match contract"
            )
    else:
        raise PreReviewStatusError("unsupported status record_version")
    if value["authority"] != STATUS_AUTHORITY:
        raise PreReviewStatusError("status projection authority marker is invalid")
    generated = value["generated_at"]
    _parse_timestamp(generated, label="generated_at")

    health = value["pipeline_health"]
    if health not in {"OK", "WARNING", "CRITICAL"}:
        raise PreReviewStatusError("pipeline_health is invalid")
    reasons = value["reasons"]
    if not isinstance(reasons, list) or not all(
        isinstance(item, str) and item for item in reasons
    ):
        raise PreReviewStatusError("status reasons are invalid")
    current_jobs = value["current_jobs"]
    if type(current_jobs) is not int or current_jobs < 0:
        raise PreReviewStatusError("current_jobs is invalid")

    states = value["states"]
    required_states = (
        ALL_STATES if record_version == EXTERNAL_STATUS_RECORD_VERSION
        else LEGACY_STATES
    )
    if not isinstance(states, dict) or set(states) != set(required_states):
        raise PreReviewStatusError("status states do not match contract")
    normalized_states: dict[str, int] = {name: 0 for name in ALL_STATES}
    for state in required_states:
        count = states[state]
        if type(count) is not int or count < 0:
            raise PreReviewStatusError("status state count is invalid")
        normalized_states[state] = count
    if sum(normalized_states.values()) != current_jobs:
        raise PreReviewStatusError("status state counts do not equal current_jobs")

    external_jobs = 0
    external_review_waiting = 0
    if record_version == EXTERNAL_STATUS_RECORD_VERSION:
        ext = value["external_source"]
        if type(ext) is not dict or set(ext) != {"jobs", "review_waiting"}:
            raise PreReviewStatusError("external status summary properties invalid")
        external_jobs = ext["jobs"]
        external_review_waiting = ext["review_waiting"]
        if (
            type(external_jobs) is not int or
            type(external_review_waiting) is not int or
            not 0 <= external_review_waiting <= external_jobs <= current_jobs or
            external_review_waiting > normalized_states["awaiting_human_review"] or
            external_jobs < sum(normalized_states[name] for name in EXTERNAL_STATES) or
            external_jobs == 0
        ):
            raise PreReviewStatusError("external status summary counts invalid")

    review = value["review_wait"]
    if not isinstance(review, dict) or set(review) != {
        "count",
        "oldest_age_seconds",
        "reminder_after_seconds",
        "reminder_due",
    }:
        raise PreReviewStatusError("review_wait does not match contract")
    if review["count"] != normalized_states["awaiting_human_review"]:
        raise PreReviewStatusError("review_wait count is inconsistent")
    oldest = review["oldest_age_seconds"]
    if oldest is not None and (type(oldest) is not int or oldest < 0):
        raise PreReviewStatusError("review_wait oldest age is invalid")
    reminder_after = review["reminder_after_seconds"]
    if type(reminder_after) is not int or reminder_after <= 0:
        raise PreReviewStatusError("review reminder threshold is invalid")
    if type(review["reminder_due"]) is not bool:
        raise PreReviewStatusError("review reminder_due is invalid")

    backpressure = value["backpressure"]
    if not isinstance(backpressure, dict) or set(backpressure) != {
        "threshold",
        "active",
    }:
        raise PreReviewStatusError("backpressure does not match contract")
    threshold = backpressure["threshold"]
    if type(threshold) is not int or threshold <= 0:
        raise PreReviewStatusError("backpressure threshold is invalid")
    if type(backpressure["active"]) is not bool:
        raise PreReviewStatusError("backpressure active is invalid")

    if record_version == LEGACY_STATUS_RECORD_VERSION:
        cadence = PlannerCadenceSnapshot(
            observed_at=generated,
            eligible=True,
            interval_seconds=NORMAL_INTERVAL_SECONDS,
            reason="legacy_status_no_cadence",
            awaiting_human_review=normalized_states["awaiting_human_review"],
            last_submission_at=None,
            next_eligible_at=generated,
            last_selection_policy=None,
            last_objective_policy=None,
            last_novelty_skip_at=None,
            last_novelty_skip_reason=None,
        )
    else:
        raw_cadence = value["planner_cadence"]
        if not isinstance(raw_cadence, dict) or set(raw_cadence) != {
            "eligible",
            "interval_seconds",
            "reason",
            "awaiting_human_review",
            "last_submission_at",
            "next_eligible_at",
            "last_selection_policy",
            "last_objective_policy",
            "last_novelty_skip_at",
            "last_novelty_skip_reason",
        }:
            raise PreReviewStatusError(
                "planner_cadence does not match contract"
            )
        if type(raw_cadence["eligible"]) is not bool:
            raise PreReviewStatusError("planner cadence eligible is invalid")
        interval = raw_cadence["interval_seconds"]
        if type(interval) is not int or interval <= 0:
            raise PreReviewStatusError("planner cadence interval is invalid")
        cadence_awaiting = raw_cadence["awaiting_human_review"]
        if (
            type(cadence_awaiting) is not int
            or cadence_awaiting < 0
            or cadence_awaiting != normalized_states["awaiting_human_review"]
        ):
            raise PreReviewStatusError(
                "planner cadence Human Review count is inconsistent"
            )
        cadence_reason = raw_cadence["reason"]
        if not isinstance(cadence_reason, str) or not cadence_reason:
            raise PreReviewStatusError("planner cadence reason is invalid")
        next_eligible = raw_cadence["next_eligible_at"]
        _parse_timestamp(next_eligible, label="planner_cadence.next_eligible_at")
        last_submission = raw_cadence["last_submission_at"]
        if last_submission is not None:
            _parse_timestamp(
                last_submission,
                label="planner_cadence.last_submission_at",
            )
        last_skip_at = raw_cadence["last_novelty_skip_at"]
        if last_skip_at is not None:
            _parse_timestamp(
                last_skip_at,
                label="planner_cadence.last_novelty_skip_at",
            )
        for name in (
            "last_selection_policy",
            "last_objective_policy",
            "last_novelty_skip_reason",
        ):
            item = raw_cadence[name]
            if item is not None and (
                not isinstance(item, str) or not item or len(item) > 512
            ):
                raise PreReviewStatusError(
                    f"planner cadence {name} is invalid"
                )
        if (
            (last_skip_at is None)
            != (raw_cadence["last_novelty_skip_reason"] is None)
        ):
            raise PreReviewStatusError(
                "planner cadence novelty skip fields are inconsistent"
            )
        cadence = PlannerCadenceSnapshot(
            observed_at=generated,
            eligible=raw_cadence["eligible"],
            interval_seconds=interval,
            reason=cadence_reason,
            awaiting_human_review=cadence_awaiting,
            last_submission_at=last_submission,
            next_eligible_at=next_eligible,
            last_selection_policy=raw_cadence["last_selection_policy"],
            last_objective_policy=raw_cadence["last_objective_policy"],
            last_novelty_skip_at=last_skip_at,
            last_novelty_skip_reason=raw_cadence["last_novelty_skip_reason"],
        )

    return PreReviewStatus(
        generated_at=generated,
        pipeline_health=health,
        reasons=tuple(reasons),
        current_jobs=current_jobs,
        states=normalized_states,
        oldest_review_wait_seconds=oldest,
        review_reminder_due=review["reminder_due"],
        review_reminder_seconds=reminder_after,
        backpressure_active=backpressure["active"],
        backpressure_threshold=threshold,
        planner_cadence=cadence,
        record_version=(
            EXTERNAL_STATUS_RECORD_VERSION if record_version == EXTERNAL_STATUS_RECORD_VERSION
            else STATUS_RECORD_VERSION
        ),
        external_jobs=external_jobs,
        external_review_waiting=external_review_waiting,
    )


def _require_status_directory(path: Path) -> Path:
    absolute = path.absolute()
    try:
        info = absolute.lstat()
    except FileNotFoundError as exc:
        raise PreReviewStatusError("status projection directory does not exist") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise PreReviewStatusError("status projection directory is unsafe")
    return absolute


def store_status(path: Path, status: PreReviewStatus) -> Path:
    destination = path.absolute()
    directory = _require_status_directory(destination.parent)
    if destination.parent != directory:
        raise PreReviewStatusError("status destination directory mismatch")
    if os.path.lexists(destination) and destination.is_symlink():
        raise PreReviewStatusError("status projection destination must not be a symlink")

    data = status.to_json_bytes()
    if parse_status(data) != status:
        raise PreReviewStatusError("status projection canonical round-trip mismatch")

    fd, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        dir=directory,
    )
    temporary_path = Path(temporary)
    try:
        os.fchmod(fd, 0o640)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise PreReviewStatusError("short write while storing status projection")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)

    try:
        os.replace(temporary_path, destination)
        dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    return destination


def load_status(path: Path) -> PreReviewStatus:
    absolute = path.absolute()
    try:
        info = absolute.lstat()
    except FileNotFoundError as exc:
        raise PreReviewStatusError("status projection does not exist") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise PreReviewStatusError("status projection must be a regular non-symlink file")
    try:
        data = absolute.read_bytes()
    except OSError as exc:
        raise PreReviewStatusError("cannot read status projection") from exc
    return parse_status(data)


def project_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="obsidian-pre-review-status-project")
    parser.add_argument("--ai-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--review-reminder-seconds",
        type=int,
        default=DEFAULT_REVIEW_REMINDER_SECONDS,
    )
    parser.add_argument(
        "--backpressure-threshold",
        type=int,
        default=DEFAULT_BACKPRESSURE_THRESHOLD,
    )
    args = parser.parse_args(argv)

    try:
        status = build_status(
            args.ai_root,
            review_reminder_seconds=args.review_reminder_seconds,
            backpressure_threshold=args.backpressure_threshold,
        )
        path = store_status(args.output, status)
    except (ArtifactLifecycleError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "event": "pre-review-status-projection",
                "status": "updated",
                "pipeline_health": status.pipeline_health,
                "current_jobs": status.current_jobs,
                "path": str(path),
            },
            sort_keys=True,
        )
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="obsidian-pre-review-status")
    parser.add_argument("--status-file", type=Path, required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        status = load_status(args.status_file)
    except (ArtifactLifecycleError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        sys.stdout.buffer.write(status.to_json_bytes())
    else:
        print(f"Pre-review pipeline: {status.pipeline_health}")
        print(f"Current jobs: {status.current_jobs}")
        print(
            "States: "
            + ", ".join(f"{name}={status.states[name]}" for name in ALL_STATES)
        )
        review_age = status.oldest_review_wait_seconds
        print(
            "Oldest Human Review wait: "
            + ("none" if review_age is None else f"{review_age}s")
        )
        print(
            "Review reminder due: "
            + ("yes" if status.review_reminder_due else "no")
        )
        print(
            "Backpressure: "
            + ("active" if status.backpressure_active else "inactive")
        )
        print(
            "Planner cadence: "
            f"{status.planner_cadence.reason}, "
            f"interval={status.planner_cadence.interval_seconds}s, "
            f"eligible={'yes' if status.planner_cadence.eligible else 'no'}"
        )
        print(
            "Next automatic generation: "
            + status.planner_cadence.next_eligible_at
        )
    return {"OK": 0, "WARNING": 1, "CRITICAL": 2}[status.pipeline_health]


if __name__ == "__main__":
    raise SystemExit(main())
