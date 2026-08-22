from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .artifact_lifecycle import (
    ArtifactLifecycleError,
    _canonical_json_bytes,
    _decode_json_object,
    _require_safe_directory,
    _require_sha256,
    _store_immutable,
    _utc_now,
    sha256_bytes,
)


CONTEXT_STAGE = "05-Context"
KNOWLEDGE_ROOT = "11-Knowledge"
MAX_QUERY_CHARS = 4096
MAX_SOURCES = 16
MAX_SOURCE_BYTES = 128 * 1024
MAX_CONTEXT_BYTES = 512 * 1024


@dataclass(frozen=True)
class ContextSource:
    path: str
    content_sha256: str
    content: str


@dataclass(frozen=True)
class ContextBundle:
    query: str
    created_at: str
    sources: tuple[ContextSource, ...]

    def to_json_bytes(self) -> bytes:
        return _canonical_json_bytes(
            {
                "record_version": 1,
                "query": self.query,
                "created_at": self.created_at,
                "sources": [
                    {
                        "path": source.path,
                        "content_sha256": source.content_sha256,
                        "content": source.content,
                    }
                    for source in self.sources
                ],
            }
        )


def _safe_source_parts(path: str) -> tuple[str, ...]:
    if not isinstance(path, str) or not path or path.startswith("/") or "\\" in path or "\x00" in path:
        raise ArtifactLifecycleError("context source path must be a relative POSIX path")
    parts = tuple(path.split("/"))
    if len(parts) < 2 or parts[0] != KNOWLEDGE_ROOT:
        raise ArtifactLifecycleError("context source must be below 11-Knowledge")
    if any(part in {"", ".", ".."} for part in parts):
        raise ArtifactLifecycleError("context source contains an unsafe path component")
    if not parts[-1].endswith(".md"):
        raise ArtifactLifecycleError("context source must be a Markdown file")
    return parts


def _read_source_nofollow(vault_root: Path, path: str) -> bytes:
    parts = _safe_source_parts(path)
    dir_flags = os.O_RDONLY | os.O_DIRECTORY
    file_flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        dir_flags |= os.O_NOFOLLOW
        file_flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        dir_flags |= os.O_CLOEXEC
        file_flags |= os.O_CLOEXEC

    fds: list[int] = []
    try:
        try:
            current = os.open(vault_root, dir_flags)
        except OSError as exc:
            raise ArtifactLifecycleError("Vault root must be an existing non-symlink directory") from exc
        fds.append(current)

        for component in parts[:-1]:
            try:
                child = os.open(component, dir_flags, dir_fd=current)
            except OSError as exc:
                raise ArtifactLifecycleError(
                    f"context source directory is not safely readable: {component}"
                ) from exc
            fds.append(child)
            current = child

        try:
            fd = os.open(parts[-1], file_flags, dir_fd=current)
        except OSError as exc:
            raise ArtifactLifecycleError(f"context source cannot be safely opened: {path}") from exc
        fds.append(fd)

        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ArtifactLifecycleError(f"context source is not a regular file: {path}")
        if info.st_size > MAX_SOURCE_BYTES:
            raise ArtifactLifecycleError(
                f"context source exceeds {MAX_SOURCE_BYTES} bytes: {path}"
            )

        chunks: list[bytes] = []
        remaining = MAX_SOURCE_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > MAX_SOURCE_BYTES:
            raise ArtifactLifecycleError(
                f"context source exceeds {MAX_SOURCE_BYTES} bytes: {path}"
            )
        return data
    finally:
        for fd in reversed(fds):
            try:
                os.close(fd)
            except OSError:
                pass


def build_context_bundle(
    vault_root: Path,
    *,
    query: str,
    source_paths: Sequence[str],
    created_at: str | None = None,
) -> ContextBundle:
    if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_CHARS:
        raise ArtifactLifecycleError(
            f"context query must be a non-empty string up to {MAX_QUERY_CHARS} characters"
        )
    if len(source_paths) > MAX_SOURCES:
        raise ArtifactLifecycleError(f"context bundle supports at most {MAX_SOURCES} sources")

    normalized_paths: list[str] = []
    for path in source_paths:
        _safe_source_parts(path)
        normalized_paths.append(path)

    seen: set[str] = set()
    sources: list[ContextSource] = []
    total = 0
    for path in sorted(normalized_paths, key=lambda value: value.casefold()):
        folded = path.casefold()
        if folded in seen:
            raise ArtifactLifecycleError(f"duplicate context source path: {path}")
        seen.add(folded)

        data = _read_source_nofollow(vault_root, path)
        total += len(data)
        if total > MAX_CONTEXT_BYTES:
            raise ArtifactLifecycleError(
                f"context source bytes exceed aggregate limit {MAX_CONTEXT_BYTES}"
            )
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ArtifactLifecycleError(f"context source is not valid UTF-8: {path}") from exc
        sources.append(
            ContextSource(
                path=path,
                content_sha256=sha256_bytes(data),
                content=content,
            )
        )

    timestamp = created_at or _utc_now()
    if not isinstance(timestamp, str) or not timestamp.endswith("Z"):
        raise ArtifactLifecycleError("context created_at must be a UTC timestamp ending in Z")
    return ContextBundle(query=query, created_at=timestamp, sources=tuple(sources))


def parse_context_bundle(data: bytes) -> ContextBundle:
    value = _decode_json_object(data, label="context bundle")
    if set(value) != {"record_version", "query", "created_at", "sources"}:
        raise ArtifactLifecycleError("context bundle properties do not match contract")
    if type(value["record_version"]) is not int or value["record_version"] != 1:
        raise ArtifactLifecycleError("context bundle record_version must be integer 1")

    query = value["query"]
    created_at = value["created_at"]
    raw_sources = value["sources"]
    if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_CHARS:
        raise ArtifactLifecycleError("context bundle query is invalid")
    if not isinstance(created_at, str) or not created_at.endswith("Z"):
        raise ArtifactLifecycleError("context bundle created_at is invalid")
    if not isinstance(raw_sources, list) or len(raw_sources) > MAX_SOURCES:
        raise ArtifactLifecycleError("context bundle sources are invalid")

    seen: set[str] = set()
    total = 0
    sources: list[ContextSource] = []
    for raw in raw_sources:
        if not isinstance(raw, dict) or set(raw) != {"path", "content_sha256", "content"}:
            raise ArtifactLifecycleError("context source properties do not match contract")
        path = raw["path"]
        digest = raw["content_sha256"]
        content = raw["content"]
        if not isinstance(path, str):
            raise ArtifactLifecycleError("context source path must be a string")
        _safe_source_parts(path)
        folded = path.casefold()
        if folded in seen:
            raise ArtifactLifecycleError("context bundle contains duplicate source paths")
        seen.add(folded)
        if not isinstance(digest, str):
            raise ArtifactLifecycleError("context source content_sha256 is invalid")
        digest = _require_sha256(digest, label="context source content_sha256")
        if not isinstance(content, str):
            raise ArtifactLifecycleError("context source content must be a string")
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_SOURCE_BYTES or sha256_bytes(encoded) != digest:
            raise ArtifactLifecycleError("context source bytes do not match content_sha256")
        total += len(encoded)
        if total > MAX_CONTEXT_BYTES:
            raise ArtifactLifecycleError("context bundle exceeds aggregate source byte limit")
        sources.append(ContextSource(path=path, content_sha256=digest, content=content))

    if [source.path for source in sources] != sorted(
        (source.path for source in sources), key=lambda path: path.casefold()
    ):
        raise ArtifactLifecycleError("context bundle sources must be sorted by path")

    return ContextBundle(query=query, created_at=created_at, sources=tuple(sources))


def store_context_bundle(ai_root: Path, bundle: ContextBundle) -> tuple[str, Path]:
    context_dir = ai_root.absolute() / CONTEXT_STAGE
    _require_safe_directory(ai_root.absolute(), create=False)
    _require_safe_directory(context_dir, create=False)
    data = bundle.to_json_bytes()
    # Parse our own canonical bytes before persistence so malformed in-memory
    # objects cannot cross the Reader -> Generator boundary.
    parse_context_bundle(data)
    digest = sha256_bytes(data)
    path = context_dir / f"{digest}.context.json"
    return digest, _store_immutable(path, data)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="obsidian-knowledge-context")
    parser.add_argument("--ai-root", type=Path, required=True)
    parser.add_argument("--vault-root", type=Path, required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--source", action="append", default=[])
    args = parser.parse_args(argv)

    try:
        bundle = build_context_bundle(
            args.vault_root,
            query=args.query,
            source_paths=args.source,
        )
        digest, path = store_context_bundle(args.ai_root, bundle)
    except (ArtifactLifecycleError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "context_sha256": digest,
                "path": str(path),
                "source_count": len(bundle.sources),
            },
            sort_keys=True,
        )
    )
    return 0
