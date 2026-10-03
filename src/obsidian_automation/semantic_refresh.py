from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Mapping, Sequence

from .artifact_lifecycle import (
    ArtifactLifecycleError,
    _canonical_json_bytes,
    _decode_json_object,
    _read_exact_file,
    _require_safe_directory,
    _require_sha256,
)
from .ollama_generator import OllamaProviderError
from .production_io import ProductionIOError, mirror_read_lock
from .semantic_corpus import (
    SemanticCorpusError,
    build_and_store_semantic_corpus,
    load_semantic_corpus_manifest,
    verify_semantic_corpus_current,
)
from .semantic_index import (
    SemanticIndexError,
    embed_semantic_plan_incremental_with_ollama,
    finalize_semantic_index,
    load_semantic_index_manifest,
    prepare_incremental_embedding_refresh_plan,
    prepare_semantic_embedding_plan,
)


INDEX_STAGE = "04-Index"
ACTIVE_BINDING_FILE = "semantic-active-index.json"
READER_CONTROL_DIR = "semantic-refresh-reader"
EMBEDDER_CONTROL_DIR = "semantic-refresh-embedder"
CONTROL_FILE = "current.json"
RECORD_VERSION = 1
PHASES = {"unchanged", "prepared"}


class SemanticRefreshError(ArtifactLifecycleError):
    """Raised when automatic Semantic Index refresh cannot complete safely."""


def _index_root(ai_root: Path) -> Path:
    root = ai_root.absolute()
    _require_safe_directory(root, create=False)
    path = root / INDEX_STAGE
    _require_safe_directory(path, create=False)
    return path


def _control_dir(ai_root: Path, name: str) -> Path:
    path = _index_root(ai_root) / name
    _require_safe_directory(path, create=True)
    return path


def _atomic_store(path: Path, data: bytes) -> Path:
    directory = path.parent
    _require_safe_directory(directory, create=True)
    if os.path.lexists(path) and path.is_symlink():
        raise SemanticRefreshError("semantic refresh control destination is unsafe")

    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=directory,
    )
    temp_path = Path(temporary)
    try:
        os.fchmod(fd, 0o660)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise SemanticRefreshError(
                    "short write while storing semantic refresh control"
                )
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)

    try:
        os.replace(temp_path, path)
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return path


def _require_identifier(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise SemanticRefreshError(f"{label} is invalid")
    return value


def _active_payload(
    *,
    semantic_index_sha256: str,
    corpus_manifest_sha256: str,
    model_identifier: str,
    model_revision: str,
) -> dict[str, object]:
    return {
        "record_version": RECORD_VERSION,
        "semantic_index_sha256": _require_sha256(
            semantic_index_sha256,
            label="active semantic index SHA",
        ),
        "corpus_manifest_sha256": _require_sha256(
            corpus_manifest_sha256,
            label="active corpus manifest SHA",
        ),
        "model_identifier": _require_identifier(
            model_identifier,
            label="active model identifier",
        ),
        "model_revision": _require_sha256(
            model_revision,
            label="active model revision",
        ),
    }


def _parse_active(data: bytes) -> dict[str, object]:
    value = _decode_json_object(data, label="active semantic index binding")
    if set(value) != {
        "record_version",
        "semantic_index_sha256",
        "corpus_manifest_sha256",
        "model_identifier",
        "model_revision",
    }:
        raise SemanticRefreshError(
            "active semantic index binding properties do not match contract"
        )
    if value["record_version"] != RECORD_VERSION:
        raise SemanticRefreshError(
            "active semantic index binding version is unsupported"
        )
    return _active_payload(
        semantic_index_sha256=value["semantic_index_sha256"],
        corpus_manifest_sha256=value["corpus_manifest_sha256"],
        model_identifier=value["model_identifier"],
        model_revision=value["model_revision"],
    )


def active_binding_path(ai_root: Path) -> Path:
    return _index_root(ai_root) / ACTIVE_BINDING_FILE


def load_active_semantic_index_binding(ai_root: Path) -> dict[str, object]:
    path = active_binding_path(ai_root)
    if not os.path.lexists(path):
        raise SemanticRefreshError("active semantic index binding is missing")
    if path.is_symlink() or not path.is_file():
        raise SemanticRefreshError("active semantic index binding is unsafe")
    return _parse_active(_read_exact_file(path))


def resolve_active_semantic_index_sha(ai_root: Path) -> str:
    return str(
        load_active_semantic_index_binding(ai_root)[
            "semantic_index_sha256"
        ]
    )


def activate_semantic_index(
    ai_root: Path,
    vault_root: Path,
    *,
    semantic_index_sha256: str,
) -> dict[str, object]:
    index_sha = _require_sha256(
        semantic_index_sha256,
        label="semantic index SHA",
    )
    index = load_semantic_index_manifest(ai_root, index_sha)
    try:
        with mirror_read_lock(ai_root):
            corpus = load_semantic_corpus_manifest(
                ai_root,
                index.corpus_manifest_sha256,
            )
            verify_semantic_corpus_current(vault_root, corpus)
    except (ProductionIOError, SemanticCorpusError) as exc:
        raise SemanticRefreshError(str(exc)) from exc

    payload = _active_payload(
        semantic_index_sha256=index_sha,
        corpus_manifest_sha256=index.corpus_manifest_sha256,
        model_identifier=index.model_identifier,
        model_revision=index.model_revision,
    )
    _atomic_store(
        active_binding_path(ai_root),
        _canonical_json_bytes(payload),
    )
    return payload


def _reader_control_path(ai_root: Path) -> Path:
    return _control_dir(ai_root, READER_CONTROL_DIR) / CONTROL_FILE


def _embedder_control_path(ai_root: Path) -> Path:
    return _control_dir(ai_root, EMBEDDER_CONTROL_DIR) / CONTROL_FILE


def _reader_payload(
    *,
    phase: str,
    previous_index_sha256: str,
    corpus_manifest_sha256: str,
    model_identifier: str,
    model_revision: str,
    plan_sha256: str | None,
    refresh_plan_sha256: str | None,
) -> dict[str, object]:
    if phase not in PHASES:
        raise SemanticRefreshError("semantic refresh reader phase is invalid")
    previous = _require_sha256(
        previous_index_sha256,
        label="previous semantic index SHA",
    )
    corpus = _require_sha256(
        corpus_manifest_sha256,
        label="refresh corpus manifest SHA",
    )
    identifier = _require_identifier(
        model_identifier,
        label="refresh model identifier",
    )
    revision = _require_sha256(
        model_revision,
        label="refresh model revision",
    )
    if phase == "unchanged":
        if plan_sha256 is not None or refresh_plan_sha256 is not None:
            raise SemanticRefreshError(
                "unchanged semantic refresh must not bind plans"
            )
        plan = None
        refresh = None
    else:
        plan = _require_sha256(
            plan_sha256,
            label="refresh embedding plan SHA",
        )
        refresh = _require_sha256(
            refresh_plan_sha256,
            label="refresh plan SHA",
        )
    return {
        "record_version": RECORD_VERSION,
        "phase": phase,
        "previous_index_sha256": previous,
        "corpus_manifest_sha256": corpus,
        "model_identifier": identifier,
        "model_revision": revision,
        "plan_sha256": plan,
        "refresh_plan_sha256": refresh,
    }


def _parse_reader_control(data: bytes) -> dict[str, object]:
    value = _decode_json_object(data, label="semantic refresh reader control")
    if set(value) != {
        "record_version",
        "phase",
        "previous_index_sha256",
        "corpus_manifest_sha256",
        "model_identifier",
        "model_revision",
        "plan_sha256",
        "refresh_plan_sha256",
    }:
        raise SemanticRefreshError(
            "semantic refresh reader control properties do not match contract"
        )
    if value["record_version"] != RECORD_VERSION:
        raise SemanticRefreshError(
            "semantic refresh reader control version is unsupported"
        )
    return _reader_payload(
        phase=value["phase"],
        previous_index_sha256=value["previous_index_sha256"],
        corpus_manifest_sha256=value["corpus_manifest_sha256"],
        model_identifier=value["model_identifier"],
        model_revision=value["model_revision"],
        plan_sha256=value["plan_sha256"],
        refresh_plan_sha256=value["refresh_plan_sha256"],
    )


def load_reader_refresh_control(ai_root: Path) -> dict[str, object]:
    path = _reader_control_path(ai_root)
    if not os.path.lexists(path):
        raise SemanticRefreshError("semantic refresh reader control is missing")
    if path.is_symlink() or not path.is_file():
        raise SemanticRefreshError("semantic refresh reader control is unsafe")
    return _parse_reader_control(_read_exact_file(path))


def prepare_refresh(
    ai_root: Path,
    vault_root: Path,
) -> dict[str, object]:
    active = load_active_semantic_index_binding(ai_root)
    previous_sha = str(active["semantic_index_sha256"])
    previous = load_semantic_index_manifest(ai_root, previous_sha)

    if (
        previous.corpus_manifest_sha256
        != active["corpus_manifest_sha256"]
        or previous.model_identifier != active["model_identifier"]
        or previous.model_revision != active["model_revision"]
    ):
        raise SemanticRefreshError(
            "active semantic index binding does not match finalized index"
        )

    corpus_sha, _path, _manifest = build_and_store_semantic_corpus(
        ai_root,
        vault_root,
    )
    if corpus_sha == previous.corpus_manifest_sha256:
        payload = _reader_payload(
            phase="unchanged",
            previous_index_sha256=previous_sha,
            corpus_manifest_sha256=corpus_sha,
            model_identifier=previous.model_identifier,
            model_revision=previous.model_revision,
            plan_sha256=None,
            refresh_plan_sha256=None,
        )
    else:
        plan_sha, _path, _plan = prepare_semantic_embedding_plan(
            ai_root,
            vault_root,
            corpus_manifest_sha256=corpus_sha,
            model_identifier=previous.model_identifier,
            model_revision=previous.model_revision,
        )
        refresh_sha, _path, _refresh = (
            prepare_incremental_embedding_refresh_plan(
                ai_root,
                plan_sha256=plan_sha,
                previous_index_sha256=previous_sha,
            )
        )
        payload = _reader_payload(
            phase="prepared",
            previous_index_sha256=previous_sha,
            corpus_manifest_sha256=corpus_sha,
            model_identifier=previous.model_identifier,
            model_revision=previous.model_revision,
            plan_sha256=plan_sha,
            refresh_plan_sha256=refresh_sha,
        )

    _atomic_store(
        _reader_control_path(ai_root),
        _canonical_json_bytes(payload),
    )
    return payload


def _embedder_payload(
    reader: Mapping[str, object],
    *,
    result_set_sha256: str | None,
    reused_count: int,
    embedded_count: int,
    removed_count: int,
) -> dict[str, object]:
    for value, label in (
        (reused_count, "reused_count"),
        (embedded_count, "embedded_count"),
        (removed_count, "removed_count"),
    ):
        if type(value) is not int or value < 0:
            raise SemanticRefreshError(f"{label} is invalid")
    phase = str(reader["phase"])
    if phase == "unchanged":
        if result_set_sha256 is not None or any(
            (reused_count, embedded_count, removed_count)
        ):
            raise SemanticRefreshError(
                "unchanged semantic refresh embedder result is invalid"
            )
        result_sha = None
    else:
        result_sha = _require_sha256(
            result_set_sha256,
            label="refresh result set SHA",
        )
    return {
        "record_version": RECORD_VERSION,
        "phase": phase,
        "previous_index_sha256": reader["previous_index_sha256"],
        "corpus_manifest_sha256": reader["corpus_manifest_sha256"],
        "plan_sha256": reader["plan_sha256"],
        "refresh_plan_sha256": reader["refresh_plan_sha256"],
        "result_set_sha256": result_sha,
        "reused_count": reused_count,
        "embedded_count": embedded_count,
        "removed_count": removed_count,
    }


def _parse_embedder_control(data: bytes) -> dict[str, object]:
    value = _decode_json_object(data, label="semantic refresh embedder control")
    if set(value) != {
        "record_version",
        "phase",
        "previous_index_sha256",
        "corpus_manifest_sha256",
        "plan_sha256",
        "refresh_plan_sha256",
        "result_set_sha256",
        "reused_count",
        "embedded_count",
        "removed_count",
    }:
        raise SemanticRefreshError(
            "semantic refresh embedder control properties do not match contract"
        )
    if value["record_version"] != RECORD_VERSION:
        raise SemanticRefreshError(
            "semantic refresh embedder control version is unsupported"
        )
    reader_like = {
        "phase": value["phase"],
        "previous_index_sha256": value["previous_index_sha256"],
        "corpus_manifest_sha256": value["corpus_manifest_sha256"],
        "plan_sha256": value["plan_sha256"],
        "refresh_plan_sha256": value["refresh_plan_sha256"],
    }
    # Validate SHA/null shape consistently.
    phase = str(reader_like["phase"])
    if phase not in PHASES:
        raise SemanticRefreshError("semantic refresh embedder phase is invalid")
    _require_sha256(
        reader_like["previous_index_sha256"],
        label="previous semantic index SHA",
    )
    _require_sha256(
        reader_like["corpus_manifest_sha256"],
        label="refresh corpus manifest SHA",
    )
    if phase == "prepared":
        _require_sha256(reader_like["plan_sha256"], label="refresh embedding plan SHA")
        _require_sha256(reader_like["refresh_plan_sha256"], label="refresh plan SHA")
    elif reader_like["plan_sha256"] is not None or reader_like["refresh_plan_sha256"] is not None:
        raise SemanticRefreshError("unchanged embedder control must not bind plans")
    return _embedder_payload(
        reader_like,
        result_set_sha256=value["result_set_sha256"],
        reused_count=value["reused_count"],
        embedded_count=value["embedded_count"],
        removed_count=value["removed_count"],
    )


def load_embedder_refresh_control(ai_root: Path) -> dict[str, object]:
    path = _embedder_control_path(ai_root)
    if not os.path.lexists(path):
        raise SemanticRefreshError("semantic refresh embedder control is missing")
    if path.is_symlink() or not path.is_file():
        raise SemanticRefreshError("semantic refresh embedder control is unsafe")
    return _parse_embedder_control(_read_exact_file(path))


def embed_refresh(
    ai_root: Path,
    *,
    base_url: str,
) -> dict[str, object]:
    reader = load_reader_refresh_control(ai_root)
    if reader["phase"] == "unchanged":
        payload = _embedder_payload(
            reader,
            result_set_sha256=None,
            reused_count=0,
            embedded_count=0,
            removed_count=0,
        )
    else:
        result_sha, _path, _result_set, stats = (
            embed_semantic_plan_incremental_with_ollama(
                ai_root,
                refresh_plan_sha256=str(
                    reader["refresh_plan_sha256"]
                ),
                base_url=base_url,
            )
        )
        payload = _embedder_payload(
            reader,
            result_set_sha256=result_sha,
            reused_count=stats.reused_count,
            embedded_count=stats.embedded_count,
            removed_count=stats.removed_count,
        )

    _atomic_store(
        _embedder_control_path(ai_root),
        _canonical_json_bytes(payload),
    )
    return payload


def _controls_match(
    reader: Mapping[str, object],
    embedder: Mapping[str, object],
) -> None:
    for key in (
        "phase",
        "previous_index_sha256",
        "corpus_manifest_sha256",
        "plan_sha256",
        "refresh_plan_sha256",
    ):
        if reader[key] != embedder[key]:
            raise SemanticRefreshError(
                f"semantic refresh controls do not match: {key}"
            )


def finalize_refresh(
    ai_root: Path,
    vault_root: Path,
) -> dict[str, object]:
    reader = load_reader_refresh_control(ai_root)
    embedder = load_embedder_refresh_control(ai_root)
    _controls_match(reader, embedder)

    active = load_active_semantic_index_binding(ai_root)
    if active["semantic_index_sha256"] != reader["previous_index_sha256"]:
        raise SemanticRefreshError(
            "active semantic index changed since refresh preparation"
        )

    if reader["phase"] == "unchanged":
        previous = load_semantic_index_manifest(
            ai_root,
            str(reader["previous_index_sha256"]),
        )
        try:
            with mirror_read_lock(ai_root):
                corpus = load_semantic_corpus_manifest(
                    ai_root,
                    previous.corpus_manifest_sha256,
                )
                verify_semantic_corpus_current(vault_root, corpus)
        except (ProductionIOError, SemanticCorpusError) as exc:
            raise SemanticRefreshError(str(exc)) from exc
        return {
            "status": "unchanged",
            "semantic_index_sha256": reader["previous_index_sha256"],
            "corpus_manifest_sha256": reader["corpus_manifest_sha256"],
            "reused_count": 0,
            "embedded_count": 0,
            "removed_count": 0,
        }

    index_sha, _path, index = finalize_semantic_index(
        ai_root,
        vault_root,
        plan_sha256=str(reader["plan_sha256"]),
        result_set_sha256=str(embedder["result_set_sha256"]),
    )
    if (
        index.corpus_manifest_sha256 != reader["corpus_manifest_sha256"]
        or index.model_identifier != active["model_identifier"]
        or index.model_revision != active["model_revision"]
    ):
        raise SemanticRefreshError(
            "finalized semantic index does not match refresh binding"
        )

    binding = _active_payload(
        semantic_index_sha256=index_sha,
        corpus_manifest_sha256=index.corpus_manifest_sha256,
        model_identifier=index.model_identifier,
        model_revision=index.model_revision,
    )
    _atomic_store(
        active_binding_path(ai_root),
        _canonical_json_bytes(binding),
    )
    return {
        "status": "activated",
        "semantic_index_sha256": index_sha,
        "previous_index_sha256": reader["previous_index_sha256"],
        "corpus_manifest_sha256": index.corpus_manifest_sha256,
        "reused_count": embedder["reused_count"],
        "embedded_count": embedder["embedded_count"],
        "removed_count": embedder["removed_count"],
    }


def _print(payload: Mapping[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="obsidian-semantic-index-refresh"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    activate = sub.add_parser("activate")
    activate.add_argument("--ai-root", type=Path, required=True)
    activate.add_argument("--vault-root", type=Path, required=True)
    activate.add_argument("--semantic-index-sha", required=True)

    prepare = sub.add_parser("prepare")
    prepare.add_argument("--ai-root", type=Path, required=True)
    prepare.add_argument("--vault-root", type=Path, required=True)

    embed = sub.add_parser("embed")
    embed.add_argument("--ai-root", type=Path, required=True)
    embed.add_argument("--base-url", required=True)

    finalize = sub.add_parser("finalize")
    finalize.add_argument("--ai-root", type=Path, required=True)
    finalize.add_argument("--vault-root", type=Path, required=True)

    resolve = sub.add_parser("resolve")
    resolve.add_argument("--ai-root", type=Path, required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "activate":
            result = activate_semantic_index(
                args.ai_root,
                args.vault_root,
                semantic_index_sha256=args.semantic_index_sha,
            )
        elif args.command == "prepare":
            result = prepare_refresh(args.ai_root, args.vault_root)
        elif args.command == "embed":
            result = embed_refresh(args.ai_root, base_url=args.base_url)
        elif args.command == "finalize":
            result = finalize_refresh(args.ai_root, args.vault_root)
        else:
            result = {
                "semantic_index_sha256": resolve_active_semantic_index_sha(
                    args.ai_root
                )
            }
    except (
        ArtifactLifecycleError,
        OSError,
        ProductionIOError,
        SemanticCorpusError,
        SemanticIndexError,
        SemanticRefreshError,
        OllamaProviderError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
