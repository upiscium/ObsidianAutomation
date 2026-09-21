from __future__ import annotations

import argparse
import json
import os
import re
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
_FRONTMATTER_KEY = re.compile(r"[a-z][a-z0-9_]*\Z")
_PLAIN_NUMBER = re.compile(
    r"[+-]?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z"
)
_DIGEST_KEY = re.compile(r"(?:^|_)(?:case_id|sha256)\Z")
_UTC_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z\Z"
)
_DATE_LIKE = re.compile(r"[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}(?:$|[Tt ].*)\Z")
_PLAIN_PUNCTUATION = frozenset(" _./:@+,-")
_PLAIN_RESERVED = frozenset(
    {
        "true",
        "false",
        "yes",
        "no",
        "on",
        "off",
    }
)


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


@dataclass(frozen=True)
class _ReviewDocument:
    frontmatter: dict[str, str | None]
    body: str


def _scalar_control(value: str) -> bool:
    return any(
        ord(character) < 0x20
        or ord(character) == 0x7F
        or 0xD800 <= ord(character) <= 0xDFFF
        for character in value
    )


def _parse_frontmatter_scalar(raw: str, *, key: str, label: str) -> str | None:
    value = raw.strip(" ")
    if "\t" in value or _scalar_control(value):
        raise ReviewIntakeError(f"{label} contains unsupported scalar syntax")
    if value == "":
        return None
    if value in {"null", "Null", "NULL", "~"}:
        return None

    if value.startswith('"') or value.endswith('"'):
        if not (value.startswith('"') and value.endswith('"')):
            raise ReviewIntakeError(f"{label} contains malformed quoted scalar")
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ReviewIntakeError(f"{label} contains malformed quoted scalar") from exc
        if not isinstance(parsed, str) or _scalar_control(parsed):
            raise ReviewIntakeError(f"{label} contains unsupported quoted scalar")
        return parsed

    if value.startswith("'") or value.endswith("'"):
        if not (value.startswith("'") and value.endswith("'")):
            raise ReviewIntakeError(f"{label} contains malformed quoted scalar")
        # Only the bounded YAML single-quoted escape (two single quotes) is
        # supported; no other YAML string semantics are accepted here.
        inner = value[1:-1]
        parsed: list[str] = []
        index = 0
        while index < len(inner):
            if inner[index] != "'":
                parsed.append(inner[index])
                index += 1
                continue
            if index + 1 >= len(inner) or inner[index + 1] != "'":
                raise ReviewIntakeError(f"{label} contains malformed quoted scalar")
            parsed.append("'")
            index += 2
        result = "".join(parsed)
        if _scalar_control(result):
            raise ReviewIntakeError(f"{label} contains unsupported quoted scalar")
        return result

    # This is intentionally not a YAML plain-scalar parser. Only the small
    # string subset emitted by Review projections is accepted. In particular,
    # YAML structural punctuation, comments, tags, aliases, and ambiguous
    # implicit scalar types are rejected instead of being interpreted.
    if (
        not value[0].isalnum()
        or not value[-1].isalnum()
        or any(
            not character.isalnum() and character not in _PLAIN_PUNCTUATION
            for character in value
        )
        or ": " in value
        or " #" in value
        or value.endswith(":")
    ):
        raise ReviewIntakeError(f"{label} contains unsupported scalar syntax")
    if value.casefold() in _PLAIN_RESERVED:
        raise ReviewIntakeError(f"{label} contains ambiguous plain scalar")
    digest_plain = bool(
        _DIGEST_KEY.search(key) and re.fullmatch(r"[0-9a-fA-F]{64}", value)
    )
    if _PLAIN_NUMBER.fullmatch(value) and not digest_plain:
        raise ReviewIntakeError(f"{label} contains ambiguous plain scalar")
    if (
        re.fullmatch(r"[+-]?0[0-9]+", value)
        or re.fullmatch(r"0[xX][0-9a-fA-F]+", value)
        or re.fullmatch(r"0[oObB][0-9a-fA-F]+", value)
        or re.fullmatch(r"[0-9]+(?::[0-9]+)+", value)
    ) and not digest_plain:
        raise ReviewIntakeError(f"{label} contains ambiguous plain scalar")
    if _DATE_LIKE.fullmatch(value) and _UTC_TIMESTAMP.fullmatch(value) is None:
        # A date-like plain scalar is YAML-typed rather than an unambiguous
        # Review string. Hashes are allowed by the key-specific exception.
        if not digest_plain:
            raise ReviewIntakeError(f"{label} contains ambiguous plain scalar")
    return value


def _parse_review_document(text: str, *, label: str) -> _ReviewDocument:
    lines = text.split("\n")
    if not lines or lines[0] != "---":
        raise ReviewIntakeError(f"{label} has no expected frontmatter")
    try:
        close = lines.index("---", 1)
    except ValueError as exc:
        raise ReviewIntakeError(f"{label} frontmatter is not closed") from exc

    frontmatter: dict[str, str | None] = {}
    for line in lines[1:close]:
        if not line or line[:1] in {" ", "\t"}:
            raise ReviewIntakeError(f"{label} contains unsupported frontmatter structure")
        key, separator, raw = line.partition(":")
        if not separator or _FRONTMATTER_KEY.fullmatch(key) is None:
            raise ReviewIntakeError(f"{label} contains malformed frontmatter key")
        if key in frontmatter:
            raise ReviewIntakeError(f"{label} contains duplicate frontmatter key: {key}")
        frontmatter[key] = _parse_frontmatter_scalar(
            raw,
            key=key,
            label=f"{label} field {key}",
        )

    if "review_request" not in frontmatter:
        raise ReviewIntakeError(f"{label} must contain exactly one review_request field")
    return _ReviewDocument(frontmatter=frontmatter, body="\n".join(lines[close + 1 :]))


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

    expected_document = _parse_review_document(
        expected,
        label="expected review projection",
    )
    remote_document = _parse_review_document(
        remote,
        label="remote review projection",
    )

    if expected_document.frontmatter["review_request"] not in {None, ""}:
        raise ReviewIntakeError("expected projection already contains a review decision")

    if expected_document.body != remote_document.body:
        raise ReviewIntakeError(
            "remote review projection changed outside review_request"
        )
    if set(expected_document.frontmatter) != set(remote_document.frontmatter):
        raise ReviewIntakeError(
            "remote review projection changed outside review_request"
        )

    expected_protected = {
        key: value
        for key, value in expected_document.frontmatter.items()
        if key != "review_request"
    }
    remote_protected = {
        key: value
        for key, value in remote_document.frontmatter.items()
        if key != "review_request"
    }
    if expected_protected != remote_protected:
        raise ReviewIntakeError(
            "remote review projection changed outside review_request"
        )

    raw_value = remote_document.frontmatter["review_request"]
    if raw_value is None or raw_value == "":
        decision = None
    elif raw_value == "approve":
        decision = "approve"
    elif raw_value == "reject":
        decision = "reject"
    else:
        raise ReviewIntakeError("review_request must be blank, approve, or reject")
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
    result_directory = ai_root.absolute() / "17-Human-Projection-Result"
    _require_safe_directory(result_directory, create=False)
    path = result_directory / f"{request_sha256}{RESULT_SUFFIX}"
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
