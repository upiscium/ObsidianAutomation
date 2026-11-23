"""Bounded, provider-neutral WebDAV URL and collection admission.

The saved WebDAV endpoint is a directory URL to which the selected Vault
folder is appended. Connection admission performs one read-only Depth:0
PROPFIND and requires exactly one matching DAV:collection. It never follows
redirects with the Basic credential, and never writes provider content.
"""
from __future__ import annotations

import base64
import http.client
import re
import urllib.error
import urllib.request
from urllib.parse import quote, unquote, urlsplit
from xml.etree import ElementTree as ET


DAV = "{DAV:}"
MAX_XML_BYTES = 64 * 1024
PROPFIND_BODY = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<d:propfind xmlns:d="DAV:"><d:prop>'
    b'<d:resourcetype/></d:prop></d:propfind>'
)


def validate_webdav_url(value: object) -> str:
    """Permit a generic HTTPS WebDAV directory, not only Nextcloud's path.

    The conservative canonical path rules avoid dot/encoded separator aliases
    because a later rclone or provider could normalize them differently.
    """
    if (type(value) is not str or not 1 <= len(value) <= 512
            or any(ord(ch) < 33 or ord(ch) == 127 for ch in value)
            or "\\" in value):
        raise ValueError("invalid_webdav_url")
    try:
        url = urlsplit(value)
        # Accessing .port and .hostname also checks malformed IPv6/port syntax.
        port, host = url.port, url.hostname
    except ValueError:
        raise ValueError("invalid_webdav_url") from None
    if (url.scheme != "https" or not host or url.username is not None
            or url.password is not None or url.query or url.fragment
            or not url.path.startswith("/") or not url.path.endswith("/")
            or "//" in url.path or not url.netloc
            or "#" in value or "?" in value or url.netloc.endswith(":")
            or (port is not None and not 1 <= port <= 65535)):
        raise ValueError("invalid_webdav_url")
    # No arbitrary percent sequence, encoded slash/backslash, dot-segment or
    # nested escaping. Canonicalization must not widen path authority.
    for raw in url.path.strip("/").split("/"):
        if not raw:
            continue
        if re.search(r"%(?![a-fA-F0-9]{2})", raw):
            raise ValueError("invalid_webdav_url")
        segment = unquote(raw)
        if (segment in (".", "..") or "%" in segment
                or "/" in segment or "\\" in segment
                or any(ord(c) < 32 or ord(c) == 127 for c in segment)):
            raise ValueError("invalid_webdav_url")
    return value


def rclone_vendor(url: str) -> str:
    """Preserve existing Nextcloud installations; use rclone's plain DAV mode.

    The path is only a backwards-compatibility hint for the rclone adapter,
    not provider identity proof or permission to use Nextcloud-only methods.
    """
    validate_webdav_url(url)
    path = urlsplit(url).path
    return "nextcloud" if path.startswith("/remote.php/dav/files/") else "other"


def vault_collection_url(base_url: str, vault_folder: str) -> str:
    validate_webdav_url(base_url)
    # vault_folder is separately schema-validated by snapshot_setup.
    return base_url + "/".join(quote(p, safe="") for p in vault_folder.split("/")) + "/"


def _collection_multistatus(raw: bytes, target_url: str) -> bool:
    if not 0 < len(raw) <= MAX_XML_BYTES:
        return False
    # Untrusted provider XML must not expand DTD-defined entities even when
    # the original wire response is bounded. Decode *only* UTF-8 and refuse
    # declarations, comments, and external/internal DTDs before XML parsing.
    # None is needed in the fixed DAV collection admission response.
    try:
        decoded = raw.decode("utf-8", "strict")
        if ("<!" in decoded or "\x00" in decoded or decoded.startswith("\ufeff")):
            return False
        declaration = re.match(r"\s*<\?xml\b([^?]*)\?>", decoded, re.I)
        if declaration is not None:
            declared = re.search(
                r"""\bencoding\s*=\s*['\"]([^'\"]+)['\"]""",
                declaration.group(1), re.I,
            )
            if declared and declared.group(1).lower() != "utf-8":
                return False
        root = ET.fromstring(decoded)
        if root.tag != DAV + "multistatus":
            return False
        responses = root.findall(DAV + "response")
        if len(responses) != 1:
            return False
        row = responses[0]
        # A response-level error with an unrelated successful propstat is
        # ambiguous and must not grant root collection authority.
        if row.find(DAV + "status") is not None:
            return False
        hrefs = row.findall(DAV + "href")
        if len(hrefs) != 1 or not isinstance(hrefs[0].text, str):
            return False
        href_text = hrefs[0].text
        if any(ord(ch) < 33 or ord(ch) == 127 for ch in href_text):
            return False
        href, want = urlsplit(href_text), urlsplit(target_url)
        if href.scheme or href.netloc:
            if (href.scheme != want.scheme
                    or href.netloc.lower() != want.netloc.lower()):
                return False
        # Compare canonical per-segment URI paths, never one fully decoded
        # string. Servers legitimately re-encode a resource differently (e.g.
        # Nextcloud returns "(" and "@" raw, "%27" for "'", lowercase hex),
        # so each segment is decoded and re-encoded canonically. Encoded
        # separators (%2F / %5C), control characters and invalid UTF-8 are
        # refused so a segment can never become a different hierarchy.
        def normalized_path(path: str) -> str | None:
            if (not path.startswith("/")
                    or re.search(r"%(?![0-9a-fA-F]{2})", path)
                    or re.search(r"%(?:2[fF]|5[cC])", path)
                    or "\\" in path):
                return None
            segments = []
            for segment in path.split("/"):
                try:
                    decoded = unquote(segment, errors="strict")
                except UnicodeDecodeError:
                    return None
                if (decoded in (".", "..")
                        or any(ord(ch) < 0x20 or ord(ch) == 0x7F
                               for ch in decoded)):
                    return None
                segments.append(quote(decoded, safe=""))
            return "/".join(segments)

        expected = normalized_path(want.path)
        if (expected is None or href.query or href.fragment
                or normalized_path(href.path) != expected):
            return False
        # A 207 is not success by itself. Exact resource-type 200 is required.
        matches = []
        for propstat in row.findall(DAV + "propstat"):
            status = (propstat.findtext(DAV + "status") or "").strip()
            prop = propstat.find(DAV + "prop")
            if prop is None or len(prop.findall(DAV + "resourcetype")) != 1:
                continue
            resourcetype = prop.find(DAV + "resourcetype")
            if (status not in ("HTTP/1.1 200 OK", "HTTP/2 200 OK", "HTTP/2.0 200 OK")
                    or [child.tag for child in resourcetype] != [DAV + "collection"]):
                return False
            matches.append(propstat)
        return len(matches) == 1
    except (ET.ParseError, ValueError, TypeError):
        return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def probe_webdav_collection(value: dict) -> bool:
    """One authenticated, no-redirect, bounded metadata read of the Vault."""
    try:
        url = vault_collection_url(value["webdav_url"], value["vault_folder"])
        username, password = value["nextcloud_user"], value["nextcloud_password"]
        if type(username) is not str or type(password) is not str:
            return False
        basic = base64.b64encode((username + ":" + password).encode()).decode()
        request = urllib.request.Request(
            url, data=PROPFIND_BODY, method="PROPFIND", headers={
                "Depth": "0", "Authorization": "Basic " + basic,
                "Content-Type": "application/xml; charset=utf-8",
            },
        )
        # Bypass ambient HTTP(S)_PROXY; provider credential admission must
        # reach the explicitly configured HTTPS origin, never an injected
        # sidecar or operator-shell proxy.
        with urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect(),
        ).open(request, timeout=8) as response:
            if response.status != 207:
                return False
            mime = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if mime not in ("application/xml", "text/xml"):
                return False
            raw = response.read(MAX_XML_BYTES + 1)
        return _collection_multistatus(raw, url)
    except (OSError, urllib.error.URLError, http.client.HTTPException,
            ValueError, UnicodeError, KeyError):
        return False
