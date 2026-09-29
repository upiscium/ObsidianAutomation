from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping, Sequence

from .artifact_lifecycle import (
    ArtifactLifecycleError,
    _canonical_json_bytes,
    _decode_json_object,
    _read_exact_file,
    _require_safe_directory,
    _require_sha256,
    _store_immutable,
    sha256_bytes,
)
from .production_io import ProductionIOError, mirror_read_lock


INDEX_STAGE = "04-Index"
SEMANTIC_CORPUS_DIR = "semantic-corpus"
MANIFEST_VERSION = 1
CHUNK_POLICY_VERSION = "heading-section-lf-v0"
DAILY_ROOT = "00-DailyNote"
IDEA_ROOT = "05-Idea"
PROJECT_ROOT = "10-Project"
KNOWLEDGE_ROOT = "11-Knowledge"
SOURCE_ROOTS = (DAILY_ROOT, IDEA_ROOT, PROJECT_ROOT, KNOWLEDGE_ROOT)
MAX_DOCUMENTS = 8192
MAX_SOURCE_BYTES = 128 * 1024
MAX_CHUNK_BYTES = 16 * 1024
MAX_CHUNKS = 32768
_ALLOWED_PROJECT_STATUSES = {
    "planning",
    "running",
    "stopped",
    "stable",
    "done",
    "cancelled",
}
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$")
_H1_RE = re.compile(r"^#[ \t]+(.+?)[ \t]*$")
_META_BIND_FENCE_RE = re.compile(r"^(?P<fence>`{3,}|~{3,})meta-bind(?:-|$)")


class SemanticCorpusError(ArtifactLifecycleError):
    """Raised when the semantic corpus cannot be derived safely."""


@dataclass(frozen=True)
class SemanticChunk:
    chunk_id: str
    ordinal: int
    start_line: int
    end_line: int
    heading_path: tuple[str, ...]
    content_sha256: str
    byte_size: int

    def payload(self) -> dict[str, object]:
        return {
            "chunk_id": self.chunk_id,
            "ordinal": self.ordinal,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "heading_path": list(self.heading_path),
            "content_sha256": self.content_sha256,
            "byte_size": self.byte_size,
        }


@dataclass(frozen=True)
class SemanticSource:
    path: str
    source_kind: str
    content_sha256: str
    byte_size: int
    metadata: Mapping[str, object]
    chunks: tuple[SemanticChunk, ...]

    def payload(self) -> dict[str, object]:
        return {
            "path": self.path,
            "source_kind": self.source_kind,
            "content_sha256": self.content_sha256,
            "byte_size": self.byte_size,
            "metadata": dict(sorted(self.metadata.items())),
            "chunks": [chunk.payload() for chunk in self.chunks],
        }


@dataclass(frozen=True)
class SemanticCorpusManifest:
    sources: tuple[SemanticSource, ...]
    warnings: tuple[str, ...]

    def to_json_bytes(self) -> bytes:
        return _canonical_json_bytes(
            {
                "record_version": MANIFEST_VERSION,
                "chunk_policy": CHUNK_POLICY_VERSION,
                "sources": [source.payload() for source in self.sources],
                "warnings": list(self.warnings),
            }
        )


def _plain_scalar(raw: str) -> str:
    value = raw.strip()
    if " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _frontmatter(text: str, *, path: str) -> tuple[dict[str, str], int]:
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, 1
    values: dict[str, str] = {}
    for index, line in enumerate(lines[1:128], start=1):
        if line.strip() == "---":
            return values, index + 2
        if not line or line[0].isspace() or ":" not in line:
            continue
        key, raw = line.split(":", 1)
        key = key.strip()
        if not key:
            continue
        if key in values:
            raise SemanticCorpusError(f"duplicate frontmatter key in {path}: {key}")
        values[key] = _plain_scalar(raw)
    raise SemanticCorpusError(f"unterminated frontmatter in {path}")


def _normalized_text(data: bytes, *, path: str) -> str:
    if len(data) > MAX_SOURCE_BYTES:
        raise SemanticCorpusError(
            f"semantic source exceeds {MAX_SOURCE_BYTES} bytes: {path}"
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SemanticCorpusError(f"semantic source is not UTF-8: {path}") from exc
    text = text.replace("\r\n", "\n")
    if "\r" in text:
        raise SemanticCorpusError(f"semantic source contains lone CR: {path}")
    return text


def _safe_tree_files(vault_root: Path, root_name: str) -> list[tuple[str, bytes]]:
    root = vault_root.absolute() / root_name
    try:
        root_info = root.lstat()
    except FileNotFoundError:
        return []
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise SemanticCorpusError(f"semantic source root is unsafe: {root_name}")

    rows: list[tuple[str, bytes]] = []
    for directory, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        directory_path = Path(directory)
        folded: dict[str, str] = {}
        for name in [*dirnames, *filenames]:
            previous = folded.get(name.casefold())
            if previous is not None and previous != name:
                raise SemanticCorpusError(
                    f"case-fold collision in {root_name}: {previous!r} / {name!r}"
                )
            folded[name.casefold()] = name

        visible_dirs: list[str] = []
        for name in sorted(dirnames, key=lambda value: (value.casefold(), value)):
            child = directory_path / name
            info = child.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise SemanticCorpusError(f"symlink directory is not allowed: {child}")
            if stat.S_ISDIR(info.st_mode) and not name.startswith("."):
                visible_dirs.append(name)
        dirnames[:] = visible_dirs

        for name in sorted(filenames, key=lambda value: (value.casefold(), value)):
            if name.startswith(".") or not name.endswith(".md"):
                continue
            path = directory_path / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise SemanticCorpusError(f"symlink source is not allowed: {path}")
            if not stat.S_ISREG(info.st_mode):
                continue
            if info.st_size > MAX_SOURCE_BYTES:
                continue
            data = path.read_bytes()
            if len(data) > MAX_SOURCE_BYTES:
                continue
            rows.append((path.relative_to(vault_root.absolute()).as_posix(), data))
            if len(rows) > MAX_DOCUMENTS:
                raise SemanticCorpusError(
                    f"semantic corpus exceeds {MAX_DOCUMENTS} Markdown files"
                )
    rows.sort(key=lambda item: (item[0].casefold(), item[0]))
    return rows


def _wikilink_target(value: str) -> str | None:
    value = value.strip()
    if not (value.startswith("[[") and value.endswith("]]")):
        return None
    target = value[2:-2].split("|", 1)[0].split("#", 1)[0].strip()
    if target.endswith(".md"):
        target = target[:-3]
    return target or None


def _project_maps(
    project_rows: Sequence[tuple[str, bytes]],
) -> tuple[dict[str, str], dict[str, tuple[str, ...]]]:
    by_path: dict[str, str] = {}
    basenames: dict[str, list[str]] = {}
    for path, data in project_rows:
        text = _normalized_text(data, path=path)
        fm, _ = _frontmatter(text, path=path)
        if fm.get("type") != "project":
            continue
        status = fm.get("status", "")
        if status not in _ALLOWED_PROJECT_STATUSES:
            continue
        stem = path[:-3]
        key = stem.casefold()
        by_path[key] = status
        basenames.setdefault(PurePosixPath(stem).name.casefold(), []).append(key)
    return by_path, {
        key: tuple(sorted(values))
        for key, values in basenames.items()
    }


def _resolve_project_status(
    note_path: str,
    project_ref: str,
    projects_by_path: Mapping[str, str],
    projects_by_basename: Mapping[str, tuple[str, ...]],
) -> str | None:
    target = _wikilink_target(project_ref)
    if target is None:
        return None
    exact = target.casefold()
    if exact in projects_by_path:
        return projects_by_path[exact]
    if "/" in target and not target.startswith(PROJECT_ROOT + "/"):
        candidate = (PurePosixPath(note_path).parent / PurePosixPath(target)).as_posix()
        normalized = str(PurePosixPath(candidate)).casefold()
        if normalized in projects_by_path:
            return projects_by_path[normalized]
    matches = projects_by_basename.get(PurePosixPath(target).name.casefold(), ())
    if len(matches) == 1:
        return projects_by_path[matches[0]]
    return None


def _body_lines(text: str, *, body_start_line: int) -> list[tuple[int, str]]:
    lines = text.split("\n")
    return [
        (line_number, lines[line_number - 1])
        for line_number in range(body_start_line, len(lines) + 1)
    ]


def _remove_meta_bind_blocks(lines: Sequence[tuple[int, str]]) -> list[tuple[int, str]]:
    result: list[tuple[int, str]] = []
    fence_char: str | None = None
    fence_length = 0
    for item in lines:
        stripped = item[1].strip()
        if fence_char is not None:
            if (
                stripped
                and set(stripped) == {fence_char}
                and len(stripped) >= fence_length
            ):
                fence_char = None
                fence_length = 0
            continue
        match = _META_BIND_FENCE_RE.match(stripped)
        if match is not None:
            fence = match.group("fence")
            fence_char = fence[0]
            fence_length = len(fence)
            continue
        result.append(item)
    return result


def _select_h1_body(
    lines: Sequence[tuple[int, str]],
    heading: str,
) -> list[tuple[int, str]]:
    start: int | None = None
    for index, (_, line) in enumerate(lines):
        match = _H1_RE.match(line)
        if match is None:
            continue
        if start is None and match.group(1).strip() == heading:
            start = index + 1
            continue
        if start is not None:
            return list(lines[start:index])
    return list(lines[start:]) if start is not None else []


def _semantic_nonempty(lines: Sequence[tuple[int, str]]) -> bool:
    for _, line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped in {"-", "*", "+", "- [ ]", "- [x]", "- [X]"}:
            continue
        return True
    return False


def _chunk_bytes(lines: Sequence[tuple[int, str]]) -> bytes:
    return ("\n".join(line for _, line in lines).strip() + "\n").encode("utf-8")


def _chunk_identity_sha256(
    *,
    source_path: str,
    source_sha256: str,
    ordinal: int,
    start_line: int,
    end_line: int,
    heading_path: Sequence[str],
    content_sha256: str,
) -> str:
    return sha256_bytes(
        _canonical_json_bytes(
            {
                "source_path": source_path,
                "source_sha256": source_sha256,
                "chunk_policy": CHUNK_POLICY_VERSION,
                "ordinal": ordinal,
                "start_line": start_line,
                "end_line": end_line,
                "heading_path": list(heading_path),
                "content_sha256": content_sha256,
            }
        )
    )


def _bounded_parts(
    lines: Sequence[tuple[int, str]],
) -> list[list[tuple[int, str]]]:
    parts: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []
    for item in lines:
        candidate = [*current, item]
        if len(_chunk_bytes(candidate)) <= MAX_CHUNK_BYTES:
            current = candidate
            continue
        if current:
            parts.append(current)
            current = [item]
        else:
            raise SemanticCorpusError(
                f"semantic chunk line exceeds {MAX_CHUNK_BYTES} bytes at line {item[0]}"
            )
        if len(_chunk_bytes(current)) > MAX_CHUNK_BYTES:
            raise SemanticCorpusError(
                f"semantic chunk line exceeds {MAX_CHUNK_BYTES} bytes at line {item[0]}"
            )
    if current:
        parts.append(current)
    return parts


def _chunk_lines(
    source_path: str,
    source_sha256: str,
    lines: Sequence[tuple[int, str]],
    *,
    base_heading_path: tuple[str, ...] = (),
) -> tuple[SemanticChunk, ...]:
    filtered = _remove_meta_bind_blocks(lines)
    if not _semantic_nonempty(filtered):
        return ()

    groups: list[tuple[tuple[str, ...], list[tuple[int, str]]]] = []
    current: list[tuple[int, str]] = []
    current_heading = base_heading_path
    stack = list(base_heading_path)

    def flush() -> None:
        nonlocal current
        if _semantic_nonempty(current):
            groups.append((current_heading, current))
        current = []

    for item in filtered:
        match = _HEADING_RE.match(item[1])
        if match is not None:
            flush()
            level = len(match.group(1))
            title = match.group(2).strip()
            stack = stack[: max(0, level - 1)]
            while len(stack) < level - 1:
                stack.append("")
            if len(stack) == level - 1:
                stack.append(title)
            else:
                stack[level - 1] = title
            current_heading = tuple(value for value in stack if value)
        current.append(item)
    flush()

    chunks: list[SemanticChunk] = []
    for heading_path, group in groups:
        for part in _bounded_parts(group):
            content = _chunk_bytes(part)
            content_sha = sha256_bytes(content)
            ordinal = len(chunks)
            chunk_id = _chunk_identity_sha256(
                source_path=source_path,
                source_sha256=source_sha256,
                ordinal=ordinal,
                start_line=part[0][0],
                end_line=part[-1][0],
                heading_path=heading_path,
                content_sha256=content_sha,
            )
            chunks.append(
                SemanticChunk(
                    chunk_id=chunk_id,
                    ordinal=ordinal,
                    start_line=part[0][0],
                    end_line=part[-1][0],
                    heading_path=heading_path,
                    content_sha256=content_sha,
                    byte_size=len(content),
                )
            )
            if len(chunks) > MAX_CHUNKS:
                raise SemanticCorpusError(
                    f"semantic source chunk count exceeds {MAX_CHUNKS}: {source_path}"
                )
    return tuple(chunks)


def _metadata(fm: Mapping[str, str], keys: Sequence[str]) -> dict[str, object]:
    return {key: fm[key] for key in keys if key in fm and fm[key] != ""}


def build_semantic_corpus(vault_root: Path) -> SemanticCorpusManifest:
    rows_by_root = {
        root: _safe_tree_files(vault_root, root)
        for root in SOURCE_ROOTS
    }
    projects_by_path, projects_by_basename = _project_maps(rows_by_root[PROJECT_ROOT])
    warnings: list[str] = []
    sources: list[SemanticSource] = []

    all_rows = [
        (root, path, data)
        for root in SOURCE_ROOTS
        for path, data in rows_by_root[root]
    ]
    for root, path, data in all_rows:
        text = _normalized_text(data, path=path)
        fm, body_start = _frontmatter(text, path=path)
        source_kind: str | None = None
        metadata: dict[str, object] = {}
        semantic_lines: list[tuple[int, str]] = []
        base_heading: tuple[str, ...] = ()
        source_type = fm.get("type")

        if root == DAILY_ROOT:
            if source_type != "daily-review":
                continue
            source_kind = "daily"
            metadata = {"date": Path(path).stem}
            semantic_lines = _select_h1_body(
                _body_lines(text, body_start_line=body_start),
                "Note",
            )
            base_heading = ("Note",)
        elif root == IDEA_ROOT:
            if source_type != "idea":
                continue
            status = fm.get("status", "")
            if status == "archived":
                continue
            if status not in {"active", "adopted"}:
                warnings.append(f"{path}: unsupported Idea status {status!r}")
                continue
            source_kind = "idea"
            metadata = _metadata(
                fm,
                ("title", "created", "workspace", "project", "status", "tags"),
            )
            semantic_lines = _body_lines(text, body_start_line=body_start)
        elif root == PROJECT_ROOT:
            if source_type == "project":
                status = fm.get("status", "")
                if status not in _ALLOWED_PROJECT_STATUSES:
                    warnings.append(f"{path}: invalid Project status {status!r}")
                    continue
                if status == "cancelled":
                    continue
                source_kind = "project"
                metadata = _metadata(fm, ("workspace", "status", "priority"))
                semantic_lines = _select_h1_body(
                    _body_lines(text, body_start_line=body_start),
                    "Project Summary",
                )
                base_heading = ("Project Summary",)
            elif source_type == "project-note":
                if fm.get("lifecycle") != "active":
                    continue
                project_status = _resolve_project_status(
                    path,
                    fm.get("project", ""),
                    projects_by_path,
                    projects_by_basename,
                )
                if project_status is None:
                    warnings.append(f"{path}: Project reference cannot be resolved")
                    continue
                if project_status == "cancelled":
                    continue
                source_kind = "project-note"
                metadata = _metadata(
                    fm,
                    ("workspace", "project", "category", "lifecycle", "tags"),
                )
                metadata["project_status"] = project_status
                semantic_lines = _body_lines(text, body_start_line=body_start)
            else:
                continue
        elif root == KNOWLEDGE_ROOT:
            if source_type != "knowledge-note" or fm.get("status") != "active":
                continue
            source_kind = "knowledge"
            metadata = _metadata(
                fm,
                ("status", "category", "maturity", "source_type", "tags"),
            )
            semantic_lines = _body_lines(text, body_start_line=body_start)

        if source_kind is None:
            continue
        source_sha = sha256_bytes(data)
        chunks = _chunk_lines(
            path,
            source_sha,
            semantic_lines,
            base_heading_path=base_heading,
        )
        if not chunks:
            continue
        sources.append(
            SemanticSource(
                path=path,
                source_kind=source_kind,
                content_sha256=source_sha,
                byte_size=len(data),
                metadata=metadata,
                chunks=chunks,
            )
        )

    sources.sort(key=lambda source: (source.path.casefold(), source.path))
    if len(sources) > MAX_DOCUMENTS:
        raise SemanticCorpusError(
            f"semantic corpus exceeds {MAX_DOCUMENTS} eligible sources"
        )
    if sum(len(source.chunks) for source in sources) > MAX_CHUNKS:
        raise SemanticCorpusError(
            f"semantic corpus exceeds {MAX_CHUNKS} chunks"
        )
    return SemanticCorpusManifest(
        sources=tuple(sources),
        warnings=tuple(sorted(warnings)),
    )


def parse_semantic_corpus_manifest(data: bytes) -> SemanticCorpusManifest:
    value = _decode_json_object(data, label="semantic corpus manifest")
    if set(value) != {"record_version", "chunk_policy", "sources", "warnings"}:
        raise SemanticCorpusError("semantic corpus manifest properties do not match contract")
    if value["record_version"] != MANIFEST_VERSION:
        raise SemanticCorpusError("unsupported semantic corpus manifest version")
    if value["chunk_policy"] != CHUNK_POLICY_VERSION:
        raise SemanticCorpusError("unsupported semantic chunk policy")
    raw_sources = value["sources"]
    raw_warnings = value["warnings"]
    if not isinstance(raw_sources, list) or len(raw_sources) > MAX_DOCUMENTS:
        raise SemanticCorpusError("semantic corpus sources are invalid")
    if not isinstance(raw_warnings, list) or not all(
        isinstance(item, str) for item in raw_warnings
    ):
        raise SemanticCorpusError("semantic corpus warnings are invalid")

    sources: list[SemanticSource] = []
    seen_paths: set[str] = set()
    chunk_count = 0
    for raw in raw_sources:
        if not isinstance(raw, dict) or set(raw) != {
            "path",
            "source_kind",
            "content_sha256",
            "byte_size",
            "metadata",
            "chunks",
        }:
            raise SemanticCorpusError("semantic source properties do not match contract")
        path = raw["path"]
        if not isinstance(path, str) or not path or path.startswith("/"):
            raise SemanticCorpusError("semantic source path is invalid")
        path_parts = PurePosixPath(path).parts
        if (
            not path_parts
            or "\\" in path
            or any(
                part in {"", ".", ".."} or part.startswith(".")
                for part in path_parts
            )
            or PurePosixPath(path).as_posix() != path
        ):
            raise SemanticCorpusError("semantic source path is unsafe")
        folded = path.casefold()
        if folded in seen_paths:
            raise SemanticCorpusError("semantic corpus contains duplicate source paths")
        seen_paths.add(folded)
        source_kind = raw["source_kind"]
        if source_kind not in {"daily", "idea", "project", "project-note", "knowledge"}:
            raise SemanticCorpusError("semantic source kind is invalid")
        expected_root = {
            "daily": DAILY_ROOT,
            "idea": IDEA_ROOT,
            "project": PROJECT_ROOT,
            "project-note": PROJECT_ROOT,
            "knowledge": KNOWLEDGE_ROOT,
        }[source_kind]
        if not path.startswith(expected_root + "/"):
            raise SemanticCorpusError("semantic source path does not match source kind")
        digest = _require_sha256(raw["content_sha256"], label="semantic source SHA")
        byte_size = raw["byte_size"]
        if type(byte_size) is not int or not 0 <= byte_size <= MAX_SOURCE_BYTES:
            raise SemanticCorpusError("semantic source byte_size is invalid")
        metadata = raw["metadata"]
        if not isinstance(metadata, dict):
            raise SemanticCorpusError("semantic source metadata is invalid")
        chunks: list[SemanticChunk] = []
        for expected_ordinal, item in enumerate(raw["chunks"]):
            if not isinstance(item, dict) or set(item) != {
                "chunk_id",
                "ordinal",
                "start_line",
                "end_line",
                "heading_path",
                "content_sha256",
                "byte_size",
            }:
                raise SemanticCorpusError("semantic chunk properties do not match contract")
            if item["ordinal"] != expected_ordinal:
                raise SemanticCorpusError("semantic chunk ordinals are not contiguous")
            start_line = item["start_line"]
            end_line = item["end_line"]
            chunk_bytes = item["byte_size"]
            heading_path = item["heading_path"]
            if (
                type(start_line) is not int
                or type(end_line) is not int
                or start_line < 1
                or end_line < start_line
                or type(chunk_bytes) is not int
                or not 0 < chunk_bytes <= MAX_CHUNK_BYTES
                or not isinstance(heading_path, list)
                or not all(isinstance(part, str) for part in heading_path)
            ):
                raise SemanticCorpusError("semantic chunk bounds are invalid")
            chunk_id = _require_sha256(item["chunk_id"], label="semantic chunk id")
            content_sha = _require_sha256(
                item["content_sha256"],
                label="semantic chunk content SHA",
            )
            expected_chunk_id = _chunk_identity_sha256(
                source_path=path,
                source_sha256=digest,
                ordinal=expected_ordinal,
                start_line=start_line,
                end_line=end_line,
                heading_path=heading_path,
                content_sha256=content_sha,
            )
            if chunk_id != expected_chunk_id:
                raise SemanticCorpusError("semantic chunk identity binding mismatch")
            chunks.append(
                SemanticChunk(
                    chunk_id=chunk_id,
                    ordinal=expected_ordinal,
                    start_line=start_line,
                    end_line=end_line,
                    heading_path=tuple(heading_path),
                    content_sha256=content_sha,
                    byte_size=chunk_bytes,
                )
            )
            chunk_count += 1
            if chunk_count > MAX_CHUNKS:
                raise SemanticCorpusError("semantic corpus has too many chunks")
        if not chunks:
            raise SemanticCorpusError("semantic source must contain at least one chunk")
        sources.append(
            SemanticSource(
                path=path,
                source_kind=source_kind,
                content_sha256=digest,
                byte_size=byte_size,
                metadata=dict(metadata),
                chunks=tuple(chunks),
            )
        )

    if [item.path for item in sources] != sorted(
        (item.path for item in sources),
        key=lambda path: (path.casefold(), path),
    ):
        raise SemanticCorpusError("semantic corpus sources must be sorted")
    if list(raw_warnings) != sorted(raw_warnings):
        raise SemanticCorpusError("semantic corpus warnings must be sorted")
    return SemanticCorpusManifest(
        sources=tuple(sources),
        warnings=tuple(raw_warnings),
    )


def _manifest_directory(ai_root: Path) -> Path:
    root = ai_root.absolute()
    index_root = root / INDEX_STAGE
    _require_safe_directory(root, create=False)
    _require_safe_directory(index_root, create=False)
    directory = index_root / SEMANTIC_CORPUS_DIR
    _require_safe_directory(directory, create=True)
    return directory


def store_semantic_corpus_manifest(
    ai_root: Path,
    manifest: SemanticCorpusManifest,
) -> tuple[str, Path]:
    data = manifest.to_json_bytes()
    parsed = parse_semantic_corpus_manifest(data)
    if parsed != manifest:
        raise SemanticCorpusError("semantic corpus canonical round-trip mismatch")
    digest = sha256_bytes(data)
    path = _manifest_directory(ai_root) / f"{digest}.semantic-corpus.json"
    return digest, _store_immutable(path, data)


def load_semantic_corpus_manifest(
    ai_root: Path,
    manifest_sha256: str,
) -> SemanticCorpusManifest:
    digest = _require_sha256(manifest_sha256, label="semantic corpus manifest SHA")
    path = _manifest_directory(ai_root) / f"{digest}.semantic-corpus.json"
    data = _read_exact_file(path)
    if sha256_bytes(data) != digest:
        raise SemanticCorpusError("semantic corpus manifest hash mismatch")
    return parse_semantic_corpus_manifest(data)


def verify_semantic_corpus_current(
    vault_root: Path,
    manifest: SemanticCorpusManifest,
) -> None:
    current = build_semantic_corpus(vault_root)
    expected = [
        (source.path, source.content_sha256)
        for source in manifest.sources
    ]
    actual = [
        (source.path, source.content_sha256)
        for source in current.sources
    ]
    if actual != expected:
        raise SemanticCorpusError("semantic corpus manifest is stale")


def materialize_semantic_chunk_bytes(
    vault_root: Path,
    source: SemanticSource,
    chunk: SemanticChunk,
) -> bytes:
    if chunk not in source.chunks:
        raise SemanticCorpusError("semantic chunk is not bound to source")

    parts = PurePosixPath(source.path).parts
    if (
        not parts
        or "\\" in source.path
        or any(part in {"", ".", ".."} or part.startswith(".") for part in parts)
    ):
        raise SemanticCorpusError("semantic source path is unsafe")

    path = vault_root.absolute().joinpath(*parts)
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise SemanticCorpusError(
            f"semantic source disappeared while materializing chunk: {source.path}"
        ) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise SemanticCorpusError(
            f"semantic source is unsafe while materializing chunk: {source.path}"
        )

    data = path.read_bytes()
    if (
        len(data) != source.byte_size
        or sha256_bytes(data) != source.content_sha256
    ):
        raise SemanticCorpusError(
            f"semantic source changed while materializing chunk: {source.path}"
        )

    text = _normalized_text(data, path=source.path)
    lines = text.split("\n")
    if chunk.end_line > len(lines):
        raise SemanticCorpusError("semantic chunk line range exceeds source")

    selected = [
        (line_number, lines[line_number - 1])
        for line_number in range(chunk.start_line, chunk.end_line + 1)
    ]
    content = _chunk_bytes(_remove_meta_bind_blocks(selected))
    if (
        len(content) != chunk.byte_size
        or sha256_bytes(content) != chunk.content_sha256
    ):
        raise SemanticCorpusError("semantic chunk content binding mismatch")

    expected_chunk_id = _chunk_identity_sha256(
        source_path=source.path,
        source_sha256=source.content_sha256,
        ordinal=chunk.ordinal,
        start_line=chunk.start_line,
        end_line=chunk.end_line,
        heading_path=chunk.heading_path,
        content_sha256=chunk.content_sha256,
    )
    if chunk.chunk_id != expected_chunk_id:
        raise SemanticCorpusError("semantic chunk identity binding mismatch")
    return content


def build_and_store_semantic_corpus(
    ai_root: Path,
    vault_root: Path,
) -> tuple[str, Path, SemanticCorpusManifest]:
    try:
        with mirror_read_lock(ai_root):
            manifest = build_semantic_corpus(vault_root)
            digest, path = store_semantic_corpus_manifest(ai_root, manifest)
    except ProductionIOError as exc:
        raise SemanticCorpusError(str(exc)) from exc
    return digest, path, manifest


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="obsidian-semantic-corpus")
    parser.add_argument("--ai-root", type=Path, required=True)
    parser.add_argument("--vault-root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        digest, path, manifest = build_and_store_semantic_corpus(
            args.ai_root,
            args.vault_root,
        )
    except (ArtifactLifecycleError, SemanticCorpusError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    counts: dict[str, int] = {}
    for source in manifest.sources:
        counts[source.source_kind] = counts.get(source.source_kind, 0) + 1
    print(
        json.dumps(
            {
                "manifest_sha256": digest,
                "path": str(path),
                "source_count": len(manifest.sources),
                "chunk_count": sum(len(source.chunks) for source in manifest.sources),
                "source_kinds": dict(sorted(counts.items())),
                "warnings": len(manifest.warnings),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
