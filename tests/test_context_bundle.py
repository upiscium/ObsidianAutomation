from __future__ import annotations

import json
from pathlib import Path

import pytest

from obsidian_automation.artifact_lifecycle import ArtifactLifecycleError, sha256_bytes
from obsidian_automation.context_bundle import (
    CONTEXT_STAGE,
    build_context_bundle,
    parse_context_bundle,
    store_context_bundle,
)


def _roots(tmp_path: Path) -> tuple[Path, Path]:
    vault = tmp_path / "vault"
    knowledge = vault / "11-Knowledge"
    knowledge.mkdir(parents=True)
    state = tmp_path / "state"
    (state / CONTEXT_STAGE).mkdir(parents=True)
    return vault, state


def test_context_bundle_reads_exact_sources_and_persists_immutable_bytes(tmp_path: Path) -> None:
    vault, state = _roots(tmp_path)
    (vault / "11-Knowledge" / "B.md").write_text("# B\n", encoding="utf-8")
    (vault / "11-Knowledge" / "A.md").write_text("# A\n", encoding="utf-8")

    bundle = build_context_bundle(
        vault,
        query="existing knowledge",
        source_paths=["11-Knowledge/B.md", "11-Knowledge/A.md"],
        created_at="2026-08-22T00:00:00Z",
    )
    digest, path = store_context_bundle(state, bundle)

    assert path == state / CONTEXT_STAGE / f"{digest}.context.json"
    assert sha256_bytes(path.read_bytes()) == digest
    parsed = parse_context_bundle(path.read_bytes())
    assert [source.path for source in parsed.sources] == [
        "11-Knowledge/A.md",
        "11-Knowledge/B.md",
    ]
    assert parsed.sources[0].content == "# A\n"
    assert parsed.sources[1].content == "# B\n"

    same_digest, same_path = store_context_bundle(state, bundle)
    assert (same_digest, same_path) == (digest, path)


def test_context_bundle_rejects_sources_outside_knowledge(tmp_path: Path) -> None:
    vault, _ = _roots(tmp_path)
    (vault / "outside.md").write_text("secret\n", encoding="utf-8")

    with pytest.raises(ArtifactLifecycleError, match="below 11-Knowledge"):
        build_context_bundle(
            vault,
            query="query",
            source_paths=["outside.md"],
        )


def test_context_bundle_does_not_follow_source_symlink(tmp_path: Path) -> None:
    vault, _ = _roots(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("outside\n", encoding="utf-8")
    (vault / "11-Knowledge" / "link.md").symlink_to(outside)

    with pytest.raises(ArtifactLifecycleError, match="safely opened"):
        build_context_bundle(
            vault,
            query="query",
            source_paths=["11-Knowledge/link.md"],
        )


def test_context_bundle_detects_content_hash_tampering(tmp_path: Path) -> None:
    vault, _ = _roots(tmp_path)
    (vault / "11-Knowledge" / "A.md").write_text("# A\n", encoding="utf-8")
    bundle = build_context_bundle(
        vault,
        query="query",
        source_paths=["11-Knowledge/A.md"],
        created_at="2026-08-22T00:00:00Z",
    )
    value = json.loads(bundle.to_json_bytes())
    value["sources"][0]["content"] = "tampered"

    with pytest.raises(ArtifactLifecycleError, match="content_sha256"):
        parse_context_bundle((json.dumps(value) + "\n").encode())


def test_context_bundle_allows_empty_retrieval_result(tmp_path: Path) -> None:
    vault, state = _roots(tmp_path)
    bundle = build_context_bundle(
        vault,
        query="no related notes",
        source_paths=[],
        created_at="2026-08-22T00:00:00Z",
    )
    digest, path = store_context_bundle(state, bundle)

    assert digest == sha256_bytes(path.read_bytes())
    assert parse_context_bundle(path.read_bytes()).sources == ()
