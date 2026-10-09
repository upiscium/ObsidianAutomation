from __future__ import annotations

import json

import pytest

from obsidian_automation.github_daily_summary import (
    GROUND_STAGE,
    PARTIAL_STAGE,
    GitHubDailySummaryError,
    SummaryClaim,
    SummaryContext,
    prompt_spec,
)
from obsidian_automation.github_daily_summary_generation import (
    _context_user_prompt,
    ollama_infer,
    openai_compatible_infer,
)


MODEL_DIGEST = "b" * 64


def _context() -> SummaryContext:
    return SummaryContext(
        stage=PARTIAL_STAGE,
        evidence_bundle_sha256="a" * 64,
        batch_index=0,
        batch_count=1,
        source_output_sha256s=(),
        events=(
            {
                "evidence_id": "c" * 64,
                "kind": "default_branch_commit",
                "repository": "upiscium/Test",
                "occurred_at": "2026-10-05T01:00:00Z",
                "url": "https://github.com/upiscium/Test/commit/" + "d" * 40,
                "actor": "upiscium",
                "entity_type": "commit",
                "number": None,
                "source_id": "d" * 40,
                "sha": "d" * 40,
                "state": None,
                "draft": None,
                "title": None,
                "body": None,
                "message": {
                    "text": "feat: implement",
                    "truncated": False,
                    "original_bytes": 15,
                    "included_bytes": 15,
                },
            },
        ),
    )


def test_ollama_adapter_uses_structured_non_thinking_chat() -> None:
    calls = []

    def transport(base_url, *, method, path, payload, timeout):
        calls.append((method, path, payload))
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
        assert path == "/api/chat"
        assert payload["stream"] is False
        assert payload["think"] is False
        assert payload["format"]["additionalProperties"] is False
        assert payload["messages"][0]["role"] == "system"
        assert payload["messages"][1]["role"] == "user"
        user = json.loads(payload["messages"][1]["content"])
        assert user["events"][0]["source_ref"] == 0
        assert user["valid_source_ref_range"] == {"minimum": 0, "maximum": 0}
        ref_schema = payload["format"]["properties"]["claims"]["items"][
            "properties"
        ]["source_refs"]
        assert ref_schema["items"] == {
            "type": "integer",
            "minimum": 0,
            "maximum": 0,
            "enum": [0],
        }
        assert ref_schema["maxItems"] == 1
        return {
            "model": "gemma3:latest",
            "done": True,
            "message": {
                "role": "assistant",
                "content": json.dumps({"claims": []}),
            },
        }

    infer = ollama_infer(
        base_url="https://ollama.example.test",
        model="gemma3",
        timeout=30.0,
        transport=transport,
    )
    response = infer(prompt_spec(PARTIAL_STAGE, source_count=1), _context())

    assert [call[1] for call in calls] == ["/api/tags", "/api/chat"]
    assert response.content == b'{"claims": []}'
    assert response.model_provider == "ollama"
    assert response.model_identifier == "gemma3:latest"
    assert response.model_revision == MODEL_DIGEST
    assert response.model_config["think"] is False


def test_openai_adapter_uses_strict_json_schema() -> None:
    calls = []

    def transport(
        base_url,
        *,
        method,
        path,
        payload,
        timeout,
        api_key=None,
    ):
        calls.append((base_url, method, path, payload, api_key))
        assert path == "/chat/completions"
        assert payload["stream"] is False
        response_format = payload["response_format"]
        assert response_format["type"] == "json_schema"
        assert response_format["json_schema"]["strict"] is True
        assert response_format["json_schema"]["schema"][
            "additionalProperties"
        ] is False
        user = json.loads(payload["messages"][1]["content"])
        assert user["events"][0]["source_ref"] == 0
        assert user["valid_source_ref_range"] == {"minimum": 0, "maximum": 0}
        refs = response_format["json_schema"]["schema"]["properties"][
            "claims"
        ]["items"]["properties"]["source_refs"]
        assert refs["items"]["enum"] == [0]
        assert refs["items"]["maximum"] == 0
        return {
            "model": "test-model",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": json.dumps({"claims": []}),
                    }
                }
            ],
        }

    infer = openai_compatible_infer(
        base_url="https://provider.example.test/v1",
        model="test-model",
        api_key="secret",
        timeout=30.0,
        transport=transport,
    )
    response = infer(prompt_spec(PARTIAL_STAGE, source_count=1), _context())

    assert len(calls) == 1
    assert response.content == b'{"claims": []}'
    assert response.model_provider == "openai-compatible"
    assert response.model_identifier == "test-model"
    assert response.model_revision == "identifier:test-model"



def test_grounding_model_input_contains_semantics_not_identity_hashes() -> None:
    evidence = _context().events[0]
    claim = SummaryClaim(
        claim_id="e" * 64,
        kind="implementation",
        repository="upiscium/Test",
        summary="Implemented the reported feature",
        evidence_ids=("c" * 64,),
    )
    context = SummaryContext(
        stage=GROUND_STAGE,
        evidence_bundle_sha256="a" * 64,
        batch_index=0,
        batch_count=1,
        source_output_sha256s=("b" * 64,),
        events=(evidence,),
        claims=(claim,),
    )
    original = json.loads(context.to_json_bytes())
    assert original["claims"][0]["claim_id"] == "e" * 64
    assert original["events"][0]["evidence_id"] == "c" * 64

    model_prompt = json.loads(_context_user_prompt(context))
    assert set(model_prompt) == {"claim", "cited_events"}
    assert model_prompt["claim"] == {
        "kind": "implementation",
        "repository": "upiscium/Test",
        "summary": "Implemented the reported feature",
    }
    assert len(model_prompt["cited_events"]) == 1
    assert model_prompt["cited_events"][0]["repository"] == "upiscium/Test"
    assert "claim_id" not in json.dumps(model_prompt)
    assert "claim_ref" not in json.dumps(model_prompt)
    assert "evidence_id" not in json.dumps(model_prompt)
    assert model_prompt["cited_events"][0]["source_id"] == "d" * 40
    schema = prompt_spec(GROUND_STAGE).output_schema
    assert set(schema["properties"]) == {"verdict", "reason"}


def test_grounding_model_input_refuses_multiple_claims() -> None:
    claim = SummaryClaim(
        claim_id="e" * 64,
        kind="implementation",
        repository="upiscium/Test",
        summary="One claim",
        evidence_ids=("c" * 64,),
    )
    mixed = SummaryContext(
        stage=GROUND_STAGE,
        evidence_bundle_sha256="a" * 64,
        batch_index=0,
        batch_count=1,
        source_output_sha256s=("b" * 64,),
        events=_context().events,
        claims=(claim, claim),
    )
    with pytest.raises(
        GitHubDailySummaryError, match="exactly one input claim"
    ):
        _context_user_prompt(mixed)



def _singleton_grounding_context() -> SummaryContext:
    claim = SummaryClaim(
        claim_id="e" * 64,
        kind="implementation",
        repository="upiscium/Test",
        summary="Implemented the reported feature",
        evidence_ids=("c" * 64,),
    )
    return SummaryContext(
        stage=GROUND_STAGE,
        evidence_bundle_sha256="a" * 64,
        batch_index=0,
        batch_count=1,
        source_output_sha256s=("b" * 64,),
        events=_context().events,
        claims=(claim,),
    )


def test_ollama_singleton_grounding_contract_in_actual_adapter() -> None:
    calls = []

    def transport(base_url, *, method, path, payload, timeout):
        calls.append(path)
        if path == "/api/tags":
            return {
                "models": [
                    {
                        "name": "gemma3:latest",
                        "model": "gemma3:latest",
                        "digest": MODEL_DIGEST,
                    },
                ],
            }
        assert path == "/api/chat"
        assert payload["format"]["required"] == ["verdict", "reason"]
        assert set(payload["format"]["properties"]) == {"verdict", "reason"}
        assert payload["format"]["additionalProperties"] is False
        assert payload["think"] is False
        user = json.loads(payload["messages"][1]["content"])
        assert set(user) == {"claim", "cited_events"}
        assert user["claim"]["summary"] == "Implemented the reported feature"
        return {
            "model": "gemma3:latest",
            "done": True,
            "message": {
                "role": "assistant",
                "content": '{"verdict":"supported","reason":"Evidence supports it"}',
            },
        }

    infer = ollama_infer(
        base_url="https://ollama.example.test",
        model="gemma3",
        timeout=30.0,
        transport=transport,
    )
    response = infer(
        prompt_spec(GROUND_STAGE), _singleton_grounding_context(),
    )
    assert calls == ["/api/tags", "/api/chat"]
    assert json.loads(response.content) == {
        "verdict": "supported",
        "reason": "Evidence supports it",
    }


def test_openai_singleton_grounding_contract_in_actual_adapter() -> None:
    def transport(
        base_url, *, method, path, payload, timeout, api_key=None,
    ):
        assert method == "POST"
        assert path == "/chat/completions"
        assert payload["response_format"]["type"] == "json_schema"
        assert payload["response_format"]["json_schema"]["strict"] is True
        schema = payload["response_format"]["json_schema"]["schema"]
        assert schema["required"] == ["verdict", "reason"]
        assert set(schema["properties"]) == {"verdict", "reason"}
        assert schema["additionalProperties"] is False
        assert set(json.loads(payload["messages"][1]["content"])) == {
            "claim", "cited_events",
        }
        return {
            "model": "test-model",
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": '{"verdict":"unsupported","reason":"Not proven"}',
                },
            }],
        }

    infer = openai_compatible_infer(
        base_url="https://provider.example.test/v1",
        model="test-model",
        api_key="secret",
        timeout=30.0,
        transport=transport,
    )
    response = infer(
        prompt_spec(GROUND_STAGE), _singleton_grounding_context(),
    )
    assert json.loads(response.content) == {
        "verdict": "unsupported",
        "reason": "Not proven",
    }
