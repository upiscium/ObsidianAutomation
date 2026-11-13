"""#303 V1/B2 HTTPS transport against a fake connection (no sockets)."""
from __future__ import annotations

import socket

import pytest

from obsidian_automation.vault_root_dav_transport import HttpsDavTransport
from obsidian_automation.vault_root_dav_verifier import DavTransportError, PROPFIND_BODY

ORIGIN = "https://nextcloud.invalid"


class FakeResponse:
    def __init__(self, status=207, body=b"<x/>", headers=None, chunks=None):
        self.status = status
        self._chunks = list(chunks if chunks is not None else [body])
        self._headers = {"Content-Type": "application/xml; charset=utf-8"}
        self._headers.update(headers or {})
        self.reads = 0

    def getheader(self, name, default=None):
        return self._headers.get(name, default)

    def read1(self, size):
        self.reads += 1
        return self._chunks.pop(0) if self._chunks else b""


class FakeSocket:
    def __init__(self):
        self.timeouts = []

    def settimeout(self, value):
        self.timeouts.append(value)


class FakeConnection:
    instances: list["FakeConnection"] = []
    response = FakeResponse()
    error = None

    def __init__(self, host, port, *, timeout, context):
        self.args = (host, port, timeout)
        self.requests = []
        self.closed = False
        self.sock = FakeSocket()
        FakeConnection.instances.append(self)

    def request(self, method, href, body, headers):
        if FakeConnection.error:
            raise FakeConnection.error
        self.requests.append((method, href, body, headers))

    def getresponse(self):
        return FakeConnection.response

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def reset():
    FakeConnection.instances = []
    FakeConnection.response = FakeResponse()
    FakeConnection.error = None


def transport():
    return HttpsDavTransport(ORIGIN, "synthetic_user", "NC-PRIVATE",
                             connection_factory=FakeConnection)


def call(t=None, **kw):
    args = dict(origin=ORIGIN, href="/remote.php/dav/files/synthetic_user/", depth="1",
                body=PROPFIND_BODY, timeout=2.0, max_bytes=100)
    args.update(kw)
    return (t or transport()).propfind(args.pop("origin"), args.pop("href"),
                                       args.pop("depth"), args.pop("body"), **args)


def test_sends_one_propfind_with_fixed_headers_and_closes():
    response = call()
    (conn,) = FakeConnection.instances
    assert conn.args == ("nextcloud.invalid", 443, 2.0) and conn.closed
    ((method, href, body, headers),) = conn.requests
    assert method == "PROPFIND" and body == PROPFIND_BODY
    assert headers["Depth"] == "1" and headers["Accept-Encoding"] == "identity"
    assert headers["Authorization"].startswith("Basic ")
    assert response.status == 207 and response.redirected is False


@pytest.mark.parametrize("origin", [
    "http://nextcloud.invalid", "https://user:pw@nextcloud.invalid",
    "https://nextcloud.invalid/path", "https://nextcloud.invalid?x=1", "ftp://x",
])
def test_rejects_non_https_or_decorated_origin(origin):
    with pytest.raises(ValueError):
        HttpsDavTransport(origin, "u", "p", connection_factory=FakeConnection)


def test_rejects_bad_credentials_and_hides_them_in_repr():
    with pytest.raises(ValueError):
        HttpsDavTransport(ORIGIN, "a:b", "p")
    assert "NC-PRIVATE" not in repr(transport())


@pytest.mark.parametrize("kw,kind", [
    ({"origin": "https://other.invalid"}, "origin_mismatch"),
    ({"depth": "infinity"}, "invalid_request"),
    ({"href": "relative"}, "invalid_request"),
])
def test_request_guards_raise_before_connecting(kw, kind):
    with pytest.raises(DavTransportError) as err:
        call(**kw)
    assert err.value.kind == kind and FakeConnection.instances == []


def test_byte_cap_is_enforced_while_reading():
    FakeConnection.response = FakeResponse(chunks=[b"x" * 60, b"x" * 60, b"x" * 60])
    with pytest.raises(DavTransportError) as err:
        call()
    assert err.value.kind == "oversize"
    assert FakeConnection.response.reads == 2  # stopped at the first overflow


def test_declared_oversize_stops_before_reading():
    FakeConnection.response = FakeResponse(headers={"Content-Length": "101"})
    with pytest.raises(DavTransportError):
        call()
    assert FakeConnection.response.reads == 0


def test_redirect_is_reported_not_followed():
    FakeConnection.response = FakeResponse(status=302, headers={"Location": "https://x"})
    assert call().redirected is True
    assert len(FakeConnection.instances) == 1


@pytest.mark.parametrize("error,kind", [
    (TimeoutError(), "timeout"), (socket.timeout(), "timeout"),
    (ConnectionRefusedError(), "connect"), (OSError(), "connect"),
])
def test_network_errors_map_without_retry(error, kind):
    FakeConnection.error = error
    with pytest.raises(DavTransportError) as err:
        call()
    assert err.value.kind == kind
    assert len(FakeConnection.instances) == 1 and FakeConnection.instances[0].closed


def test_total_deadline_bounds_a_slow_trickle(monkeypatch):
    import obsidian_automation.vault_root_dav_transport as t
    # start, before getresponse, then before each read1
    ticks = iter([0.0, 0.5, 1.0, 1.5, 2.5])
    monkeypatch.setattr(t.time, "monotonic", lambda: next(ticks))
    FakeConnection.response = FakeResponse(chunks=[b"x", b"x", b"x", b"x"])
    with pytest.raises(DavTransportError) as err:
        call(timeout=2.0)
    assert err.value.kind == "timeout"
    (conn,) = FakeConnection.instances
    # each socket wait is shrunk to the time left, so one read cannot overrun
    assert conn.sock.timeouts == [1.5, 1.0, 0.5]
    assert FakeConnection.response.reads == 2


def test_ocs_current_user_is_one_fixed_get():
    from obsidian_automation.vault_root_dav_transport import OCS_USER_PATH
    FakeConnection.response = FakeResponse(status=200, body=b"{}")
    response = transport().ocs_current_user(ORIGIN, timeout=2.0, max_bytes=100)
    ((method, href, body, headers),) = FakeConnection.instances[0].requests
    assert (method, href, body) == ("GET", OCS_USER_PATH, None)
    assert headers["OCS-APIRequest"] == "true" and response.status == 200
    with pytest.raises(DavTransportError):
        transport().ocs_current_user("https://other.invalid", timeout=2.0, max_bytes=100)
