from __future__ import annotations

import pytest

from obsidian_automation.artifact_lifecycle import ArtifactLifecycleError
from obsidian_automation.openai_compatible import (
    OpenAICompatibleProviderError,
    chat_content,
    identifier_revision,
    validated_base_url,
    validated_options,
)


MODEL = "qwen3:14b"


def test_base_url_normalizes_v1_and_rejects_remote_plain_http() -> None:
    assert validated_base_url("https://llm.example.invalid") == "https://llm.example.invalid/v1"
    assert validated_base_url("https://llm.example.invalid/v1") == "https://llm.example.invalid/v1"
    assert validated_base_url("http://127.0.0.1:11434") == "http://127.0.0.1:11434/v1"

    with pytest.raises(OpenAICompatibleProviderError, match="require HTTPS"):
        validated_base_url("http://llm.example.invalid")


def test_identifier_revision_explicitly_marks_non_immutable_binding() -> None:
    assert identifier_revision(MODEL) == f"identifier:{MODEL}"


def test_options_cannot_override_protocol_fields() -> None:
    with pytest.raises(ArtifactLifecycleError, match="must not override"):
        validated_options({"model": "other"})

    with pytest.raises(ArtifactLifecycleError, match="must not override"):
        validated_options({"messages": []})


def test_chat_content_uses_bounded_openai_compatible_contract() -> None:
    observed = {}

    def transport(base_url: str, **kwargs):
        observed["base_url"] = base_url
        observed.update(kwargs)
        return {
            "model": MODEL,
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": '{"ok":true}',
                    }
                }
            ],
        }

    identity, data = chat_content(
        "https://llm.example.invalid/v1",
        model=MODEL,
        system_prompt="system",
        user_prompt="user",
        options={"temperature": 0},
        timeout=30,
        api_key="secret-value",
        transport=transport,
    )

    assert identity.identifier == MODEL
    assert identity.revision == f"identifier:{MODEL}"
    assert data == b'{"ok":true}'
    assert observed["base_url"] == "https://llm.example.invalid/v1"
    assert observed["method"] == "POST"
    assert observed["path"] == "/chat/completions"
    assert observed["api_key"] == "secret-value"
    assert observed["payload"] == {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "user"},
        ],
        "stream": False,
        "temperature": 0,
    }
    assert "secret-value" not in repr(observed["payload"])


def test_chat_content_rejects_response_model_mismatch() -> None:
    def transport(_base_url: str, **_kwargs):
        return {
            "model": "unexpected-model",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": '{"ok":true}',
                    }
                }
            ],
        }

    with pytest.raises(OpenAICompatibleProviderError, match="does not match"):
        chat_content(
            "https://llm.example.invalid/v1",
            model=MODEL,
            system_prompt="system",
            user_prompt="user",
            options={"temperature": 0},
            timeout=30,
            transport=transport,
        )


@pytest.mark.parametrize(
    "response",
    [
        {"model": MODEL, "choices": []},
        {"model": MODEL, "choices": [{"message": {"role": "assistant", "content": ""}}]},
        {"model": MODEL, "choices": [{"message": {"role": "tool", "content": "{}"}}]},
    ],
)
def test_chat_content_fails_closed_on_invalid_response(response) -> None:
    def transport(_base_url: str, **_kwargs):
        return response

    with pytest.raises(OpenAICompatibleProviderError):
        chat_content(
            "https://llm.example.invalid/v1",
            model=MODEL,
            system_prompt="system",
            user_prompt="user",
            options={"temperature": 0},
            timeout=30,
            transport=transport,
        )
