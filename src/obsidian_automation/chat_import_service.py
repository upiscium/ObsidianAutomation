"""Manual chat-history import panel backend (ChatGPT / Claude export ZIP).

Private-intake only: the ZIP is stored under a 0700 root outside the Vault,
structure-inspected without values, published to the private ready directory
and inventoried by count. No Vault write, no WebDAV, no network, no LLM, no
schedule. Built only with ``--chat-import-ui``; every response is count-only.
"""
from __future__ import annotations

import os
import secrets
import threading
from pathlib import Path
from typing import Callable

from .chatgpt_export_inspect import InspectionError, inspect_export
from .chatgpt_export_intake import (
    IntakeError, InventoryReceipt, inventory_ready, inventory_summary,
    publish_archive,
)
from .chatgpt_export_normalize import PARSER_VERSIONS

PROVIDERS = tuple(PARSER_VERSIONS)
MAX_UPLOAD_BYTES = 2 * 1024**3
_CHUNK = 1024 * 1024


class ImportRefused(ValueError):
    def __init__(self, code: str, http: int = 409):
        super().__init__(code)
        self.code, self.http = code, http


def scope_for(provider: str) -> str:
    # A scope is bound to one provider contract (parser version).
    return "private-" + provider


def _receipt(item: InventoryReceipt) -> dict:
    return {
        "status": item.status, "added": item.added, "changed": item.changed,
        "unchanged": item.unchanged, "revision_only": item.revision_only,
        "absent_not_deleted": item.absent_not_deleted,
        "quarantined_slots": item.quarantined_slots,
        "retained_messages": item.retained_messages,
    }


class ChatImportService:
    def __init__(self, state: Path, *, max_bytes: int = MAX_UPLOAD_BYTES):
        self.root = state / "chat-import"
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    def status(self) -> dict:
        try:
            summary = inventory_summary(self.root)
        except IntakeError as exc:
            return {"version": 1, "enabled": True, "error": str(exc),
                    "providers": list(PROVIDERS), "max_upload_bytes": self.max_bytes,
                    "vault_write": False}
        return {"version": 1, "enabled": True, "providers": list(PROVIDERS),
                "max_upload_bytes": self.max_bytes, "vault_write": False,
                **summary}

    def upload(self, read: Callable[[int], bytes], length: int) -> dict:
        """Store, inspect and publish one ZIP. ``read`` enforces I/O deadlines."""
        if type(length) is not int or length < 1:
            raise ImportRefused("invalid_request", 400)
        if length > self.max_bytes:
            raise ImportRefused("upload_too_large", 413)
        if not self._lock.acquire(blocking=False):
            raise ImportRefused("import_busy")
        temp = None
        try:
            try:
                self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
                incoming = self.root / "incoming"
                incoming.mkdir(mode=0o700, exist_ok=True)
                temp = incoming / (secrets.token_hex(16) + ".part")
                fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | getattr(os, "O_NOFOLLOW", 0), 0o600)
            except OSError:
                raise ImportRefused("import_storage_unavailable", 503) from None
            written = 0
            with os.fdopen(fd, "wb") as out:
                while written < length:
                    chunk = read(min(_CHUNK, length - written))
                    if not chunk:
                        raise ImportRefused("upload_truncated", 400)
                    out.write(chunk)
                    written += len(chunk)
                out.flush()
                os.fsync(out.fileno())
            try:
                report = inspect_export(temp)
            except InspectionError:
                raise ImportRefused("not_a_valid_export", 422) from None
            provider = report.get("provider")
            if provider not in PROVIDERS:
                raise ImportRefused("unsupported_export_format", 422)
            try:
                published = publish_archive(self.root, temp)
            except IntakeError as exc:
                raise ImportRefused(str(exc), 422) from None
            counts = report.get("counts", {})
            return {
                "provider": provider,
                "inspection": report.get("status"),
                "counts": {
                    "conversations": counts.get("conversations", 0),
                    "messages": counts.get("messages", counts.get("claude_messages", 0)),
                    "empty_slots": counts.get("empty_conversation_slots", 0),
                },
                "publish": published.status, "bytes": published.bytes,
                "vault_write": False,
            }
        except OSError:
            raise ImportRefused("import_storage_unavailable", 503) from None
        finally:
            if temp is not None:
                try:
                    os.unlink(temp)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass
            self._lock.release()

    def inventory(self, provider: object, commit: object) -> dict:
        if provider not in PROVIDERS or type(commit) is not bool:
            raise ImportRefused("invalid_request", 400)
        if not self._lock.acquire(blocking=False):
            raise ImportRefused("import_busy")
        try:
            try:
                receipts = inventory_ready(
                    self.root, scope=scope_for(provider), provider=provider,
                    commit=commit, max_archives=16 if commit else 1)
            except IntakeError as exc:
                raise ImportRefused(str(exc), 422) from None
            return {
                "provider": provider,
                "status": "committed_inventory_only" if commit else "preview",
                "runs": [_receipt(item) for item in receipts],
                "generation_jobs_queued": 0, "vault_write": False,
            }
        finally:
            self._lock.release()
