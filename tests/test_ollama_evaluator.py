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
    create_evaluation_request,
    load_evaluation_record,
    store_evaluation_context,
)
from obsidian_automation.generation_artifact import build_generation_record, store_generation_record
from obsidian_automation.knowledge_index import build_knowledge_index, store_knowledge_index
from obsidian_automation.knowledge_validator import validate_proposal
from obsidian_automation.ollama_evaluator import (
    ADAPTER_VERSION,
    OllamaProviderError,
    evaluate_knowledge_note_with_ollama,
)


DIGEST = "4eb23ef187e2c5462566d6a1d3bbbc2f1346d0b4327cbb66d58fffbcc9b2b05c"
REVISION = "d" * 40


def _note(body: str) -> str:
    return (
        "---\n"
        "type: knowledge-note\n"
        "status: active\n"
        "category: manual\n"
        "maturity: draft\n"
        "source_type: self\n"
        "---\n\n"
        f"{body}\n"
    )


def _proposal(target: str, body: str, mutation_id: str) -> bytes:
    return (
        json.dumps(
            {
                "contract_version": 1,
                "operation": "create_note",
                "mutation_id": mutation_id,
                "target": {"path": target},
                "content": _note(body),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


def _roots(tmp_path: Path) -> tuple[Path, Path]:
    vault = tmp_path / "vault"
    (vault / "11-Knowledge").mkdir(parents=True)
    state = tmp_path / "state"
    state.mkdir()
    for stage in (
        "00-Untrusted",
        "04-Index",
        "05-Context",
        "10-Validation",
        EVALUATION_REQUEST_STAGE,
        EVALUATION_CONTEXT_STAGE,
        EVALUATION_STAGE,
    ):
        (state / stage).mkdir()
    return vault, state


def _fixture(tmp_path: Path) -> tuple[Path, Path, str, str, str]:
    vault, state = _roots(tmp_path)
    existing_path = "11-Knowledge/Nextcloud+RemotelySaveでObsidianVaultを共有する方法.md"
    (vault / existing_path).write_text(
        _note("# 概要\n\nNextcloud の WebDAV と RemotelySave で Obsidian Vault を共有する方法。"),
        encoding="utf-8",
    )

    proposal_sha, _ = store_untrusted_proposal(
        state,
        _proposal(
            "11-Knowledge/Nextcloud_RemotelySaveでObsidianVaultを共有する方法.md",
            "# 概要\n\nNextcloud の WebDAV と RemotelySave で Obsidian Vault を共有する方法。",
            "ollama-evaluator-test",
        ),
    )
    validation = validate_proposal(state, vault, proposal_sha)
    assert validation["result"] == "accepted"

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

    generation_context = build_context_bundle(
        vault,
        query="Nextcloud Obsidian Vault 共有",
        source_paths=[existing_path],
        created_at="2026-08-24T00:00:00Z",
    )
    generation_context_sha, _ = store_context_bundle(state, generation_context)
    generation = build_generation_record(
        state,
        context_sha256=generation_context_sha,
        proposal_sha256=proposal_sha,
        implementation_revision="a" * 40,
        prompt_template_version="knowledge-note-generator-v0",
        prompt_template_sha256="b" * 64,
        model_provider="ollama",
        model_identifier="gemma4:12b",
        model_revision=DIGEST,
        model_config={"temperature": 0},
        generated_at="2026-08-24T00:01:00Z",
    )
    generation_sha, _ = store_generation_record(state, generation)
    return vault, state, proposal_sha, generation_sha, evaluation_context_sha


def _transport_with_output(output: dict[str, object], calls: list[dict[str, object]]):
    def transport(base_url: str, **kwargs: object) -> dict[str, object]:
        calls.append({"base_url": base_url, **kwargs})
        if kwargs["path"] == "/api/tags":
            return {
                "models": [
                    {"name": "gemma4:12b", "model": "gemma4:12b", "digest": DIGEST}
                ]
            }
        assert kwargs["path"] == "/api/chat"
        return {
            "model": "gemma4:12b",
            "done": True,
            "message": {
                "role": "assistant",
                "content": json.dumps(output, ensure_ascii=False, separators=(",", ":")),
            },
        }

    return transport


def test_near_duplicate_e2e_persists_likely_and_deterministic_do_not_proceed(tmp_path: Path) -> None:
    _, state, proposal_sha, generation_sha, evaluation_context_sha = _fixture(tmp_path)
    calls: list[dict[str, object]] = []
    transport = _transport_with_output(
        {
            "groundedness": "pass",
            "redundancy": "likely",
            "consistency": "pass",
            "findings": [
                "redundancy: 11-Knowledge/Nextcloud+RemotelySaveでObsidianVaultを共有する方法.md と核心手順が実質的に同一。"
            ],
        },
        calls,
    )

    result = evaluate_knowledge_note_with_ollama(
        state,
        proposal_sha256=proposal_sha,
        generation_sha256=generation_sha,
        evaluation_context_sha256=evaluation_context_sha,
        base_url="https://ollama.arc.upiscium.dev",
        model="gemma4:12b",
        implementation_revision=REVISION,
        transport=transport,
    )

    assert result.redundancy == "likely"
    assert result.recommendation == "do_not_proceed"
    assert result.model_revision == DIGEST
    assert result.evaluation_path.is_file()
    record = load_evaluation_record(state, result.evaluation_sha256)
    assert record.assessment.redundancy == "likely"
    assert record.assessment.recommendation == "do_not_proceed"
    assert record.model_config == {
        "adapter_version": ADAPTER_VERSION,
        "think": False,
        "options": {"temperature": 0},
    }

    chat = calls[1]
    payload = chat["payload"]
    assert isinstance(payload, dict)
    assert payload["stream"] is False
    assert payload["think"] is False
    assert payload["options"] == {"temperature": 0}
    assert isinstance(payload["format"], dict)
    user_payload = json.loads(payload["messages"][1]["content"])
    assert "score" not in json.dumps(user_payload, ensure_ascii=False)


def test_malformed_model_output_is_rejected_before_evaluation_persistence(tmp_path: Path) -> None:
    _, state, proposal_sha, generation_sha, evaluation_context_sha = _fixture(tmp_path)
    transport = _transport_with_output(
        {
            "groundedness": "pass",
            "redundancy": "likely",
            "consistency": "pass",
            "findings": [],
            "recommendation": "proceed",
        },
        [],
    )

    with pytest.raises(OllamaProviderError, match="properties do not match"):
        evaluate_knowledge_note_with_ollama(
            state,
            proposal_sha256=proposal_sha,
            generation_sha256=generation_sha,
            evaluation_context_sha256=evaluation_context_sha,
            base_url="https://ollama.arc.upiscium.dev",
            model="gemma4:12b",
            implementation_revision=REVISION,
            transport=transport,
        )
    assert list((state / EVALUATION_STAGE).iterdir()) == []


def test_cross_bound_generation_is_rejected_before_provider_contact(tmp_path: Path) -> None:
    vault, state, proposal_sha, _, evaluation_context_sha = _fixture(tmp_path)
    other_sha, _ = store_untrusted_proposal(
        state,
        _proposal("11-Knowledge/other.md", "# Other\n\nDifferent note.", "other"),
    )
    assert validate_proposal(state, vault, other_sha)["result"] == "accepted"
    context = build_context_bundle(
        vault,
        query="other",
        source_paths=[],
        created_at="2026-08-24T00:02:00Z",
    )
    context_sha, _ = store_context_bundle(state, context)
    generation = build_generation_record(
        state,
        context_sha256=context_sha,
        proposal_sha256=other_sha,
        implementation_revision="a" * 40,
        prompt_template_version="knowledge-note-generator-v0",
        prompt_template_sha256="b" * 64,
        model_provider="ollama",
        model_identifier="gemma4:12b",
        model_revision=DIGEST,
        model_config={},
        generated_at="2026-08-24T00:03:00Z",
    )
    other_generation_sha, _ = store_generation_record(state, generation)
    calls: list[dict[str, object]] = []

    with pytest.raises(ArtifactLifecycleError, match="another proposal"):
        evaluate_knowledge_note_with_ollama(
            state,
            proposal_sha256=proposal_sha,
            generation_sha256=other_generation_sha,
            evaluation_context_sha256=evaluation_context_sha,
            base_url="https://ollama.arc.upiscium.dev",
            model="gemma4:12b",
            implementation_revision=REVISION,
            transport=lambda *args, **kwargs: calls.append(kwargs) or {},
        )
    assert calls == []


def test_remote_plain_http_is_rejected_before_provider_contact(tmp_path: Path) -> None:
    _, state, proposal_sha, generation_sha, evaluation_context_sha = _fixture(tmp_path)
    calls: list[dict[str, object]] = []
    with pytest.raises(OllamaProviderError, match="require HTTPS"):
        evaluate_knowledge_note_with_ollama(
            state,
            proposal_sha256=proposal_sha,
            generation_sha256=generation_sha,
            evaluation_context_sha256=evaluation_context_sha,
            base_url="http://10.12.0.2:11434",
            model="gemma4:12b",
            implementation_revision=REVISION,
            transport=lambda *args, **kwargs: calls.append(kwargs) or {},
        )
    assert calls == []


def test_nonfinite_options_are_rejected_before_provider_contact(tmp_path: Path) -> None:
    _, state, proposal_sha, generation_sha, evaluation_context_sha = _fixture(tmp_path)
    calls: list[dict[str, object]] = []
    with pytest.raises(ArtifactLifecycleError, match="strict JSON"):
        evaluate_knowledge_note_with_ollama(
            state,
            proposal_sha256=proposal_sha,
            generation_sha256=generation_sha,
            evaluation_context_sha256=evaluation_context_sha,
            base_url="https://ollama.arc.upiscium.dev",
            model="gemma4:12b",
            implementation_revision=REVISION,
            options={"temperature": float("nan")},
            transport=lambda *args, **kwargs: calls.append(kwargs) or {},
        )
    assert calls == []
