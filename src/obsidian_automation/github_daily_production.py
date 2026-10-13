from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import stat
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo

from .artifact_lifecycle import (
    ArtifactLifecycleError,
    _canonical_json_bytes,
    _read_exact_file,
    _rename_noreplace,
    _require_safe_directory,
    _same_immutable_bytes,
    _write_all,
    sha256_bytes,
)
from .github_daily_activity import (
    GitHubDailyActivityError,
    collect_daily_evidence,
    persist_daily_evidence,
)
from .github_daily_progress import (
    DailyProgressConflict,
    DailyProgressError,
    DailyProgressTargetMissing,
    apply_projection,
    load_grounded_summary,
    load_projection,
    make_projection,
    persist_transport_result,
    parse_transport_result,
    preflight_transport_result,
    store_projection,
)
from .github_daily_summary import (
    GitHubDailySummaryError,
    load_evidence_bundle,
)
from .github_daily_resume import _read_bounded as _read_bounded_stage_ref
from .github_daily_summary_generation import (
    ollama_infer,
    openai_compatible_infer,
)
from .github_daily_summary import (
    SummaryRetryPolicy,
    _admit_retry_policy,
    _exclusive_summary_pipeline_root,
    _run_pipeline_unlocked,
)
from .github_project_watcher import (
    GitHubClient,
    GitHubProjectWatcherError,
    load_config,
)
from .ollama_generator import OllamaProviderError
from .openai_compatible import OpenAICompatibleProviderError
from .production_io import ProductionIOError, canonical_io_lock
from .webdav_create import WebDAVCreateError, _read_password


RECORD_VERSION = 1
CANONICAL_TIMEZONE = "Asia/Tokyo"
DEFAULT_DAILY_ROOT = Path(
    "/var/lib/obsidian-github-pipeline/daily-progress"
)
DEFAULT_PIPELINE_ROOT = Path("/var/lib/obsidian-github-pipeline")
DEFAULT_WATCHER_CONFIG = Path("/etc/obsidian-github-sync/config.toml")
DEFAULT_DAILY_WRITER_PASSWORD = Path(
    "/etc/obsidian-github-daily-writer/webdav-password"
)

SCHEDULE_DIR = "00-Schedule"
EVIDENCE_DIR = "10-Evidence"
SUMMARY_DIR = "20-Summary"
PROJECTION_DIR = "30-Projection"
TRANSPORT_DIR = "40-Transport"

_REF_SUFFIX = ".ref.json"
# Stage refs are fixed-shape metadata records, not arbitrary model outputs.
# A conservative 64-KiB cap bounds all CLI readers and prevents FIFO DoS.
MAX_STAGE_REF_BYTES = 64 * 1024
# Transport receipts are fixed-schema metadata (hashes, status and timestamps)
# rather than full Daily content. Do not let Live Smoke read unbounded or
# blocking payloads from a claimed completed transport result.
MAX_TRANSPORT_RESULT_BYTES = 64 * 1024
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_REV_RE = re.compile(r"^[0-9a-f]{40,64}$")


class DailyProductionError(RuntimeError):
    """Raised when the recurring Daily Progress workflow cannot advance."""


def _canonical_date(value: str) -> str:
    if not isinstance(value, str) or _DATE_RE.fullmatch(value) is None:
        raise DailyProductionError("date must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise DailyProductionError("date must be YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise DailyProductionError("date must be canonical YYYY-MM-DD")
    return value


def previous_jst_date(now: datetime | None = None) -> str:
    zone = ZoneInfo(CANONICAL_TIMEZONE)
    current = now or datetime.now(zone)
    if current.tzinfo is None:
        raise DailyProductionError("scheduler time must include timezone")
    local = current.astimezone(zone)
    return (local.date() - timedelta(days=1)).isoformat()


def _require_dir(path: Path, *, label: str) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise DailyProductionError(f"{label} does not exist") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise DailyProductionError(
            f"{label} must be a non-symlink directory"
        )


def _stage_dir(root: Path, name: str) -> Path:
    _require_dir(root, label="Daily Progress production root")
    path = root / name
    _require_dir(path, label=f"Daily Progress {name}")
    return path


def _ref_path(directory: Path, target_date: str) -> Path:
    return directory / f"{_canonical_date(target_date)}{_REF_SUFFIX}"


def _read_ref(
    path: Path,
    *,
    stage: str,
    target_date: str,
) -> dict[str, object]:
    try:
        # Resume's bounded/no-follow/nonblocking reader refuses FIFO, symlink,
        # hardlink and oversized metadata before parsing any JSON.
        data = _read_bounded_stage_ref(path, max_bytes=MAX_STAGE_REF_BYTES)
    except ArtifactLifecycleError as exc:
        raise DailyProductionError("stage ref cannot be safely read") from exc
    try:
        value = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise DailyProductionError("stage ref is invalid JSON") from exc
    if not isinstance(value, dict):
        raise DailyProductionError("stage ref must be an object")
    if (type(value.get("record_version")) is not int
            or value["record_version"] != RECORD_VERSION):
        raise DailyProductionError("stage ref version is unsupported")
    if value.get("stage") != stage:
        raise DailyProductionError("stage ref kind does not match path")
    if value.get("date") != _canonical_date(target_date):
        raise DailyProductionError("stage ref date does not match path")
    try:
        canonical = _canonical_json_bytes(value)
    except UnicodeError as exc:
        # json.loads accepts escaped lone surrogates, but strict UTF-8
        # canonicalization cannot encode them. Refuse as a managed invalid
        # stage ref rather than leaking a raw UnicodeEncodeError traceback.
        raise DailyProductionError("stage ref is not canonical JSON") from exc
    if canonical != data:
        raise DailyProductionError("stage ref is not canonical JSON")
    return value


def _write_ref(
    path: Path,
    payload: Mapping[str, object],
) -> bytes:
    """Publish a complete immutable stage ref, never an in-progress final file.

    The CAS rename primitive from merged #293 provides no-overwrite semantics,
    but stage refs retain their legacy explicit 0640 mode regardless of the
    invoking shell's umask. Payload bytes remain owner-only until fully fsynced.
    """
    data = _canonical_json_bytes(payload)
    _require_safe_directory(path.parent, create=True)
    parent_flags = (
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        parent_fd = os.open(path.parent, parent_flags)
    except OSError as exc:
        raise DailyProductionError("stage ref parent is not safely readable") from exc
    staged_name = f".obsidian-daily-ref-{secrets.token_hex(16)}.tmp"
    temp_exists = False
    fd: int | None = None
    try:
        try:
            os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if not _same_immutable_bytes(parent_fd, path.name, data):
                raise DailyProductionError(
                    "stage ref already exists with different bytes"
                )
            os.fsync(parent_fd)
            return data

        flags = (
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
        )
        fd = os.open(staged_name, flags, 0o600, dir_fd=parent_fd)
        temp_exists = True
        os.fchmod(fd, 0o600)
        _write_all(fd, data)
        os.fsync(fd)
        # Do not make any payload bytes group-readable before the full fsync.
        os.fchmod(fd, 0o640)
        os.fsync(fd)
        os.close(fd)
        fd = None
        try:
            _rename_noreplace(parent_fd, staged_name, path.name)
        except FileExistsError as exc:
            if not _same_immutable_bytes(parent_fd, path.name, data):
                raise DailyProductionError(
                    "stage ref already exists with different bytes"
                ) from exc
            os.unlink(staged_name, dir_fd=parent_fd)
            temp_exists = False
        else:
            temp_exists = False
        os.fsync(parent_fd)
        return data
    except (OSError, ArtifactLifecycleError) as exc:
        raise DailyProductionError("stage ref atomic publication failed") from exc
    finally:
        try:
            if fd is not None:
                os.close(fd)
            if temp_exists:
                try:
                    os.unlink(staged_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
        finally:
            os.close(parent_fd)


def enqueue_date(
    root: Path,
    target_date: str,
) -> Path:
    target = _canonical_date(target_date)
    schedule = _stage_dir(root, SCHEDULE_DIR)
    path = _ref_path(schedule, target)
    _write_ref(
        path,
        {
            "record_version": RECORD_VERSION,
            "stage": "schedule",
            "date": target,
        },
    )
    return path


def _reaffirm_ref_directory_durable(directory: Path) -> None:
    """Re-commit validated existing ref names before declaring stages complete.

    A prior writer may have atomically published a complete ref then failed
    fsync(parent). Merely observing its pathname on retry does NOT prove its
    directory entry survived a later crash. This retries the missing boundary.
    """
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(directory, flags)
    except OSError as exc:
        raise DailyProductionError("cannot open stage-ref durability boundary") from exc
    try:
        try:
            os.fsync(fd)
        except OSError as exc:
            raise DailyProductionError("existing stage refs are not durable") from exc
    finally:
        os.close(fd)


def scheduled_dates(root: Path) -> tuple[str, ...]:
    schedule = _stage_dir(root, SCHEDULE_DIR)
    rows: list[str] = []
    for path in schedule.glob(f"*{_REF_SUFFIX}"):
        name = path.name[: -len(_REF_SUFFIX)]
        target = _canonical_date(name)
        _read_ref(path, stage="schedule", target_date=target)
        rows.append(target)
    if rows:
        # A previous schedule publication may have committed data but failed
        # the directory fsync. Never enqueue later stages on such a ref until
        # the directory entry is durably revalidated.
        _reaffirm_ref_directory_durable(schedule)
    return tuple(sorted(set(rows)))


def _sha_field(
    value: Mapping[str, object],
    key: str,
) -> str:
    digest = value.get(key)
    if not isinstance(digest, str) or _SHA_RE.fullmatch(digest) is None:
        raise DailyProductionError(f"{key} must be lowercase SHA-256")
    return digest


def _pending_dates(
    root: Path,
    completed_stage: str,
    completed_dir: str,
) -> tuple[str, ...]:
    directory = _stage_dir(root, completed_dir)
    pending: list[str] = []
    found_complete = False
    for target in scheduled_dates(root):
        path = _ref_path(directory, target)
        if not os.path.lexists(path):
            pending.append(target)
            continue
        # Also reject dangling symlink refs rather than treating them as
        # missing and initiating a potentially expensive fresh model call.
        _read_ref(path, stage=completed_stage, target_date=target)
        found_complete = True
    if found_complete:
        _reaffirm_ref_directory_durable(directory)
    return tuple(pending)


def collect_pending(
    *,
    root: Path,
    watcher_config: Path,
) -> tuple[str, ...]:
    evidence_dir = _stage_dir(root, EVIDENCE_DIR)
    config = load_config(watcher_config)
    api = GitHubClient(
        api_base=config.github_api_base,
        token=os.environ.get(config.github_token_env),
        timeout_seconds=config.request_timeout_seconds,
    )
    completed: list[str] = []
    for target in _pending_dates(root, "evidence", EVIDENCE_DIR):
        bundle = collect_daily_evidence(
            config,
            target_date=date.fromisoformat(target),
            client=api,
        )
        artifact = persist_daily_evidence(evidence_dir, bundle)
        if artifact.name != (
            f"{bundle.sha256}.github-daily-evidence.json"
        ):
            raise DailyProductionError("evidence artifact path is invalid")
        _write_ref(
            _ref_path(evidence_dir, target),
            {
                "record_version": RECORD_VERSION,
                "stage": "evidence",
                "date": target,
                "evidence_sha256": bundle.sha256,
            },
        )
        completed.append(target)
    return tuple(completed)


def _evidence_for_date(
    root: Path,
    target: str,
):
    evidence_dir = _stage_dir(root, EVIDENCE_DIR)
    ref = _read_ref(
        _ref_path(evidence_dir, target),
        stage="evidence",
        target_date=target,
    )
    digest = _sha_field(ref, "evidence_sha256")
    path = evidence_dir / f"{digest}.github-daily-evidence.json"
    bundle = load_evidence_bundle(path)
    if bundle.date != target:
        raise DailyProductionError(
            "evidence artifact date does not match stage ref"
        )
    return digest, path, bundle


def _summary_for_date(
    root: Path,
    target: str,
    *,
    bundle,
):
    summary_root = _stage_dir(root, SUMMARY_DIR)
    ref = _read_ref(
        _ref_path(summary_root, target),
        stage="summary",
        target_date=target,
    )
    evidence_sha = _sha_field(ref, "evidence_sha256")
    if evidence_sha != bundle.sha256:
        raise DailyProductionError(
            "summary ref evidence binding mismatch"
        )
    digest = _sha_field(ref, "grounded_summary_sha256")
    path = (
        summary_root
        / "github-daily-summary"
        / "final"
        / f"{digest}.github-daily-grounded-summary.json"
    )
    summary = load_grounded_summary(path, bundle=bundle)
    if summary.sha256 != digest:
        raise DailyProductionError("summary ref artifact mismatch")
    return digest, path, summary


def summarize_pending(
    *,
    root: Path,
    provider: str,
    base_url: str,
    model: str,
    implementation_revision: str,
    timeout: float = 120.0,
    api_key: str | None = None,
    retry_policy: SummaryRetryPolicy | None = None,
) -> tuple[str, ...]:
    if (
        not isinstance(implementation_revision, str)
        or _GIT_REV_RE.fullmatch(implementation_revision) is None
    ):
        raise DailyProductionError(
            "implementation revision must be lowercase 40..64 hex"
        )
    retry_policy = _admit_retry_policy(retry_policy)
    summary_root = _stage_dir(root, SUMMARY_DIR)
    if provider == "ollama":
        infer = ollama_infer(
            base_url=base_url,
            model=model,
            timeout=timeout,
        )
    elif provider == "openai-compatible":
        infer = openai_compatible_infer(
            base_url=base_url,
            model=model,
            timeout=timeout,
            api_key=api_key,
        )
    else:
        raise DailyProductionError("unsupported Daily summary provider")

    completed: list[str] = []
    # A date's Summary ref must be committed under the same admission lock
    # as its model inference and CAS writes. Otherwise another runner can
    # select the same pending date after run_pipeline returns but before
    # _write_ref completes. This is a trusted, local inode lock; direct
    # read-only scanners and other stage producers have distinct authority.
    with _exclusive_summary_pipeline_root(summary_root):
        for target in _pending_dates(root, "summary", SUMMARY_DIR):
            evidence_sha, evidence_path, _bundle = _evidence_for_date(
                root,
                target,
            )
            # The outer production transaction owns the lock across ref
            # publication; avoid recursively taking the nonblocking lock.
            result = _run_pipeline_unlocked(
                evidence_path=evidence_path,
                state_root=summary_root,
                infer=infer,
                implementation_revision=implementation_revision,
                retry_policy=retry_policy,
            )
            if result.evidence_bundle_sha256 != evidence_sha:
                raise DailyProductionError(
                    "summary result evidence binding mismatch"
                )
            _write_ref(
                _ref_path(summary_root, target),
                {
                    "record_version": RECORD_VERSION,
                    "stage": "summary",
                    "date": target,
                    "evidence_sha256": evidence_sha,
                    "grounded_summary_sha256": (
                        result.grounded_summary_sha256
                    ),
                },
            )
            completed.append(target)
    return tuple(completed)


def render_pending(root: Path) -> tuple[str, ...]:
    projection_dir = _stage_dir(root, PROJECTION_DIR)
    completed: list[str] = []
    for target in _pending_dates(root, "projection", PROJECTION_DIR):
        evidence_sha, _evidence_path, bundle = _evidence_for_date(
            root,
            target,
        )
        summary_sha, _summary_path, summary = _summary_for_date(
            root,
            target,
            bundle=bundle,
        )
        projection = make_projection(
            bundle=bundle,
            summary=summary,
        )
        if projection.date != target:
            raise DailyProductionError(
                "projection target date mismatch"
            )
        path = store_projection(projection_dir, projection)
        if path.name != (
            f"{projection.sha256}.github-daily-progress.json"
        ):
            raise DailyProductionError(
                "projection artifact path is invalid"
            )
        _write_ref(
            _ref_path(projection_dir, target),
            {
                "record_version": RECORD_VERSION,
                "stage": "projection",
                "date": target,
                "evidence_sha256": evidence_sha,
                "grounded_summary_sha256": summary_sha,
                "projection_sha256": projection.sha256,
            },
        )
        completed.append(target)
    return tuple(completed)


def _projection_for_date(
    root: Path,
    target: str,
):
    projection_dir = _stage_dir(root, PROJECTION_DIR)
    ref = _read_ref(
        _ref_path(projection_dir, target),
        stage="projection",
        target_date=target,
    )
    digest = _sha_field(ref, "projection_sha256")
    path = projection_dir / f"{digest}.github-daily-progress.json"
    projection = load_projection(path)
    if projection.sha256 != digest or projection.date != target:
        raise DailyProductionError(
            "projection ref artifact mismatch"
        )
    if (
        _sha_field(ref, "evidence_sha256")
        != projection.evidence_bundle_sha256
        or _sha_field(ref, "grounded_summary_sha256")
        != projection.grounded_summary_sha256
    ):
        raise DailyProductionError(
            "projection ref upstream binding mismatch"
        )
    return digest, path, projection


def apply_pending(
    *,
    root: Path,
    pipeline_root: Path,
    base_url: str,
    username: str,
    password_file: Path,
    timeout: float = 30.0,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    transport_dir = _stage_dir(root, TRANSPORT_DIR)
    password = _read_password(password_file)
    completed: list[str] = []
    retryable: list[str] = []

    for target in _pending_dates(root, "transport", TRANSPORT_DIR):
        projection_sha, _projection_path, projection = (
            _projection_for_date(root, target)
        )
        result_path = (
            transport_dir
            / f"{projection_sha}.github-daily-progress.transport-result.json"
        )
        try:
            with canonical_io_lock(pipeline_root):
                # A valid prior receipt is admitted under the Writer I/O lock.
                # Compare the desired full-note hash before PUT rather than
                # discovering external state drift after a remote CAS effect.
                existing = preflight_transport_result(result_path, projection)
                result = apply_projection(
                    projection,
                    base_url=base_url,
                    username=username,
                    password=password,
                    timeout=timeout,
                    expected_after_content_sha256=(
                        existing.after_content_sha256 if existing is not None else None
                    ),
                )
                result_bytes = persist_transport_result(
                    result_path,
                    result,
                )
        except DailyProgressTargetMissing:
            retryable.append(target)
            continue
        except DailyProgressError as exc:
            if exc.reason_code in {
                "ambiguous_transport",
                "transport_network",
                "http_get_failure",
            }:
                retryable.append(target)
                continue
            raise

        result_sha = sha256_bytes(result_bytes)
        # The immutable receipt's historical outcome is authoritative. A
        # crash after an "applied" receipt but before Stage Ref publication
        # can legitimately replay as "already_desired"; do not write a ref
        # pairing the old receipt SHA with the newer runtime outcome.
        persisted_receipt = parse_transport_result(result_bytes)
        if persisted_receipt.projection_sha256 != projection_sha:
            raise DailyProductionError("transport receipt projection binding mismatch")
        _write_ref(
            _ref_path(transport_dir, target),
            {
                "record_version": RECORD_VERSION,
                "stage": "transport",
                "date": target,
                "projection_sha256": projection_sha,
                "transport_result_sha256": result_sha,
                "outcome": persisted_receipt.outcome,
            },
        )
        completed.append(target)

    return tuple(completed), tuple(retryable)


def live_status(root: Path, target_date: str) -> dict[str, object]:
    target = _canonical_date(target_date)
    schedule = _stage_dir(root, SCHEDULE_DIR)
    if not os.path.lexists(_ref_path(schedule, target)):
        raise DailyProductionError("live canary date is not scheduled")
    _read_ref(
        _ref_path(schedule, target),
        stage="schedule",
        target_date=target,
    )
    # Live acceptance must not infer durable completion from a visible name:
    # a prior publisher may have returned after a failed parent fsync.
    _reaffirm_ref_directory_durable(schedule)

    evidence_dir = _stage_dir(root, EVIDENCE_DIR)
    summary_dir = _stage_dir(root, SUMMARY_DIR)
    projection_dir = _stage_dir(root, PROJECTION_DIR)
    transport_dir = _stage_dir(root, TRANSPORT_DIR)

    stage_paths = {
        "evidence": _ref_path(evidence_dir, target),
        "summary": _ref_path(summary_dir, target),
        "projection": _ref_path(projection_dir, target),
        "transport": _ref_path(transport_dir, target),
    }
    state: dict[str, object] = {}
    for name, path in stage_paths.items():
        state[name] = "completed" if os.path.lexists(path) else "pending"

    if state["evidence"] == "completed":
        evidence_sha, _evidence_path, bundle = _evidence_for_date(
            root,
            target,
        )
        _reaffirm_ref_directory_durable(evidence_dir)
    else:
        return {
            "record_version": RECORD_VERSION,
            "date": target,
            "stages": state,
            "complete": False,
        }

    if state["summary"] == "completed":
        summary_sha, _summary_path, _summary = _summary_for_date(
            root,
            target,
            bundle=bundle,
        )
        _reaffirm_ref_directory_durable(summary_dir)
    else:
        return {
            "record_version": RECORD_VERSION,
            "date": target,
            "stages": state,
            "complete": False,
        }

    if state["projection"] == "completed":
        projection_sha, _projection_path, projection = (
            _projection_for_date(root, target)
        )
        if (
            projection.evidence_bundle_sha256 != evidence_sha
            or projection.grounded_summary_sha256 != summary_sha
        ):
            raise DailyProductionError(
                "live canary projection upstream binding mismatch"
            )
        _reaffirm_ref_directory_durable(projection_dir)
    else:
        return {
            "record_version": RECORD_VERSION,
            "date": target,
            "stages": state,
            "complete": False,
        }

    if state["transport"] != "completed":
        return {
            "record_version": RECORD_VERSION,
            "date": target,
            "stages": state,
            "complete": False,
        }

    transport_ref = _read_ref(
        stage_paths["transport"],
        stage="transport",
        target_date=target,
    )
    if _sha_field(
        transport_ref,
        "projection_sha256",
    ) != projection_sha:
        raise DailyProductionError(
            "live canary transport projection binding mismatch"
        )
    result_sha = _sha_field(
        transport_ref,
        "transport_result_sha256",
    )
    outcome = transport_ref.get("outcome")
    if (
        not isinstance(outcome, str)
        or outcome not in {"applied", "recovered", "already_desired"}
    ):
        raise DailyProductionError(
            "transport ref has unsupported outcome"
        )
    result_path = (
        transport_dir
        / f"{projection_sha}.github-daily-progress.transport-result.json"
    )
    try:
        result_data = _read_bounded_stage_ref(
            result_path, max_bytes=MAX_TRANSPORT_RESULT_BYTES
        )
    except ArtifactLifecycleError as exc:
        raise DailyProductionError(
            "live canary transport result cannot be safely read"
        ) from exc
    if sha256_bytes(result_data) != result_sha:
        raise DailyProductionError(
            "live canary transport result SHA mismatch"
        )
    try:
        result_value = json.loads(result_data)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise DailyProductionError(
            "live canary transport result is invalid JSON"
        ) from exc
    if (
        not isinstance(result_value, dict)
        or result_value.get("projection_sha256") != projection_sha
        or result_value.get("outcome") != outcome
    ):
        raise DailyProductionError(
            "live canary transport result binding mismatch"
        )
    # Only report transport-complete after all four stage-ref parent entries
    # and the scheduled-date entry have passed durability revalidation.
    _reaffirm_ref_directory_durable(transport_dir)
    state["transport_outcome"] = outcome
    return {
        "record_version": RECORD_VERSION,
        "date": target,
        "stages": state,
        "complete": True,
    }


def schedule_main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="obsidian-github-daily-production-schedule"
    )
    parser.add_argument("--root", type=Path, default=DEFAULT_DAILY_ROOT)
    parser.add_argument("--date")
    args = parser.parse_args(
        list(argv) if argv is not None else None
    )
    try:
        target = (
            _canonical_date(args.date)
            if args.date
            else previous_jst_date()
        )
        path = enqueue_date(args.root, target)
    except (DailyProductionError, OSError) as exc:
        print(f"github-daily-schedule: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "event": "github-daily-scheduled",
                "date": target,
                "path": str(path),
            },
            sort_keys=True,
        )
    )
    return 0


def collect_main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="obsidian-github-daily-production-collect"
    )
    parser.add_argument("--root", type=Path, default=DEFAULT_DAILY_ROOT)
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_WATCHER_CONFIG,
    )
    args = parser.parse_args(
        list(argv) if argv is not None else None
    )
    try:
        completed = collect_pending(
            root=args.root,
            watcher_config=args.config,
        )
    except (
        DailyProductionError,
        GitHubDailyActivityError,
        GitHubProjectWatcherError,
        OSError,
    ) as exc:
        print(f"github-daily-collect: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "event": "github-daily-collected",
                "completed": list(completed),
            },
            sort_keys=True,
        )
    )
    return 0


def summary_main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="obsidian-github-daily-production-summary"
    )
    parser.add_argument("--root", type=Path, default=DEFAULT_DAILY_ROOT)
    parser.add_argument(
        "--provider",
        choices=("ollama", "openai-compatible"),
        required=True,
    )
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--implementation-revision", required=True)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--transient-retries", type=int, default=0)
    parser.add_argument("--retry-backoff-seconds", type=float, default=0.0)
    args = parser.parse_args(
        list(argv) if argv is not None else None
    )
    try:
        policy = SummaryRetryPolicy(
            max_transient_retries=args.transient_retries,
            backoff_seconds=args.retry_backoff_seconds,
        )
        completed = summarize_pending(
            root=args.root,
            provider=args.provider,
            base_url=args.base_url,
            model=args.model,
            implementation_revision=args.implementation_revision,
            timeout=args.timeout,
            api_key=os.environ.get("OPENAI_API_KEY"),
            retry_policy=policy,
        )
    except (
        DailyProductionError,
        GitHubDailySummaryError,
        OllamaProviderError,
        OpenAICompatibleProviderError,
        OSError,
    ) as exc:
        print(f"github-daily-summary: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "event": "github-daily-summarized",
                "completed": list(completed),
            },
            sort_keys=True,
        )
    )
    return 0


def render_main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="obsidian-github-daily-production-render"
    )
    parser.add_argument("--root", type=Path, default=DEFAULT_DAILY_ROOT)
    args = parser.parse_args(
        list(argv) if argv is not None else None
    )
    try:
        completed = render_pending(args.root)
    except (DailyProductionError, DailyProgressError, OSError) as exc:
        print(f"github-daily-render: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "event": "github-daily-rendered",
                "completed": list(completed),
            },
            sort_keys=True,
        )
    )
    return 0


def apply_main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="obsidian-github-daily-production-apply"
    )
    parser.add_argument("--root", type=Path, default=DEFAULT_DAILY_ROOT)
    parser.add_argument(
        "--pipeline-root",
        type=Path,
        default=DEFAULT_PIPELINE_ROOT,
    )
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument(
        "--password-file",
        type=Path,
        default=DEFAULT_DAILY_WRITER_PASSWORD,
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(
        list(argv) if argv is not None else None
    )
    try:
        completed, retryable = apply_pending(
            root=args.root,
            pipeline_root=args.pipeline_root,
            base_url=args.base_url,
            username=args.username,
            password_file=args.password_file,
            timeout=args.timeout,
        )
    except (
        DailyProductionError,
        DailyProgressConflict,
        DailyProgressError,
        ProductionIOError,
        WebDAVCreateError,
        OSError,
    ) as exc:
        print(f"github-daily-apply: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "event": "github-daily-applied",
                "completed": list(completed),
                "retryable": list(retryable),
            },
            sort_keys=True,
        )
    )
    return 0


def smoke_main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="obsidian-github-daily-production-smoke"
    )
    parser.add_argument(
        "--profile",
        choices=("safe", "live"),
        required=True,
    )
    parser.add_argument("--root", type=Path, default=DEFAULT_DAILY_ROOT)
    parser.add_argument("--date")
    args = parser.parse_args(
        list(argv) if argv is not None else None
    )
    try:
        if args.profile == "safe":
            sample = previous_jst_date(
                datetime(
                    2026,
                    10,
                    7,
                    0,
                    10,
                    tzinfo=ZoneInfo(CANONICAL_TIMEZONE),
                )
            )
            if sample != "2026-10-06":
                raise DailyProductionError(
                    "JST previous-day contract failed"
                )
            result = {
                "profile": "safe",
                "jst_previous_day": "passed",
            }
        else:
            if not args.date:
                raise DailyProductionError(
                    "live profile requires --date"
                )
            result = {
                "profile": "live",
                **live_status(args.root, args.date),
            }
            if result["complete"] is not True:
                raise DailyProductionError(
                    "live canary is not transport-complete"
                )
    except (
        DailyProductionError, ArtifactLifecycleError,
        GitHubDailySummaryError, DailyProgressError, OSError,
    ) as exc:
        # Preserve actionable, reviewed production failure reasons while
        # suppressing arbitrary lower-layer exception text or source paths.
        reason = str(exc) if isinstance(exc, DailyProductionError) else type(exc).__name__
        print(f"github-daily-smoke: {reason}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0
