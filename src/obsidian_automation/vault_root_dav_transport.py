"""#303 V1/B2: bounded HTTPS ``PROPFIND``-only transport.

Not constructed by any running service. It only becomes reachable through
``vault_root_dav_verifier`` and therefore only after every provider gate is
VERIFIED. HTTPS only, fixed origin, no proxy, no redirect following, no
retry, no compression, and the byte cap is enforced while reading.
"""
from __future__ import annotations

import base64
import http.client
import ssl
import time
from urllib.parse import urlsplit

from .vault_root_dav_verifier import DavResponse, DavTransportError

_READ_CHUNK = 16 * 1024


class HttpsDavTransport:
    def __init__(self, origin: str, user: str, password: str, *,
                 context: ssl.SSLContext | None = None, connection_factory=None):
        parts = urlsplit(origin)
        if (parts.scheme != "https" or not parts.hostname or parts.path
                or parts.query or parts.fragment or parts.username or parts.password):
            raise ValueError("invalid_origin")
        if (type(user) is not str or type(password) is not str or not user
                or not password or ":" in user
                or any(ord(c) < 32 for c in user + password)):
            raise ValueError("invalid_credentials")
        self._origin = origin
        self._host = parts.hostname
        self._port = parts.port or 443
        token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
        self._authorization = "Basic " + token
        self._context = context or ssl.create_default_context()
        self._factory = connection_factory or http.client.HTTPSConnection

    def __repr__(self) -> str:  # never show credentials in logs/tracebacks
        return "HttpsDavTransport(<redacted>)"

    def propfind(self, origin: str, href: str, depth: str, body: bytes, *,
                 timeout: float, max_bytes: int) -> DavResponse:
        if origin != self._origin:
            raise DavTransportError("origin_mismatch")
        if depth not in ("0", "1") or not href.startswith("/") or not href.isascii():
            raise DavTransportError("invalid_request")
        # Socket timeouts apply per operation; also bound the whole exchange
        # so a slow trickle cannot hold the caller's Job locks indefinitely.
        deadline = time.monotonic() + timeout

        def shrink_timeout() -> None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DavTransportError("timeout")
            if conn.sock is not None:
                conn.sock.settimeout(remaining)

        conn = self._factory(self._host, self._port, timeout=timeout,
                             context=self._context)
        try:
            conn.request("PROPFIND", href, body=body, headers={
                "Authorization": self._authorization,
                "Depth": depth,
                "Content-Type": "application/xml; charset=utf-8",
                "Accept-Encoding": "identity",
            })
            shrink_timeout()
            response = conn.getresponse()
            declared = response.getheader("Content-Length")
            if declared is not None and (not declared.isdecimal()
                                         or int(declared) > max_bytes):
                raise DavTransportError("oversize")
            chunks, total = [], 0
            while True:
                shrink_timeout()
                # read1: at most one socket read, so the deadline is checked
                # between reads instead of after a full buffered chunk.
                part = response.read1(_READ_CHUNK)
                if not part:
                    break
                total += len(part)
                if total > max_bytes:
                    raise DavTransportError("oversize")
                chunks.append(part)
            return DavResponse(
                status=response.status,
                content_type=response.getheader("Content-Type", ""),
                content_encoding=response.getheader("Content-Encoding", ""),
                redirected=300 <= response.status < 400,
                body=b"".join(chunks),
            )
        except DavTransportError:
            raise
        except (TimeoutError, OSError) as err:
            raise DavTransportError(
                "timeout" if isinstance(err, TimeoutError) else "connect") from None
        except (http.client.HTTPException, ValueError):
            raise DavTransportError("connect") from None
        finally:
            conn.close()
