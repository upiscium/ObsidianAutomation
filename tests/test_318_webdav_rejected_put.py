"""Issue #318: rejected or ambiguous WebDAV PUT never compensates with DELETE.

Disposable loopback HTTP and synthetic transport only. No live Vault/Nextcloud.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import threading

import pytest

from obsidian_automation import webdav_create
from obsidian_automation.webdav_create import (
    WebDAVCreateError,
    WebDAVTargetExists,
    conditional_create,
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _DAVState:
    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.events: list[tuple[str, str, str | None]] = []
        self.force_status: int | None = None
        self.lose_reply = False
        self.apply_before_reply_loss = False


@contextmanager
def _synthetic_dav(state: _DAVState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:
            return

        def do_PUT(self) -> None:
            content = self.rfile.read(int(self.headers["Content-Length"]))
            state.events.append(("PUT", self.path, self.headers.get("If-None-Match")))
            if state.lose_reply:
                # Simulate ambiguous transport loss before the client sees a
                # response, both when the server has applied the request and
                # when it has not. Never allow a rejected precondition to write.
                if (state.apply_before_reply_loss and self.path not in state.files
                        and self.headers.get("If-None-Match") == "*"):
                    state.files[self.path] = content
                self.close_connection = True
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self.connection.close()
                return
            if state.force_status is not None:
                self.send_response(state.force_status)
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
            state.files[self.path] = content
            self.send_response(201)
            self.send_header("ETag", '"v1"')
            self.end_headers()

        def do_GET(self) -> None:
            state.events.append(("GET", self.path, None))
            if self.path not in state.files:
                self.send_response(404)
                self.end_headers()
                return
            content = state.files[self.path]
            self.send_response(200)
            self.send_header("ETag", '"v1"')
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def do_DELETE(self) -> None:
            state.events.append(("DELETE", self.path, None))
            state.files.pop(self.path, None)
            self.send_response(204)
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/vault"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _create(base_url: str, target: str, content: bytes):
    return conditional_create(
        base_url=base_url, target_path=target, content=content,
        username="fixture", password="fixture-only",
        timeout=2.0, allow_http=True,
    )


@pytest.mark.parametrize("response", [412, 401, 403, 503],
                         ids=["conflict-412", "unauthorized-401",
                              "forbidden-403", "server-error-503"])
def test_rejected_put_preserves_preexisting_canonical_bytes_without_delete(response: int):
    state = _DAVState()
    target = "/vault/11-Knowledge/human.md"
    original = b"# Human-owned canonical note\n\x00\xff\n"
    state.files[target] = original
    if response != 412:
        state.force_status = response
    before_hash = _sha(state.files[target])

    with _synthetic_dav(state) as base:
        error = WebDAVTargetExists if response == 412 else WebDAVCreateError
        with pytest.raises(error):
            _create(base, "11-Knowledge/human.md", b"# Automated replacement\n")

    assert _sha(state.files[target]) == before_hash
    assert state.files[target] == original
    assert state.events == [("PUT", target, "*")]
    assert not any(method == "DELETE" for method, _, _ in state.events)


@pytest.mark.parametrize("response", [401, 403, 503],
                         ids=["unauthorized", "forbidden", "server-error"])
def test_refused_create_of_absent_target_remains_absent(response: int):
    state = _DAVState()
    target = "/vault/11-Knowledge/new.md"
    state.force_status = response
    with _synthetic_dav(state) as base:
        with pytest.raises(WebDAVCreateError):
            _create(base, "11-Knowledge/new.md", b"new content")
    assert target not in state.files
    assert state.events == [("PUT", target, "*")]


@pytest.mark.parametrize("preexisting", [False, True], ids=["absent", "existing"])
def test_ambiguous_lost_put_response_never_retries_or_deletes(preexisting: bool):
    state = _DAVState()
    target = "/vault/11-Knowledge/uncertain.md"
    previous = b"canonical before transport loss\n"
    if preexisting:
        state.files[target] = previous
    digest = _sha(previous)
    state.lose_reply = True

    with _synthetic_dav(state) as base:
        with pytest.raises(WebDAVCreateError, match="PUT failed"):
            _create(base, "11-Knowledge/uncertain.md", b"candidate bytes")

    assert (target in state.files) is preexisting
    if preexisting:
        assert state.files[target] == previous and _sha(state.files[target]) == digest
    assert state.events == [("PUT", target, "*")]


def test_lost_success_response_never_rolls_back_successfully_created_bytes():
    state = _DAVState()
    state.lose_reply = True
    state.apply_before_reply_loss = True
    target = "/vault/11-Knowledge/created-before-lost-ack.md"
    desired = b"created canonical data before reply vanished\n"

    with _synthetic_dav(state) as base:
        with pytest.raises(WebDAVCreateError, match="PUT failed"):
            _create(base, "11-Knowledge/created-before-lost-ack.md", desired)

    # Client cannot conclude the create was acknowledged. But it must NOT
    # destroy bytes that the server may already have accepted.
    assert state.files[target] == desired
    assert _sha(state.files[target]) == _sha(desired)
    assert state.events == [("PUT", target, "*")]


def test_synthetic_timeout_keeps_existing_bytes_and_does_not_retry(monkeypatch):
    state = _DAVState()
    target = "/dav/canonical.md"
    previous = b"important\n"
    state.files[target] = previous
    closed = []

    class TimedOutConnection:
        def request(self, method, path, *, body, headers):
            assert method == "PUT"
            assert headers["If-None-Match"] == "*"
            state.events.append((method, path, headers["If-None-Match"]))

        def getresponse(self):
            raise TimeoutError("simulated delayed Nextcloud response")

        def close(self):
            closed.append(True)

    monkeypatch.setattr(webdav_create, "_connection",
                        lambda _parsed, *, timeout: TimedOutConnection())
    with pytest.raises(WebDAVCreateError, match="PUT failed"):
        _create("https://fixture.invalid/dav", "canonical.md", b"candidate")

    assert closed == [True]
    assert state.files[target] == previous
    assert state.events == [("PUT", target, "*")]


def test_partial_multi_file_failure_preserves_success_and_conflicting_original():
    state = _DAVState()
    first = "/vault/11-Knowledge/new.md"
    second = "/vault/11-Knowledge/existing.md"
    old = b"# Existing user text\n"
    new = b"# First approved create\n"
    state.files[second] = old

    with _synthetic_dav(state) as base:
        first_result = _create(base, "11-Knowledge/new.md", new)
        assert first_result.status_code == 201
        with pytest.raises(WebDAVTargetExists):
            _create(base, "11-Knowledge/existing.md", b"should fail")

    assert _sha(state.files[first]) == _sha(new)
    assert _sha(state.files[second]) == _sha(old)
    assert state.files[second] == old
    assert state.events == [
        ("PUT", first, "*"),
        ("GET", first, None),  # exact-byte readback of the successful create
        ("PUT", second, "*"),
    ]
    assert not any(method == "DELETE" for method, _, _ in state.events)
