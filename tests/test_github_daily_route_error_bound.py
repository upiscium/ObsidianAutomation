"""Zero-network adversarial HTTP failure-boundary tests for Daily v2 (#334)."""
from __future__ import annotations

import io
import json
import ssl
import urllib.error
import urllib.request

import pytest

from obsidian_automation import github_daily_source_route as route
from obsidian_automation import github_project_watcher as watcher


API = "https://api.github.com"
PATH = "/repos/upiscium/Test/issues/comments/123"
URL = API + PATH


class _UnreadableErrorBody:
    """A synthetic malicious error stream with no safe finite read bound."""
    def __init__(self):
        self.read_attempts = 0
        self.closed = False

    def read(self, *args, **kwargs):
        self.read_attempts += 1
        raise AssertionError("HTTP error body was read without an approved cap")

    def close(self):
        self.closed = True


class _GoodBody:
    def __init__(self, *, raw: bytes, url: str = URL):
        self.raw = raw
        self.url = url
        self.requested_lengths = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def geturl(self):
        return self.url

    def read(self, length):
        self.requested_lengths.append(length)
        return self.raw[:length]


class _Opener:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def open(self, request, *, timeout):
        self.calls.append((request.full_url, timeout))
        if not self.outcomes:
            raise AssertionError("Unexpected additional GitHub API request")
        item = self.outcomes.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _mock_opener(monkeypatch, outcomes):
    opener = _Opener(outcomes)
    observed_handlers = []

    def build_opener(*handlers):
        observed_handlers.append(handlers)
        return opener

    monkeypatch.setattr(route.urllib.request, "build_opener", build_opener)
    return opener, observed_handlers


def _http_error(status, *, body):
    return urllib.error.HTTPError(
        URL, status,
        "MALICIOUS_ERROR_MESSAGE_NEVER_PRINT",  # not for diagnostics
        {"Location": "https://other.invalid/LEAKED_TOKEN"}, body,
    )


@pytest.mark.parametrize("status", [301, 302, 307, 308, 400, 401, 403, 404, 409, 429, 500])
def test_primary_http_errors_never_read_remote_body(monkeypatch, status):
    body = _UnreadableErrorBody()
    error = _http_error(status, body=body)
    opener, seen = _mock_opener(monkeypatch, [error])
    client = route._PinnedGitHubClient(api_base=API, token="synthetic-only")

    with pytest.raises(watcher.GitHubAPIHTTPError) as caught:
        client._request_json(PATH)

    assert caught.value.status == status
    assert caught.value.path == PATH
    assert caught.value.detail == "source HTTP response rejected without body read"
    assert body.read_attempts == 0
    assert body.closed
    assert opener.calls == [(URL, client.timeout_seconds)]
    assert "MALICIOUS_ERROR_MESSAGE" not in str(caught.value)
    assert "LEAKED_TOKEN" not in str(caught.value)
    assert any(isinstance(handler, route._NeverRedirect) for handler in seen[0])
    assert any(isinstance(handler, urllib.request.ProxyHandler)
               and handler.proxies == {} for handler in seen[0])


def test_tls_compat_fallback_http_error_does_not_read_body(monkeypatch):
    body = _UnreadableErrorBody()
    error = _http_error(403, body=body)
    first_tls_failure = urllib.error.URLError(ssl.SSLEOFError(8, "synthetic TLS failure"))
    opener, _ = _mock_opener(monkeypatch, [first_tls_failure, error])
    client = route._PinnedGitHubClient(api_base=API, token="synthetic-only")
    monkeypatch.setattr(
        client, "_tls12_compat_context", lambda: ssl.create_default_context(),
    )

    with pytest.raises(watcher.GitHubAPIHTTPError) as caught:
        client._request_json(PATH)

    assert caught.value.status == 403
    assert caught.value.path == PATH
    assert body.read_attempts == 0
    assert body.closed
    assert [request[0] for request in opener.calls] == [URL, URL]


def test_success_path_remains_json_and_byte_bounded(monkeypatch):
    content = json.dumps({"verified": True}).encode("utf-8")
    response = _GoodBody(raw=content)
    opener, _ = _mock_opener(monkeypatch, [response])
    client = route._PinnedGitHubClient(api_base=API, token="synthetic-only")
    assert client._request_json(PATH) == {"verified": True}
    assert response.requested_lengths == [client._MAX_RESPONSE_BYTES + 1]
    assert response.closed
    assert opener.calls[0][0] == URL


def test_oversized_success_is_rejected_with_single_bounded_read(monkeypatch):
    limit = route._PinnedGitHubClient._MAX_RESPONSE_BYTES
    response = _GoodBody(raw=b"x" * (limit + 1))
    _mock_opener(monkeypatch, [response])
    client = route._PinnedGitHubClient(api_base=API, token="synthetic-only")
    with pytest.raises(route.DailySourceRouteError, match="read bound"):
        client._request_json(PATH)
    assert response.requested_lengths == [limit + 1]
    assert response.closed


@pytest.mark.parametrize("url", [
    "http://api.github.com/repos/upiscium/Test/issues/2",
    "https://evil.invalid/repos/upiscium/Test/issues/2",
    "https://api.github.com.evil.invalid/repos/upiscium/Test/issues/2",
    "https://name:secret@api.github.com/repos/upiscium/Test/issues/2",
    "https://api.github.com:444/repos/upiscium/Test/issues/2",
    "https://api.github.com/users/secret",
])
def test_invalid_destination_rejected_before_network(monkeypatch, url):
    def must_not_build_opener(*args):
        raise AssertionError("Unapproved destination reached network setup")
    monkeypatch.setattr(route.urllib.request, "build_opener", must_not_build_opener)
    client = route._PinnedGitHubClient(api_base=API, token="synthetic-only")
    with pytest.raises(route.DailySourceRouteError, match="external API URL"):
        client._urlopen_bytes(urllib.request.Request(url))


def test_success_with_response_destination_drift_stays_fail_closed(monkeypatch):
    response = _GoodBody(raw=b'{"ok":true}', url="https://evil.invalid/token")
    _mock_opener(monkeypatch, [response])
    client = route._PinnedGitHubClient(api_base=API, token="synthetic-only")
    with pytest.raises(route.DailySourceRouteError, match="changed destination"):
        client._request_json(PATH)
    assert response.closed


class _FaultyCloseBody(_UnreadableErrorBody):
    def close(self):
        self.closed = True
        raise OSError("DO_NOT_LOG_HTTP_RESPONSE_CLOSE_TEXT")


def test_error_stream_close_failure_is_sanitized_and_fails_closed(monkeypatch):
    body = _FaultyCloseBody()
    _mock_opener(monkeypatch, [_http_error(503, body=body)])
    client = route._PinnedGitHubClient(api_base=API, token="synthetic-only")
    with pytest.raises(route.DailySourceRouteError, match="error stream close failed") as caught:
        client._request_json(PATH)
    assert body.read_attempts == 0
    assert body.closed
    assert "DO_NOT_LOG" not in str(caught.value)


def test_invalid_synthetic_http_status_also_closes_unread_error_body(monkeypatch):
    body = _UnreadableErrorBody()
    _mock_opener(monkeypatch, [_http_error("invalid", body=body)])
    client = route._PinnedGitHubClient(api_base=API, token="synthetic-only")
    with pytest.raises(route.DailySourceRouteError, match="HTTP status invalid"):
        client._request_json(PATH)
    assert body.read_attempts == 0
    assert body.closed
