from __future__ import annotations

import json
from pathlib import Path

import pytest

from obsidian_automation.pre_review_production_canary import (
    PreReviewCanaryError,
    run_canary,
)


GEN_MODEL = "gemma3:12b"
EVAL_MODEL = "gemma3:12b-eval"
REVISION = "a" * 40
OLLAMA_DIGEST = "d" * 64


def _transport(base_url: str, **kwargs):
    assert base_url == "https://openai.example.invalid/v1"
    assert kwargs["path"] == "/chat/completions"
    assert kwargs["api_key"] is None
    payload = kwargs["payload"]
    assert payload["temperature"] == 0
    model = payload["model"]
    messages = payload["messages"]
    response_format = payload["response_format"]
    assert response_format["type"] == "json_schema"
    schema_contract = response_format["json_schema"]
    assert schema_contract["strict"] is True
    assert isinstance(schema_contract["name"], str) and schema_contract["name"]
    schema = schema_contract["schema"]
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    user_payload = json.loads(messages[1]["content"])

    if "dimension" in user_payload:
        assert payload["reasoning_effort"] == "low"
        content = {
            "assessment": "pass",
            "findings": [],
        }
    else:
        assert payload["reasoning_effort"] == "none"
        content = {
            "title": "Disposable Pre Review Canary",
            "category": "summary",
            "source_type": "self",
            "body": (
                "# Disposable Pre Review Canary\n\n"
                "This note exists only to verify the pre-review pipeline. "
                "It has no canonical write authority."
            ),
        }

    return {
        "model": model,
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        content,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                }
            }
        ],
    }


def _ollama_transport(base_url: str, **kwargs):
    assert base_url == "https://ollama.example.invalid"
    path = kwargs["path"]

    if path == "/api/tags":
        return {
            "models": [
                {
                    "name": "gemma4:12b",
                    "model": "gemma4:12b",
                    "digest": OLLAMA_DIGEST,
                }
            ]
        }

    assert path == "/api/chat"
    payload = kwargs["payload"]
    assert payload["model"] == "gemma4:12b"
    assert payload["stream"] is False
    assert payload["options"] == {"temperature": 0}

    if payload["think"] is False:
        content = {
            "title": "Disposable Native Ollama Canary",
            "category": "summary",
            "source_type": "self",
            "body": (
                "# Disposable Native Ollama Canary\n\n"
                "This note verifies native Ollama pre-review without canonical write authority."
            ),
        }
    else:
        assert payload["think"] == "low"
        messages = payload["messages"]
        user_payload = json.loads(messages[1]["content"])
        assert user_payload["dimension"] in {
            "groundedness",
            "redundancy",
            "consistency",
        }
        content = {
            "assessment": "pass" if user_payload["dimension"] != "redundancy" else "none",
            "findings": [],
        }

    return {
        "model": "gemma4:12b",
        "done": True,
        "done_reason": "stop",
        "message": {
            "role": "assistant",
            "content": json.dumps(
                content,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        },
    }


def test_disposable_canary_supports_native_ollama_role_thinking(
    tmp_path: Path,
) -> None:
    scratch = tmp_path / "pre-review-native-ollama"

    result = run_canary(
        scratch_root=scratch,
        generator_base_url="https://ollama.example.invalid/v1",
        evaluator_base_url="https://ollama.example.invalid/v1",
        generator_provider="ollama",
        generator_model="gemma4:12b",
        generator_model_revision=OLLAMA_DIGEST,
        evaluator_provider="ollama",
        evaluator_model="gemma4:12b",
        evaluator_model_revision=OLLAMA_DIGEST,
        deployed_revision=REVISION,
        transport=_ollama_transport,
    )

    assert result["status"] == "passed"
    assert result["live_pipeline"]["final_state"] == "awaiting_human_review"
    assert result["canonical_write_connected"] is False


def test_disposable_canary_covers_wave_d_acceptance_without_canonical_write(
    tmp_path: Path,
) -> None:
    scratch = tmp_path / "pre-review-canary"

    result = run_canary(
        scratch_root=scratch,
        generator_base_url="https://openai.example.invalid/v1",
        evaluator_base_url="https://openai.example.invalid/v1",
        generator_model=GEN_MODEL,
        evaluator_model=EVAL_MODEL,
        deployed_revision=REVISION,
        transport=_transport,
    )

    assert result["status"] == "passed"
    assert result["canonical_write_connected"] is False
    assert result["live_pipeline"]["duplicate_submit"] == "passed"
    assert result["live_pipeline"]["final_state"] == "awaiting_human_review"
    assert result["live_pipeline"]["post_review_writes"] == 0

    assert result["crash_resume"] == {
        "generation": 2,
        "validation": 2,
        "evaluation_context": 2,
        "evaluation": 2,
    }
    assert result["provider_failure"] == {
        "attempts": 3,
        "final_state": "retry_exhausted",
    }
    assert result["backpressure"]["awaiting_human_review"] == 8
    assert result["backpressure"]["backpressure_active"] is True
    assert result["mirror_conflict"]["serialized"] is True


def test_canary_refuses_production_state_path() -> None:
    with pytest.raises(PreReviewCanaryError, match="below /tmp or /var/tmp|overlap"):
        run_canary(
            scratch_root=Path("/var/lib/obsidian-ai/state/canary"),
            generator_base_url="https://openai.example.invalid/v1",
        evaluator_base_url="https://openai.example.invalid/v1",
            generator_model=GEN_MODEL,
            evaluator_model=EVAL_MODEL,
            deployed_revision=REVISION,
            transport=_transport,
        )


def test_canary_refuses_existing_scratch_root(tmp_path: Path) -> None:
    scratch = tmp_path / "existing"
    scratch.mkdir()

    with pytest.raises(PreReviewCanaryError, match="must not already exist"):
        run_canary(
            scratch_root=scratch,
            generator_base_url="https://openai.example.invalid/v1",
        evaluator_base_url="https://openai.example.invalid/v1",
            generator_model=GEN_MODEL,
            evaluator_model=EVAL_MODEL,
            deployed_revision=REVISION,
            transport=_transport,
        )
