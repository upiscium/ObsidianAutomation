from __future__ import annotations

from pathlib import Path

import pytest

from obsidian_automation.artifact_lifecycle import ArtifactLifecycleError, sha256_bytes
from obsidian_automation.context_bundle import MAX_CONTEXT_BYTES, load_context_bundle
from obsidian_automation.knowledge_index import (
    INDEX_STAGE,
    MAX_DOCUMENT_BYTES,
    build_knowledge_index,
    load_knowledge_index,
    rank_documents,
    retrieve_context,
    store_knowledge_index,
    tokenize,
)


def _roots(tmp_path: Path) -> tuple[Path, Path]:
    vault = tmp_path / "vault"
    (vault / "11-Knowledge").mkdir(parents=True)
    state = tmp_path / "state"
    (state / INDEX_STAGE).mkdir(parents=True)
    (state / "05-Context").mkdir()
    return vault, state


def _note(*, status: str = "active", category: str = "summary", body: str) -> str:
    return (
        "---\n"
        "type: knowledge-note\n"
        f"status: {status}\n"
        f"category: {category}\n"
        "maturity: draft\n"
        "source_type: self\n"
        "---\n"
        f"{body}\n"
    )


def test_tokenizer_handles_ascii_and_japanese_deterministically() -> None:
    tokens = tokenize("NixOSでRA受信を設定する")
    assert "nixos" in tokens
    assert "受" in tokens
    assert "受信" in tokens


def test_index_contains_only_active_knowledge_notes(tmp_path: Path) -> None:
    vault, _ = _roots(tmp_path)
    (vault / "11-Knowledge" / "Active.md").write_text(
        _note(body="# NixOS RA\nIPv6 RAを受信する。"),
        encoding="utf-8",
    )
    (vault / "11-Knowledge" / "Archived.md").write_text(
        _note(status="archived", body="# old\n古い情報。"),
        encoding="utf-8",
    )
    (vault / "11-Knowledge" / "Other.md").write_text(
        "---\ntype: project-note\n---\n# not knowledge\n",
        encoding="utf-8",
    )

    index = build_knowledge_index(vault)

    assert [doc.path for doc in index.documents] == ["11-Knowledge/Active.md"]
    assert index.documents[0].status == "active"


def test_index_is_content_addressed_and_round_trips(tmp_path: Path) -> None:
    vault, state = _roots(tmp_path)
    (vault / "11-Knowledge" / "A.md").write_text(
        _note(body="# A\nalpha beta"), encoding="utf-8"
    )

    index = build_knowledge_index(vault)
    digest, path = store_knowledge_index(state, index)

    assert sha256_bytes(path.read_bytes()) == digest
    assert load_knowledge_index(state, digest) == index
    same_digest, same_path = store_knowledge_index(state, index)
    assert (same_digest, same_path) == (digest, path)


def test_bm25_prefers_relevant_title_and_heading(tmp_path: Path) -> None:
    vault, _ = _roots(tmp_path)
    (vault / "11-Knowledge" / "NixOS RA.md").write_text(
        _note(body="# NixOSでRAを受信する\nnetworkdでIPv6 Router Advertisementを受信する。"),
        encoding="utf-8",
    )
    (vault / "11-Knowledge" / "Minecraft.md").write_text(
        _note(body="# Minecraft\nNeoForge server configuration."),
        encoding="utf-8",
    )

    index = build_knowledge_index(vault)
    ranked = rank_documents(index, "NixOS RA 受信")

    assert ranked
    assert ranked[0].path == "11-Knowledge/NixOS RA.md"
    assert ranked[0].score > 0


def test_retrieval_builds_exact_context_from_ranked_notes(tmp_path: Path) -> None:
    vault, state = _roots(tmp_path)
    note = _note(body="# Proxmox LXC GPU\nLXCはホストカーネル経由でGPUを共有できる。")
    (vault / "11-Knowledge" / "LXC GPU.md").write_text(note, encoding="utf-8")
    (vault / "11-Knowledge" / "Other.md").write_text(
        _note(body="# Other\nUnrelated content."), encoding="utf-8"
    )

    index = build_knowledge_index(vault)
    index_sha, _ = store_knowledge_index(state, index)
    result = retrieve_context(
        state,
        vault,
        index_sha256=index_sha,
        query="LXC GPU共有",
        top_k=1,
    )

    assert result["index_sha256"] == index_sha
    selected = result["selected"]
    assert isinstance(selected, list)
    assert selected[0]["path"] == "11-Knowledge/LXC GPU.md"
    context = load_context_bundle(state, str(result["context_sha256"]))
    assert len(context.sources) == 1
    assert context.sources[0].path == "11-Knowledge/LXC GPU.md"
    assert context.sources[0].content == note


def test_retrieval_respects_context_aggregate_byte_budget(tmp_path: Path) -> None:
    vault, state = _roots(tmp_path)
    payload = "alpha " * 18000
    for index in range(5):
        (vault / "11-Knowledge" / f"Large-{index}.md").write_text(
            _note(body=f"# Alpha {index}\n{payload}"),
            encoding="utf-8",
        )

    index_sha, _ = store_knowledge_index(state, build_knowledge_index(vault))
    result = retrieve_context(
        state,
        vault,
        index_sha256=index_sha,
        query="alpha",
        top_k=5,
    )

    selected = result["selected"]
    assert isinstance(selected, list)
    assert len(selected) == 4
    context = load_context_bundle(state, str(result["context_sha256"]))
    assert sum(len(source.content.encode("utf-8")) for source in context.sources) <= MAX_CONTEXT_BYTES


def test_index_rejects_source_larger_than_context_source_limit(tmp_path: Path) -> None:
    vault, _ = _roots(tmp_path)
    oversized = "x" * (MAX_DOCUMENT_BYTES + 1)
    (vault / "11-Knowledge" / "TooLarge.md").write_text(
        _note(body=oversized),
        encoding="utf-8",
    )

    with pytest.raises(ArtifactLifecycleError, match="exceeds"):
        build_knowledge_index(vault)


def test_retrieval_fails_closed_when_index_is_stale(tmp_path: Path) -> None:
    vault, state = _roots(tmp_path)
    path = vault / "11-Knowledge" / "A.md"
    path.write_text(_note(body="# A\nalpha"), encoding="utf-8")
    index = build_knowledge_index(vault)
    index_sha, _ = store_knowledge_index(state, index)

    path.write_text(_note(body="# A\nbeta"), encoding="utf-8")

    with pytest.raises(ArtifactLifecycleError, match="stale"):
        retrieve_context(
            state,
            vault,
            index_sha256=index_sha,
            query="alpha",
            top_k=1,
        )


def test_retrieval_fails_closed_on_added_active_note(tmp_path: Path) -> None:
    vault, state = _roots(tmp_path)
    (vault / "11-Knowledge" / "A.md").write_text(
        _note(body="# A\nalpha"), encoding="utf-8"
    )
    index = build_knowledge_index(vault)
    index_sha, _ = store_knowledge_index(state, index)

    (vault / "11-Knowledge" / "B.md").write_text(
        _note(body="# B\nalpha alpha"), encoding="utf-8"
    )

    with pytest.raises(ArtifactLifecycleError, match="stale"):
        retrieve_context(
            state,
            vault,
            index_sha256=index_sha,
            query="alpha",
            top_k=1,
        )


def test_index_rejects_symlink_in_retrieval_corpus(tmp_path: Path) -> None:
    vault, _ = _roots(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text(_note(body="# secret\noutside"), encoding="utf-8")
    (vault / "11-Knowledge" / "link.md").symlink_to(outside)

    with pytest.raises(ArtifactLifecycleError, match="symlink"):
        build_knowledge_index(vault)


def test_empty_match_produces_empty_context(tmp_path: Path) -> None:
    vault, state = _roots(tmp_path)
    (vault / "11-Knowledge" / "A.md").write_text(
        _note(body="# Alpha\nalpha"), encoding="utf-8"
    )
    index_sha, _ = store_knowledge_index(state, build_knowledge_index(vault))

    result = retrieve_context(
        state,
        vault,
        index_sha256=index_sha,
        query="zzzznotfound",
        top_k=3,
    )

    assert result["selected"] == []
    context = load_context_bundle(state, str(result["context_sha256"]))
    assert context.sources == ()
