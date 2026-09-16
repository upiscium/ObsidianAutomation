from __future__ import annotations

import json
from dataclasses import replace

import pytest

from obsidian_automation import generator_contract
from obsidian_automation.artifact_lifecycle import ArtifactLifecycleError, sha256_bytes
from obsidian_automation.canonical_mutation import CreateNoteMutation
from obsidian_automation.generator_contract import (
    KnowledgeGeneratorOutput,
    assemble_knowledge_note_proposal,
)
from obsidian_automation.knowledge_note_layout import (
    KNOWLEDGE_METADATA_EMBED,
    NOTE_LAYOUT_VERSION,
)
from obsidian_automation.knowledge_note_policy import validate_knowledge_note_v0


OUTPUT = KnowledgeGeneratorOutput(
    title="Metadata UI fixture", category="summary", source_type="self",
    body="# Summary\n\nHuman-readable content.\n",
)


def test_proposal_binds_ui_before_validation_and_review() -> None:
    raw = assemble_knowledge_note_proposal(context_sha256="a" * 64, output=OUTPUT)
    value = json.loads(raw)
    expected = (
        "---\ntype: knowledge-note\nstatus: active\ncategory: summary\n"
        "maturity: draft\nsource_type: self\n---\n\n"
        + KNOWLEDGE_METADATA_EMBED + "\n" + OUTPUT.body
    )
    assert value["content"] == expected
    assert value["content"].count(KNOWLEDGE_METADATA_EMBED) == 1
    assert assemble_knowledge_note_proposal(context_sha256="a" * 64, output=OUTPUT) == raw


def test_layout_is_domain_separated_from_historical_mutation_identity() -> None:
    context_sha = "a" * 64
    value = json.loads(assemble_knowledge_note_proposal(context_sha256=context_sha, output=OUTPUT))
    old_digest = sha256_bytes(context_sha.encode("ascii") + b"\0" + OUTPUT.to_json_bytes())
    new_digest = sha256_bytes(
        NOTE_LAYOUT_VERSION.encode("ascii") + b"\0"
        + context_sha.encode("ascii") + b"\0" + OUTPUT.to_json_bytes()
    )
    assert value["mutation_id"] == f"knowledge-gen-v0-{new_digest}"
    assert value["mutation_id"] != f"knowledge-gen-v0-{old_digest}"


def test_total_size_check_includes_metadata_ui(monkeypatch: pytest.MonkeyPatch) -> None:
    value = json.loads(assemble_knowledge_note_proposal(context_sha256="a" * 64, output=OUTPUT))
    size = len(value["content"].encode("utf-8"))
    monkeypatch.setattr(generator_contract, "MAX_CONTENT_BYTES", size)
    assemble_knowledge_note_proposal(context_sha256="a" * 64, output=OUTPUT)
    monkeypatch.setattr(generator_contract, "MAX_CONTENT_BYTES", size - 1)
    with pytest.raises(ArtifactLifecycleError, match="byte limit"):
        assemble_knowledge_note_proposal(context_sha256="a" * 64, output=OUTPUT)


def test_ui_only_output_does_not_become_a_proposal() -> None:
    with pytest.raises(ArtifactLifecycleError, match="beyond metadata UI"):
        assemble_knowledge_note_proposal(
            context_sha256="a" * 64,
            output=replace(OUTPUT, body=KNOWLEDGE_METADATA_EMBED),
        )


def test_existing_v0_proposals_without_ui_remain_valid() -> None:
    # This is an isolated fixture, not a rewrite of any stored artifact.
    content = (
        "---\ntype: knowledge-note\nstatus: active\ncategory: summary\n"
        "maturity: draft\nsource_type: self\n---\n\n" + OUTPUT.body
    )
    old_digest = sha256_bytes(b"a" * 64 + b"\0" + OUTPUT.to_json_bytes())
    mutation = CreateNoteMutation(
        contract_version=1, operation="create_note",
        mutation_id=f"knowledge-gen-v0-{old_digest}",
        target_path="11-Knowledge/Metadata UI fixture.md", content=content,
    )
    validate_knowledge_note_v0(mutation)
