"""Static, non-secret Connections contract; no live config or credential access.

Validation here is syntax-only. It does not authorize network requests, prove
that a destination is safe, or indicate that a connection has been verified.
"""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlsplit


class Provider(str, Enum):
    NEXTCLOUD = "nextcloud"
    GITEA = "gitea"
    GITHUB = "github"
    AI = "ai"


_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,62}[A-Za-z0-9])?\Z", re.ASCII)
_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z", re.ASCII)
_BAD_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")
_PATH = re.compile(r"/[A-Za-z0-9/._~!$&'()*+,;=:@%-]*\Z", re.ASCII)
_IP_NUMBER = re.compile(r"(?:[0-9]+|0[xX][0-9A-Fa-f]+)\Z", re.ASCII)


def validate_provider(value: object) -> Provider:
    """Accept only the four exact, known provider IDs."""
    if not isinstance(value, str):
        raise ValueError("invalid provider")
    try:
        return Provider(value)
    except ValueError as exc:
        raise ValueError("invalid provider") from exc


def validate_name(value: object) -> str:
    """Validate a bounded ASCII identifier, not an account or secret value."""
    if not isinstance(value, str) or not _NAME.fullmatch(value) or ".." in value:
        raise ValueError("invalid connection name")
    return value


def validate_url(value: object) -> str:
    """Validate HTTPS endpoint syntax only; never resolve, connect, or read files."""
    if not isinstance(value, str) or not 1 <= len(value) <= 2048:
        raise ValueError("invalid connection URL")
    if not value.startswith("https://") or any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in value):
        raise ValueError("invalid connection URL")
    if any(c in value for c in "\\?#"):
        raise ValueError("invalid connection URL")
    try:
        parts = urlsplit(value)
        host = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise ValueError("invalid connection URL") from exc
    if (parts.scheme != "https" or not host or "@" in parts.netloc
            or not 1 <= len(host) <= 253 or not host.isascii()
            or not all(_LABEL.fullmatch(label) for label in host.split("."))
            # Numeric IPv4 aliases (127.1, 2130706433, 0x7f000001) are
            # interpreted as IP addresses by some HTTP stacks.
            or all(_IP_NUMBER.fullmatch(label) for label in host.split("."))
            or (port is not None and not 1 <= port <= 65535)
            or (":" in parts.netloc and port is None)
            or (parts.path and (not _PATH.fullmatch(parts.path) or _BAD_PERCENT.search(parts.path)))):
        raise ValueError("invalid connection URL")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("invalid connection URL")
    return value


@dataclass(frozen=True, slots=True)
class ConnectionSpec:
    name: str
    provider: Provider

    def __post_init__(self) -> None:
        validate_name(self.name)
        # Enum is a str subclass; disallow untyped strings in the registry.
        if not isinstance(self.provider, Provider):
            raise ValueError("invalid provider")


REGISTRY: tuple[ConnectionSpec, ...] = (
    ConnectionSpec("nextcloud", Provider.NEXTCLOUD),
    ConnectionSpec("gitea", Provider.GITEA),
    ConnectionSpec("github", Provider.GITHUB),
    ConnectionSpec("ai", Provider.AI),
)


def connections_projection() -> dict[str, object]:
    """Only static registry membership is known; no live configuration is read."""
    return {
        "version": 1,
        "authority": "observation_only",
        "connections": [
            {"name": spec.name, "provider": spec.provider.value,
             "configured": None, "verification": "unknown"}
            for spec in REGISTRY
        ],
    }
