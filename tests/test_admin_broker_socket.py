"""Disposable Linux AF_UNIX tests; never creates a production listener."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import stat
import struct
import tempfile
import threading
import time

import pytest

from obsidian_automation.admin_broker_socket import (
    BrokerSocketClient, LocalBrokerProtocolError, MAX_FRAME,
    ReadOnlyBrokerSocket, _capabilities,
)

RID = "12345678-1234-4234-8234-123456789abc"


@pytest.fixture
def private_path():
    # Keep absolute sockaddr_un name under 100 UTF-8 bytes on Nix pytest.
    with tempfile.TemporaryDirectory(prefix="oabs-", dir="/tmp") as folder:
        os.chmod(folder, 0o700)
        yield Path(folder) / "admin.sock"


def server(path, *, uid=None, gid=None):
    return ReadOnlyBrokerSocket(
        path,
        allowed_peer_uid=os.geteuid() if uid is None else uid,
        allowed_peer_gid=os.getegid() if gid is None else gid,
        require_root=False,
    )


def with_one_client(srv, op):
    result = []
    error = []
    def work():
        try:
            result.append(op())
        except BaseException as exc:
            error.append(exc)
    t = threading.Thread(target=work)
    t.start()
    assert srv.serve_once()
    t.join(timeout=3)
    assert not t.is_alive()
    if error:
        raise error[0]
    return result[0]


def raw_request(path, body: bytes, *, announced: int | None = None):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(2)
        conn.connect(str(path))
        conn.sendall(struct.pack("!I", len(body) if announced is None else announced))
        if body:
            conn.sendall(body)
        head = conn.recv(4)
        if not head:
            return None
        n = struct.unpack("!I", head)[0]
        payload = bytearray()
        while len(payload) < n:
            payload.extend(conn.recv(n-len(payload)))
        return json.loads(payload)


def test_default_root_authority_refuses_nonroot_init(private_path, monkeypatch):
    sock = ReadOnlyBrokerSocket(private_path, allowed_peer_uid=os.geteuid())
    monkeypatch.setattr(os, "geteuid", lambda: 12345)
    with pytest.raises(LocalBrokerProtocolError, match="privileged_socket_required"):
        sock.open()
    assert not private_path.exists()


def test_same_uid_peer_gets_disabled_capabilities(private_path):
    with server(private_path) as srv:
        result = with_one_client(srv, lambda: BrokerSocketClient(private_path).query({
            "version": 1, "op": "capabilities",
        }))
        assert result == _capabilities()
        assert result["authority"] == "observation_only"
        assert result["operations"] == {"pause": False, "resume": False, "run_once": False}
        info = private_path.lstat()
        assert stat.S_ISSOCK(info.st_mode)
        assert stat.S_IMODE(info.st_mode) == 0o660
    assert not private_path.exists()


def test_unauthorized_uid_is_disconnected_without_payload(private_path):
    with server(private_path, uid=os.geteuid() + 1) as srv:
        def request():
            try:
                BrokerSocketClient(private_path).query({
                    "version": 1, "op": "capabilities",
                })
            except LocalBrokerProtocolError as error:
                return str(error)
            return "unexpected_success"
        assert with_one_client(srv, request) in {"truncated_frame", "socket_unavailable", "io_unavailable"}


def test_unauthorized_gid_is_disconnected(private_path):
    with server(private_path, gid=os.getegid() + 1) as srv:
        def request():
            try:
                BrokerSocketClient(private_path).query({
                    "version": 1, "op": "capabilities",
                })
            except LocalBrokerProtocolError as error:
                return str(error)
            return "unexpected_success"
        assert with_one_client(srv, request) in {"truncated_frame", "socket_unavailable", "io_unavailable"}


@pytest.mark.parametrize("payload", [
    {"version": 1, "op": "pause", "service_id": "github-sync"},
    {"version": 1, "op": "run_once"},
    {"version": 1, "op": "apply", "command": "systemctl stop"},
    {"version": 1, "op": "capabilities", "enable_effects": True},
    {"version": 1.0, "op": "capabilities"},
    {"version": True, "op": "capabilities"},
    {"version": 1, "op": "receipt_lookup", "request_id": "../../foo"},
    {"version": 1, "op": "unknown"},
    [],
    "capabilities",
    None,
])
def test_no_command_or_malformed_query_is_ever_accepted(private_path, payload):
    with server(private_path) as srv:
        result = with_one_client(
            srv,
            lambda: BrokerSocketClient(private_path).query(payload),
        )
        assert result == {"version": 1, "error": "read_only_request_rejected"}


def test_duplicate_json_keys_and_invalid_utf8_rejected(private_path):
    inputs = [b'{"version":1,"version":1,"op":"capabilities"}', b"\xff"]
    with server(private_path) as srv:
        assert with_one_client(
            srv, lambda: raw_request(private_path, inputs[0]),
        ) == {"version": 1, "error": "read_only_request_rejected"}
        assert with_one_client(
            srv, lambda: raw_request(private_path, inputs[1]),
        ) == {"version": 1, "error": "read_only_request_rejected"}


def test_overlarge_request_rejected_before_reading_payload(private_path):
    with server(private_path) as srv:
        result = with_one_client(srv, lambda: raw_request(
            private_path, b"", announced=MAX_FRAME + 1,
        ))
        assert result == {"version": 1, "error": "read_only_request_rejected"}


def test_receipt_lookup_is_not_exposed_by_socket_even_with_valid_uuid(private_path):
    with server(private_path) as srv:
        result = with_one_client(srv, lambda: BrokerSocketClient(private_path).query({
            "version": 1, "op": "receipt_lookup", "request_id": RID,
        }))
        assert result == {"version": 1, "error": "read_only_request_rejected"}


def test_existing_socket_symlink_and_regular_file_are_never_overwritten(private_path):
    private_path.symlink_to("/etc/passwd")
    with pytest.raises(LocalBrokerProtocolError, match="socket_path_occupied"):
        server(private_path).open()
    assert private_path.is_symlink()
    private_path.unlink()
    private_path.write_text("existing")
    with pytest.raises(LocalBrokerProtocolError, match="socket_path_occupied"):
        server(private_path).open()
    assert private_path.read_text() == "existing"


def test_untrusted_world_accessible_directory_denied(private_path):
    private_path.parent.chmod(0o707)
    with pytest.raises(LocalBrokerProtocolError, match="unsafe_socket_parent"):
        server(private_path).open()
    assert not private_path.exists()


def test_symlinked_parent_refused(private_path):
    link = private_path.parent / "link"
    link.symlink_to(private_path.parent, target_is_directory=True)
    with pytest.raises(LocalBrokerProtocolError, match="unsafe_socket_parent"):
        server(link / "admin.sock").open()
    assert not (link / "admin.sock").exists()


def test_close_never_unlinks_replaced_socket_path(private_path):
    with server(private_path) as srv:
        original = private_path.parent / "previous.sock"
        private_path.rename(original)
        private_path.write_text("replacement")
    assert private_path.read_text() == "replacement"
    assert original.is_socket()


def test_two_listeners_refuse_same_path(private_path):
    with server(private_path):
        with pytest.raises(LocalBrokerProtocolError, match="socket_lease_busy"):
            server(private_path).open()


def test_socket_can_serve_multiple_sequential_requests(private_path):
    with server(private_path) as srv:
        for _ in range(4):
            result = with_one_client(srv, lambda: BrokerSocketClient(private_path).query({
                "version": 1, "op": "capabilities",
            }))
            assert result["authority"] == "observation_only"


def test_socket_backend_failure_stays_unavailable(private_path):
    client = BrokerSocketClient(private_path)
    with pytest.raises(LocalBrokerProtocolError, match="socket_unavailable"):
        client.query({"version": 1, "op": "capabilities"})


def test_capability_lookup_absent_socket_fail_closed(private_path):
    from obsidian_automation.admin_broker_socket import capabilities_from_socket
    assert capabilities_from_socket(private_path) == {
        "version": 1, "authority": "observation_only",
        "operations": {"pause": False, "resume": False, "run_once": False},
        "reason": "broker_unavailable",
    }


def test_capability_lookup_verified_read_only_socket(private_path):
    from obsidian_automation.admin_broker_socket import capabilities_from_socket
    with server(private_path) as srv:
        result = with_one_client(
            srv, lambda: capabilities_from_socket(private_path),
        )
        assert result == _capabilities()


@pytest.mark.parametrize("observer,expected_reason", [
    (None, "broker_unavailable"),
    (lambda: _capabilities(), "cross_updater_exclusion_unqualified"),
    (lambda: {"version": True, "authority": "observation_only",
              "operations": {"pause": False, "resume": False, "run_once": False},
              "reason": "cross_updater_exclusion_unqualified"}, "broker_unavailable"),
    (lambda: {"version": 1, "authority": "local_operator",
              "operations": {"pause": True, "resume": True, "run_once": True},
              "reason": "qualified"}, "broker_unavailable"),
])
def test_dashboard_capability_api_no_write_and_fails_closed(observer, expected_reason):
    import http.client
    from obsidian_automation.admin_dashboard import make_server
    from obsidian_automation.admin_broker_socket import _unavailable_capabilities
    srv = make_server(
        port=0, observer=lambda: [], ai_observer=lambda: {},
        capabilities_observer=observer or _unavailable_capabilities,
    )
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    port = srv.server_address[1]
    def query(method="GET", host=None, origin=None, path="/api/v1/operation-capabilities"):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        headers = {"Host": host or f"127.0.0.1:{port}"}
        if origin is not None:
            headers["Origin"] = origin
        try:
            conn.request(method, path, headers=headers)
            reply = conn.getresponse()
            return reply.status, dict(reply.getheaders()), reply.read()
        finally:
            conn.close()
    try:
        assert srv.server_address[0] == "127.0.0.1"
        code, headers, raw = query()
        result = json.loads(raw)
        assert code == 200
        assert result["reason"] == expected_reason
        assert result["authority"] == "observation_only"
        assert result["operations"] == {
            "pause": False, "resume": False, "run_once": False,
        }
        assert headers["Cache-Control"] == "no-store"
        assert "default-src 'none'" in headers["Content-Security-Policy"]
        assert query(method="POST")[0] == 405
        assert query(method="PUT")[0] == 405
        assert query(host="attack.invalid")[0] == 403
        assert query(origin="https://attack.invalid")[0] == 403
        assert query(path="/api/v1/operation-capabilities?debug=true")[0] == 404
    finally:
        srv.shutdown()
        thread.join(timeout=3)
        srv.server_close()


def test_dashboard_broker_observer_exception_does_not_leak():
    import http.client
    from obsidian_automation.admin_dashboard import make_server
    def fail():
        raise RuntimeError("token=NOT_FOR_HTTP")
    srv = make_server(
        port=0, observer=lambda: [], ai_observer=lambda: {},
        capabilities_observer=fail,
    )
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        port = srv.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        conn.request("GET", "/api/v1/operation-capabilities",
                     headers={"Host": f"127.0.0.1:{port}"})
        response = conn.getresponse()
        raw = response.read()
        assert response.status == 200
        assert json.loads(raw)["reason"] == "broker_unavailable"
        assert b"NOT_FOR_HTTP" not in raw
        conn.close()
    finally:
        srv.shutdown()
        thread.join(timeout=3)
        srv.server_close()


@pytest.mark.parametrize("spoofed", [
    {"version": True, "authority": "observation_only",
     "operations": {"pause": False, "resume": False, "run_once": False},
     "reason": "cross_updater_exclusion_unqualified"},
    {"version": 1, "authority": "observation_only",
     "operations": {"pause": True, "resume": False, "run_once": False},
     "reason": "cross_updater_exclusion_unqualified"},
    {"version": 1, "authority": "observation_only",
     "operations": {"pause": 0, "resume": 0, "run_once": 0},
     "reason": "cross_updater_exclusion_unqualified"},
])
def test_socket_client_never_trusts_spoofed_enabled_capabilities(
    private_path, monkeypatch, spoofed,
):
    from obsidian_automation.admin_broker_socket import (
        BrokerSocketClient, capabilities_from_socket,
    )
    monkeypatch.setattr(BrokerSocketClient, "query", lambda *args: spoofed)
    assert capabilities_from_socket(private_path)["reason"] == "broker_unavailable"


def test_post_bind_setup_failure_cleans_only_its_own_socket(private_path, monkeypatch):
    original_chmod = os.chmod
    injected = [False]
    def failed_chmod(path, mode, **kwargs):
        if str(path) == str(private_path) and not injected[0]:
            injected[0] = True
            raise OSError("synthetic chmod failure")
        return original_chmod(path, mode, **kwargs)
    monkeypatch.setattr(os, "chmod", failed_chmod)
    with pytest.raises(OSError, match="synthetic chmod failure"):
        server(private_path).open()
    assert not private_path.exists()
    # A post-bind error must not strand the socket forever.
    with server(private_path):
        assert private_path.is_socket()
    assert not private_path.exists()


def test_broadened_parent_permissions_block_shutdown_unlink(private_path):
    with server(private_path):
        private_path.parent.chmod(0o770)
    # The socket is intentionally preserved when the previously trusted
    # directory becomes group-writable. Recovery requires local operator.
    assert private_path.is_socket()
    private_path.parent.chmod(0o700)
    private_path.unlink()


def test_total_request_deadline_is_shared_across_all_partial_reads(monkeypatch):
    from obsidian_automation.admin_broker_socket import _recv_exact
    fake_now = [0.0]
    class Drip:
        def __init__(self):
            self.calls = []
        def settimeout(self, seconds):
            self.calls.append(seconds)
        def recv(self, size):
            fake_now[0] += 0.6  # sender took 0.6s for each byte
            return b"x"
    monkeypatch.setattr(time, "monotonic", lambda: fake_now[0])
    client = Drip()
    with pytest.raises(LocalBrokerProtocolError, match="request_deadline_expired"):
        _recv_exact(client, 4, deadline=1.0)
    assert client.calls == [1.0, pytest.approx(0.4)]




def test_first_post_bind_stat_failure_is_cleaned_under_lease(private_path, monkeypatch):
    original_stat = os.stat
    count = [0]
    def fail_first_created_stat(path, *args, **kwargs):
        if path == private_path.name and kwargs.get("dir_fd") is not None:
            count[0] += 1
            if count[0] == 2:
                raise OSError("synthetic post-bind stat failure")
        return original_stat(path, *args, **kwargs)
    monkeypatch.setattr(os, "stat", fail_first_created_stat)
    with pytest.raises(OSError, match="synthetic post-bind stat failure"):
        server(private_path).open()
    assert count[0] >= 3
    assert not private_path.exists()
    # First failure cleaned only its own socket; the stable lease can be
    # acquired again without manual filesystem deletion.
    with server(private_path):
        assert private_path.is_socket()
    assert not private_path.exists()


def test_socket_lease_inode_symlink_and_hardlink_refused(private_path):
    lease = private_path.parent / "admin.sock.lock"
    lease.symlink_to(private_path.parent / "unknown")
    with pytest.raises(LocalBrokerProtocolError, match="unsafe_socket_lease"):
        server(private_path).open()
    assert not private_path.exists()
    lease.unlink()
    lease.write_text("")
    lease.chmod(0o600)
    os.link(lease, private_path.parent / "second-hardlink")
    with pytest.raises(LocalBrokerProtocolError, match="unsafe_socket_lease"):
        server(private_path).open()
    assert not private_path.exists()


def test_socket_lease_survives_normal_close(private_path):
    with server(private_path):
        lease = private_path.parent / "admin.sock.lock"
        assert lease.is_file()
        assert stat.S_IMODE(lease.stat().st_mode) == 0o600
    assert lease.is_file()
    assert not private_path.exists()
    with server(private_path):
        assert private_path.is_socket()


def test_failed_open_cleanup_error_still_releases_socket_lease(
    private_path, monkeypatch,
):
    real_stat = os.stat
    observed = [0]
    def interrupted_stat(path, *args, **kwargs):
        if path == private_path.name and kwargs.get("dir_fd") is not None:
            observed[0] += 1
            if observed[0] in (2, 3):
                raise OSError("synthetic stat failure including cleanup")
        return real_stat(path, *args, **kwargs)
    monkeypatch.setattr(os, "stat", interrupted_stat)
    with pytest.raises(OSError):
        server(private_path).open()
    assert private_path.is_socket()
    # The inode couldn't be inspected, so the prototype intentionally leaves
    # it for manual recovery, but must NOT retain the exclusive flock.
    private_path.unlink()
    with server(private_path):
        assert private_path.is_socket()
    assert not private_path.exists()


def test_close_exception_still_releases_fds_and_socket(private_path):
    with server(private_path) as srv:
        raw_listener = srv._listener
        class BrokenClose:
            def close(self):
                raise OSError("synthetic listener close failure")
        srv._listener = BrokenClose()
        try:
            with pytest.raises(OSError, match="synthetic listener close failure"):
                srv.close()
        finally:
            raw_listener.close()
        assert not private_path.exists()
        # Stable lease no longer held even when original close failed.
        with server(private_path):
            assert private_path.is_socket()
    assert not private_path.exists()
