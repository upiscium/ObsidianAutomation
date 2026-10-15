from __future__ import annotations

import json

import pytest

from obsidian_automation.admin_connections import (
    ConnectionSpec, Provider, REGISTRY, connections_projection,
    validate_name, validate_provider, validate_url,
)
from test_admin_dashboard import response, serving


@pytest.mark.parametrize("provider", ["nextcloud", "gitea", "github", "ai"])
def test_provider_allowlist(provider: str) -> None:
    assert validate_provider(provider).value == provider


@pytest.mark.parametrize("invalid", ["GitHub", "openai", "ollama", "", "nextcloud ", None, 1])
def test_provider_rejects_unknown(invalid: object) -> None:
    with pytest.raises(ValueError):
        validate_provider(invalid)


@pytest.mark.parametrize("name", ["nextcloud", "ai-generator_01", "a.b", "a" * 64])
def test_name_accepts_bounded_identifiers(name: str) -> None:
    assert validate_name(name) == name


@pytest.mark.parametrize("invalid", ["", "a" * 65, ".hidden", "ends-", "a..b", "../secret", "a/b", "a b", "a\n", "é", 9, None])
def test_name_rejects_unsafe_identifiers(invalid: object) -> None:
    with pytest.raises(ValueError):
        validate_name(invalid)


@pytest.mark.parametrize("url", [
    "https://nextcloud.internal", "https://private-host:8443/remote.php/dav",
    "https://gitea.example.org/api/v1", "https://github.com", "https://ai.internal/v1",
    "https://sub.example.com/path%20segment",
])
def test_url_accepts_https_hostnames_without_network(url: str) -> None:
    assert validate_url(url) == url


@pytest.mark.parametrize("url", [
    "", "http://private-host", "HTTPS://private-host", "https:/private-host",
    "https:private-host", "https:///path", "https://", "https://u:p@host.example",
    "https://@host.example", "https://host.example?", "https://host.example?q=a",
    "https://host.example#", "https://host.example/#fragment", "https://host.example\\@evil.example",
    "https://bad_host", "https://-bad.example", "https://bad-.example", "https://a..b",
    "https://host.example.", "https://" + "a" * 64 + ".example",
    "https://" + ".".join(["a" * 63] * 5), "https://host.example:0",
    "https://host.example:65536", "https://host.example:abc", "https://host.example:",
    "https://127.0.0.1", "https://127.1", "https://2130706433",
    "https://0x7f000001", "https://0x7f.0.0.1", "https://[::1]", "https://host.example/%GG",
    "https://host.example/%", "https://host.example/space here", " https://host.example",
    "https://host.example\n", "https://host.example/é", "https://host.example/" + "a" * 2048,
    42, None,
])
def test_url_rejects_unsafe_or_malformed_syntax(url: object) -> None:
    with pytest.raises(ValueError):
        validate_url(url)


def test_typed_registry_and_projection_are_fixed_nonsecret_values() -> None:
    assert len(REGISTRY) == 4
    assert all(isinstance(item, ConnectionSpec) and isinstance(item.provider, Provider) for item in REGISTRY)
    with pytest.raises(ValueError):
        ConnectionSpec("test", "github")  # type: ignore[arg-type]
    expected = {
        "version": 1, "authority": "observation_only",
        "connections": [
            {"name": name, "provider": name, "configured": None, "verification": "unknown"}
            for name in ("nextcloud", "gitea", "github", "ai")
        ],
    }
    assert connections_projection() == expected
    assert connections_projection() == expected  # independent fresh projection


def test_connections_http_is_read_only_and_redacted() -> None:
    def prohibited(*args: object, **kwargs: object) -> None:
        raise AssertionError("status observer must not be consulted")

    with serving(prohibited, ai_observer=prohibited) as server:
        code, headers, body = response(server, path="/api/v1/connections")
        assert code == 200
        assert json.loads(body) == connections_projection()
        assert headers["Cache-Control"] == "no-store"
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert "Access-Control-Allow-Origin" not in headers
        for forbidden in (b"secret", b"token", b"endpoint", b"url", b"verified_at"):
            assert forbidden not in body
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            assert response(server, method=method, path="/api/v1/connections")[0] == 405
        assert response(server, method="HEAD", path="/api/v1/connections")[2] == b""
        assert response(server, path="/api/v1/connections?name=github")[0] == 404
        assert response(server, path="/api/v1/connections/github")[0] == 404
        assert response(server, path="/api/v1/connections", host="evil.example")[0] == 403
        assert response(server, path="/api/v1/connections", origin="https://evil.example")[0] == 403
