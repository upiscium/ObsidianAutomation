"""One-field-at-a-time setup while retaining atomic, verified provider commits.

Incomplete first-time setup is a private 0600 draft, NOT an active provider.
The browser sees per-field states and explicitly nonsecret configured values;
passwords and access tokens are never returned. Complete configurations go through
the existing provider probe
and single-active-writer admission gates in the HTTP controller.
"""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from urllib.parse import urlsplit

from .snapshot_setup import (
    NEXTCLOUD_FIELDS, GITEA_FIELDS, _atomic, validate_nextcloud, validate,
)
from .webdav_connection import validate_webdav_url

_GROUPS = {
    "nextcloud": frozenset(NEXTCLOUD_FIELDS),
    "gitea": frozenset(GITEA_FIELDS),
    "github": frozenset({"github_token"}),
}
_SECRET = frozenset({"nextcloud_password", "git_token", "github_token"})
_FILE = "connection-field-drafts.json"
_MAX = 8192


class FieldError(ValueError):
    pass


def _validate_field(group: str, field: object, value: object):
    if (group not in _GROUPS or type(field) is not str
        or field not in _GROUPS[group] or type(value) is not str
        or not 1 <= len(value) <= 512
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)):
        raise FieldError("invalid_field")
    if field == "webdav_url":
        try:
            validate_webdav_url(value)
        except ValueError:
            raise FieldError("invalid_field") from None
    elif field == "git_url":
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or not parsed.path.endswith(".git")):
            raise FieldError("invalid_field")
    if field == "vault_folder":
        # "/" = the generic WebDAV URL itself is the Vault root; its
        # vendor restriction is enforced when the whole connection commits.
        if value != "/" and (re.fullmatch(r"[A-Za-z0-9_./ -]{1,100}", value) is None
            or any(p in ("", ".", "..") for p in value.split("/"))):
            raise FieldError("invalid_field")
    if field in ("nextcloud_user","git_user") and ":" in value:
        raise FieldError("invalid_field")
    if field == "branch":
        if (re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,100}",value) is None
            or ".." in value or value.endswith(("/", ".lock"))):
            raise FieldError("invalid_field")
    if field == "github_token":
        # The existing DailySetup token contract remains the authority.
        from .github_daily_setup import DailySetup
        DailySetup.validate_payload({"github_token": value})


class FieldDrafts:
    def __init__(self, store):
        self.store = store
        self.path = store.state / _FILE

    def _load(self):
        flags = os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(self.path, flags)
        except FileNotFoundError:
            return {key: {} for key in _GROUPS}
        except OSError:
            raise FieldError("unreadable_field_draft") from None
        try:
            with os.fdopen(fd, "rb") as stream:
                meta = os.fstat(stream.fileno())
                if (not stat.S_ISREG(meta.st_mode)
                    or stat.S_IMODE(meta.st_mode) != 0o600
                    or meta.st_size > _MAX):
                    raise FieldError("unreadable_field_draft")
                raw = stream.read(_MAX+1)
                if len(raw) > _MAX:
                    raise FieldError("unreadable_field_draft")
            result = json.loads(raw)
        except (OSError, UnicodeError, ValueError):
            raise FieldError("unreadable_field_draft") from None
        if type(result) is not dict or set(result) != {"version", "groups"} or result["version"] != 1:
            raise FieldError("unreadable_field_draft")
        data = result["groups"]
        if type(data) is not dict or set(data) != set(_GROUPS):
            raise FieldError("unreadable_field_draft")
        for group, fields in data.items():
            if type(fields) is not dict or not set(fields) <= _GROUPS[group]:
                raise FieldError("unreadable_field_draft")
            for field, value in fields.items():
                try:
                    _validate_field(group, field, value)
                except ValueError:
                    raise FieldError("unreadable_field_draft") from None
        return data

    def _write(self, groups):
        # _atomic creates mode-0600 and fsyncs the new file and directory.
        serialized = json.dumps({"version": 1, "groups": groups},
                                ensure_ascii=False, separators=(",",":"))
        if len(serialized.encode("utf-8")) > _MAX:
            raise FieldError("draft_limit_exceeded")
        _atomic(self.path, serialized)

    def _active(self, group: str, daily_setup=None):
        if group == "nextcloud":
            return self.store.load_nextcloud()
        if group == "gitea":
            full = self.store.load()
            return {k: full[k] for k in GITEA_FIELDS} if full is not None else None
        if group == "github" and daily_setup is not None and daily_setup.configured():
            # The token is never loaded or returned merely to draw a checkmark.
            return {"github_token": "(private)"}
        return None

    def status(self, group: str, daily_setup=None):
        if group not in _GROUPS:
            raise FieldError("invalid_group")
        drafts = self._load()[group]
        active = self._active(group, daily_setup)
        # An existing GitHub credential may have lost its source-generation
        # binding after a Nextcloud rotation. Never show a green checkmark
        # until DailySetup actually re-verifies it against the active source.
        github_stale = False
        if group == "github" and active is None and "github_token" not in drafts:
            token_path = self.store.state / "github-daily" / "github.token"
            try:
                meta = token_path.lstat()
                github_stale = (stat.S_ISREG(meta.st_mode)
                                and stat.S_IMODE(meta.st_mode) == 0o600)
            except FileNotFoundError:
                pass
            except OSError:
                github_stale = True
        states = {field: ("verified" if active is not None
                          else "saved" if field in drafts
                          else "stale" if github_stale else "missing")
                  for field in sorted(_GROUPS[group])}
        # Only nonsecret values are ever reflected to the private WebUI.
        # An active configuration wins over any older staged draft. Unconfigured
        # fields can show their privately staged nonsecret value before verify.
        # The secret field names never occur as keys here, and even an empty
        # or masked credential is never fabricated as an actual value.
        visible = {
            field: (active if active is not None else drafts)[field]
            for field in sorted(_GROUPS[group] - _SECRET)
            if field in (active if active is not None else drafts)
        }
        return {"version": 1, "fields": states, "values": visible,
                "ready_to_verify": (active is None
                    and set(drafts) == _GROUPS[group]
                    and (group == "nextcloud"
                         or self.store.load_nextcloud() is not None))}

    def prepare(self, group: str, payload: object, daily_setup=None):
        """Return ('unchanged'|'staged'|'commit', candidate-or-None).

        Caller owns the fence and source revision; 'staged' has no active
        credential/canonical mutation. Values are never returned to the UI.
        """
        if type(payload) is not dict or set(payload) != {"field", "value"}:
            raise FieldError("invalid_field")
        field, value = payload["field"], payload["value"]
        _validate_field(group, field, value)
        groups = self._load()
        active = self._active(group, daily_setup)
        if active is not None and active.get(field) == value:
            return "unchanged", None
        candidate = dict(active or groups[group])
        candidate[field] = value
        if set(candidate) != _GROUPS[group]:
            groups[group] = candidate
            self._write(groups)
            return "staged", None
        if group == "nextcloud":
            validate_nextcloud(candidate)
        elif group == "gitea":
            if self.store.load_nextcloud() is None:
                groups[group] = candidate
                self._write(groups)
                return "staged", None
            validate({**self.store.load_nextcloud(), **candidate})
        else:
            _validate_field("github", field, value)
            if self.store.load_nextcloud() is None:
                groups[group] = candidate
                self._write(groups)
                return "staged", None
        return "commit", candidate

    def complete(self, group: str, daily_setup=None):
        """Explicitly verify a complete staged group after prerequisites arrive."""
        if group not in _GROUPS:
            raise FieldError("invalid_group")
        if self._active(group, daily_setup) is not None:
            return "unchanged", None
        candidate = dict(self._load()[group])
        if set(candidate) != _GROUPS[group]:
            raise FieldError("incomplete_settings")
        if group == "nextcloud":
            validate_nextcloud(candidate)
        elif group == "gitea":
            source = self.store.load_nextcloud()
            if source is None:
                raise FieldError("nextcloud_missing")
            validate({**source, **candidate})
        else:
            if self.store.load_nextcloud() is None:
                raise FieldError("nextcloud_missing")
            _validate_field("github", "github_token", candidate["github_token"])
        return "commit", candidate

    def committed(self, group: str):
        groups = self._load()
        if groups[group]:
            groups[group] = {}
            self._write(groups)
