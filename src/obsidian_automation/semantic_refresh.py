from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import tempfile
from typing import Mapping, Sequence

from .artifact_lifecycle import (
    ArtifactLifecycleError,
    _canonical_json_bytes,
    _decode_json_object,
    _read_exact_file,
    _require_safe_directory,
    _require_sha256,
    sha256_bytes,
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


RECORD_VERSION = 1
INDEX_STAGE = "04-Index"
REFRESH_ROOT = "semantic-refresh"
READER_DIR = "reader"
EMBEDDER_DIR = "embedder"
ACTIVE_DIR = "active"
CURRENT_FILE = "current.json"
DEFAULT_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_BATCH_SIZE = 32
ACTIVE_TOKEN = "active"


class SemanticRefreshError(RuntimeError):
    """Raised when the automatic Semantic Index refresh contract fails closed."""


@dataclass(frozen=True)
class RefreshPrepare:
    action: str
    previous_index_sha256: str
    corpus_manifest_sha256: str
    plan_sha256: str | None
    refresh_plan_sha256: str | None
    prepared_at: str

    def to_json_bytes(self) -> bytes:
        return _canonical_json_bytes(
            {
                "record_version": RECORD_VERSION,
                "action": self.action,
                "previous_index_sha256": self.previous_index_sha256,
                "corpus_manifest_sha256": self.corpus_manifest_sha256,
                "plan_sha256": self.plan_sha256,
                "refresh_plan_sha256": self.refresh_plan_sha256,
                "prepared_at": self.prepared_at,
            }
        )


@dataclass(frozen=True)
class RefreshEmbedResult:
    prepare_sha256: str
    action: str
    result_set_sha256: str | None
    reused_count: int
    embedded_count: int
    removed_count: int
    completed_at: str

    def to_json_bytes(self) -> bytes:
        return _canonical_json_bytes(
            {
                "record_version": RECORD_VERSION,
                "prepare_sha256": self.prepare_sha256,
                "action": self.action,
                "result_set_sha256": self.result_set_sha256,
                "reused_count": self.reused_count,
                "embedded_count": self.embedded_count,
                "removed_count": self.removed_count,
                "completed_at": self.completed_at,
            }
        )


@dataclass(frozen=True)
class ActiveSemanticIndex:
    semantic_index_sha256: str
    corpus_manifest_sha256: str
    previous_index_sha256: str | None
    activation_source: str
    prepare_sha256: str
    embed_result_sha256: str
    activated_at: str

    def to_json_bytes(self) -> bytes:
        return _canonical_json_bytes(
            {
                "record_version": RECORD_VERSION,
                "semantic_index_sha256": self.semantic_index_sha256,
                "corpus_manifest_sha256": self.corpus_manifest_sha256,
                "previous_index_sha256": self.previous_index_sha256,
                "activation_source": self.activation_source,
                "prepare_sha256": self.prepare_sha256,
                "embed_result_sha256": self.embed_result_sha256,
                "activated_at": self.activated_at,
            }
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _refresh_root(ai_root: Path) -> Path:
    root = ai_root.absolute()
    _require_safe_directory(root, create=False)
    index_root = root / INDEX_STAGE
    _require_safe_directory(index_root, create=False)
    refresh = index_root / REFRESH_ROOT
    _require_safe_directory(refresh, create=False)
    return refresh


def _handoff_path(ai_root: Path, role: str) -> Path:
    if role not in {READER_DIR, EMBEDDER_DIR, ACTIVE_DIR}:
        raise SemanticRefreshError("semantic refresh handoff role is invalid")
    directory = _refresh_root(ai_root) / role
    _require_safe_directory(directory, create=False)
    return directory / CURRENT_FILE


def _atomic_replace(path: Path, data: bytes) -> Path:
    _require_safe_directory(path.parent, create=False)
    if os.path.lexists(path) and path.is_symlink():
        raise SemanticRefreshError("semantic refresh destination is a symlink")
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    temp_path = Path(temporary)
    try:
        os.fchmod(fd, 0o640)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise SemanticRefreshError("short write while storing semantic refresh state")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(temp_path, path)
        dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return path


def _require_timestamp(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.endswith("Z") or len(value) > 64:
        raise SemanticRefreshError(f"{label} is invalid")
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise SemanticRefreshError(f"{label} is invalid") from exc
    return value


def _optional_sha(value: object, *, label: str) -> str | None:
    if value is None:
        return None
    try:
        return _require_sha256(value, label=label)
    except ArtifactLifecycleError as exc:
        raise SemanticRefreshError(str(exc)) from exc


def _nonnegative_int(value: object, *, label: str) -> int:
    if type(value) is not int or value < 0:
        raise SemanticRefreshError(f"{label} is invalid")
    return value


def parse_refresh_prepare(data: bytes) -> RefreshPrepare:
    try:
        value = _decode_json_object(data, label="semantic refresh prepare")
    except ArtifactLifecycleError as exc:
        raise SemanticRefreshError(str(exc)) from exc
    if set(value) != {
        "record_version",
        "action",
        "previous_index_sha256",
        "corpus_manifest_sha256",
        "plan_sha256",
        "refresh_plan_sha256",
        "prepared_at",
    } or value["record_version"] != RECORD_VERSION:
        raise SemanticRefreshError("semantic refresh prepare properties do not match contract")
    action = value["action"]
    if action not in {"noop", "refresh"}:
        raise SemanticRefreshError("semantic refresh prepare action is invalid")
    previous = _require_sha(value["previous_index_sha256"], "previous index SHA")
    corpus = _require_sha(value["corpus_manifest_sha256"], "corpus manifest SHA")
    plan = _optional_sha(value["plan_sha256"], label="embedding plan SHA")
    refresh = _optional_sha(
        value["refresh_plan_sha256"],
        label="embedding refresh plan SHA",
    )
    if action == "noop" and (plan is not None or refresh is not None):
        raise SemanticRefreshError("noop semantic refresh prepare must not bind embedding work")
    if action == "refresh" and (plan is None or refresh is None):
        raise SemanticRefreshError("refresh semantic prepare requires plan identities")
    return RefreshPrepare(
        action=action,
        previous_index_sha256=previous,
        corpus_manifest_sha256=corpus,
        plan_sha256=plan,
        refresh_plan_sha256=refresh,
        prepared_at=_require_timestamp(value["prepared_at"], label="prepared_at"),
    )


def parse_refresh_embed_result(data: bytes) -> RefreshEmbedResult:
    try:
        value = _decode_json_object(data, label="semantic refresh embed result")
    except ArtifactLifecycleError as exc:
        raise SemanticRefreshError(str(exc)) from exc
    if set(value) != {
        "record_version",
        "prepare_sha256",
        "action",
        "result_set_sha256",
        "reused_count",
        "embedded_count",
        "removed_count",
        "completed_at",
    } or value["record_version"] != RECORD_VERSION:
        raise SemanticRefreshError("semantic refresh embed result properties do not match contract")
    action = value["action"]
    if action not in {"noop", "refresh"}:
        raise SemanticRefreshError("semantic refresh embed result action is invalid")
    result_set = _optional_sha(value["result_set_sha256"], label="result set SHA")
    if action == "noop" and result_set is not None:
        raise SemanticRefreshError("noop embed result must not bind a result set")
    if action == "refresh" and result_set is None:
        raise SemanticRefreshError("refresh embed result requires a result set")
    counts = {
        name: _nonnegative_int(value[name], label=name)
        for name in ("reused_count", "embedded_count", "removed_count")
    }
    if action == "noop" and any(counts.values()):
        raise SemanticRefreshError("noop embed result counts must be zero")
    return RefreshEmbedResult(
        prepare_sha256=_require_sha(value["prepare_sha256"], "prepare SHA"),
        action=action,
        result_set_sha256=result_set,
        reused_count=counts["reused_count"],
        embedded_count=counts["embedded_count"],
        removed_count=counts["removed_count"],
        completed_at=_require_timestamp(value["completed_at"], label="completed_at"),
    )


def parse_active_semantic_index(data: bytes) -> ActiveSemanticIndex:
    try:
        value = _decode_json_object(data, label="active semantic index binding")
    except ArtifactLifecycleError as exc:
        raise SemanticRefreshError(str(exc)) from exc
    if set(value) != {
        "record_version",
        "semantic_index_sha256",
        "corpus_manifest_sha256",
        "previous_index_sha256",
        "activation_source",
        "prepare_sha256",
        "embed_result_sha256",
        "activated_at",
    } or value["record_version"] != RECORD_VERSION:
        raise SemanticRefreshError("active semantic index binding properties do not match contract")
    source = value["activation_source"]
    if source not in {"seed-current", "incremental-refresh"}:
        raise SemanticRefreshError("active semantic index activation_source is invalid")
    return ActiveSemanticIndex(
        semantic_index_sha256=_require_sha(value["semantic_index_sha256"], "semantic index SHA"),
        corpus_manifest_sha256=_require_sha(value["corpus_manifest_sha256"], "corpus manifest SHA"),
        previous_index_sha256=_optional_sha(
            value["previous_index_sha256"],
            label="previous semantic index SHA",
        ),
        activation_source=source,
        prepare_sha256=_require_sha(value["prepare_sha256"], "prepare SHA"),
        embed_result_sha256=_require_sha(value["embed_result_sha256"], "embed result SHA"),
        activated_at=_require_timestamp(value["activated_at"], label="activated_at"),
    )


def _require_sha(value: object, label: str) -> str:
    try:
        return _require_sha256(value, label=label)
    except ArtifactLifecycleError as exc:
        raise SemanticRefreshError(str(exc)) from exc


def load_active_semantic_index(ai_root: Path) -> ActiveSemanticIndex | None:
    path = _handoff_path(ai_root, ACTIVE_DIR)
    if not os.path.lexists(path):
        return None
    if path.is_symlink() or not path.is_file():
        raise SemanticRefreshError("active semantic index binding path is unsafe")
    return parse_active_semantic_index(_read_exact_file(path))


def resolve_active_semantic_index(ai_root: Path, vault_root: Path) -> str:
    active = load_active_semantic_index(ai_root)
    if active is None:
        raise SemanticRefreshError("active semantic index binding does not exist")
    index = load_semantic_index_manifest(ai_root, active.semantic_index_sha256)
    if index.corpus_manifest_sha256 != active.corpus_manifest_sha256:
        raise SemanticRefreshError("active semantic index binding does not match index corpus")
    corpus = load_semantic_corpus_manifest(ai_root, active.corpus_manifest_sha256)
    try:
        with mirror_read_lock(ai_root):
            verify_semantic_corpus_current(vault_root, corpus)
    except (ProductionIOError, SemanticCorpusError) as exc:
        raise SemanticRefreshError(str(exc)) from exc
    return active.semantic_index_sha256


def _previous_index_sha(ai_root: Path, seed_index_sha256: str | None) -> str:
    active = load_active_semantic_index(ai_root)
    if active is not None:
        index = load_semantic_index_manifest(
            ai_root,
            active.semantic_index_sha256,
        )
        if index.corpus_manifest_sha256 != active.corpus_manifest_sha256:
            raise SemanticRefreshError(
                "active semantic index binding does not match finalized index"
            )
        return active.semantic_index_sha256
    if seed_index_sha256 is None:
        raise SemanticRefreshError(
            "semantic refresh requires active binding or exact seed index SHA"
        )
    return _require_sha(seed_index_sha256, "seed semantic index SHA")


def prepare_refresh(
    ai_root: Path,
    vault_root: Path,
    *,
    seed_index_sha256: str | None,
    prepared_at: str | None = None,
) -> tuple[str, RefreshPrepare]:
    previous_sha = _previous_index_sha(ai_root, seed_index_sha256)
    previous = load_semantic_index_manifest(ai_root, previous_sha)
    corpus_sha, _path, _corpus = build_and_store_semantic_corpus(
        ai_root,
        vault_root,
    )
    if corpus_sha == previous.corpus_manifest_sha256:
        prepare = RefreshPrepare(
            action="noop",
            previous_index_sha256=previous_sha,
            corpus_manifest_sha256=corpus_sha,
            plan_sha256=None,
            refresh_plan_sha256=None,
            prepared_at=prepared_at or _utc_now(),
        )
    else:
        plan_sha, _plan_path, _plan = prepare_semantic_embedding_plan(
            ai_root,
            vault_root,
            corpus_manifest_sha256=corpus_sha,
            model_identifier=previous.model_identifier,
            model_revision=previous.model_revision,
        )
        refresh_sha, _refresh_path, _refresh = (
            prepare_incremental_embedding_refresh_plan(
                ai_root,
                plan_sha256=plan_sha,
                previous_index_sha256=previous_sha,
            )
        )
        prepare = RefreshPrepare(
            action="refresh",
            previous_index_sha256=previous_sha,
            corpus_manifest_sha256=corpus_sha,
            plan_sha256=plan_sha,
            refresh_plan_sha256=refresh_sha,
            prepared_at=prepared_at or _utc_now(),
        )
    data = prepare.to_json_bytes()
    digest = sha256_bytes(data)
    _atomic_replace(_handoff_path(ai_root, READER_DIR), data)
    return digest, prepare


def embed_refresh(
    ai_root: Path,
    *,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    batch_size: int = DEFAULT_BATCH_SIZE,
    completed_at: str | None = None,
) -> tuple[str, RefreshEmbedResult]:
    prepare_path = _handoff_path(ai_root, READER_DIR)
    if not prepare_path.is_file() or prepare_path.is_symlink():
        raise SemanticRefreshError("semantic refresh prepare handoff is missing or unsafe")
    prepare_data = _read_exact_file(prepare_path)
    prepare_sha = sha256_bytes(prepare_data)
    prepare = parse_refresh_prepare(prepare_data)

    if prepare.action == "noop":
        result = RefreshEmbedResult(
            prepare_sha256=prepare_sha,
            action="noop",
            result_set_sha256=None,
            reused_count=0,
            embedded_count=0,
            removed_count=0,
            completed_at=completed_at or _utc_now(),
        )
    else:
        assert prepare.refresh_plan_sha256 is not None
        result_set_sha, _path, _result_set, stats = (
            embed_semantic_plan_incremental_with_ollama(
                ai_root,
                refresh_plan_sha256=prepare.refresh_plan_sha256,
                base_url=base_url,
                timeout=timeout,
                batch_size=batch_size,
            )
        )
        result = RefreshEmbedResult(
            prepare_sha256=prepare_sha,
            action="refresh",
            result_set_sha256=result_set_sha,
            reused_count=stats.reused_count,
            embedded_count=stats.embedded_count,
            removed_count=stats.removed_count,
            completed_at=completed_at or _utc_now(),
        )
    data = result.to_json_bytes()
    digest = sha256_bytes(data)
    _atomic_replace(_handoff_path(ai_root, EMBEDDER_DIR), data)
    return digest, result


def finalize_refresh(
    ai_root: Path,
    vault_root: Path,
    *,
    activated_at: str | None = None,
) -> tuple[ActiveSemanticIndex, bool]:
    prepare_path = _handoff_path(ai_root, READER_DIR)
    result_path = _handoff_path(ai_root, EMBEDDER_DIR)
    if (
        not prepare_path.is_file()
        or prepare_path.is_symlink()
        or not result_path.is_file()
        or result_path.is_symlink()
    ):
        raise SemanticRefreshError("semantic refresh handoff is missing or unsafe")

    prepare_data = _read_exact_file(prepare_path)
    result_data = _read_exact_file(result_path)
    prepare_sha = sha256_bytes(prepare_data)
    result_sha = sha256_bytes(result_data)
    prepare = parse_refresh_prepare(prepare_data)
    result = parse_refresh_embed_result(result_data)

    if result.prepare_sha256 != prepare_sha or result.action != prepare.action:
        raise SemanticRefreshError("semantic refresh prepare/embed handoff mismatch")

    if prepare.action == "noop":
        previous = load_semantic_index_manifest(
            ai_root,
            prepare.previous_index_sha256,
        )
        if previous.corpus_manifest_sha256 != prepare.corpus_manifest_sha256:
            raise SemanticRefreshError("noop semantic refresh previous index corpus mismatch")
        corpus = load_semantic_corpus_manifest(
            ai_root,
            prepare.corpus_manifest_sha256,
        )
        try:
            with mirror_read_lock(ai_root):
                verify_semantic_corpus_current(vault_root, corpus)
        except (ProductionIOError, SemanticCorpusError) as exc:
            raise SemanticRefreshError(str(exc)) from exc
        index_sha = prepare.previous_index_sha256
        changed = False
        source = "seed-current"
    else:
        if prepare.plan_sha256 is None or result.result_set_sha256 is None:
            raise SemanticRefreshError("refresh finalization identities are incomplete")
        index_sha, _path, manifest = finalize_semantic_index(
            ai_root,
            vault_root,
            plan_sha256=prepare.plan_sha256,
            result_set_sha256=result.result_set_sha256,
        )
        if manifest.corpus_manifest_sha256 != prepare.corpus_manifest_sha256:
            raise SemanticRefreshError("final semantic index corpus does not match prepare handoff")
        changed = index_sha != prepare.previous_index_sha256
        source = "incremental-refresh"

    active = ActiveSemanticIndex(
        semantic_index_sha256=index_sha,
        corpus_manifest_sha256=prepare.corpus_manifest_sha256,
        previous_index_sha256=(
            prepare.previous_index_sha256
            if index_sha != prepare.previous_index_sha256
            else None
        ),
        activation_source=source,
        prepare_sha256=prepare_sha,
        embed_result_sha256=result_sha,
        activated_at=activated_at or _utc_now(),
    )
    _atomic_replace(
        _handoff_path(ai_root, ACTIVE_DIR),
        active.to_json_bytes(),
    )
    return active, changed


def _optional_seed(value: str | None) -> str | None:
    if value in {None, "", "disabled", "none", "off"}:
        return None
    return value


def _print(value: Mapping[str, object]) -> None:
    import json

    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="obsidian-semantic-refresh")
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare")
    prepare.add_argument("--ai-root", type=Path, required=True)
    prepare.add_argument("--vault-root", type=Path, required=True)
    prepare.add_argument("--seed-index-sha")

    embed = sub.add_parser("embed")
    embed.add_argument("--ai-root", type=Path, required=True)
    embed.add_argument("--base-url", default=DEFAULT_BASE_URL)
    embed.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    embed.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)

    finalize = sub.add_parser("finalize")
    finalize.add_argument("--ai-root", type=Path, required=True)
    finalize.add_argument("--vault-root", type=Path, required=True)

    resolve = sub.add_parser("resolve")
    resolve.add_argument("--ai-root", type=Path, required=True)
    resolve.add_argument("--vault-root", type=Path, required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            digest, value = prepare_refresh(
                args.ai_root,
                args.vault_root,
                seed_index_sha256=_optional_seed(args.seed_index_sha),
            )
            _print(
                {
                    "event": "semantic-refresh",
                    "stage": "prepare",
                    "prepare_sha256": digest,
                    "action": value.action,
                    "previous_index_sha256": value.previous_index_sha256,
                    "corpus_manifest_sha256": value.corpus_manifest_sha256,
                    "plan_sha256": value.plan_sha256,
                    "refresh_plan_sha256": value.refresh_plan_sha256,
                }
            )
        elif args.command == "embed":
            digest, value = embed_refresh(
                args.ai_root,
                base_url=args.base_url,
                timeout=args.timeout,
                batch_size=args.batch_size,
            )
            _print(
                {
                    "event": "semantic-refresh",
                    "stage": "embed",
                    "embed_result_sha256": digest,
                    "action": value.action,
                    "result_set_sha256": value.result_set_sha256,
                    "reused_count": value.reused_count,
                    "embedded_count": value.embedded_count,
                    "removed_count": value.removed_count,
                }
            )
        elif args.command == "finalize":
            active, changed = finalize_refresh(
                args.ai_root,
                args.vault_root,
            )
            _print(
                {
                    "event": "semantic-refresh",
                    "stage": "finalize",
                    "semantic_index_sha256": active.semantic_index_sha256,
                    "corpus_manifest_sha256": active.corpus_manifest_sha256,
                    "activation_source": active.activation_source,
                    "changed": changed,
                }
            )
        else:
            _print(
                {
                    "event": "semantic-refresh",
                    "stage": "resolve",
                    "semantic_index_sha256": resolve_active_semantic_index(
                        args.ai_root,
                        args.vault_root,
                    ),
                }
            )
    except (
        ArtifactLifecycleError,
        SemanticCorpusError,
        SemanticIndexError,
        SemanticRefreshError,
        ProductionIOError,
        OllamaProviderError,
        OSError,
    ) as exc:
        print(f"error: {exc}", file=os.sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
