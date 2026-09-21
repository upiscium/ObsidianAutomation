from __future__ import annotations

import argparse
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence
from urllib.parse import urlsplit

from .artifact_lifecycle import (
    ArtifactLifecycleError,
    _read_exact_file,
    _require_safe_directory,
    _require_sha256,
    ensure_artifact_layout,
    load_review_record,
    sha256_bytes,
)
from .evaluation_artifact import load_evaluation_record
from .human_projection import (
    HumanProjectionError,
    ProjectionRequest,
    parse_request,
    parse_result,
)
from .knowledge_review import create_evaluation_bound_review
from .webdav_create import (
    WebDAVCreateError,
    _authorization,
    _connection,
    _read_password,
    build_target_url,
)


MAX_REMOTE_REVIEW_BYTES = 768 * 1024
REQUEST_SUFFIX = ".projection.json"
RESULT_SUFFIX = ".projection-result.json"


class ReviewIntakeError(ArtifactLifecycleError):
    """Raised when a Human Review projection cannot be accepted safely."""


@dataclass(frozen=True)
class RemoteReview:
    status_code: int
    content: bytes | None
    etag: str | None


RemoteRead = Callable[..., RemoteReview]


def _read_remote_review(
    *,
    base_url: str,
    target_path: str,
    username: str,
    password: str,
    timeout: float,
    allow_http: bool = False,
) -> RemoteReview:
    if not username:
        raise ReviewIntakeError("review intake username must not be empty")
    if not password:
        raise ReviewIntakeError("review intake password must not be empty")

    target_url = build_target_url(
        base_url,
        target_path,
        allow_http=allow_http,
    )
    parsed = urlsplit(target_url)
    conn = _connection(parsed, timeout=timeout)
    try:
        conn.request(
            "GET",
            parsed.path,
            headers={"Authorization": _authorization(username, password)},
        )
        response = conn.getresponse()
        data = response.read(MAX_REMOTE_REVIEW_BYTES + 1)
        status = response.status
        etag = response.getheader("ETag")
    except OSError as exc:
        raise ReviewIntakeError(f"review intake GET failed: {exc}") from exc
    finally:
        conn.close()

    if len(data) > MAX_REMOTE_REVIEW_BYTES:
        raise ReviewIntakeError("remote review projection exceeds maximum size")
    if status == 404:
        return RemoteReview(status, None, etag)
    if status != 200:
        raise ReviewIntakeError(
            f"review intake GET returned unexpected HTTP status {status}"
        )
    return RemoteReview(status, data, etag)


def _canonical_text(data: bytes, *, label: str) -> str:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReviewIntakeError(f"{label} must be UTF-8") from exc
    text = text.replace("\r\n", "\n")
    if "\r" in text:
        raise ReviewIntakeError(f"{label} contains a lone CR line ending")
    return text


def _review_request_line(lines: list[str], *, label: str) -> tuple[int, str]:
    if not lines or lines[0] != "---":
        raise ReviewIntakeError(f"{label} has no expected frontmatter")
    try:
        close = lines.index("---", 1)
    except ValueError as exc:
        raise ReviewIntakeError(f"{label} frontmatter is not closed") from exc

    matches = [
        (index, line)
        for index, line in enumerate(lines[1:close], start=1)
        if line.startswith("review_request:")
    ]
    if len(matches) != 1:
        raise ReviewIntakeError(
            f"{label} must contain exactly one review_request field"
        )
    return matches[0]


def extract_review_decision(
    expected_content: str,
    remote_content: bytes,
) -> str | None:
    expected = _canonical_text(
        expected_content.encode("utf-8"),
        label="expected review projection",
    )
    remote = _canonical_text(
        remote_content,
        label="remote review projection",
    )

    expected_lines = expected.split("\n")
    remote_lines = remote.split("\n")
    expected_index, expected_line = _review_request_line(
        expected_lines,
        label="expected review projection",
    )
    remote_index, remote_line = _review_request_line(
        remote_lines,
        label="remote review projection",
    )

    if expected_index != remote_index:
        raise ReviewIntakeError("review_request field moved from projected location")
    if expected_line.strip() != "review_request:":
        raise ReviewIntakeError("expected projection already contains a review decision")

    raw_value = remote_line.partition(":")[2].strip()
    if raw_value in {"", '""', "''", "null", "~"}:
        decision = None
    elif raw_value in {"approve", '"approve"', "'approve'"}:
        decision = "approve"
    elif raw_value in {"reject", '"reject"', "'reject'"}:
        decision = "reject"
    else:
        raise ReviewIntakeError("review_request must be blank, approve, or reject")

    restored = list(remote_lines)
    restored[remote_index] = expected_line
    if restored != expected_lines:
        raise ReviewIntakeError(
            "remote review projection changed outside review_request"
        )
    return decision


def _load_projection_request(path: Path) -> tuple[str, ProjectionRequest]:
    if not path.name.endswith(REQUEST_SUFFIX):
        raise ReviewIntakeError("projection request filename is invalid")
    digest = _require_sha256(
        path.name[: -len(REQUEST_SUFFIX)],
        label="projection request SHA",
    )
    data = _read_exact_file(path)
    if sha256_bytes(data) != digest:
        raise ReviewIntakeError("projection request artifact hash mismatch")
    request = parse_request(data)
    return digest, request


def _review_requests(ai_root: Path) -> list[tuple[str, ProjectionRequest]]:
    root = ai_root.absolute()
    directory = root / "16-Human-Projection" / "evaluator"
    _require_safe_directory(root, create=False)
    _require_safe_directory(root / "16-Human-Projection", create=False)
    _require_safe_directory(directory, create=False)

    result: list[tuple[str, ProjectionRequest]] = []
    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        if path.name.startswith(".") or not path.name.endswith(REQUEST_SUFFIX):
            continue
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ReviewIntakeError("evaluator projection queue contains unsafe entry")
        digest, request = _load_projection_request(path)
        if request.stage == "review":
            result.append((digest, request))
    return result


def _require_projection_result(
    ai_root: Path,
    request_sha256: str,
    request: ProjectionRequest,
) -> None:
    path = (
        ai_root.absolute()
        / "17-Human-Projection-Result"
        / f"{request_sha256}{RESULT_SUFFIX}"
    )
    data = _read_exact_file(path)
    result = parse_result(data)
    if result.request_sha256 != request_sha256:
        raise ReviewIntakeError("projection result is bound to another request")
    if result.target_path != request.target_path:
        raise ReviewIntakeError("projection result target binding mismatch")
    if result.content_sha256 != request.content_sha256:
        raise ReviewIntakeError("projection result content binding mismatch")
    if result.result not in {"created", "already_matching"}:
        raise ReviewIntakeError("review projection is not safely published")


def run_review_intake(
    ai_root: Path,
    *,
    base_url: str,
    username: str,
    password: str,
    approver: str,
    timeout: float = 30.0,
    max_requests: int = 16,
    allow_http: bool = False,
    read_remote: RemoteRead | None = None,
) -> dict[str, object]:
    if type(max_requests) is not int or not 1 <= max_requests <= 64:
        raise ReviewIntakeError("max_requests must be in [1, 64]")
    if not isinstance(approver, str) or not approver or len(approver) > 256:
        raise ReviewIntakeError("approver is invalid")

    reader = read_remote or _read_remote_review
    processed = 0
    waiting = 0
    missing = 0
    existing = 0

    for request_sha, request in _review_requests(ai_root):
        _require_projection_result(ai_root, request_sha, request)

        evaluation_sha = _require_sha256(
            request.source_sha256,
            label="review evaluation SHA",
        )
        evaluation = load_evaluation_record(ai_root, evaluation_sha)

        review_path = (
            ensure_artifact_layout(ai_root).review
            / f"{evaluation.mutation_sha256}.approval.json"
        )
        if os.path.lexists(review_path):
            review = load_review_record(ai_root, evaluation.mutation_sha256)
            if (
                review.record_version != 2
                or review.evaluation_sha256 != evaluation_sha
            ):
                raise ReviewIntakeError(
                    "existing authoritative Review is bound to another evaluation"
                )
            existing += 1
            continue

        remote = reader(
            base_url=base_url,
            target_path=request.target_path,
            username=username,
            password=password,
            timeout=timeout,
            allow_http=allow_http,
        )
        if remote.status_code == 404 or remote.content is None:
            missing += 1
            continue

        decision = extract_review_decision(request.content, remote.content)
        if decision is None:
            waiting += 1
            continue

        create_evaluation_bound_review(
            ai_root,
            evaluation_sha256=evaluation_sha,
            decision=decision,
            approver=approver,
        )
        processed += 1
        if processed >= max_requests:
            break

    return {
        "event": "ai-review-intake",
        "status": "completed",
        "processed": processed,
        "waiting": waiting,
        "missing": missing,
        "existing": existing,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="obsidian-ai-review-intake")
    parser.add_argument("--ai-root", type=Path, required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--password-file", type=Path, required=True)
    parser.add_argument("--approver", required=True)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--max-requests", type=int, default=16)
    args = parser.parse_args(argv)

    try:
        result = run_review_intake(
            args.ai_root,
            base_url=args.base_url,
            username=args.username,
            password=_read_password(args.password_file),
            approver=args.approver,
            timeout=args.timeout,
            max_requests=args.max_requests,
        )
    except (
        ArtifactLifecycleError,
        HumanProjectionError,
        ReviewIntakeError,
        WebDAVCreateError,
        OSError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    import json

    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0
