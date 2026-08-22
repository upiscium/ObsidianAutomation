from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import ssl
import stat
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Sequence
from urllib.parse import quote, urlsplit, urlunsplit


class WebDAVCreateError(RuntimeError):
    """Raised when a conditional WebDAV create cannot be completed safely."""


class WebDAVTargetExists(WebDAVCreateError):
    """Raised when the remote target already exists."""


@dataclass(frozen=True)
class WebDAVCreateResult:
    target_url: str
    content_sha256: str
    status_code: int
    etag: str | None


def _safe_components(path: str) -> tuple[str, ...]:
    if not path or path.startswith("/") or "\\" in path or "\x00" in path:
        raise WebDAVCreateError("target path must be a relative POSIX path")
    parts = tuple(path.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        raise WebDAVCreateError("target path contains an unsafe path component")
    return parts


def build_target_url(base_url: str, target_path: str, *, allow_http: bool = False) -> str:
    parsed = urlsplit(base_url)
    allowed_schemes = {"https"} | ({"http"} if allow_http else set())
    if parsed.scheme not in allowed_schemes:
        raise WebDAVCreateError("WebDAV base URL must use HTTPS")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise WebDAVCreateError("WebDAV base URL must contain a host and no embedded credentials")
    if parsed.query or parsed.fragment:
        raise WebDAVCreateError("WebDAV base URL must not contain query or fragment components")

    parts = _safe_components(target_path)
    encoded_target = "/".join(quote(part, safe="") for part in parts)
    base_path = parsed.path.rstrip("/")
    full_path = f"{base_path}/{encoded_target}"
    netloc = parsed.hostname
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, full_path, "", ""))


def _read_password(path: Path) -> str:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise WebDAVCreateError("password file does not exist") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise WebDAVCreateError("password file must be a regular non-symlink file")
    data = path.read_bytes()
    if data.endswith(b"\n"):
        data = data[:-1]
    if b"\n" in data or b"\r" in data or not data:
        raise WebDAVCreateError("password file must contain exactly one non-empty line")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WebDAVCreateError("password file must be UTF-8") from exc


def _connection(parsed, *, timeout: float):
    if parsed.scheme == "https":
        return http.client.HTTPSConnection(
            parsed.hostname,
            parsed.port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
    return http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=timeout)


def _authorization(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def conditional_create(
    *,
    base_url: str,
    target_path: str,
    content: bytes,
    username: str,
    password: str,
    timeout: float = 30.0,
    allow_http: bool = False,
) -> WebDAVCreateResult:
    if not username:
        raise WebDAVCreateError("username must not be empty")
    if not password:
        raise WebDAVCreateError("password must not be empty")
    if not content:
        raise WebDAVCreateError("content must not be empty")

    target_url = build_target_url(base_url, target_path, allow_http=allow_http)
    parsed = urlsplit(target_url)
    auth = _authorization(username, password)
    headers = {
        "Authorization": auth,
        "Content-Length": str(len(content)),
        "Content-Type": "text/markdown; charset=utf-8",
        "If-None-Match": "*",
    }

    conn = _connection(parsed, timeout=timeout)
    try:
        conn.request("PUT", parsed.path, body=content, headers=headers)
        response = conn.getresponse()
        response.read()
        status = response.status
        etag = response.getheader("ETag")
    except OSError as exc:
        raise WebDAVCreateError(f"WebDAV PUT failed: {exc}") from exc
    finally:
        conn.close()

    if status == 412:
        raise WebDAVTargetExists("remote target already exists; refusing overwrite")
    if not 200 <= status < 300:
        raise WebDAVCreateError(f"WebDAV PUT returned unexpected HTTP status {status}")

    verify = _connection(parsed, timeout=timeout)
    try:
        verify.request("GET", parsed.path, headers={"Authorization": auth})
        response = verify.getresponse()
        remote = response.read()
        verify_status = response.status
        verify_etag = response.getheader("ETag")
    except OSError as exc:
        raise WebDAVCreateError(f"WebDAV verification GET failed: {exc}") from exc
    finally:
        verify.close()

    if verify_status != 200:
        raise WebDAVCreateError(
            f"WebDAV verification GET returned unexpected HTTP status {verify_status}"
        )
    expected_sha = hashlib.sha256(content).hexdigest()
    if hashlib.sha256(remote).hexdigest() != expected_sha:
        raise WebDAVCreateError("remote bytes do not match the requested create content")

    return WebDAVCreateResult(
        target_url=target_url,
        content_sha256=expected_sha,
        status_code=status,
        etag=verify_etag or etag,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="obsidian-webdav-create",
        description="Create one WebDAV file only if the remote target does not already exist.",
    )
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--target-path", required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--password-file", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=30.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        content = args.source.read_bytes()
        password = _read_password(args.password_file)
        result = conditional_create(
            base_url=args.base_url,
            target_path=args.target_path,
            content=content,
            username=args.username,
            password=password,
            timeout=args.timeout,
        )
    except WebDAVTargetExists as exc:
        print(f"conflict: {exc}", file=sys.stderr)
        return 3
    except (OSError, WebDAVCreateError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "result": "created",
                "target_path": args.target_path,
                "content_sha256": result.content_sha256,
                "http_status": result.status_code,
                "etag": result.etag,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
