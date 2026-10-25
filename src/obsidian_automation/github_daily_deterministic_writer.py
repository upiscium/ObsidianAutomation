"""Verified deterministic B4 Daily section delivery using the existing Writer.

The source is a private, immutable Standard GitHub CAS capture, NOT an
untrusted browser-supplied Markdown block or optional LLM text. This module
does nothing unless its callable is explicitly invoked by an opted-in app.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re
from typing import Callable

from .github_daily_deterministic_digest import DeterministicDigestError
from .github_daily_progress import (
    DailyProgressConflict, DailyProgressError, HTTPTransport,
    _apply_project_progress_section, daily_target_path,
)
from .github_daily_standard_source import read_standard_digest
from .snapshot_setup import SetupStore, validate_nextcloud
from .webdav_create import WebDAVCreateError, build_target_url

_SHA = re.compile(r"[a-f0-9]{64}\Z")
_GOOD = frozenset(("applied", "recovered", "already_desired"))


@dataclass(frozen=True)
class _Section:
    target_path: str
    section_body: str


def _verified_receipt(value: object) -> tuple[str, str, str]:
    """Admit only a B4 non-publishing Preview for further canonical verification."""
    if (
        type(value) is not dict
        or value.get("version") != 1
        or value.get("status") != "preview_ready"
        or value.get("model_used") is not False
        or value.get("publication_authorized") is not False
        or value.get("vault_modified") is not False
        or any(type(value.get(k)) is not str or _SHA.fullmatch(value[k]) is None
               for k in ("capture_sha256", "digest_sha256"))
        or type(value.get("date_jst")) is not str
    ):
        raise DailyProgressError("invalid source-verified Preview identity")
    target = value["date_jst"]
    try:
        daily_target_path(target)
    except (ValueError, DailyProgressError) as exc:
        raise DailyProgressError("invalid Preview date") from exc
    return target, value["capture_sha256"], value["digest_sha256"]


def publish_verified_daily(
    preview_result: dict,
    *,
    cas_root: Path,
    shared: SetupStore,
    transport: HTTPTransport | None = None,
    timeout: float = 30.0,
) -> dict[str, object]:
    """Apply only one verified B4 section, never create a missing Daily Note.

    A successful receipt proves exact canonical post-GET bytes. The existing
    Writer's strong ETag, If-Match, ambiguous transport handling, frontmatter,
    unique H1 and section-only preservation rules are reused unchanged.
    """
    day, capture_sha, digest_sha = _verified_receipt(preview_result)

    # Re-admit *all* original Standard CAS stage bytes and digest, so a
    # crafted Preview JSON or Markdown filename alone can never cause a PUT.
    try:
        digest = read_standard_digest(cas_root, capture_sha)
    except (ValueError, OSError, DeterministicDigestError) as exc:
        raise DailyProgressError("verified Standard source unavailable") from exc
    if (
        digest.capture_sha256 != capture_sha
        or digest.date != day
        or digest.sha256 != digest_sha
        or digest.report().get("model_used") is not False
        or digest.report().get("verified_cas_source") is not True
    ):
        raise DailyProgressError("Preview and Standard digest identity mismatch")

    source = shared.load_nextcloud()
    if type(source) is not dict:
        raise DailyProgressError("shared Nextcloud source not configured")
    try:
        source = validate_nextcloud(source)
        # The WebDAV base must point to the selected Vault directory, not
        # the user's Nextcloud account root.
        base = build_target_url(source["webdav_url"], source["vault_folder"]) + "/"
    except (ValueError, WebDAVCreateError) as exc:
        raise DailyProgressError("invalid shared Nextcloud Vault root") from exc
    original_source = tuple(source[k] for k in (
        "webdav_url", "vault_folder", "nextcloud_user", "nextcloud_password"))

    # Same section-body convention as the existing local Preview.
    section = _Section(
        target_path=daily_target_path(day),
        section_body=digest.render().rstrip("\n") + "\n\n",
    )
    outcome, before, after = _apply_project_progress_section(
        section, base_url=base,
        username=source["nextcloud_user"],
        password=source["nextcloud_password"],
        timeout=timeout, transport=transport,
    )
    if outcome not in _GOOD:
        raise DailyProgressError("unverified remote publication outcome")
    # A source swap during a long remote write must not leave a green receipt
    # for the *new* source. Normal WebUI operations refuse updates while run
    # is in flight, but this also handles out-of-band changes conservatively.
    updated_source = shared.load_nextcloud()
    if (type(updated_source) is not dict or tuple(updated_source.get(k) for k in (
            "webdav_url", "vault_folder", "nextcloud_user", "nextcloud_password"))
            != original_source):
        raise DailyProgressConflict("Nextcloud source changed during Daily publication",
                                    reason_code="source_changed")

    return {
        "version": 1,
        "status": "published",
        "date_jst": day,
        "capture_sha256": capture_sha,
        "digest_sha256": digest_sha,
        "publication_outcome": outcome,
        "target_path": section.target_path,
        "before_content_sha256": sha256(before).hexdigest(),
        "after_content_sha256": sha256(after).hexdigest(),
        "vault_modified": before != after,
        "model_used": False,
        "publication_verified": True,
    }
