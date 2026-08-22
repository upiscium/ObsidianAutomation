from __future__ import annotations

import json
from pathlib import Path

import pytest

from obsidian_automation.artifact_lifecycle import (
    ArtifactLifecycleError,
    sha256_bytes,
    store_untrusted_proposal,
)
from obsidian_automation.context_bundle import (
    CONTEXT_STAGE,
    build_context_bundle,
    store_context_bundle,
)
from obsidian_automation.generation_artifact import (
    UNTRUSTED_STAGE,
    build_generation_record,
    load_generation_record,
    parse_generation_record,
    store_generation_record,
)


def _state_with_inputs(tmp_path: Path) -> tuple[Path, str, str]:
    state = tmp_path / "state"
    state.mkdir()
    (state / CONTEXT_STAGE).mkdir()
    (state / UNTRUSTED_STAGE).mkdir()

    vault = tmp_path / "vault"
    (vault / "11-Knowledge").mkdir(parents=True)
    (vault / "11-Knowledge" / "Existing.md").write_text(
        "# Existing\n",
        encoding="utf-8",
    )
    context = build_context_bundle(
        vault,
        query="create an example note",
        source_paths=["11-Knowledge/Existing.md"],
        created_at="2026-08-22T00:00:00Z",
    )
    context_sha, _ = store_context_bundle(state, context)

    proposal = (
        b'{"contract_version":1,"operation":"create_note",'
        b'"mutation_id":"generated-1",'
        b'"target":{"path":"11-Knowledge/Generated.md"},'
        b'"content":"# Generated\\n"}\n'
    )
    proposal_sha, _ = store_untrusted_proposal(state, proposal)
    return state, context_sha, proposal_sha


def _record(state: Path, context_sha: str, proposal_sha: str):
    return build_generation_record(
        state,
        context_sha256=context_sha,
        proposal_sha256=proposal_sha,
        implementation_revision="77518e9cd115ed60c8cedf0a4de2f344fd48a920",
        prompt_template_version="knowledge-note-generator-v0",
        prompt_template_sha256="a" * 64,
        model_provider="ollama",
        model_identifier="example:latest",
        model_revision="sha256:model-digest",
        model_config={
            "temperature": 0.1,
            "seed": 42,
            "stop": ["<END>"],
        },
        generated_at="2026-08-22T00:01:00Z",
    )


def test_generation_record_binds_exact_context_and_proposal(tmp_path: Path) -> None:
    state, context_sha, proposal_sha = _state_with_inputs(tmp_path)
    record = _record(state, context_sha, proposal_sha)
    digest, path = store_generation_record(state, record)

    assert path == state / UNTRUSTED_STAGE / f"{digest}.generation.json"
    assert sha256_bytes(path.read_bytes()) == digest

    loaded = load_generation_record(state, digest)
    assert loaded == record
    assert loaded.context_sha256 == context_sha
    assert loaded.proposal_sha256 == proposal_sha
    assert loaded.generator.prompt_template_version == "knowledge-note-generator-v0"
    assert loaded.model.provider == "ollama"
    assert loaded.model_config["seed"] == 42

    second_digest, second_path = store_generation_record(state, record)
    assert (second_digest, second_path) == (digest, path)


def test_generation_record_requires_existing_exact_context(tmp_path: Path) -> None:
    state, _, proposal_sha = _state_with_inputs(tmp_path)

    with pytest.raises(ArtifactLifecycleError, match="cannot safely open artifact"):
        build_generation_record(
            state,
            context_sha256="b" * 64,
            proposal_sha256=proposal_sha,
            implementation_revision="rev",
            prompt_template_version="v0",
            prompt_template_sha256="a" * 64,
            model_provider="ollama",
            model_identifier="model",
            model_revision="revision",
            model_config={},
        )


def test_generation_record_requires_existing_exact_proposal(tmp_path: Path) -> None:
    state, context_sha, _ = _state_with_inputs(tmp_path)

    with pytest.raises(ArtifactLifecycleError, match="cannot safely open artifact"):
        build_generation_record(
            state,
            context_sha256=context_sha,
            proposal_sha256="c" * 64,
            implementation_revision="rev",
            prompt_template_version="v0",
            prompt_template_sha256="a" * 64,
            model_provider="ollama",
            model_identifier="model",
            model_revision="revision",
            model_config={},
        )


def test_generation_record_parser_rejects_unknown_and_duplicate_properties(
    tmp_path: Path,
) -> None:
    state, context_sha, proposal_sha = _state_with_inputs(tmp_path)
    record = _record(state, context_sha, proposal_sha)
    value = json.loads(record.to_json_bytes())
    value["extra"] = True

    with pytest.raises(ArtifactLifecycleError, match="properties"):
        parse_generation_record((json.dumps(value) + "\n").encode())

    duplicate = record.to_json_bytes().decode().replace(
        '"record_version":1}',
        '"record_version":1,"record_version":1}',
        1,
    )
    with pytest.raises(ArtifactLifecycleError, match="duplicate"):
        parse_generation_record(duplicate.encode())


def test_generation_record_rejects_unsafe_model_config(tmp_path: Path) -> None:
    state, context_sha, proposal_sha = _state_with_inputs(tmp_path)

    with pytest.raises(ArtifactLifecycleError, match="finite"):
        build_generation_record(
            state,
            context_sha256=context_sha,
            proposal_sha256=proposal_sha,
            implementation_revision="rev",
            prompt_template_version="v0",
            prompt_template_sha256="a" * 64,
            model_provider="ollama",
            model_identifier="model",
            model_revision="revision",
            model_config={"temperature": float("nan")},
        )


def test_generation_record_load_detects_artifact_hash_mismatch(tmp_path: Path) -> None:
    state, context_sha, proposal_sha = _state_with_inputs(tmp_path)
    record = _record(state, context_sha, proposal_sha)
    digest, path = store_generation_record(state, record)
    path.write_bytes(path.read_bytes() + b" ")

    with pytest.raises(ArtifactLifecycleError, match="artifact hash mismatch"):
        load_generation_record(state, digest)
