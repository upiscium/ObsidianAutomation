from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence
from urllib.parse import urlsplit

from .artifact_lifecycle import (
    ArtifactLifecycleError,
    _canonical_json_bytes,
    _read_exact_file,
    _require_safe_directory,
    _require_sha256,
    _store_immutable,
    _utc_now,
    load_review_record,
    sha256_bytes,
)
from .human_projection import (
    LEGACY_PROJECTION_ROOT,
    PROJECTION_ROOT,
    PROJECTION_ROOTS,
    REQUEST_STAGE,
    RESULT_STAGE,
    STAGE_FOLDERS,
    parse_request,
    parse_result,
    projection_root_from_target_path,
)
from .production_io import ProductionIOError, canonical_io_lock
from .webdav_create import (
    WebDAVCreateError,
    _authorization,
    _connection,
    _read_password,
    build_target_url,
)


RECORD_VERSION = 1
CLEANUP_REQUEST_SUFFIX = ".projection-cleanup.json"
CLEANUP_RESULT_SUFFIX = ".projection-cleanup-result.json"
PROJECTION_REQUEST_SUFFIX = ".projection.json"
PROJECTION_RESULT_SUFFIX = ".projection-result.json"
CLEANUP_STAGES = (
    "input",
    "context",
    "generation",
    "validation",
    "evaluation",
    "review",
)
MAX_BATCH = 64


class HumanProjectionCleanupError(ArtifactLifecycleError):
    """Raised when a rejected-case projection cleanup is unsafe or invalid."""


@dataclass(frozen=True)
class ProjectionCleanupRequest:
    case_id: str
    review_projection_request_sha256: str
    evaluation_sha256: str
    mutation_sha256: str
    review_sha256: str
    created_at: str

    def to_json_bytes(self) -> bytes:
        return _canonical_json_bytes(
            {
                "record_version": RECORD_VERSION,
                "case_id": self.case_id,
                "review_projection_request_sha256": self.review_projection_request_sha256,
                "evaluation_sha256": self.evaluation_sha256,
                "mutation_sha256": self.mutation_sha256,
                "review_sha256": self.review_sha256,
                "created_at": self.created_at,
            }
        )


@dataclass(frozen=True)
class ProjectionCleanupTargetResult:
    target_path: str
    result: str


@dataclass(frozen=True)
class ProjectionCleanupResult:
    cleanup_request_sha256: str
    case_id: str
    targets: tuple[ProjectionCleanupTargetResult, ...]
    completed_at: str

    def to_json_bytes(self) -> bytes:
        return _canonical_json_bytes(
            {
                "record_version": RECORD_VERSION,
                "cleanup_request_sha256": self.cleanup_request_sha256,
                "case_id": self.case_id,
                "targets": [
                    {
                        "target_path": item.target_path,
                        "result": item.result,
                    }
                    for item in self.targets
                ],
                "completed_at": self.completed_at,
            }
        )


def _case_id(value: object) -> str:
    return _require_sha256(value, label="cleanup case_id")


def cleanup_target_paths(
    case_id: str,
    *,
    projection_root: str = PROJECTION_ROOT,
) -> tuple[str, ...]:
    case = _case_id(case_id)
    if projection_root not in PROJECTION_ROOTS:
        raise HumanProjectionCleanupError("cleanup projection root is unsupported")
    return tuple(
        f"{projection_root}/{STAGE_FOLDERS[stage]}/{case}.md"
        for stage in CLEANUP_STAGES
    )


def build_cleanup_request(
    *,
    case_id: str,
    review_projection_request_sha256: str,
    evaluation_sha256: str,
    mutation_sha256: str,
    review_sha256: str,
    created_at: str,
) -> ProjectionCleanupRequest:
    request = ProjectionCleanupRequest(
        case_id=_case_id(case_id),
        review_projection_request_sha256=_require_sha256(
            review_projection_request_sha256,
            label="review_projection_request_sha256",
        ),
        evaluation_sha256=_require_sha256(
            evaluation_sha256,
            label="cleanup evaluation_sha256",
        ),
        mutation_sha256=_require_sha256(
            mutation_sha256,
            label="cleanup mutation_sha256",
        ),
        review_sha256=_require_sha256(
            review_sha256,
            label="cleanup review_sha256",
        ),
        created_at=created_at,
    )
    return parse_cleanup_request(request.to_json_bytes())


def parse_cleanup_request(data: bytes) -> ProjectionCleanupRequest:
    from .artifact_lifecycle import _decode_json_object

    value = _decode_json_object(data, label="projection cleanup request")
    required = {
        "record_version",
        "case_id",
        "review_projection_request_sha256",
        "evaluation_sha256",
        "mutation_sha256",
        "review_sha256",
        "created_at",
    }
    if set(value) != required or value["record_version"] != RECORD_VERSION:
        raise HumanProjectionCleanupError(
            "projection cleanup request properties do not match contract"
        )
    created_at = value["created_at"]
    if not isinstance(created_at, str) or not created_at.endswith("Z"):
        raise HumanProjectionCleanupError("projection cleanup created_at is invalid")
    return ProjectionCleanupRequest(
        case_id=_case_id(value["case_id"]),
        review_projection_request_sha256=_require_sha256(
            value["review_projection_request_sha256"],
            label="review_projection_request_sha256",
        ),
        evaluation_sha256=_require_sha256(
            value["evaluation_sha256"],
            label="cleanup evaluation_sha256",
        ),
        mutation_sha256=_require_sha256(
            value["mutation_sha256"],
            label="cleanup mutation_sha256",
        ),
        review_sha256=_require_sha256(
            value["review_sha256"],
            label="cleanup review_sha256",
        ),
        created_at=created_at,
    )


def _reviewer_request_dir(ai_root: Path) -> Path:
    root = ai_root.absolute()
    _require_safe_directory(root, create=False)
    requests = root / REQUEST_STAGE
    _require_safe_directory(requests, create=False)
    reviewer = requests / "reviewer"
    _require_safe_directory(reviewer, create=False)
    return reviewer


def _result_dir(ai_root: Path) -> Path:
    root = ai_root.absolute()
    _require_safe_directory(root, create=False)
    result = root / RESULT_STAGE
    _require_safe_directory(result, create=False)
    return result


def store_cleanup_request(
    ai_root: Path,
    request: ProjectionCleanupRequest,
) -> tuple[str, Path]:
    normalized = parse_cleanup_request(request.to_json_bytes())
    data = normalized.to_json_bytes()
    digest = sha256_bytes(data)
    path = _reviewer_request_dir(ai_root) / f"{digest}{CLEANUP_REQUEST_SUFFIX}"
    return digest, _store_immutable(path, data)


def parse_cleanup_result(data: bytes) -> ProjectionCleanupResult:
    from .artifact_lifecycle import _decode_json_object

    value = _decode_json_object(data, label="projection cleanup result")
    required = {
        "record_version",
        "cleanup_request_sha256",
        "case_id",
        "targets",
        "completed_at",
    }
    if set(value) != required or value["record_version"] != RECORD_VERSION:
        raise HumanProjectionCleanupError(
            "projection cleanup result properties do not match contract"
        )
    request_sha = _require_sha256(
        value["cleanup_request_sha256"],
        label="cleanup_request_sha256",
    )
    case = _case_id(value["case_id"])
    raw_targets = value["targets"]
    if not isinstance(raw_targets, list) or len(raw_targets) != len(CLEANUP_STAGES):
        raise HumanProjectionCleanupError("projection cleanup target results are invalid")
    if not raw_targets or not isinstance(raw_targets[0], dict):
        raise HumanProjectionCleanupError("projection cleanup target results are invalid")
    first_target = raw_targets[0].get("target_path")
    try:
        projection_root = projection_root_from_target_path(first_target)
    except HumanProjectionError as exc:
        raise HumanProjectionCleanupError(
            "projection cleanup target root is invalid"
        ) from exc
    expected_paths = cleanup_target_paths(
        case,
        projection_root=projection_root,
    )
    targets: list[ProjectionCleanupTargetResult] = []
    for raw, expected_path in zip(raw_targets, expected_paths):
        if (
            not isinstance(raw, dict)
            or set(raw) != {"target_path", "result"}
            or raw["target_path"] != expected_path
            or raw["result"] not in {"deleted", "already_absent"}
        ):
            raise HumanProjectionCleanupError(
                "projection cleanup target result does not match contract"
            )
        targets.append(
            ProjectionCleanupTargetResult(
                target_path=expected_path,
                result=raw["result"],
            )
        )
    completed_at = value["completed_at"]
    if not isinstance(completed_at, str) or not completed_at.endswith("Z"):
        raise HumanProjectionCleanupError(
            "projection cleanup completed_at is invalid"
        )
    return ProjectionCleanupResult(
        cleanup_request_sha256=request_sha,
        case_id=case,
        targets=tuple(targets),
        completed_at=completed_at,
    )


def _store_cleanup_result(ai_root: Path, result: ProjectionCleanupResult) -> Path:
    normalized = parse_cleanup_result(result.to_json_bytes())
    return _store_immutable(
        _result_dir(ai_root)
        / f"{normalized.cleanup_request_sha256}{CLEANUP_RESULT_SUFFIX}",
        normalized.to_json_bytes(),
    )


def _existing_cleanup_result(
    ai_root: Path,
    request_sha256: str,
) -> ProjectionCleanupResult | None:
    digest = _require_sha256(request_sha256, label="cleanup request SHA")
    path = _result_dir(ai_root) / f"{digest}{CLEANUP_RESULT_SUFFIX}"
    if not os.path.lexists(path):
        return None
    result = parse_cleanup_result(_read_exact_file(path))
    if result.cleanup_request_sha256 != digest:
        raise HumanProjectionCleanupError(
            "projection cleanup result is bound to another request"
        )
    return result


def _iter_cleanup_requests(ai_root: Path) -> list[Path]:
    directory = _reviewer_request_dir(ai_root)
    paths: list[Path] = []
    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        if path.name.startswith(".") or not path.name.endswith(CLEANUP_REQUEST_SUFFIX):
            continue
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise HumanProjectionCleanupError(
                "projection cleanup queue contains unsafe entry"
            )
        paths.append(path)
    return paths


def _load_cleanup_request_path(path: Path) -> tuple[str, ProjectionCleanupRequest]:
    if not path.name.endswith(CLEANUP_REQUEST_SUFFIX):
        raise HumanProjectionCleanupError("projection cleanup filename is invalid")
    digest = _require_sha256(
        path.name[: -len(CLEANUP_REQUEST_SUFFIX)],
        label="projection cleanup filename SHA",
    )
    data = _read_exact_file(path)
    if sha256_bytes(data) != digest:
        raise HumanProjectionCleanupError(
            "projection cleanup request artifact hash mismatch"
        )
    return digest, parse_cleanup_request(data)


def _verify_cleanup_binding(
    ai_root: Path,
    request: ProjectionCleanupRequest,
) -> str:
    root = ai_root.absolute()

    projection_path = (
        root
        / REQUEST_STAGE
        / "evaluator"
        / f"{request.review_projection_request_sha256}{PROJECTION_REQUEST_SUFFIX}"
    )
    projection_data = _read_exact_file(projection_path)
    if sha256_bytes(projection_data) != request.review_projection_request_sha256:
        raise HumanProjectionCleanupError(
            "review projection request artifact hash mismatch"
        )
    projection = parse_request(projection_data)
    if (
        projection.stage != "review"
        or projection.case_id != request.case_id
        or projection.source_sha256 != request.evaluation_sha256
    ):
        raise HumanProjectionCleanupError(
            "cleanup request does not match the authoritative review projection"
        )

    projection_result_path = (
        root
        / RESULT_STAGE
        / f"{request.review_projection_request_sha256}{PROJECTION_RESULT_SUFFIX}"
    )
    projection_result = parse_result(_read_exact_file(projection_result_path))
    if (
        projection_result.request_sha256
        != request.review_projection_request_sha256
        or projection_result.target_path != projection.target_path
        or projection_result.content_sha256 != projection.content_sha256
        or projection_result.result not in {"created", "already_matching"}
    ):
        raise HumanProjectionCleanupError(
            "review projection was not safely published"
        )

    review_path = (
        root
        / "20-Review"
        / f"{request.mutation_sha256}.approval.json"
    )
    review_data = _read_exact_file(review_path)
    if sha256_bytes(review_data) != request.review_sha256:
        raise HumanProjectionCleanupError(
            "cleanup request review SHA does not match authoritative Review"
        )
    review = load_review_record(root, request.mutation_sha256)
    if (
        review.record_version != 2
        or review.decision != "reject"
        or review.evaluation_sha256 != request.evaluation_sha256
    ):
        raise HumanProjectionCleanupError(
            "cleanup requires an exact evaluation-bound Reject Review"
        )
    try:
        return projection_root_from_target_path(projection.target_path)
    except HumanProjectionError as exc:
        raise HumanProjectionCleanupError(
            "review projection target root is invalid"
        ) from exc


def _delete_remote_target(
    *,
    base_url: str,
    target_path: str,
    username: str,
    password: str,
    timeout: float,
    allow_http: bool,
) -> str:
    if not username:
        raise HumanProjectionCleanupError("cleanup username must not be empty")
    if not password:
        raise HumanProjectionCleanupError("cleanup password must not be empty")

    target_url = build_target_url(
        base_url,
        target_path,
        allow_http=allow_http,
    )
    parsed = urlsplit(target_url)
    auth = _authorization(username, password)

    conn = _connection(parsed, timeout=timeout)
    try:
        conn.request("DELETE", parsed.path, headers={"Authorization": auth})
        response = conn.getresponse()
        response.read()
        status = response.status
    except OSError as exc:
        raise HumanProjectionCleanupError(
            f"WebDAV DELETE failed before a trustworthy response was obtained: {exc}"
        ) from exc
    finally:
        conn.close()

    if status == 404:
        return "already_absent"
    if not 200 <= status < 300:
        raise HumanProjectionCleanupError(
            f"WebDAV DELETE returned unexpected HTTP status {status} for {target_path}"
        )

    verify = _connection(parsed, timeout=timeout)
    try:
        verify.request("GET", parsed.path, headers={"Authorization": auth})
        response = verify.getresponse()
        response.read()
        verify_status = response.status
    except OSError as exc:
        raise HumanProjectionCleanupError(
            f"WebDAV post-delete GET failed: {exc}"
        ) from exc
    finally:
        verify.close()

    if verify_status != 404:
        raise HumanProjectionCleanupError(
            "projection target still exists after DELETE"
        )
    return "deleted"


RemoteDelete = Callable[..., str]


def run_cleanup_sync(
    ai_root: Path,
    *,
    base_url: str,
    username: str,
    password: str,
    timeout: float = 30.0,
    max_requests: int = 16,
    allow_http: bool = False,
    delete_remote: RemoteDelete | None = None,
) -> dict[str, object]:
    if type(max_requests) is not int or not 1 <= max_requests <= MAX_BATCH:
        raise HumanProjectionCleanupError(
            f"max_requests must be 1..{MAX_BATCH}"
        )
    deleter = delete_remote or _delete_remote_target
    processed = 0
    deleted = 0
    already_absent = 0

    with canonical_io_lock(ai_root):
        for path in _iter_cleanup_requests(ai_root):
            digest, request = _load_cleanup_request_path(path)
            if _existing_cleanup_result(ai_root, digest) is not None:
                continue

            projection_root = _verify_cleanup_binding(ai_root, request)
            targets: list[ProjectionCleanupTargetResult] = []
            for target_path in cleanup_target_paths(
                request.case_id,
                projection_root=projection_root,
            ):
                result = deleter(
                    base_url=base_url,
                    target_path=target_path,
                    username=username,
                    password=password,
                    timeout=timeout,
                    allow_http=allow_http,
                )
                if result not in {"deleted", "already_absent"}:
                    raise HumanProjectionCleanupError(
                        "cleanup transport returned an invalid result"
                    )
                targets.append(
                    ProjectionCleanupTargetResult(
                        target_path=target_path,
                        result=result,
                    )
                )
                if result == "deleted":
                    deleted += 1
                else:
                    already_absent += 1

            _store_cleanup_result(
                ai_root,
                ProjectionCleanupResult(
                    cleanup_request_sha256=digest,
                    case_id=request.case_id,
                    targets=tuple(targets),
                    completed_at=_utc_now(),
                ),
            )
            processed += 1
            if processed >= max_requests:
                break

    return {
        "event": "ai-human-projection-cleanup-sync",
        "status": "completed",
        "processed": processed,
        "deleted": deleted,
        "already_absent": already_absent,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="obsidian-ai-human-projection-cleanup-sync"
    )
    parser.add_argument("--ai-root", type=Path, required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--password-file", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--max-requests", type=int, default=16)
    args = parser.parse_args(argv)

    try:
        result = run_cleanup_sync(
            args.ai_root,
            base_url=args.base_url,
            username=args.username,
            password=_read_password(args.password_file),
            timeout=args.timeout,
            max_requests=args.max_requests,
        )
    except (
        ArtifactLifecycleError,
        HumanProjectionCleanupError,
        ProductionIOError,
        WebDAVCreateError,
        OSError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
