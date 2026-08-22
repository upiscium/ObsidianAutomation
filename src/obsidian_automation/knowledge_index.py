from __future__ import annotations

import argparse
import json
import math
import os
import re
import stat
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
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
from .context_bundle import (
    MAX_CONTEXT_BYTES,
    MAX_SOURCE_BYTES,
    MAX_SOURCES,
    build_context_bundle,
    store_context_bundle,
)


INDEX_STAGE = "04-Index"
KNOWLEDGE_ROOT = "11-Knowledge"
TOKENIZER_VERSION = "nfkc-ascii-cjk-bigram-v0"
RANKER_VERSION = "bm25-fieldboost-v0"
MAX_DOCUMENTS = 4096
MAX_DOCUMENT_BYTES = MAX_SOURCE_BYTES
MAX_INDEX_BYTES = 64 * 1024 * 1024
DEFAULT_TOP_K = 8
MAX_TOP_K = MAX_SOURCES
BM25_K1 = 1.2
BM25_B = 0.75

_ASCII_WORD_RE = re.compile(r"[a-z0-9]+(?:[_+.-][a-z0-9]+)*")
_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$")
_WINDOWS_FORBIDDEN = set('<>:"|?*')
_ALLOWED_STATUS = {"active", "outdated", "archived", "deleted"}


@dataclass(frozen=True)
class IndexedDocument:
    path: str
    content_sha256: str
    byte_size: int
    title: str
    status: str
    category: str
    maturity: str
    source_type: str
    token_count: int
    term_freq: Mapping[str, int]


@dataclass(frozen=True)
class KnowledgeIndex:
    documents: tuple[IndexedDocument, ...]

    def to_json_bytes(self) -> bytes:
        return _canonical_json_bytes(
            {
                "record_version": 1,
                "tokenizer": TOKENIZER_VERSION,
                "ranker": RANKER_VERSION,
                "documents": [
                    {
                        "path": doc.path,
                        "content_sha256": doc.content_sha256,
                        "byte_size": doc.byte_size,
                        "title": doc.title,
                        "status": doc.status,
                        "category": doc.category,
                        "maturity": doc.maturity,
                        "source_type": doc.source_type,
                        "token_count": doc.token_count,
                        "term_freq": dict(sorted(doc.term_freq.items())),
                    }
                    for doc in self.documents
                ],
            }
        )


@dataclass(frozen=True)
class RankedDocument:
    path: str
    content_sha256: str
    score: float


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (
        0x3040 <= code <= 0x30FF
        or 0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
        or 0xFF66 <= code <= 0xFF9F
    )


def tokenize(text: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    tokens: list[str] = list(_ASCII_WORD_RE.findall(normalized))
    cjk_run: list[str] = []

    def flush_cjk() -> None:
        nonlocal cjk_run
        if not cjk_run:
            return
        tokens.extend(cjk_run)
        tokens.extend(
            cjk_run[index] + cjk_run[index + 1]
            for index in range(len(cjk_run) - 1)
        )
        cjk_run = []

    for ch in normalized:
        if _is_cjk(ch):
            cjk_run.append(ch)
        else:
            flush_cjk()
    flush_cjk()
    return tuple(token for token in tokens if token)


def _safe_component(component: str) -> None:
    if component in {"", ".", ".."} or component.startswith("."):
        raise ArtifactLifecycleError(f"unsafe Knowledge path component: {component!r}")
    if component != component.strip():
        raise ArtifactLifecycleError("Knowledge path component has edge whitespace")
    if any(ch in _WINDOWS_FORBIDDEN or ord(ch) < 0x20 for ch in component):
        raise ArtifactLifecycleError(
            "Knowledge path contains a cross-platform unsafe character"
        )


def _validate_index_path(path: str) -> tuple[str, ...]:
    if not isinstance(path, str) or not path.startswith(f"{KNOWLEDGE_ROOT}/"):
        raise ArtifactLifecycleError("Knowledge index path is invalid")
    parts = tuple(path.split("/"))
    if len(parts) < 2 or parts[0] != KNOWLEDGE_ROOT:
        raise ArtifactLifecycleError("Knowledge index path is invalid")
    for component in parts[1:]:
        _safe_component(component)
    if not parts[-1].endswith(".md"):
        raise ArtifactLifecycleError("Knowledge index path must reference Markdown")
    return parts


def _open_directory(parent_fd: int, component: str) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        return os.open(component, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise ArtifactLifecycleError(
            f"Knowledge directory is not safely readable: {component}"
        ) from exc


def _open_regular_file(parent_fd: int, name: str) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise ArtifactLifecycleError(f"Knowledge file is not safely readable: {name}") from exc
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        os.close(fd)
        raise ArtifactLifecycleError(f"Knowledge source is not a regular file: {name}")
    if info.st_size > MAX_DOCUMENT_BYTES:
        os.close(fd)
        raise ArtifactLifecycleError(
            f"Knowledge source exceeds {MAX_DOCUMENT_BYTES} bytes: {name}"
        )
    return fd


def _read_fd_limited(fd: int, *, path: str) -> bytes:
    chunks: list[bytes] = []
    remaining = MAX_DOCUMENT_BYTES + 1
    while remaining > 0:
        chunk = os.read(fd, min(65536, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b"".join(chunks)
    if len(data) > MAX_DOCUMENT_BYTES:
        raise ArtifactLifecycleError(
            f"Knowledge source exceeds {MAX_DOCUMENT_BYTES} bytes: {path}"
        )
    return data


def _scan_markdown_files(vault_root: Path) -> list[tuple[str, bytes]]:
    root_flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        root_flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        root_flags |= os.O_CLOEXEC

    try:
        vault_fd = os.open(vault_root, root_flags)
    except OSError as exc:
        raise ArtifactLifecycleError("Vault root must be a safe directory") from exc

    knowledge_fd: int | None = None
    try:
        knowledge_fd = _open_directory(vault_fd, KNOWLEDGE_ROOT)
        results: list[tuple[str, bytes]] = []

        def walk(dir_fd: int, prefix: tuple[str, ...]) -> None:
            names = os.listdir(dir_fd)
            folded: dict[str, str] = {}
            for name in names:
                alias = folded.get(name.casefold())
                if alias is not None and alias != name:
                    raise ArtifactLifecycleError(
                        f"case-fold collision in Knowledge tree: {alias!r} / {name!r}"
                    )
                folded[name.casefold()] = name

            for name in sorted(names, key=lambda value: (value.casefold(), value)):
                if name.startswith("."):
                    continue
                _safe_component(name)
                try:
                    info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                except OSError as exc:
                    raise ArtifactLifecycleError(
                        f"cannot inspect Knowledge path component: {name}"
                    ) from exc
                if stat.S_ISLNK(info.st_mode):
                    raise ArtifactLifecycleError(
                        f"symlink is not allowed in Knowledge retrieval corpus: {name}"
                    )
                if stat.S_ISDIR(info.st_mode):
                    child = _open_directory(dir_fd, name)
                    try:
                        walk(child, prefix + (name,))
                    finally:
                        os.close(child)
                    continue
                if not stat.S_ISREG(info.st_mode) or not name.endswith(".md"):
                    continue
                fd = _open_regular_file(dir_fd, name)
                try:
                    relative = "/".join((KNOWLEDGE_ROOT,) + prefix + (name,))
                    data = _read_fd_limited(fd, path=relative)
                finally:
                    os.close(fd)
                results.append((relative, data))
                if len(results) > MAX_DOCUMENTS:
                    raise ArtifactLifecycleError(
                        f"Knowledge corpus exceeds {MAX_DOCUMENTS} Markdown files"
                    )

        walk(knowledge_fd, ())
        return results
    finally:
        if knowledge_fd is not None:
            os.close(knowledge_fd)
        os.close(vault_fd)


def _frontmatter_scalars(text: str) -> dict[str, str]:
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        return {}
    values: dict[str, str] = {}
    for line in lines[1:64]:
        if line == "---":
            return values
        if not line or line[:1].isspace() or ":" not in line:
            continue
        key, raw = line.split(":", 1)
        if key in values:
            raise ArtifactLifecycleError(f"duplicate Knowledge frontmatter key: {key}")
        value = raw.strip()
        if value.startswith(("'", '"', "[", "{")):
            continue
        values[key] = value
    return {}


def _headings(text: str) -> tuple[str, ...]:
    return tuple(
        match.group(1)
        for line in text.splitlines()
        if (match := _HEADING_RE.match(line)) is not None
    )


def _document_from_bytes(path: str, data: bytes) -> IndexedDocument | None:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArtifactLifecycleError(f"Knowledge source is not UTF-8: {path}") from exc

    fm = _frontmatter_scalars(text)
    if fm.get("type") != "knowledge-note":
        return None
    status = fm.get("status", "")
    if status not in _ALLOWED_STATUS:
        raise ArtifactLifecycleError(f"invalid Knowledge status in {path}: {status!r}")
    if status != "active":
        return None

    title = Path(path).stem
    headings = _headings(text)
    category = fm.get("category", "")
    maturity = fm.get("maturity", "")
    source_type = fm.get("source_type", "")

    weighted: list[str] = list(tokenize(text))
    for _ in range(3):
        weighted.extend(tokenize(title))
    for heading in headings:
        for _ in range(2):
            weighted.extend(tokenize(heading))
    for value in (category, maturity, source_type):
        weighted.extend(tokenize(value))

    tf = Counter(weighted)
    return IndexedDocument(
        path=path,
        content_sha256=sha256_bytes(data),
        byte_size=len(data),
        title=title,
        status=status,
        category=category,
        maturity=maturity,
        source_type=source_type,
        token_count=sum(tf.values()),
        term_freq=dict(tf),
    )


def build_knowledge_index(vault_root: Path) -> KnowledgeIndex:
    documents: list[IndexedDocument] = []
    for path, data in _scan_markdown_files(vault_root):
        doc = _document_from_bytes(path, data)
        if doc is not None:
            documents.append(doc)
    documents.sort(key=lambda doc: (doc.path.casefold(), doc.path))
    return KnowledgeIndex(documents=tuple(documents))


def _index_directory(ai_root: Path) -> Path:
    root = ai_root.absolute()
    index_dir = root / INDEX_STAGE
    _require_safe_directory(root, create=False)
    _require_safe_directory(index_dir, create=False)
    return index_dir


def store_knowledge_index(ai_root: Path, index: KnowledgeIndex) -> tuple[str, Path]:
    data = index.to_json_bytes()
    if len(data) > MAX_INDEX_BYTES:
        raise ArtifactLifecycleError(f"Knowledge index exceeds {MAX_INDEX_BYTES} bytes")
    parsed = parse_knowledge_index(data)
    if parsed != index:
        raise ArtifactLifecycleError("Knowledge index canonical round-trip mismatch")
    digest = sha256_bytes(data)
    path = _index_directory(ai_root) / f"{digest}.index.json"
    return digest, _store_immutable(path, data)


def parse_knowledge_index(data: bytes) -> KnowledgeIndex:
    if len(data) > MAX_INDEX_BYTES:
        raise ArtifactLifecycleError(f"Knowledge index exceeds {MAX_INDEX_BYTES} bytes")
    value = _decode_json_object(data, label="Knowledge index")
    if set(value) != {"record_version", "tokenizer", "ranker", "documents"}:
        raise ArtifactLifecycleError("Knowledge index properties do not match contract")
    if type(value["record_version"]) is not int or value["record_version"] != 1:
        raise ArtifactLifecycleError("Knowledge index record_version must be integer 1")
    if value["tokenizer"] != TOKENIZER_VERSION or value["ranker"] != RANKER_VERSION:
        raise ArtifactLifecycleError("Knowledge index algorithm version is unsupported")
    raw_documents = value["documents"]
    if not isinstance(raw_documents, list) or len(raw_documents) > MAX_DOCUMENTS:
        raise ArtifactLifecycleError("Knowledge index documents are invalid")

    documents: list[IndexedDocument] = []
    seen: set[str] = set()
    for raw in raw_documents:
        required = {
            "path",
            "content_sha256",
            "byte_size",
            "title",
            "status",
            "category",
            "maturity",
            "source_type",
            "token_count",
            "term_freq",
        }
        if not isinstance(raw, dict) or set(raw) != required:
            raise ArtifactLifecycleError(
                "Knowledge index document properties do not match contract"
            )
        path = raw["path"]
        digest = raw["content_sha256"]
        byte_size = raw["byte_size"]
        title = raw["title"]
        status = raw["status"]
        category = raw["category"]
        maturity = raw["maturity"]
        source_type = raw["source_type"]
        token_count = raw["token_count"]
        raw_tf = raw["term_freq"]

        if not isinstance(path, str):
            raise ArtifactLifecycleError("Knowledge index path is invalid")
        _validate_index_path(path)
        folded = path.casefold()
        if folded in seen:
            raise ArtifactLifecycleError("Knowledge index contains duplicate paths")
        seen.add(folded)
        if not isinstance(digest, str):
            raise ArtifactLifecycleError("Knowledge index content_sha256 is invalid")
        digest = _require_sha256(digest, label="Knowledge index content_sha256")
        if type(byte_size) is not int or not 0 <= byte_size <= MAX_DOCUMENT_BYTES:
            raise ArtifactLifecycleError("Knowledge index byte_size is invalid")
        if not all(
            isinstance(value, str)
            for value in (title, status, category, maturity, source_type)
        ):
            raise ArtifactLifecycleError("Knowledge index metadata must be strings")
        if status != "active":
            raise ArtifactLifecycleError("Knowledge index may contain only active notes")
        if type(token_count) is not int or token_count < 0:
            raise ArtifactLifecycleError("Knowledge index token_count is invalid")
        if not isinstance(raw_tf, dict):
            raise ArtifactLifecycleError("Knowledge index term_freq is invalid")

        term_freq: dict[str, int] = {}
        total = 0
        for term, count in raw_tf.items():
            if (
                not isinstance(term, str)
                or not term
                or type(count) is not int
                or count <= 0
            ):
                raise ArtifactLifecycleError(
                    "Knowledge index term frequency entry is invalid"
                )
            term_freq[term] = count
            total += count
        if total != token_count:
            raise ArtifactLifecycleError(
                "Knowledge index token_count does not match term_freq"
            )
        documents.append(
            IndexedDocument(
                path=path,
                content_sha256=digest,
                byte_size=byte_size,
                title=title,
                status=status,
                category=category,
                maturity=maturity,
                source_type=source_type,
                token_count=token_count,
                term_freq=term_freq,
            )
        )

    expected = sorted(documents, key=lambda doc: (doc.path.casefold(), doc.path))
    if documents != expected:
        raise ArtifactLifecycleError("Knowledge index documents must be sorted by path")
    return KnowledgeIndex(documents=tuple(documents))


def load_knowledge_index(ai_root: Path, index_sha256: str) -> KnowledgeIndex:
    digest = _require_sha256(index_sha256, label="index_sha256")
    path = _index_directory(ai_root) / f"{digest}.index.json"
    data = _read_exact_file(path)
    if sha256_bytes(data) != digest:
        raise ArtifactLifecycleError("Knowledge index artifact hash mismatch")
    return parse_knowledge_index(data)


def verify_index_current(vault_root: Path, index: KnowledgeIndex) -> None:
    current = build_knowledge_index(vault_root)
    expected = [(doc.path, doc.content_sha256) for doc in index.documents]
    observed = [(doc.path, doc.content_sha256) for doc in current.documents]
    if observed != expected:
        raise ArtifactLifecycleError(
            "Knowledge index is stale; rebuild before retrieval"
        )


def rank_documents(index: KnowledgeIndex, query: str) -> tuple[RankedDocument, ...]:
    query_terms = Counter(tokenize(query))
    if not query_terms or not index.documents:
        return ()

    n_docs = len(index.documents)
    avg_dl = sum(doc.token_count for doc in index.documents) / n_docs
    if avg_dl <= 0:
        return ()

    doc_freq: Counter[str] = Counter()
    for doc in index.documents:
        for term in doc.term_freq:
            if term in query_terms:
                doc_freq[term] += 1

    ranked: list[RankedDocument] = []
    for doc in index.documents:
        score = 0.0
        for term, qtf in query_terms.items():
            tf = doc.term_freq.get(term, 0)
            if tf <= 0:
                continue
            df = doc_freq[term]
            idf = math.log(1.0 + (n_docs - df + 0.5) / (df + 0.5))
            denominator = tf + BM25_K1 * (
                1.0 - BM25_B + BM25_B * (doc.token_count / avg_dl)
            )
            score += qtf * idf * (tf * (BM25_K1 + 1.0) / denominator)
        if score > 0.0:
            ranked.append(
                RankedDocument(
                    path=doc.path,
                    content_sha256=doc.content_sha256,
                    score=score,
                )
            )

    ranked.sort(key=lambda item: (-item.score, item.path.casefold(), item.path))
    return tuple(ranked)


def _select_with_context_limit(
    index: KnowledgeIndex,
    ranked: Sequence[RankedDocument],
    *,
    top_k: int,
) -> tuple[RankedDocument, ...]:
    by_path = {doc.path: doc for doc in index.documents}
    selected: list[RankedDocument] = []
    total_bytes = 0
    for item in ranked:
        if len(selected) >= top_k:
            break
        size = by_path[item.path].byte_size
        if total_bytes + size > MAX_CONTEXT_BYTES:
            continue
        selected.append(item)
        total_bytes += size
    return tuple(selected)


def retrieve_context(
    ai_root: Path,
    vault_root: Path,
    *,
    index_sha256: str,
    query: str,
    top_k: int = DEFAULT_TOP_K,
) -> dict[str, object]:
    if type(top_k) is not int or not 1 <= top_k <= MAX_TOP_K:
        raise ArtifactLifecycleError(
            f"top_k must be an integer in 1..{MAX_TOP_K}"
        )
    index = load_knowledge_index(ai_root, index_sha256)
    verify_index_current(vault_root, index)
    ranked = rank_documents(index, query)
    selected = _select_with_context_limit(index, ranked, top_k=top_k)

    bundle = build_context_bundle(
        vault_root,
        query=query,
        source_paths=[item.path for item in selected],
    )
    indexed_by_path = {doc.path: doc for doc in index.documents}
    for source in bundle.sources:
        expected = indexed_by_path[source.path].content_sha256
        if source.content_sha256 != expected:
            raise ArtifactLifecycleError(
                "Knowledge source changed during context construction"
            )
    context_sha, context_path = store_context_bundle(ai_root, bundle)

    return {
        "index_sha256": _require_sha256(index_sha256, label="index_sha256"),
        "context_sha256": context_sha,
        "context_path": str(context_path),
        "query": query,
        "matched_count": len(ranked),
        "selected": [
            {
                "path": item.path,
                "content_sha256": item.content_sha256,
                "score": format(item.score, ".12g"),
            }
            for item in selected
        ],
    }


def index_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="obsidian-knowledge-index")
    parser.add_argument("--ai-root", type=Path, required=True)
    parser.add_argument("--vault-root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        index = build_knowledge_index(args.vault_root)
        digest, path = store_knowledge_index(args.ai_root, index)
    except (ArtifactLifecycleError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "index_sha256": digest,
                "path": str(path),
                "document_count": len(index.documents),
            },
            sort_keys=True,
        )
    )
    return 0


def retrieve_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="obsidian-knowledge-retrieve")
    parser.add_argument("--ai-root", type=Path, required=True)
    parser.add_argument("--vault-root", type=Path, required=True)
    parser.add_argument("--index-sha256", required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    args = parser.parse_args(argv)
    try:
        result = retrieve_context(
            args.ai_root,
            args.vault_root,
            index_sha256=args.index_sha256,
            query=args.query,
            top_k=args.top_k,
        )
    except (ArtifactLifecycleError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0
