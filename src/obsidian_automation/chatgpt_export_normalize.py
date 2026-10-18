"""Memory-only, private selected-branch normalization for ChatGPT Export.

This is an experimental parser contract for #62, NOT an accepted real-Export
importer or a production submission path. Never serialize/print its objects or
pass them to public logging. There are deliberately no side-effecting CLIs.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Sequence
import zipfile
import zlib

from .chatgpt_export_inspect import (
    InspectionError, Limits, _json, _members, _zip_preflight,
)

NORMALIZER_VERSION = "chatgpt-selected-branch-v0"
_ALLOWED_ROLES = frozenset({"user", "assistant", "system", "developer", "tool"})
_VISIBLE_CONTENT = frozenset({"text", "multimodal_text"})
_SHA = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
_MAX_TEXT_BYTES = 24 * 1024 * 1024
_MAX_MESSAGE_BYTES = 128 * 1024


class NormalizeError(ValueError):
    """Only fixed error codes are used. Never include input text or paths."""


@dataclass(frozen=True, repr=False)
class SelectedMessage:
    node_id: str
    parent_node_id: str | None
    message_id: str
    role: str
    created_at: int | float | None
    content_type: str
    text: str
    text_sha256: str


@dataclass(frozen=True, repr=False)
class SelectedConversation:
    conversation_id: str
    messages: tuple[SelectedMessage, ...]
    selected_path_nodes: int
    off_branch_nodes: int
    unverified_child_links: int
    omitted_reasons: tuple[tuple[str, int], ...]
    content_sha256: str
    source_revision_sha256: str


@dataclass(frozen=True, repr=False)
class SelectedBatch:
    scope: str
    parser_version: str
    archive_sha256: str
    import_run_sha256: str
    conversations: tuple[SelectedConversation, ...]
    selected_json_members: int
    unselected_json_members: int
    quarantined_conversation_slots: int
    quarantine_reasons: tuple[tuple[str, int], ...]
    graph_reciprocity_unverified: bool
    # This MUST NOT be reinterpreted as a real-Export or production acceptance.
    real_schema_accepted: bool = False


@dataclass(frozen=True)
class InventoryDelta:
    added: int
    changed: int
    unchanged: int
    absent_not_deleted: int


def _hash(value: object) -> str:
    data = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _identifier(value: object, code: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise NormalizeError(code)
    return value


def _timestamp(value: object) -> int | float | None:
    if value is not None and (type(value) not in (int, float)):
        raise NormalizeError("unsupported_timestamp")
    return value


def _selected_path(mapping: dict, current: object) -> tuple[list[tuple[str, dict]], int]:
    """Follow selected parent chain. Optional children cannot prove reciprocity."""
    node_id = _identifier(current, "missing_current_node")
    path: list[tuple[str, dict]] = []
    visited: set[str] = set()
    child_id: str | None = None
    unverified_child_links = 0
    while True:
        if node_id in visited:
            raise NormalizeError("selected_branch_cycle")
        visited.add(node_id)
        node = mapping.get(node_id)
        if not isinstance(node, dict):
            raise NormalizeError("selected_branch_missing_node")
        if "id" in node and node["id"] != node_id:
            raise NormalizeError("node_id_mismatch")

        # The actual 2026-10 Export does not contain node.children at all.
        # Missing children is not evidence of reciprocity, and never permits
        # a missing/invalid parent. Track the verification gap explicitly.
        if "children" in node:
            children = node["children"]
            if not isinstance(children, list) or any(not isinstance(x, str) for x in children):
                raise NormalizeError("invalid_branch_children")
            if child_id is not None and child_id not in children:
                raise NormalizeError("nonreciprocal_parent_edge")
        elif child_id is not None:
            unverified_child_links += 1
        if "parent" not in node:
            raise NormalizeError("missing_branch_parent")
        path.append((node_id, node))
        parent = node["parent"]
        if parent is None:
            return list(reversed(path)), unverified_child_links
        node_id = _identifier(parent, "invalid_branch_parent")
        child_id = path[-1][0]


def _select_message(node_id: str, node: dict) -> tuple[SelectedMessage | None, str]:
    message = node.get("message")
    if message is None:
        return None, "placeholder"
    if not isinstance(message, dict):
        raise NormalizeError("invalid_selected_message")
    message_id = _identifier(message.get("id"), "invalid_message_id")
    author = message.get("author")
    if not isinstance(author, dict):
        raise NormalizeError("invalid_message_author")
    role = author.get("role")
    if not isinstance(role, str) or role not in _ALLOWED_ROLES:
        raise NormalizeError("unknown_author_role")

    if role not in {"user", "assistant"}:
        return None, "nonconversation_role"

    metadata = message.get("metadata")
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        raise NormalizeError("invalid_message_metadata")
    hidden = metadata.get("is_visually_hidden_from_conversation", False)
    if type(hidden) is not bool:
        raise NormalizeError("invalid_visibility_metadata")
    if hidden:
        return None, "hidden_message"

    recipient = message.get("recipient")
    channel = message.get("channel")
    if recipient is not None and not isinstance(recipient, str):
        raise NormalizeError("invalid_message_recipient")
    if channel is not None and not isinstance(channel, str):
        raise NormalizeError("invalid_message_channel")
    if role == "assistant" and (recipient not in (None, "all") or channel not in (None, "final")):
        return None, "nonfinal_assistant_message"

    content = message.get("content")
    if not isinstance(content, dict):
        return None, "unsupported_content"
    content_type = content.get("content_type")
    # Real Export contains assistant-only thoughts/reasoning_recap records.
    # They are NOT final user-visible answers or independent knowledge evidence.
    if isinstance(content_type, str) and content_type in {"thoughts", "reasoning_recap"}:
        return None, "internal_reasoning"
    if not isinstance(content_type, str) or content_type not in _VISIBLE_CONTENT:
        return None, "unsupported_content"
    parts = content.get("parts")
    if not isinstance(parts, list) or any(type(part) is not str for part in parts):
        return None, "nontext_parts"
    if not parts or all(not part for part in parts):
        return None, "empty_text"

    text = "\n".join(parts)
    if len(text.encode("utf-8")) > _MAX_MESSAGE_BYTES:
        return None, "oversize_text"
    return SelectedMessage(
        node_id=node_id,
        parent_node_id=node["parent"],
        message_id=message_id,
        role=role,
        created_at=_timestamp(message.get("create_time")),
        content_type=content_type,
        text=text,
        text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    ), ""


def _normalize_one(conversation: object, limits: Limits) -> SelectedConversation:
    if not isinstance(conversation, dict):
        raise NormalizeError("invalid_conversation")
    conv_id = _identifier(conversation.get("id", conversation.get("conversation_id")), "invalid_conversation_id")
    if "conversation_id" in conversation and conversation["conversation_id"] != conv_id:
        raise NormalizeError("conversation_id_mismatch")
    mapping = conversation.get("mapping")
    if not isinstance(mapping, dict) or not mapping or len(mapping) > limits.nodes:
        raise NormalizeError("invalid_conversation_mapping")
    path, unverified_child_links = _selected_path(mapping, conversation.get("current_node"))
    omissions: Counter[str] = Counter()
    retained: list[SelectedMessage] = []
    semantic: list[dict] = []
    seen_message_ids: set[str] = set()
    for node_id, node in path:
        item, omitted = _select_message(node_id, node)
        message = node.get("message")
        message_id = message.get("id") if isinstance(message, dict) else None
        if message_id is not None:
            if message_id in seen_message_ids:
                raise NormalizeError("duplicate_selected_message_id")
            seen_message_ids.add(message_id)
        if omitted:
            omissions[omitted] += 1
            # Internal reasoning, placeholders and unsupported attachments are
            # retained only in the raw archive/source revision, never in
            # semantic content or LLM generation input.
        else:
            assert item is not None
            retained.append(item)
            semantic.append({"role": item.role, "content_type": item.content_type,
                             "content_hash": item.text_sha256})
    return SelectedConversation(
        conversation_id=conv_id,
        messages=tuple(retained),
        selected_path_nodes=len(path),
        off_branch_nodes=len(mapping) - len(path),
        unverified_child_links=unverified_child_links,
        omitted_reasons=tuple(sorted(omissions.items())),
        content_sha256=_hash({"version": NORMALIZER_VERSION, "conversation": conv_id, "path": semantic}),
        # Full parsed conversation revision (including titles, metadata and
        # alternate branches), separate from user-visible semantic contents.
        # The archive_sha256 also binds exact original ZIP bytes.
        source_revision_sha256=_hash({"version": NORMALIZER_VERSION, "source": conversation}),
    )


def normalize_export(
    path: Path, *, scope: str, members: Sequence[str] = (),
    input_format: str = "zip", limits: Limits = Limits(),
) -> SelectedBatch:
    """Read private Export, preserving ONLY selected branch user/final text.

    No writes, no LLM, no logs, no scheduler. Do not use for production until
    separately validated against a real, privately held Export ZIP.
    """
    if not isinstance(scope, str) or not _SHA.fullmatch(scope):
        raise NormalizeError("invalid_source_scope")
    if input_format not in ("zip", "json") or (input_format == "json" and members):
        raise NormalizeError("invalid_input_options")
    if any(type(v) is not int or v <= 0 for v in vars(limits).values()):
        raise NormalizeError("invalid_limits")
    seen_conversations: set[str] = set()
    normalized: list[SelectedConversation] = []
    quarantine: Counter[str] = Counter()
    node_count = 0
    text_bytes = 0

    def consume(root: object) -> None:
        nonlocal node_count, text_bytes
        if not isinstance(root, list):
            raise NormalizeError("unsupported_conversations_root")
        for item in root:
            if len(normalized) + sum(quarantine.values()) >= limits.conversations:
                raise NormalizeError("conversation_limit")
            # One structurally empty {} entry is present in the actual
            # 2026-10 Export. Explicit quarantine: never silently import
            # it or claim full coverage, while allowing valid conversations.
            if item == {}:
                quarantine["empty_conversation_slot"] += 1
                continue
            if isinstance(item, dict):
                mapping = item.get("mapping")
                if isinstance(mapping, dict):
                    node_count += len(mapping)
                    if node_count > limits.nodes:
                        raise NormalizeError("node_limit")
            obj = _normalize_one(item, limits)
            if obj.conversation_id in seen_conversations:
                raise NormalizeError("duplicate_conversation_id")
            seen_conversations.add(obj.conversation_id)
            text_bytes += sum(len(m.text.encode("utf-8")) for m in obj.messages)
            if text_bytes > _MAX_TEXT_BYTES:
                raise NormalizeError("selected_text_limit")
            normalized.append(obj)

    try:
        flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(path, flags), "rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise NormalizeError("input_not_regular")
            bound = limits.archive_bytes if input_format == "zip" else min(limits.member_bytes, limits.total_json_bytes)
            if before.st_size > bound:
                raise NormalizeError("input_size_limit")

            if input_format == "json":
                data = source.read(bound + 1)
                if len(data) > bound:
                    raise NormalizeError("input_size_limit")
                consume(_json(data))
                chosen_names = ["<single-json>"]
                unselected = 0
            else:
                _zip_preflight(source, before.st_size, limits)
                with zipfile.ZipFile(source) as archive:
                    selected, _, unselected = _members(archive, members, limits)
                    chosen_names = [info.filename for info in selected]
                    for info in selected:
                        with archive.open(info) as entry:
                            raw = entry.read(limits.member_bytes + 1)
                        if len(raw) != info.file_size or len(raw) > limits.member_bytes:
                            raise NormalizeError("json_member_size_mismatch")
                        consume(_json(raw))

            archive_hash = hashlib.sha256()
            source.seek(0)
            total = 0
            while block := source.read(1024 * 1024):
                total += len(block)
                if total > before.st_size:
                    raise NormalizeError("input_changed_during_read")
                archive_hash.update(block)
            after = os.fstat(source.fileno())
            fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
            if total != before.st_size or any(getattr(before, f) != getattr(after, f) for f in fields):
                raise NormalizeError("input_changed_during_read")
    except NormalizeError:
        raise
    except InspectionError as exc:
        # Do not include an input path or exception details in default tracebacks.
        raise NormalizeError(str(exc)) from None
    except (OSError, ValueError, EOFError, RuntimeError, NotImplementedError,
            zipfile.BadZipFile, zlib.error, MemoryError):
        raise NormalizeError("input_unreadable_or_corrupt") from None

    raw_digest = archive_hash.hexdigest()
    run_digest = _hash({
        "raw_archive_sha256": raw_digest, "scope": scope,
        "parser": NORMALIZER_VERSION, "selected_members": chosen_names,
    })
    return SelectedBatch(
        scope=scope, parser_version=NORMALIZER_VERSION,
        archive_sha256=raw_digest, import_run_sha256=run_digest,
        conversations=tuple(normalized), selected_json_members=len(chosen_names),
        unselected_json_members=unselected,
        quarantined_conversation_slots=sum(quarantine.values()),
        quarantine_reasons=tuple(sorted(quarantine.items())),
        graph_reciprocity_unverified=any(c.unverified_child_links for c in normalized),
    )


def inventory_delta(previous: SelectedBatch, current: SelectedBatch) -> InventoryDelta:
    """No-delete content delta; renamed archives and titles do not regenerate."""
    if previous.scope != current.scope or previous.parser_version != current.parser_version:
        raise NormalizeError("incompatible_inventory")
    old = {c.conversation_id: c.content_sha256 for c in previous.conversations}
    new = {c.conversation_id: c.content_sha256 for c in current.conversations}
    return InventoryDelta(
        added=len(new.keys() - old.keys()),
        changed=sum(old[k] != new[k] for k in new.keys() & old.keys()),
        unchanged=sum(old[k] == new[k] for k in new.keys() & old.keys()),
        absent_not_deleted=len(old.keys() - new.keys()),
    )
