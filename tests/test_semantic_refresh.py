from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

import obsidian_automation.semantic_refresh as semantic_refresh
from obsidian_automation.artifact_lifecycle import _canonical_json_bytes
from obsidian_automation.ollama_generator import OllamaProviderError
from obsidian_automation.production_io import mirror_read_lock
from obsidian_automation.semantic_corpus import (
    build_semantic_corpus,
    store_semantic_corpus_manifest,
)
from obsidian_automation.semantic_index import (
    SemanticIndexError,
    embed_semantic_plan_incremental_with_ollama,
    embed_semantic_plan_with_ollama,
    finalize_semantic_index,
    load_semantic_index_manifest,
    prepare_incremental_embedding_refresh_plan,
    prepare_semantic_embedding_plan,
)
from obsidian_automation.semantic_refresh import (
    SemanticRefreshError,
    activate_semantic_index,
    embed_refresh,
    finalize_refresh,
    load_active_semantic_index_binding,
    prepare_refresh,
    resolve_active_semantic_index_sha,
)


MODEL = "qwen3-embedding:4b"
MODEL_DIGEST = "a" * 64


def _note(frontmatter: str, body: str) -> str:
    return f"---\n{frontmatter}---\n{body}\n"


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    for root in (
        "00-DailyNote",
        "05-Idea",
        "10-Project",
        "11-Knowledge",
    ):
        (vault / root).mkdir(parents=True)

    (vault / "05-Idea" / "Idea.md").write_text(
        _note(
            "type: idea\nstatus: active\n",
            "# Idea\nReusable unchanged evidence.",
        ),
        encoding="utf-8",
    )
    (vault / "11-Knowledge" / "Knowledge.md").write_text(
        _note(
            "type: knowledge-note\n"
            "status: active\n"
            "category: system\n"
            "maturity: stable\n"
            "source_type: self\n",
            "# Knowledge\nInitial semantic content.",
        ),
        encoding="utf-8",
    )
    return vault


def _state(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    (state / "04-Index").mkdir(parents=True)
    (state / "24-Locks" / "read-view").mkdir(parents=True)
    return state


def _transport(calls: list[str]):
    def transport(
        base_url: str,
        *,
        method: str,
        path: str,
        payload,
        timeout: float,
    ):
        calls.append(path)
        if path == "/api/tags":
            return {
                "models": [
                    {
                        "name": MODEL,
                        "model": MODEL,
                        "digest": MODEL_DIGEST,
                    }
                ]
            }
        assert path == "/api/embed"
        return {
            "model": MODEL,
            "embeddings": [
                [
                    float(index + 1),
                    float(len(text.encode("utf-8"))),
                    0.5,
                ]
                for index, text in enumerate(payload["input"])
            ],
        }

    return transport


def _initial_index(tmp_path: Path) -> tuple[Path, Path, str]:
    vault = _vault(tmp_path)
    state = _state(tmp_path)
    corpus = build_semantic_corpus(vault)
    corpus_sha, _ = store_semantic_corpus_manifest(state, corpus)
    plan_sha, _, _ = prepare_semantic_embedding_plan(
        state,
        vault,
        corpus_manifest_sha256=corpus_sha,
        model_identifier=MODEL,
        model_revision=MODEL_DIGEST,
    )
    result_sha, _, _ = embed_semantic_plan_with_ollama(
        state,
        plan_sha256=plan_sha,
        base_url="http://127.0.0.1:11434",
        transport=_transport([]),
    )
    index_sha, _, _ = finalize_semantic_index(
        state,
        vault,
        plan_sha256=plan_sha,
        result_set_sha256=result_sha,
    )
    return vault, state, index_sha


def _patch_incremental_provider(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[str],
) -> None:
    original = embed_semantic_plan_incremental_with_ollama

    def wrapped(
        ai_root: Path,
        *,
        refresh_plan_sha256: str,
        base_url: str,
    ):
        return original(
            ai_root,
            refresh_plan_sha256=refresh_plan_sha256,
            base_url=base_url,
            transport=_transport(calls),
        )

    monkeypatch.setattr(
        semantic_refresh,
        "embed_semantic_plan_incremental_with_ollama",
        wrapped,
    )


def _append_evidence(vault: Path, evidence: str) -> None:
    path = vault / "11-Knowledge" / "Knowledge.md"
    path.write_text(
        path.read_text(encoding="utf-8") + f"\n{evidence}\n",
        encoding="utf-8",
    )


def _control_paths(state: Path) -> tuple[Path, Path]:
    index = state / "04-Index"
    return (
        index / semantic_refresh.READER_CONTROL_DIR / semantic_refresh.CONTROL_FILE,
        index / semantic_refresh.EMBEDDER_CONTROL_DIR / semantic_refresh.CONTROL_FILE,
    )


def _changed_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path, str]:
    vault, state, index_sha = _initial_index(tmp_path)
    activate_semantic_index(state, vault, semantic_index_sha256=index_sha)
    _append_evidence(vault, "New evidence requiring incremental embedding.")
    assert prepare_refresh(state, vault)["phase"] == "prepared"
    _patch_incremental_provider(monkeypatch, [])
    embed_refresh(state, base_url="http://127.0.0.1:11434")
    return vault, state, index_sha


def _different_full_index(
    state: Path,
    vault: Path,
    plan_sha: str,
) -> str:
    """Build another valid result, with distinct vectors, for the same plan."""
    transport = _transport([])

    def different_transport(base_url: str, **kwargs):
        response = transport(base_url, **kwargs)
        if kwargs["path"] == "/api/embed":
            response["embeddings"] = [
                [vector[0] + 10.0, *vector[1:]]
                for vector in response["embeddings"]
            ]
        return response

    result_sha, _, _ = embed_semantic_plan_with_ollama(
        state,
        plan_sha256=plan_sha,
        base_url="http://127.0.0.1:11434",
        transport=different_transport,
    )
    index_sha, _, _ = finalize_semantic_index(
        state,
        vault,
        plan_sha256=plan_sha,
        result_set_sha256=result_sha,
    )
    return index_sha


def _other_process_can_lock(path: Path) -> bool:
    """Probe the actual flock using a fresh process and independent descriptor."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import fcntl, os, sys\n"
            "fd = os.open(sys.argv[1], os.O_RDWR | os.O_NOFOLLOW)\n"
            "try:\n"
            "    try:\n"
            "        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "    except BlockingIOError:\n"
            "        sys.exit(2)\n"
            "finally:\n"
            "    os.close(fd)\n",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode in {0, 2}, result.stderr
    return result.returncode == 0


def test_activate_and_unchanged_refresh_keep_exact_index(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    vault, state, index_sha = _initial_index(tmp_path)

    binding = activate_semantic_index(
        state,
        vault,
        semantic_index_sha256=index_sha,
    )
    assert binding["semantic_index_sha256"] == index_sha
    assert resolve_active_semantic_index_sha(state) == index_sha
    active_path = semantic_refresh.active_binding_path(state)
    active_before = active_path.read_bytes()

    def forbidden_provider(*args, **kwargs):
        raise AssertionError("unchanged refresh must not call the embedding provider")

    monkeypatch.setattr(
        semantic_refresh,
        "embed_semantic_plan_incremental_with_ollama",
        forbidden_provider,
    )

    prepared = prepare_refresh(state, vault)
    assert prepared["phase"] == "unchanged"

    embedded = embed_refresh(
        state,
        base_url="http://127.0.0.1:11434",
    )
    assert embedded["phase"] == "unchanged"

    finalized = finalize_refresh(state, vault)
    assert finalized["status"] == "unchanged"
    assert finalized["semantic_index_sha256"] == index_sha
    assert resolve_active_semantic_index_sha(state) == index_sha
    assert active_path.read_bytes() == active_before

    # A second no-op must preserve even the operational binding's exact bytes.
    prepare_refresh(state, vault)
    embed_refresh(state, base_url="http://127.0.0.1:11434")
    assert finalize_refresh(state, vault)["status"] == "unchanged"
    assert active_path.read_bytes() == active_before


def test_incremental_refresh_activates_only_finalized_current_index(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    vault, state, index_sha = _initial_index(tmp_path)
    activate_semantic_index(
        state,
        vault,
        semantic_index_sha256=index_sha,
    )

    knowledge = vault / "11-Knowledge" / "Knowledge.md"
    knowledge.write_text(
        _note(
            "type: knowledge-note\n"
            "status: active\n"
            "category: system\n"
            "maturity: stable\n"
            "source_type: self\n",
            "# Knowledge\nChanged semantic content.",
        ),
        encoding="utf-8",
    )

    prepared = prepare_refresh(state, vault)
    assert prepared["phase"] == "prepared"
    assert prepared["previous_index_sha256"] == index_sha

    calls: list[str] = []
    _patch_incremental_provider(monkeypatch, calls)
    embedded = embed_refresh(
        state,
        base_url="http://127.0.0.1:11434",
    )
    assert embedded["reused_count"] >= 1
    assert embedded["embedded_count"] >= 1
    assert "/api/embed" in calls

    finalized = finalize_refresh(state, vault)
    assert finalized["status"] == "activated"
    assert finalized["previous_index_sha256"] == index_sha
    assert finalized["semantic_index_sha256"] != index_sha

    active = load_active_semantic_index_binding(state)
    assert (
        active["semantic_index_sha256"]
        == finalized["semantic_index_sha256"]
    )


def test_mirror_change_after_embedding_leaves_previous_binding_active(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    vault, state, index_sha = _initial_index(tmp_path)
    activate_semantic_index(
        state,
        vault,
        semantic_index_sha256=index_sha,
    )

    knowledge = vault / "11-Knowledge" / "Knowledge.md"
    knowledge.write_text(
        _note(
            "type: knowledge-note\n"
            "status: active\n"
            "category: system\n"
            "maturity: stable\n"
            "source_type: self\n",
            "# Knowledge\nFirst changed content.",
        ),
        encoding="utf-8",
    )
    assert prepare_refresh(state, vault)["phase"] == "prepared"

    _patch_incremental_provider(monkeypatch, [])
    embed_refresh(
        state,
        base_url="http://127.0.0.1:11434",
    )

    knowledge.write_text(
        _note(
            "type: knowledge-note\n"
            "status: active\n"
            "category: system\n"
            "maturity: stable\n"
            "source_type: self\n",
            "# Knowledge\nSecond changed content races finalization.",
        ),
        encoding="utf-8",
    )

    with pytest.raises(SemanticIndexError, match="stale"):
        finalize_refresh(state, vault)

    assert resolve_active_semantic_index_sha(state) == index_sha


def test_missing_active_binding_fails_closed(tmp_path: Path) -> None:
    state = _state(tmp_path)
    with pytest.raises(
        SemanticRefreshError,
        match="binding is missing",
    ):
        resolve_active_semantic_index_sha(state)


def test_mirror_change_in_publication_gap_preserves_active_binding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    vault, state, _ = _changed_refresh(tmp_path, monkeypatch)
    active_path = semantic_refresh.active_binding_path(state)
    active_before = active_path.read_bytes()
    original_finalize = semantic_refresh.finalize_semantic_index
    finalized: list[str] = []

    def finalize_then_pull(*args, **kwargs):
        result = original_finalize(*args, **kwargs)
        finalized.append(result[0])
        # This is the precise gap after immutable finalization has released its
        # mirror lock. A real cooperating pull can change the mirror here.
        with mirror_read_lock(state):
            _append_evidence(vault, "Pull completed before active publication.")
        return result

    monkeypatch.setattr(
        semantic_refresh,
        "finalize_semantic_index",
        finalize_then_pull,
    )
    with pytest.raises((SemanticIndexError, SemanticRefreshError), match="stale"):
        finalize_refresh(state, vault)

    assert len(finalized) == 1
    assert active_path.read_bytes() == active_before


@pytest.mark.parametrize("stage", ["activate", "prepare", "finalize"])
def test_reader_publication_holds_real_locks(
    stage: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    if stage == "finalize":
        vault, state, index_sha = _changed_refresh(tmp_path, monkeypatch)
    else:
        vault, state, index_sha = _initial_index(tmp_path)
        if stage == "prepare":
            activate_semantic_index(state, vault, semantic_index_sha256=index_sha)

    reader_path, _ = _control_paths(state)
    reader_lock = reader_path.parent / "refresh.lock"
    mirror_lock = state / "24-Locks" / "read-view" / "mirror-read.lock"
    target_path = (
        reader_path
        if stage == "prepare"
        else semantic_refresh.active_binding_path(state)
    )
    original_store = semantic_refresh._atomic_store
    publications: list[Path] = []

    def checked_store(path: Path, data: bytes) -> Path:
        if path == target_path:
            # Do not mock the lock: independent processes try the real flock.
            assert not _other_process_can_lock(reader_lock)
            if stage != "prepare":
                assert not _other_process_can_lock(mirror_lock)
            publications.append(path)
        return original_store(path, data)

    monkeypatch.setattr(semantic_refresh, "_atomic_store", checked_store)
    if stage == "activate":
        activate_semantic_index(state, vault, semantic_index_sha256=index_sha)
    elif stage == "prepare":
        prepare_refresh(state, vault)
    else:
        finalize_refresh(state, vault)

    assert publications == [target_path]
    assert _other_process_can_lock(reader_lock)
    assert _other_process_can_lock(mirror_lock)


def test_active_change_after_immutable_finalize_is_not_overwritten(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    vault, state, _ = _changed_refresh(tmp_path, monkeypatch)
    reader_path, _ = _control_paths(state)
    reader = json.loads(reader_path.read_bytes())
    alternative_sha = _different_full_index(state, vault, reader["plan_sha256"])
    alternative = load_semantic_index_manifest(state, alternative_sha)
    replacement = dict(load_active_semantic_index_binding(state))
    replacement.update(
        semantic_index_sha256=alternative_sha,
        corpus_manifest_sha256=alternative.corpus_manifest_sha256,
        model_identifier=alternative.model_identifier,
        model_revision=alternative.model_revision,
    )
    replacement_bytes = _canonical_json_bytes(replacement)
    active_path = semantic_refresh.active_binding_path(state)
    original_finalize = semantic_refresh.finalize_semantic_index

    def finalize_then_replace_binding(*args, **kwargs):
        result = original_finalize(*args, **kwargs)
        assert result[0] != alternative_sha
        # Inject an external replacement at the publication boundary. Cooperating
        # Reader commands are serialized separately by the real-lock test.
        semantic_refresh._atomic_store(active_path, replacement_bytes)
        return result

    monkeypatch.setattr(
        semantic_refresh,
        "finalize_semantic_index",
        finalize_then_replace_binding,
    )
    with pytest.raises(SemanticRefreshError, match="active|binding|changed"):
        finalize_refresh(state, vault)

    assert active_path.read_bytes() == replacement_bytes


@pytest.mark.parametrize("change", ["metadata", "serialization"])
def test_embed_result_binds_complete_reader_control_bytes(
    change: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    vault, state, _ = _changed_refresh(tmp_path, monkeypatch)
    active_path = semantic_refresh.active_binding_path(state)
    active_before = active_path.read_bytes()
    reader_path, embedder_path = _control_paths(state)
    original_bytes = reader_path.read_bytes()
    embedded = json.loads(embedder_path.read_bytes())
    assert embedded["reader_control_sha256"] == hashlib.sha256(original_bytes).hexdigest()

    reader = json.loads(original_bytes)
    if change == "metadata":
        # The old partial comparison did not bind this Reader-controlled field.
        reader["model_revision"] = "b" * 64
        replacement = _canonical_json_bytes(reader)
    else:
        # JSON-equivalent bytes still belong to a different exact prepare ticket.
        replacement = json.dumps(reader, indent=2).encode("utf-8")
    assert replacement != original_bytes
    reader_path.write_bytes(replacement)

    with pytest.raises(SemanticRefreshError, match="control|handoff|prepare|binding"):
        finalize_refresh(state, vault)

    assert active_path.read_bytes() == active_before
    assert reader_path.read_bytes() == replacement


def test_reader_control_change_in_publication_gap_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    vault, state, _ = _changed_refresh(tmp_path, monkeypatch)
    active_path = semantic_refresh.active_binding_path(state)
    active_before = active_path.read_bytes()
    reader_path, _ = _control_paths(state)
    original_bytes = reader_path.read_bytes()
    replacement = json.dumps(json.loads(original_bytes), indent=2).encode("utf-8")
    assert replacement != original_bytes
    original_finalize = semantic_refresh.finalize_semantic_index

    def finalize_then_replace_control(*args, **kwargs):
        result = original_finalize(*args, **kwargs)
        reader_path.write_bytes(replacement)
        return result

    monkeypatch.setattr(
        semantic_refresh,
        "finalize_semantic_index",
        finalize_then_replace_control,
    )
    with pytest.raises(SemanticRefreshError, match="control|handoff|prepare|changed"):
        finalize_refresh(state, vault)

    assert active_path.read_bytes() == active_before
    assert reader_path.read_bytes() == replacement


def test_valid_result_set_for_another_refresh_plan_cannot_activate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    vault, state, initial_sha = _initial_index(tmp_path)
    initial = load_semantic_index_manifest(state, initial_sha)
    other_previous_sha = _different_full_index(
        state,
        vault,
        initial.embedding_plan_sha256,
    )
    assert other_previous_sha != initial_sha
    activate_semantic_index(state, vault, semantic_index_sha256=initial_sha)
    active_path = semantic_refresh.active_binding_path(state)
    active_before = active_path.read_bytes()
    _append_evidence(vault, "A changed corpus shared by two valid refresh plans.")
    prepared = prepare_refresh(state, vault)
    _patch_incremental_provider(monkeypatch, [])
    embed_refresh(state, base_url="http://127.0.0.1:11434")

    other_refresh_sha, _, _ = prepare_incremental_embedding_refresh_plan(
        state,
        plan_sha256=str(prepared["plan_sha256"]),
        previous_index_sha256=other_previous_sha,
    )
    assert other_refresh_sha != prepared["refresh_plan_sha256"]
    other_result_sha, _, _, _ = embed_semantic_plan_incremental_with_ollama(
        state,
        refresh_plan_sha256=other_refresh_sha,
        base_url="http://127.0.0.1:11434",
        transport=_transport([]),
    )
    # This is an internally valid immutable result set for the exact full plan.
    # Only its Reader-selected incremental lineage is wrong.
    _, _, other_index = finalize_semantic_index(
        state,
        vault,
        plan_sha256=str(prepared["plan_sha256"]),
        result_set_sha256=other_result_sha,
    )
    assert other_index.corpus_manifest_sha256 == prepared["corpus_manifest_sha256"]

    _, embedder_path = _control_paths(state)
    embedded = json.loads(embedder_path.read_bytes())
    embedded["result_set_sha256"] = other_result_sha
    embedder_path.write_bytes(_canonical_json_bytes(embedded))

    with pytest.raises(SemanticRefreshError, match="refresh|result|binding"):
        finalize_refresh(state, vault)

    assert active_path.read_bytes() == active_before


def test_provider_failure_preserves_active_and_previous_embedder_handoff(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    vault, state, initial_sha = _initial_index(tmp_path)
    activate_semantic_index(state, vault, semantic_index_sha256=initial_sha)
    prepare_refresh(state, vault)
    embed_refresh(state, base_url="http://127.0.0.1:11434")
    active_path = semantic_refresh.active_binding_path(state)
    active_before = active_path.read_bytes()
    _, embedder_path = _control_paths(state)
    result_before = embedder_path.read_bytes()

    _append_evidence(vault, "New evidence while the embedding provider is unavailable.")
    assert prepare_refresh(state, vault)["phase"] == "prepared"

    def unavailable_provider(*args, **kwargs):
        raise OllamaProviderError("embedding provider unavailable")

    monkeypatch.setattr(
        semantic_refresh,
        "embed_semantic_plan_incremental_with_ollama",
        unavailable_provider,
    )
    with pytest.raises(OllamaProviderError, match="unavailable"):
        embed_refresh(state, base_url="http://127.0.0.1:11434")

    assert active_path.read_bytes() == active_before
    assert embedder_path.read_bytes() == result_before
    with pytest.raises(SemanticRefreshError):
        finalize_refresh(state, vault)
    assert active_path.read_bytes() == active_before
