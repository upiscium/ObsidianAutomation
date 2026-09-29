from __future__ import annotations

import os
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .artifact_lifecycle import (
    ArtifactLifecycleError,
    _canonical_json_bytes,
    _decode_json_object,
    _read_exact_file,
    _require_safe_directory,
)


CADENCE_RECORD_VERSION = 1
CADENCE_FILE = "input-planner-cadence.json"
HARD_MINIMUM_INTERVAL_SECONDS = 15 * 60
NORMAL_INTERVAL_SECONDS = 60 * 60
ONE_REVIEW_INTERVAL_SECONDS = 90 * 60
MULTI_REVIEW_INTERVAL_SECONDS = 180 * 60
MAX_IDENTITY_CHARS = 256
MAX_REASON_CHARS = 512


class PlannerCadenceError(ArtifactLifecycleError):
    """Raised when durable Planner cadence state is invalid."""


@dataclass(frozen=True)
class PlannerCadenceState:
    last_submission_at: str | None
    last_selection_policy: str | None
    last_objective_policy: str | None
    last_novelty_skip_at: str | None
    last_novelty_skip_reason: str | None

    def to_json_bytes(self) -> bytes:
        return _canonical_json_bytes(
            {
                "record_version": CADENCE_RECORD_VERSION,
                "last_submission_at": self.last_submission_at,
                "last_selection_policy": self.last_selection_policy,
                "last_objective_policy": self.last_objective_policy,
                "last_novelty_skip_at": self.last_novelty_skip_at,
                "last_novelty_skip_reason": self.last_novelty_skip_reason,
            }
        )


@dataclass(frozen=True)
class PlannerCadenceSnapshot:
    observed_at: str
    eligible: bool
    interval_seconds: int
    reason: str
    awaiting_human_review: int
    last_submission_at: str | None
    next_eligible_at: str
    last_selection_policy: str | None
    last_objective_policy: str | None
    last_novelty_skip_at: str | None
    last_novelty_skip_reason: str | None

    def payload(self) -> dict[str, object]:
        return {
            "eligible": self.eligible,
            "interval_seconds": self.interval_seconds,
            "reason": self.reason,
            "awaiting_human_review": self.awaiting_human_review,
            "last_submission_at": self.last_submission_at,
            "next_eligible_at": self.next_eligible_at,
            "last_selection_policy": self.last_selection_policy,
            "last_objective_policy": self.last_objective_policy,
            "last_novelty_skip_at": self.last_novelty_skip_at,
            "last_novelty_skip_reason": self.last_novelty_skip_reason,
        }


def _orchestration_root(ai_root: Path) -> Path:
    root = ai_root.absolute()
    _require_safe_directory(root, create=False)
    orchestration = root / "02-Orchestration"
    _require_safe_directory(orchestration, create=True)
    return orchestration


def parse_utc_z(value: object, *, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise PlannerCadenceError(f"{label} must be a UTC Z timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise PlannerCadenceError(f"{label} is not a valid timestamp") from exc
    if parsed.tzinfo is None:
        raise PlannerCadenceError(f"{label} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def normalize_now(value: datetime | None = None) -> datetime:
    observed = value or datetime.now(timezone.utc)
    if observed.tzinfo is None:
        raise PlannerCadenceError("Planner cadence clock must be timezone-aware")
    return observed.astimezone(timezone.utc)


def utc_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _optional_identity(value: object, *, label: str) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > MAX_IDENTITY_CHARS
        or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)
    ):
        raise PlannerCadenceError(f"{label} is invalid")
    return value


def _optional_reason(value: object) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > MAX_REASON_CHARS
        or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)
    ):
        raise PlannerCadenceError("novelty skip reason is invalid")
    return value


def parse_cadence_state(data: bytes) -> PlannerCadenceState:
    value = _decode_json_object(data, label="input planner cadence state")
    if set(value) != {
        "record_version",
        "last_submission_at",
        "last_selection_policy",
        "last_objective_policy",
        "last_novelty_skip_at",
        "last_novelty_skip_reason",
    }:
        raise PlannerCadenceError(
            "input planner cadence state properties do not match contract"
        )
    if value["record_version"] != CADENCE_RECORD_VERSION:
        raise PlannerCadenceError("unsupported input planner cadence state version")

    last_submission = value["last_submission_at"]
    if last_submission is not None:
        parse_utc_z(last_submission, label="last_submission_at")
    last_skip_at = value["last_novelty_skip_at"]
    if last_skip_at is not None:
        parse_utc_z(last_skip_at, label="last_novelty_skip_at")
    reason = _optional_reason(value["last_novelty_skip_reason"])
    if (last_skip_at is None) != (reason is None):
        raise PlannerCadenceError(
            "novelty skip timestamp and reason must either both be present or both be null"
        )

    return PlannerCadenceState(
        last_submission_at=last_submission,
        last_selection_policy=_optional_identity(
            value["last_selection_policy"],
            label="last_selection_policy",
        ),
        last_objective_policy=_optional_identity(
            value["last_objective_policy"],
            label="last_objective_policy",
        ),
        last_novelty_skip_at=last_skip_at,
        last_novelty_skip_reason=reason,
    )


def load_cadence_state(ai_root: Path) -> PlannerCadenceState:
    path = _orchestration_root(ai_root) / CADENCE_FILE
    try:
        info = path.lstat()
    except FileNotFoundError:
        return PlannerCadenceState(None, None, None, None, None)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise PlannerCadenceError("input planner cadence state path is unsafe")
    return parse_cadence_state(_read_exact_file(path))


def _store_cadence_state(
    ai_root: Path,
    state: PlannerCadenceState,
) -> Path:
    directory = _orchestration_root(ai_root)
    destination = directory / CADENCE_FILE
    if os.path.lexists(destination) and destination.is_symlink():
        raise PlannerCadenceError("input planner cadence state destination is unsafe")

    data = state.to_json_bytes()
    if parse_cadence_state(data) != state:
        raise PlannerCadenceError("input planner cadence state round-trip mismatch")

    fd, temporary = tempfile.mkstemp(prefix=".input-planner-cadence.", dir=directory)
    temporary_path = Path(temporary)
    try:
        os.fchmod(fd, 0o660)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise PlannerCadenceError(
                    "short write while storing input planner cadence state"
                )
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


def cadence_interval_seconds(awaiting_human_review: int) -> tuple[int, str]:
    if type(awaiting_human_review) is not int or awaiting_human_review < 0:
        raise PlannerCadenceError(
            "awaiting_human_review must be a non-negative integer"
        )
    if awaiting_human_review >= 2:
        configured = MULTI_REVIEW_INTERVAL_SECONDS
        reason = "human_review_backlog_2_plus"
    elif awaiting_human_review == 1:
        configured = ONE_REVIEW_INTERVAL_SECONDS
        reason = "human_review_backlog_1"
    else:
        configured = NORMAL_INTERVAL_SECONDS
        reason = "normal_interval"
    return max(HARD_MINIMUM_INTERVAL_SECONDS, configured), reason


def cadence_snapshot(
    ai_root: Path,
    *,
    awaiting_human_review: int,
    now: datetime | None = None,
) -> PlannerCadenceSnapshot:
    observed = normalize_now(now)
    state = load_cadence_state(ai_root)
    interval, reason = cadence_interval_seconds(awaiting_human_review)

    if state.last_submission_at is None:
        eligible = True
        next_eligible = observed
        reason = "first_submission_ready"
    else:
        submitted = parse_utc_z(
            state.last_submission_at,
            label="last_submission_at",
        )
        if submitted > observed:
            raise PlannerCadenceError("last_submission_at is in the future")
        next_eligible = submitted + timedelta(seconds=interval)
        eligible = observed >= next_eligible

    return PlannerCadenceSnapshot(
        observed_at=utc_z(observed),
        eligible=eligible,
        interval_seconds=interval,
        reason=reason,
        awaiting_human_review=awaiting_human_review,
        last_submission_at=state.last_submission_at,
        next_eligible_at=utc_z(next_eligible),
        last_selection_policy=state.last_selection_policy,
        last_objective_policy=state.last_objective_policy,
        last_novelty_skip_at=state.last_novelty_skip_at,
        last_novelty_skip_reason=state.last_novelty_skip_reason,
    )


def record_submission(
    ai_root: Path,
    *,
    submitted_at: str,
    selection_policy: str,
    objective_policy: str,
) -> PlannerCadenceState:
    submitted = parse_utc_z(submitted_at, label="submitted_at")
    selection = _optional_identity(selection_policy, label="selection_policy")
    objective = _optional_identity(objective_policy, label="objective_policy")
    assert selection is not None
    assert objective is not None

    previous = load_cadence_state(ai_root)
    if previous.last_submission_at is not None:
        existing = parse_utc_z(
            previous.last_submission_at,
            label="last_submission_at",
        )
        if submitted < existing:
            return previous

    state = PlannerCadenceState(
        last_submission_at=utc_z(submitted),
        last_selection_policy=selection,
        last_objective_policy=objective,
        last_novelty_skip_at=previous.last_novelty_skip_at,
        last_novelty_skip_reason=previous.last_novelty_skip_reason,
    )
    _store_cadence_state(ai_root, state)
    return state


def record_novelty_skip(
    ai_root: Path,
    *,
    skipped_at: str,
    reason: str,
) -> PlannerCadenceState:
    skipped = parse_utc_z(skipped_at, label="skipped_at")
    normalized_reason = _optional_reason(reason)
    assert normalized_reason is not None
    previous = load_cadence_state(ai_root)
    state = PlannerCadenceState(
        last_submission_at=previous.last_submission_at,
        last_selection_policy=previous.last_selection_policy,
        last_objective_policy=previous.last_objective_policy,
        last_novelty_skip_at=utc_z(skipped),
        last_novelty_skip_reason=normalized_reason,
    )
    _store_cadence_state(ai_root, state)
    return state
