from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo

from .artifact_lifecycle import _canonical_json_bytes, _read_exact_file, sha256_bytes
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
    store_projection,
)
from .github_daily_summary import (
    GitHubDailySummaryError,
    load_evidence_bundle,
)
from .github_daily_summary_generation import (
    ollama_infer,
    openai_compatible_infer,
)
from .github_daily_summary import run_pipeline
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
    data = _read_exact_file(path)
    try:
        value = json.loads(data)
    except json.JSONDecodeError as exc:
        raise DailyProductionError("stage ref is invalid JSON") from exc
    if not isinstance(value, dict):
        raise DailyProductionError("stage ref must be an object")
    if value.get("record_version") != RECORD_VERSION:
        raise DailyProductionError("stage ref version is unsupported")
    if value.get("stage") != stage:
        raise DailyProductionError("stage ref kind does not match path")
    if value.get("date") != _canonical_date(target_date):
        raise DailyProductionError("stage ref date does not match path")
    if _canonical_json_bytes(value) != data:
        raise DailyProductionError("stage ref is not canonical JSON")
    return value


def _write_ref(
    path: Path,
    payload: Mapping[str, object],
) -> bytes:
    data = _canonical_json_bytes(payload)
    if path.exists() or path.is_symlink():
        existing = _read_exact_file(path)
        if existing != data:
            raise DailyProductionError(
                "stage ref already exists with different bytes"
            )
        return existing

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = os.open(path, flags, 0o640)
    except FileExistsError:
        return _write_ref(path, payload)
    except OSError as exc:
        raise DailyProductionError("cannot create stage ref") from exc
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise DailyProductionError(
                    "short write while persisting stage ref"
                )
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return data


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


def scheduled_dates(root: Path) -> tuple[str, ...]:
    schedule = _stage_dir(root, SCHEDULE_DIR)
    rows: list[str] = []
    for path in schedule.glob(f"*{_REF_SUFFIX}"):
        name = path.name[: -len(_REF_SUFFIX)]
        target = _canonical_date(name)
        _read_ref(path, stage="schedule", target_date=target)
        rows.append(target)
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
    for target in scheduled_dates(root):
        path = _ref_path(directory, target)
        if not path.exists():
            pending.append(target)
            continue
        _read_ref(path, stage=completed_stage, target_date=target)
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
) -> tuple[str, ...]:
    if (
        not isinstance(implementation_revision, str)
        or _GIT_REV_RE.fullmatch(implementation_revision) is None
    ):
        raise DailyProductionError(
            "implementation revision must be lowercase 40..64 hex"
        )
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
    for target in _pending_dates(root, "summary", SUMMARY_DIR):
        evidence_sha, evidence_path, _bundle = _evidence_for_date(
            root,
            target,
        )
        result = run_pipeline(
            evidence_path=evidence_path,
            state_root=summary_root,
            infer=infer,
            implementation_revision=implementation_revision,
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
                result = apply_projection(
                    projection,
                    base_url=base_url,
                    username=username,
                    password=password,
                    timeout=timeout,
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
        _write_ref(
            _ref_path(transport_dir, target),
            {
                "record_version": RECORD_VERSION,
                "stage": "transport",
                "date": target,
                "projection_sha256": projection_sha,
                "transport_result_sha256": result_sha,
                "outcome": result.outcome,
            },
        )
        completed.append(target)

    return tuple(completed), tuple(retryable)


def live_status(root: Path, target_date: str) -> dict[str, object]:
    target = _canonical_date(target_date)
    schedule = _stage_dir(root, SCHEDULE_DIR)
    if not _ref_path(schedule, target).exists():
        raise DailyProductionError("live canary date is not scheduled")
    _read_ref(
        _ref_path(schedule, target),
        stage="schedule",
        target_date=target,
    )

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
        state[name] = "completed" if path.exists() else "pending"

    if state["evidence"] == "completed":
        evidence_sha, _evidence_path, bundle = _evidence_for_date(
            root,
            target,
        )
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
    if outcome not in {
        "applied",
        "recovered",
        "already_desired",
    }:
        raise DailyProductionError(
            "transport ref has unsupported outcome"
        )
    result_path = (
        transport_dir
        / f"{projection_sha}.github-daily-progress.transport-result.json"
    )
    result_data = _read_exact_file(result_path)
    if sha256_bytes(result_data) != result_sha:
        raise DailyProductionError(
            "live canary transport result SHA mismatch"
        )
    try:
        result_value = json.loads(result_data)
    except json.JSONDecodeError as exc:
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
    args = parser.parse_args(
        list(argv) if argv is not None else None
    )
    try:
        completed = summarize_pending(
            root=args.root,
            provider=args.provider,
            base_url=args.base_url,
            model=args.model,
            implementation_revision=args.implementation_revision,
            timeout=args.timeout,
            api_key=os.environ.get("OPENAI_API_KEY"),
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
    except (DailyProductionError, OSError) as exc:
        print(f"github-daily-smoke: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0
