from __future__ import annotations

import json
from pathlib import Path

import pytest

from obsidian_automation.artifact_lifecycle import sha256_bytes
from obsidian_automation.semantic_corpus import (
    CHUNK_POLICY_VERSION,
    SemanticCorpusError,
    build_semantic_corpus,
    load_semantic_corpus_manifest,
    parse_semantic_corpus_manifest,
    store_semantic_corpus_manifest,
    verify_semantic_corpus_current,
)


def _note(frontmatter: str, body: str) -> str:
    return f"---\n{frontmatter}---\n{body}\n"


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    for root in ("00-DailyNote", "05-Idea", "10-Project", "11-Knowledge"):
        (vault / root).mkdir(parents=True)

    daily = vault / "00-DailyNote" / "2026" / "09"
    daily.mkdir(parents=True)
    (daily / "2026-09-28.md").write_text(
        _note(
            "type: daily-review\ncondition: good\n",
            "# Work\nSecret work boilerplate.\n"
            "# Note\nDaily semantic insight.\n"
            "## Observation\nA reusable observation.\n"
            "# Tasks\n- [ ] ignored task",
        ),
        encoding="utf-8",
    )
    (daily / "2026-09-27.md").write_text(
        _note("type: daily-review\n", "# Note\n-\n# Tasks\n- [ ] empty"),
        encoding="utf-8",
    )

    ideas = vault / "05-Idea"
    (ideas / "Active.md").write_text(
        _note(
            "type: idea\ntitle: Active Idea\ncreated: 2026-09-28\n"
            "workspace: '[[03-Workspace/Lab/Lab|Lab]]'\n"
            "project: \nstatus: active\ntags: [semantic, planner]\n",
            "~~~meta-bind-embed\n[[idea-meta]]\n~~~\n"
            "# Active Idea\nConnect semantic clusters to planning.",
        ),
        encoding="utf-8",
    )
    (ideas / "Adopted.md").write_text(
        _note(
            "type: idea\ntitle: Adopted Idea\ncreated: 2026-09-27\n"
            "workspace: '[[03-Workspace/Lab/Lab|Lab]]'\n"
            "project: '[[10-Project/Running/Running|Running]]'\nstatus: adopted\n",
            "# Adopted Idea\nRetain as project provenance.",
        ),
        encoding="utf-8",
    )
    (ideas / "Archived.md").write_text(
        _note("type: idea\nstatus: archived\n", "# Archived\nDo not index."),
        encoding="utf-8",
    )

    running = vault / "10-Project" / "Running"
    running.mkdir(parents=True)
    (running / "Running.md").write_text(
        _note(
            "type: project\nworkspace: '[[03-Workspace/Lab/Lab|Lab]]'\n"
            "status: running\npriority: high\n",
            "# Project Summary\nSemantic planner project.\n"
            "# Details\nProject-entry boilerplate should not be an anchor.",
        ),
        encoding="utf-8",
    )
    (running / "Design.md").write_text(
        _note(
            "type: project-note\n"
            "project: '[[10-Project/Running/Running|Running]]'\n"
            "workspace: '[[03-Workspace/Lab/Lab|Lab]]'\n"
            "category: design\nlifecycle: active\ntags: [ai]\n",
            "# Design\nHybrid retrieval design.",
        ),
        encoding="utf-8",
    )
    (running / "Archived.md").write_text(
        _note(
            "type: project-note\n"
            "project: '[[10-Project/Running/Running|Running]]'\n"
            "lifecycle: archived\n",
            "# Old\nDo not index.",
        ),
        encoding="utf-8",
    )

    cancelled = vault / "10-Project" / "Cancelled"
    cancelled.mkdir()
    (cancelled / "Cancelled.md").write_text(
        _note("type: project\nstatus: cancelled\n", "# Project Summary\nCancelled."),
        encoding="utf-8",
    )
    (cancelled / "Note.md").write_text(
        _note(
            "type: project-note\nproject: '[[Cancelled]]'\nlifecycle: active\n",
            "# Note\nMust not index.",
        ),
        encoding="utf-8",
    )

    knowledge = vault / "11-Knowledge"
    (knowledge / "Active.md").write_text(
        _note(
            "type: knowledge-note\nstatus: active\ncategory: system\n"
            "maturity: stable\nsource_type: self\ntags: [retrieval]\n",
            "# Active Knowledge\nReusable semantic knowledge.",
        ),
        encoding="utf-8",
    )
    (knowledge / "Archived.md").write_text(
        _note(
            "type: knowledge-note\nstatus: archived\ncategory: system\n",
            "# Archived Knowledge\nDo not index.",
        ),
        encoding="utf-8",
    )
    return vault


def _state(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    (state / "04-Index").mkdir(parents=True)
    return state


def test_cross_vault_manifest_filters_source_kinds_and_metadata(tmp_path: Path) -> None:
    manifest = build_semantic_corpus(_vault(tmp_path))

    observed = [(source.source_kind, source.path) for source in manifest.sources]
    assert observed == [
        ("daily", "00-DailyNote/2026/09/2026-09-28.md"),
        ("idea", "05-Idea/Active.md"),
        ("idea", "05-Idea/Adopted.md"),
        ("project-note", "10-Project/Running/Design.md"),
        ("project", "10-Project/Running/Running.md"),
        ("knowledge", "11-Knowledge/Active.md"),
    ]
    assert not manifest.warnings

    active_idea = next(source for source in manifest.sources if source.path == "05-Idea/Active.md")
    assert active_idea.metadata["status"] == "active"
    assert active_idea.metadata["workspace"] == "[[03-Workspace/Lab/Lab|Lab]]"
    assert active_idea.metadata["tags"] == "[semantic, planner]"

    project_note = next(
        source for source in manifest.sources
        if source.path == "10-Project/Running/Design.md"
    )
    assert project_note.metadata["project_status"] == "running"

    knowledge = next(
        source for source in manifest.sources
        if source.path == "11-Knowledge/Active.md"
    )
    assert knowledge.metadata["category"] == "system"
    assert knowledge.metadata["maturity"] == "stable"


def test_daily_and_project_anchor_chunking_excludes_unrelated_sections(tmp_path: Path) -> None:
    manifest = build_semantic_corpus(_vault(tmp_path))

    daily = next(source for source in manifest.sources if source.source_kind == "daily")
    assert [chunk.heading_path for chunk in daily.chunks] == [
        ("Note",),
        ("Note", "Observation"),
    ]
    assert daily.chunks[0].content_sha256 == sha256_bytes(
        b"Daily semantic insight.\n"
    )
    assert daily.chunks[1].content_sha256 == sha256_bytes(
        b"## Observation\nA reusable observation.\n"
    )

    project = next(source for source in manifest.sources if source.source_kind == "project")
    assert len(project.chunks) == 1
    assert project.chunks[0].heading_path == ("Project Summary",)
    assert project.chunks[0].content_sha256 == sha256_bytes(
        b"Semantic planner project.\n"
    )


def test_chunk_identity_is_deterministic_and_bound_to_source_sha(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    first = build_semantic_corpus(vault)
    second = build_semantic_corpus(vault)
    assert first.to_json_bytes() == second.to_json_bytes()
    assert CHUNK_POLICY_VERSION == "heading-section-lf-v0"

    source = next(item for item in first.sources if item.source_kind == "knowledge")
    original_ids = [chunk.chunk_id for chunk in source.chunks]

    path = vault / source.path
    path.write_text(path.read_text(encoding="utf-8") + "\nAdditional detail.\n", encoding="utf-8")
    changed = build_semantic_corpus(vault)
    changed_source = next(item for item in changed.sources if item.path == source.path)
    assert changed_source.content_sha256 != source.content_sha256
    assert [chunk.chunk_id for chunk in changed_source.chunks] != original_ids


def test_manifest_store_load_and_stale_verification(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    state = _state(tmp_path)
    manifest = build_semantic_corpus(vault)

    digest, path = store_semantic_corpus_manifest(state, manifest)
    assert path.name == f"{digest}.semantic-corpus.json"
    assert load_semantic_corpus_manifest(state, digest) == manifest
    assert parse_semantic_corpus_manifest(path.read_bytes()) == manifest

    verify_semantic_corpus_current(vault, manifest)
    daily = vault / "00-DailyNote" / "2026" / "09" / "2026-09-28.md"
    daily.write_text(
        daily.read_text(encoding="utf-8").replace(
            "Daily semantic insight.",
            "Changed semantic insight.",
        ),
        encoding="utf-8",
    )
    with pytest.raises(SemanticCorpusError, match="stale"):
        verify_semantic_corpus_current(vault, manifest)


def test_casefold_collision_and_symlink_fail_closed(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    knowledge = vault / "11-Knowledge"
    (knowledge / "case.md").write_text(
        _note("type: knowledge-note\nstatus: active\n", "# Case\nOne."),
        encoding="utf-8",
    )
    (knowledge / "CASE.md").write_text(
        _note("type: knowledge-note\nstatus: active\n", "# Case\nTwo."),
        encoding="utf-8",
    )
    with pytest.raises(SemanticCorpusError, match="case-fold collision"):
        build_semantic_corpus(vault)

    (knowledge / "CASE.md").unlink()
    (knowledge / "case.md").unlink()
    target = knowledge / "Target.md"
    target.write_text(
        _note("type: knowledge-note\nstatus: active\n", "# Target\nSource."),
        encoding="utf-8",
    )
    (knowledge / "Alias.md").symlink_to(target)
    with pytest.raises(SemanticCorpusError, match="symlink source"):
        build_semantic_corpus(vault)


def test_manifest_rejects_tampered_chunk_identity(tmp_path: Path) -> None:
    manifest = build_semantic_corpus(_vault(tmp_path))
    value = json.loads(manifest.to_json_bytes())
    value["sources"][0]["chunks"][0]["chunk_id"] = "f" * 64

    with pytest.raises(SemanticCorpusError, match="identity binding mismatch"):
        parse_semantic_corpus_manifest(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
