from __future__ import annotations

import json
from pathlib import Path

import pytest

from obsidian_automation.artifact_lifecycle import ArtifactLifecycleError, store_untrusted_proposal
from obsidian_automation.context_bundle import build_context_bundle, store_context_bundle
from obsidian_automation.evaluation_artifact import (
    EVALUATION_CONTEXT_STAGE,
    EVALUATION_REQUEST_STAGE,
    EVALUATION_STAGE,
    build_evaluation_context,
    build_evaluation_record,
    create_evaluation_request,
    load_evaluation_context,
    load_evaluation_record,
    load_evaluation_request,
    store_evaluation_context,
    store_evaluation_record,
)
from obsidian_automation.generation_artifact import build_generation_record, store_generation_record
from obsidian_automation.knowledge_index import build_knowledge_index, store_knowledge_index
from obsidian_automation.knowledge_validator import validate_proposal


def _knowledge_note(body: str, *, category: str = "manual") -> str:
    return (
        "---\n"
        "type: knowledge-note\n"
        "status: active\n"
        f"category: {category}\n"
        "maturity: draft\n"
        "source_type: self\n"
        "---\n\n"
        f"{body}\n"
    )


def _proposal(target: str, body: str) -> bytes:
    payload = {
        "contract_version": 1,
        "operation": "create_note",
        "mutation_id": "evaluation-test",
        "target": {"path": target},
        "content": _knowledge_note(body),
    }
    return (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def _roots(tmp_path: Path) -> tuple[Path, Path]:
    vault = tmp_path / "vault"
    knowledge = vault / "11-Knowledge"
    knowledge.mkdir(parents=True)
    state = tmp_path / "state"
    state.mkdir()
    for stage in (
        "04-Index",
        "05-Context",
        EVALUATION_REQUEST_STAGE,
        EVALUATION_CONTEXT_STAGE,
        EVALUATION_STAGE,
    ):
        (state / stage).mkdir()
    return vault, state


def _accepted_fixture(tmp_path: Path) -> tuple[Path, Path, str, str]:
    vault, state = _roots(tmp_path)
    existing = vault / "11-Knowledge" / "Nextcloud+RemotelySaveでObsidianVaultを共有する方法.md"
    existing.write_text(
        _knowledge_note(
            "# NextcloudとObsidian\n\nRemotelySave と WebDAV を使って Obsidian Vault を共有する。"
        ),
        encoding="utf-8",
    )
    proposal_sha, _ = store_untrusted_proposal(
        state,
        _proposal(
            "11-Knowledge/Nextcloud_RemotelySaveでObsidianVaultを共有する方法.md",
            "# 概要\n\nNextcloud の WebDAV と RemotelySave で Obsidian Vault を共有する方法。",
        ),
    )
    validation = validate_proposal(state, vault, proposal_sha)
    assert validation["result"] == "accepted"
    mutation_sha = validation["mutation_sha256"]
    assert isinstance(mutation_sha, str)
    return vault, state, proposal_sha, mutation_sha


def test_evaluation_request_is_deterministic_and_bound_to_accepted_validation(tmp_path: Path) -> None:
    _, state, proposal_sha, mutation_sha = _accepted_fixture(tmp_path)

    first_sha, first_path, first = create_evaluation_request(state, proposal_sha)
    second_sha, second_path, second = create_evaluation_request(state, proposal_sha)

    assert (first_sha, first_path, first) == (second_sha, second_path, second)
    assert first.proposal_sha256 == proposal_sha
    assert first.mutation_sha256 == mutation_sha
    assert "Nextcloud_RemotelySave" in first.query
    assert "WebDAV" in first.query
    assert load_evaluation_request(state, first_sha) == first


def test_evaluation_request_rejects_unvalidated_proposal(tmp_path: Path) -> None:
    _, state = _roots(tmp_path)
    proposal_sha, _ = store_untrusted_proposal(
        state,
        _proposal("11-Knowledge/unvalidated.md", "# Test\n\nnot validated"),
    )
    with pytest.raises(ArtifactLifecycleError, match="cannot safely open artifact"):
        create_evaluation_request(state, proposal_sha)


def test_reader_builds_recall_biased_context_with_existing_duplicate_candidate(tmp_path: Path) -> None:
    vault, state, proposal_sha, mutation_sha = _accepted_fixture(tmp_path)
    request_sha, _, request = create_evaluation_request(state, proposal_sha)
    index = build_knowledge_index(vault)
    index_sha, _ = store_knowledge_index(state, index)

    context = build_evaluation_context(
        state,
        vault,
        request_sha256=request_sha,
        index_sha256=index_sha,
        created_at="2026-08-24T00:00:00Z",
    )
    context_sha, context_path = store_evaluation_context(state, context)

    assert context_path == state / EVALUATION_CONTEXT_STAGE / f"{context_sha}.context.json"
    assert context.proposal_sha256 == proposal_sha
    assert context.mutation_sha256 == mutation_sha
    assert context.query == request.query
    assert context.candidates
    assert context.candidates[0].path.endswith("Nextcloud+RemotelySaveでObsidianVaultを共有する方法.md")
    assert load_evaluation_context(state, context_sha) == context


def test_evaluation_record_binds_generation_validation_and_evaluation_context(tmp_path: Path) -> None:
    vault, state, proposal_sha, mutation_sha = _accepted_fixture(tmp_path)
    request_sha, _, _ = create_evaluation_request(state, proposal_sha)
    index_sha, _ = store_knowledge_index(state, build_knowledge_index(vault))
    evaluation_context = build_evaluation_context(
        state,
        vault,
        request_sha256=request_sha,
        index_sha256=index_sha,
        created_at="2026-08-24T00:00:00Z",
    )
    evaluation_context_sha, _ = store_evaluation_context(state, evaluation_context)

    original_context = build_context_bundle(
        vault,
        query="Nextcloud Obsidian Vault 共有",
        source_paths=["11-Knowledge/Nextcloud+RemotelySaveでObsidianVaultを共有する方法.md"],
        created_at="2026-08-24T00:00:00Z",
    )
    original_context_sha, _ = store_context_bundle(state, original_context)
    generation = build_generation_record(
        state,
        context_sha256=original_context_sha,
        proposal_sha256=proposal_sha,
        implementation_revision="a" * 40,
        prompt_template_version="knowledge-note-generator-v0",
        prompt_template_sha256="b" * 64,
        model_provider="ollama",
        model_identifier="gemma4:12b",
        model_revision="c" * 64,
        model_config={"temperature": 0},
        generated_at="2026-08-24T00:01:00Z",
    )
    generation_sha, _ = store_generation_record(state, generation)

    record = build_evaluation_record(
        state,
        proposal_sha256=proposal_sha,
        mutation_sha256=mutation_sha,
        generation_sha256=generation_sha,
        evaluation_context_sha256=evaluation_context_sha,
        implementation_revision="d" * 40,
        prompt_template_version="knowledge-note-evaluator-v0",
        prompt_template_sha256="e" * 64,
        model_provider="ollama",
        model_identifier="gemma4:12b",
        model_revision="c" * 64,
        model_config={"temperature": 0},
        groundedness="pass",
        redundancy="likely",
        consistency="pass",
        recommendation="do_not_proceed",
        findings=["既存Knowledge Noteと実質的に重複している。"],
        evaluated_at="2026-08-24T00:02:00Z",
    )
    evaluation_sha, path = store_evaluation_record(state, record)

    assert path == state / EVALUATION_STAGE / f"{evaluation_sha}.evaluation.json"
    loaded = load_evaluation_record(state, evaluation_sha)
    assert loaded.assessment.redundancy == "likely"
    assert loaded.assessment.recommendation == "do_not_proceed"
    assert loaded.proposal_sha256 == proposal_sha


def test_evaluation_record_cannot_cross_bind_another_mutation(tmp_path: Path) -> None:
    vault, state, proposal_sha, _ = _accepted_fixture(tmp_path)
    request_sha, _, _ = create_evaluation_request(state, proposal_sha)
    index_sha, _ = store_knowledge_index(state, build_knowledge_index(vault))
    context = build_evaluation_context(
        state,
        vault,
        request_sha256=request_sha,
        index_sha256=index_sha,
        created_at="2026-08-24T00:00:00Z",
    )
    context_sha, _ = store_evaluation_context(state, context)

    with pytest.raises(ArtifactLifecycleError, match="accepted validation"):
        build_evaluation_record(
            state,
            proposal_sha256=proposal_sha,
            mutation_sha256="f" * 64,
            generation_sha256="a" * 64,
            evaluation_context_sha256=context_sha,
            implementation_revision="d" * 40,
            prompt_template_version="knowledge-note-evaluator-v0",
            prompt_template_sha256="e" * 64,
            model_provider="ollama",
            model_identifier="gemma4:12b",
            model_revision="c" * 64,
            model_config={},
            groundedness="unknown",
            redundancy="possible",
            consistency="unknown",
            recommendation="manual_review",
            findings=[],
        )
