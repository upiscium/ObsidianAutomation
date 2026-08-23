from __future__ import annotations

import json
from pathlib import Path

import pytest

from obsidian_automation.artifact_lifecycle import ArtifactLifecycleError
from obsidian_automation.context_bundle import ContextBundle, store_context_bundle
from obsidian_automation.generation_artifact import load_generation_record
from obsidian_automation.ollama_generator import (
    ADAPTER_VERSION,
    OllamaProviderError,
    _validated_base_url,
    generate_knowledge_note_with_ollama,
    resolve_ollama_model,
)


IMPLEMENTATION_REVISION = "a" * 40
MODEL_DIGEST = "b" * 64


def _state_with_context(tmp_path: Path) -> tuple[Path, str]:
    state = tmp_path / "state"
    (state / "00-Untrusted").mkdir(parents=True)
    (state / "05-Context").mkdir()
    bundle = ContextBundle(
        query="Nextcloud Obsidian Vault sharing",
        created_at="2026-08-23T00:00:00Z",
        sources=(),
    )
    context_sha, _ = store_context_bundle(state, bundle)
    return state, context_sha


def _successful_transport(calls: list[tuple[str, str, object]]):
    def transport(base_url, *, method, path, payload, timeout):
        calls.append((method, path, payload))
        assert base_url == "https://ollama.example.test:11434"
        assert timeout == 30.0
        if path == "/api/tags":
            assert method == "GET"
            assert payload is None
            return {
                "models": [
                    {
                        "name": "gemma3:latest",
                        "model": "gemma3:latest",
                        "digest": MODEL_DIGEST,
                    }
                ]
            }
        assert path == "/api/chat"
        assert method == "POST"
        assert payload["model"] == "gemma3:latest"
        assert payload["stream"] is False
        assert payload["think"] is False
        assert payload["options"] == {"temperature": 0}
        assert payload["format"]["additionalProperties"] is False
        assert [message["role"] for message in payload["messages"]] == ["system", "user"]
        return {
            "model": "gemma3:latest",
            "done": True,
            "message": {
                "role": "assistant",
                "content": json.dumps(
                    {
                        "title": "NextcloudとObsidianを共有する方法",
                        "category": "manual",
                        "source_type": "self",
                        "body": "# 概要\n\nContextに基づく下書きです。",
                    },
                    ensure_ascii=False,
                ),
            },
        }

    return transport


def test_remote_http_is_rejected_and_loopback_http_is_allowed() -> None:
    with pytest.raises(OllamaProviderError, match="HTTPS"):
        _validated_base_url("http://10.0.0.20:11434")

    assert _validated_base_url("http://127.0.0.1:11434") == "http://127.0.0.1:11434"
    assert _validated_base_url("http://localhost:11434/") == "http://localhost:11434"
    assert _validated_base_url("https://ollama.example.test:11434") == "https://ollama.example.test:11434"


@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret@ollama.example.test",
        "https://ollama.example.test/api",
        "https://ollama.example.test?x=1",
        "https://ollama.example.test#fragment",
    ],
)
def test_base_url_rejects_embedded_authority_or_routing_state(url: str) -> None:
    with pytest.raises(OllamaProviderError):
        _validated_base_url(url)


def test_model_resolution_accepts_implicit_latest_alias() -> None:
    def transport(base_url, *, method, path, payload, timeout):
        return {
            "models": [
                {
                    "name": "gemma3:latest",
                    "model": "gemma3:latest",
                    "digest": MODEL_DIGEST,
                }
            ]
        }

    identity = resolve_ollama_model(
        "https://ollama.example.test",
        "gemma3",
        timeout=10,
        transport=transport,
    )
    assert identity.requested_identifier == "gemma3"
    assert identity.identifier == "gemma3:latest"
    assert identity.digest == MODEL_DIGEST


def test_model_resolution_rejects_missing_model() -> None:
    def transport(base_url, *, method, path, payload, timeout):
        return {"models": []}

    with pytest.raises(OllamaProviderError, match="not installed"):
        resolve_ollama_model(
            "https://ollama.example.test",
            "missing",
            transport=transport,
        )


def test_generator_e2e_persists_proposal_and_generation_provenance(tmp_path: Path) -> None:
    state, context_sha = _state_with_context(tmp_path)
    calls: list[tuple[str, str, object]] = []

    result = generate_knowledge_note_with_ollama(
        state,
        context_sha256=context_sha,
        base_url="https://ollama.example.test:11434",
        model="gemma3",
        implementation_revision=IMPLEMENTATION_REVISION,
        timeout=30.0,
        transport=_successful_transport(calls),
    )

    assert [call[1] for call in calls] == ["/api/tags", "/api/chat"]
    assert result.context_sha256 == context_sha
    assert result.model_identifier == "gemma3:latest"
    assert result.model_revision == MODEL_DIGEST
    assert result.proposal_path.name == f"{result.proposal_sha256}.proposal.json"
    assert result.generation_path.name == f"{result.generation_sha256}.generation.json"

    proposal = json.loads(result.proposal_path.read_text(encoding="utf-8"))
    assert proposal["contract_version"] == 1
    assert proposal["operation"] == "create_note"
    assert proposal["target"]["path"] == "11-Knowledge/NextcloudとObsidianを共有する方法.md"
    assert proposal["content"].startswith(
        "---\ntype: knowledge-note\nstatus: active\ncategory: manual\nmaturity: draft\nsource_type: self\n---\n"
    )

    record = load_generation_record(state, result.generation_sha256)
    assert record.context_sha256 == context_sha
    assert record.proposal_sha256 == result.proposal_sha256
    assert record.generator.implementation_revision == IMPLEMENTATION_REVISION
    assert record.generator.prompt_template_version == result.prompt_template_version
    assert record.generator.prompt_template_sha256 == result.prompt_template_sha256
    assert record.model.provider == "ollama"
    assert record.model.identifier == "gemma3:latest"
    assert record.model.revision == MODEL_DIGEST
    assert record.model_config == {
        "adapter_version": ADAPTER_VERSION,
        "think": False,
        "options": {"temperature": 0},
    }


def test_malformed_model_output_is_rejected_before_proposal_persistence(tmp_path: Path) -> None:
    state, context_sha = _state_with_context(tmp_path)

    def transport(base_url, *, method, path, payload, timeout):
        if path == "/api/tags":
            return {
                "models": [
                    {
                        "name": "gemma3:latest",
                        "model": "gemma3:latest",
                        "digest": MODEL_DIGEST,
                    }
                ]
            }
        return {
            "model": "gemma3:latest",
            "done": True,
            "message": {"role": "assistant", "content": "not-json"},
        }

    with pytest.raises(ArtifactLifecycleError, match="generator output"):
        generate_knowledge_note_with_ollama(
            state,
            context_sha256=context_sha,
            base_url="https://ollama.example.test",
            model="gemma3",
            implementation_revision=IMPLEMENTATION_REVISION,
            transport=transport,
        )

    assert list((state / "00-Untrusted").iterdir()) == []


def test_incomplete_chat_response_is_rejected(tmp_path: Path) -> None:
    state, context_sha = _state_with_context(tmp_path)

    def transport(base_url, *, method, path, payload, timeout):
        if path == "/api/tags":
            return {
                "models": [
                    {
                        "name": "gemma3:latest",
                        "model": "gemma3:latest",
                        "digest": MODEL_DIGEST,
                    }
                ]
            }
        return {
            "model": "gemma3:latest",
            "done": False,
            "message": {"role": "assistant", "content": "{}"},
        }

    with pytest.raises(OllamaProviderError, match="not complete"):
        generate_knowledge_note_with_ollama(
            state,
            context_sha256=context_sha,
            base_url="https://ollama.example.test",
            model="gemma3",
            implementation_revision=IMPLEMENTATION_REVISION,
            transport=transport,
        )


def test_invalid_options_fail_before_provider_contact(tmp_path: Path) -> None:
    state, context_sha = _state_with_context(tmp_path)
    called = False

    def transport(base_url, *, method, path, payload, timeout):
        nonlocal called
        called = True
        raise AssertionError("provider must not be contacted")

    with pytest.raises(ArtifactLifecycleError, match="strict JSON"):
        generate_knowledge_note_with_ollama(
            state,
            context_sha256=context_sha,
            base_url="https://ollama.example.test",
            model="gemma3",
            implementation_revision=IMPLEMENTATION_REVISION,
            options={"temperature": float("nan")},
            transport=transport,
        )
    assert called is False


def test_implementation_revision_must_be_exact_commit_digest(tmp_path: Path) -> None:
    state, context_sha = _state_with_context(tmp_path)
    with pytest.raises(ArtifactLifecycleError, match="implementation revision"):
        generate_knowledge_note_with_ollama(
            state,
            context_sha256=context_sha,
            base_url="https://ollama.example.test",
            model="gemma3",
            implementation_revision="main",
            transport=lambda *args, **kwargs: {},
        )
