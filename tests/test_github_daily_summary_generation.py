from __future__ import annotations

import json

from obsidian_automation.github_daily_summary import (
    PARTIAL_STAGE,
    SummaryContext,
    prompt_spec,
)
from obsidian_automation.github_daily_summary_generation import (
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
    response = infer(prompt_spec(PARTIAL_STAGE), _context())

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
    response = infer(prompt_spec(PARTIAL_STAGE), _context())

    assert len(calls) == 1
    assert response.content == b'{"claims": []}'
    assert response.model_provider == "openai-compatible"
    assert response.model_identifier == "test-model"
    assert response.model_revision == "identifier:test-model"
