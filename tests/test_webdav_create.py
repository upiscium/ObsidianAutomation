from __future__ import annotations

import base64
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from obsidian_automation.webdav_create import (
    WebDAVCreateError,
    WebDAVTargetExists,
    _read_password,
    build_target_url,
    conditional_create,
)


class _State:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.if_none_match: list[str | None] = []
        self.authorization: list[str | None] = []
        self.corrupt_get = False


def _server(state: _State):
    expected_auth = "Basic " + base64.b64encode(b"writer:secret").decode("ascii")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:
            pass

        def do_PUT(self) -> None:
            state.if_none_match.append(self.headers.get("If-None-Match"))
            state.authorization.append(self.headers.get("Authorization"))
            if self.headers.get("Authorization") != expected_auth:
                self.send_response(401)
                self.end_headers()
                return
            if self.headers.get("If-None-Match") != "*":
                self.send_response(400)
                self.end_headers()
                return
            if self.path in state.files:
                self.send_response(412)
                self.end_headers()
                return
            length = int(self.headers["Content-Length"])
            state.files[self.path] = self.rfile.read(length)
            self.send_response(201)
            self.send_header("ETag", '"created"')
            self.end_headers()

        def do_GET(self) -> None:
            if self.headers.get("Authorization") != expected_auth:
                self.send_response(401)
                self.end_headers()
                return
            if self.path not in state.files:
                self.send_response(404)
                self.end_headers()
                return
            data = state.files[self.path]
            if state.corrupt_get:
                data = b"different"
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("ETag", '"verified"')
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_conditional_create_sets_precondition_and_verifies_bytes() -> None:
    state = _State()
    server, thread = _server(state)
    try:
        base = f"http://127.0.0.1:{server.server_port}/remote.php/dav/files/writer/Vault"
        result = conditional_create(
            base_url=base,
            target_path="11-Knowledge/example note.md",
            content=b"# Example\n",
            username="writer",
            password="secret",
            allow_http=True,
        )
        assert result.status_code == 201
        assert result.etag == '"verified"'
        assert state.if_none_match == ["*"]
        assert state.authorization[0] is not None
        assert state.files[
            "/remote.php/dav/files/writer/Vault/11-Knowledge/example%20note.md"
        ] == b"# Example\n"
    finally:
        server.shutdown()
        thread.join()


def test_existing_target_returns_conflict_without_overwrite() -> None:
    state = _State()
    server, thread = _server(state)
    try:
        base = f"http://127.0.0.1:{server.server_port}/dav/Vault"
        target = "/dav/Vault/11-Knowledge/existing.md"
        state.files[target] = b"human\n"
        with pytest.raises(WebDAVTargetExists, match="refusing overwrite"):
            conditional_create(
                base_url=base,
                target_path="11-Knowledge/existing.md",
                content=b"agent\n",
                username="writer",
                password="secret",
                allow_http=True,
            )
        assert state.files[target] == b"human\n"
    finally:
        server.shutdown()
        thread.join()


def test_success_is_rejected_if_verification_bytes_differ() -> None:
    state = _State()
    state.corrupt_get = True
    server, thread = _server(state)
    try:
        base = f"http://127.0.0.1:{server.server_port}/dav/Vault"
        with pytest.raises(WebDAVCreateError, match="remote bytes"):
            conditional_create(
                base_url=base,
                target_path="11-Knowledge/example.md",
                content=b"expected\n",
                username="writer",
                password="secret",
                allow_http=True,
            )
    finally:
        server.shutdown()
        thread.join()


def test_target_path_and_base_url_are_restricted() -> None:
    with pytest.raises(WebDAVCreateError, match="HTTPS"):
        build_target_url("http://example.test/dav", "11-Knowledge/a.md")
    with pytest.raises(WebDAVCreateError, match="unsafe"):
        build_target_url("https://example.test/dav", "../a.md")
    with pytest.raises(WebDAVCreateError, match="embedded credentials"):
        build_target_url("https://user:pass@example.test/dav", "11-Knowledge/a.md")


def test_password_file_rejects_symlink(tmp_path: Path) -> None:
    password = tmp_path / "password"
    password.write_text("secret\n")
    link = tmp_path / "link"
    link.symlink_to(password)
    with pytest.raises(WebDAVCreateError, match="non-symlink"):
        _read_password(link)
