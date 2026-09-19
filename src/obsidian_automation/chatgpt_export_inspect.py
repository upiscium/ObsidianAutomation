"""Read-only, value-free structure inspection; NOT an Export importer.

Run with Python 3.11+ as a module or as this standalone file. No network calls,
archive extraction, output file writes, or lifecycle/credential access.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import stat
import struct
import sys
from typing import BinaryIO, Sequence
import zipfile
import zlib


class InspectionError(ValueError):
    """Messages are fixed error codes, never values from input or OS errors."""


@dataclass(frozen=True)
class Limits:
    archive_bytes: int = 4 * 1024**3
    directory_bytes: int = 8 * 1024**2
    entries: int = 20000
    member_bytes: int = 128 * 1024**2
    total_json_bytes: int = 256 * 1024**2
    compression_ratio: int = 200
    conversations: int = 50000
    nodes: int = 500000


CANDIDATE = re.compile(r"conversations(?:[-_][0-9]+)?\.json\Z")
FIELDS = {
    "conversation": ("id", "conversation_id", "title", "create_time", "update_time", "mapping", "current_node"),
    "node": ("id", "parent", "children", "message"),
    "message": ("id", "author", "create_time", "update_time", "content", "metadata", "recipient", "channel", "status"),
    "author": ("role", "name", "metadata"),
    "content": ("content_type", "parts"),
}
ROLES = ("user", "assistant", "system", "developer", "tool")
CONTENT_TYPES = ("text", "multimodal_text", "image", "audio", "audio_transcription", "tether_browsing_display_text")


def _type(value: object) -> str:
    return {type(None): "null", bool: "boolean", int: "number", float: "number", str: "string", list: "array", dict: "object"}[type(value)]


def _pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise InspectionError("duplicate_json_key")
        result[key] = value
    return result


def _constant(_value: str) -> None:
    raise InspectionError("nonfinite_json_number")


def _float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise InspectionError("nonfinite_json_number")
    return result


def _json(data: bytes) -> object:
    try:
        return json.loads(data.decode("utf-8-sig"), object_pairs_hook=_pairs, parse_constant=_constant, parse_float=_float)
    except InspectionError:
        raise
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise InspectionError("invalid_or_overdeep_json") from exc


def _safe_name(name: str) -> bool:
    if not name or len(name) > 1024 or "\\" in name or ":" in name:
        return False
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        return False
    return all(part not in {"", ".", ".."} for part in name.rstrip("/").split("/"))


def _zip_preflight(source: BinaryIO, size: int, limits: Limits) -> None:
    # Bound the central-directory allocation BEFORE constructing ZipFile.
    source.seek(max(0, size - 65557))
    tail = source.read(65557)
    pos = tail.rfind(b"PK\x05\x06")
    if pos < 0 or len(tail) - pos < 22:
        raise InspectionError("invalid_zip_directory")
    _, disk, start_disk, disk_count, count, length, offset, comment = struct.unpack_from("<4s4H2LH", tail, pos)
    if len(tail) - pos != 22 + comment:
        raise InspectionError("invalid_zip_directory")
    if disk or start_disk or disk_count != count:
        raise InspectionError("multidisk_zip_unsupported")
    if tail[max(0, pos - 20):pos - 16] == b"PK\x06\x07" or count == 65535 or length == 0xFFFFFFFF or offset == 0xFFFFFFFF:
        raise InspectionError("zip64_directory_unsupported")
    if count > limits.entries or length > limits.directory_bytes or offset + length > size:
        raise InspectionError("zip_directory_limit")
    source.seek(0)


def _members(archive: zipfile.ZipFile, selected: Sequence[str], limits: Limits) -> tuple[list[zipfile.ZipInfo], int, int]:
    entries = archive.infolist()
    if len(entries) > limits.entries:
        raise InspectionError("zip_entry_limit")
    seen = set()
    json_members = []
    for info in entries:
        name = info.orig_filename
        if not _safe_name(name) or name != info.filename:
            raise InspectionError("unsafe_zip_member")
        folded = name.rstrip("/").casefold()
        if folded in seen:
            raise InspectionError("duplicate_zip_member")
        seen.add(folded)
        kind = stat.S_IFMT(info.external_attr >> 16)
        if kind not in {0, stat.S_IFREG, stat.S_IFDIR}:
            raise InspectionError("nonregular_zip_member")
        if info.flag_bits & 1:
            raise InspectionError("encrypted_zip_unsupported")
        if not info.is_dir() and name.endswith(".json"):
            json_members.append(info)
    if selected:
        if len(set(selected)) != len(selected) or any(not _safe_name(n) for n in selected):
            raise InspectionError("invalid_member_selection")
        available = {info.filename: info for info in json_members}
        if any(n not in available for n in selected):
            raise InspectionError("selected_json_member_missing")
        chosen = [available[n] for n in selected]
    else:
        chosen = [i for i in json_members if CANDIDATE.fullmatch(i.filename)]
        if any(i.filename == "conversations.json" for i in chosen) and len(chosen) > 1:
            raise InspectionError("ambiguous_member_selection")
    if not chosen:
        raise InspectionError("conversation_member_selection_required")
    total = 0
    for info in chosen:
        if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
            raise InspectionError("selected_compression_unsupported")
        if info.file_size > limits.member_bytes:
            raise InspectionError("json_member_limit")
        if info.file_size > limits.compression_ratio * max(1, info.compress_size):
            raise InspectionError("json_compression_ratio_limit")
        total += info.file_size
    if total > limits.total_json_bytes:
        raise InspectionError("total_json_limit")
    return sorted(chosen, key=lambda i: i.filename), len(entries), len(json_members) - len(chosen)


def _chain(mapping: dict, current: object) -> tuple[str, int]:
    if not isinstance(current, str) or current not in mapping:
        return "missing_current_node", 0
    seen = set()
    while current is not None:
        if not isinstance(current, str):
            return "invalid_parent", len(seen)
        if current in seen:
            return "cycle", len(seen)
        if current not in mapping:
            return "missing_parent", len(seen)
        seen.add(current)
        node = mapping[current]
        if not isinstance(node, dict) or "parent" not in node:
            return "invalid_node", len(seen)
        current = node["parent"]
    return "resolved", len(seen)


class _Profile:
    def __init__(self, limits: Limits):
        self.limits = limits
        self.counts: Counter = Counter()
        self.types = {scope: {key: Counter() for key in keys} for scope, keys in FIELDS.items()}
        self.unknown = Counter()
        self.roles = Counter()
        self.content_types = Counter()
        self.chains = Counter()
        self.parts = Counter()
        self.review = False

    def fields(self, scope: str, obj: dict) -> None:
        for key, counts in self.types[scope].items():
            counts[_type(obj[key]) if key in obj else "missing"] += 1
        self.unknown[scope] += sum(key not in FIELDS[scope] for key in obj)

    def consume(self, root: object) -> None:
        if not isinstance(root, list):
            self.counts["unsupported_roots"] += 1
            self.review = True
            return
        for conv in root:
            self.counts["conversations"] += 1
            if self.counts["conversations"] > self.limits.conversations:
                raise InspectionError("conversation_limit")
            if not isinstance(conv, dict):
                self.counts["nonobject_conversations"] += 1
                self.review = True
                continue
            self.fields("conversation", conv)
            mapping = conv.get("mapping")
            if not isinstance(mapping, dict):
                self.counts["missing_or_invalid_mapping"] += 1
                self.review = True
                continue
            self.counts["nodes"] += len(mapping)
            if self.counts["nodes"] > self.limits.nodes:
                raise InspectionError("node_limit")
            status, length = _chain(mapping, conv.get("current_node"))
            self.chains[status] += 1
            self.counts["selected_path_nodes"] += length
            self.review |= status != "resolved"
            for node in mapping.values():
                if not isinstance(node, dict):
                    self.counts["nonobject_nodes"] += 1
                    self.review = True
                    continue
                self.fields("node", node)
                children = node.get("children")
                if isinstance(children, list) and len(children) > 1:
                    self.counts["branching_nodes"] += 1
                message = node.get("message")
                if message is None:
                    self.counts["placeholder_nodes"] += 1
                    continue
                if not isinstance(message, dict):
                    self.counts["nonobject_messages"] += 1
                    self.review = True
                    continue
                self.counts["messages"] += 1
                self.fields("message", message)
                author = message.get("author")
                if isinstance(author, dict):
                    self.fields("author", author)
                    role = author.get("role")
                    self.roles[role if isinstance(role, str) and role in ROLES else "other"] += 1
                else:
                    self.roles["missing_or_invalid"] += 1
                content = message.get("content")
                if isinstance(content, dict):
                    self.fields("content", content)
                    kind = content.get("content_type")
                    self.content_types[kind if isinstance(kind, str) and kind in CONTENT_TYPES else "other"] += 1
                    parts = content.get("parts")
                    if isinstance(parts, list):
                        self.parts.update(_type(part) for part in parts)
                else:
                    self.content_types["missing_or_invalid"] += 1

    def result(self) -> dict:
        self.review |= any(self.roles.get(k, 0) or self.content_types.get(k, 0) for k in ("other", "missing_or_invalid"))
        return {
            "record_version": 1, "inspection_only": True,
            "status": "schema_review_required" if self.review or not self.counts["conversations"] else "inspected",
            "counts": dict(self.counts), "known_field_types": self.types,
            "unknown_field_occurrences": dict(self.unknown),
            "roles": dict(self.roles), "content_types": dict(self.content_types),
            "parts_types": dict(self.parts), "selected_parent_chains": dict(self.chains),
            "full_graph_validated": False, "import_compatibility_established": False,
        }


def inspect_export(path: Path, *, members: Sequence[str] = (), input_format: str = "zip", limits: Limits = Limits()) -> dict:
    if input_format not in {"zip", "json"} or (input_format == "json" and members):
        raise InspectionError("invalid_input_options")
    if any(type(value) is not int or value <= 0 for value in vars(limits).values()):
        raise InspectionError("invalid_limits")
    profile = _Profile(limits)
    try:
        # O_NONBLOCK avoids hanging on a FIFO; fstat rejects non-regular files.
        flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(path, flags), "rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise InspectionError("input_not_regular")
            if before.st_size > (limits.archive_bytes if input_format == "zip" else limits.member_bytes):
                raise InspectionError("input_size_limit")
            if input_format == "json":
                data = source.read(min(limits.member_bytes, limits.total_json_bytes) + 1)
                if len(data) > min(limits.member_bytes, limits.total_json_bytes):
                    raise InspectionError("json_member_limit")
                profile.consume(_json(data))
                selection = {"mode": "json_file", "selected_json_members": 1, "unselected_json_members": 0}
            else:
                _zip_preflight(source, before.st_size, limits)
                with zipfile.ZipFile(source) as archive:
                    chosen, count, unselected = _members(archive, members, limits)
                    for info in chosen:
                        with archive.open(info) as entry:
                            data = entry.read(limits.member_bytes + 1)
                        if len(data) != info.file_size or len(data) > limits.member_bytes:
                            raise InspectionError("json_member_size_mismatch")
                        profile.consume(_json(data))
                    selection = {"mode": "explicit_members" if members else "candidate_names", "archive_entries": count,
                                 "selected_json_members": len(chosen), "unselected_json_members": unselected}
            after = os.fstat(source.fileno())
            fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
            if any(getattr(before, f) != getattr(after, f) for f in fields):
                raise InspectionError("input_changed_during_read")
    except InspectionError:
        raise
    except (OSError, ValueError, zipfile.BadZipFile, NotImplementedError, RuntimeError, EOFError, zlib.error) as exc:
        raise InspectionError("input_unreadable_or_corrupt") from exc
    report = profile.result()
    report["selection"] = selection
    return report


class _Parser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise InspectionError("invalid_arguments")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _Parser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--format", choices=("zip", "json"), default="zip")
    parser.add_argument("--member", action="append", default=[])
    try:
        args = parser.parse_args(argv)
        report = inspect_export(args.input, members=args.member, input_format=args.format)
    except (InspectionError, MemoryError) as exc:
        code = str(exc) if isinstance(exc, InspectionError) else "memory_limit"
        print(json.dumps({"record_version": 1, "inspection_only": True, "status": "error", "error_code": code}, sort_keys=True))
        return 2
    print(json.dumps(report, sort_keys=True, indent=2))
    return 3 if report["status"] == "schema_review_required" else 0


if __name__ == "__main__":
    sys.exit(main())
