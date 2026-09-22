from __future__ import annotations

import pytest

import obsidian_automation.openai_compatible as openai_compatible
from obsidian_automation.artifact_lifecycle import ArtifactLifecycleError
from obsidian_automation.openai_compatible import (
    OpenAICompatibleProviderError,
    chat_content,
    identifier_revision,
    validated_base_url,
    validated_options,
)


MODEL = "qwen3:14b"
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ok"],
    "properties": {"ok": {"type": "boolean"}},
}


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

    with pytest.raises(ArtifactLifecycleError, match="must not override"):
        validated_options({"response_format": {"type": "json_object"}})


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
        output_schema=SCHEMA,
        schema_name="test_result",
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
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "test_result",
                "strict": True,
                "schema": SCHEMA,
            },
        },
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
            output_schema=SCHEMA,
            schema_name="test_result",
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
            output_schema=SCHEMA,
            schema_name="test_result",
            options={"temperature": 0},
            timeout=30,
            transport=transport,
        )


def test_request_json_enforces_total_response_read_timeout(monkeypatch) -> None:
    class Response:
        fp = None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, size: int) -> bytes:
            assert size == 1
            return b"x"

    class Opener:
        def open(self, _request, timeout: float):
            assert timeout == 1.0
            return Response()

    clock = iter((0.0, 0.5, 2.0))
    monkeypatch.setattr(openai_compatible.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(openai_compatible, "_direct_opener", lambda: Opener())

    with pytest.raises(OpenAICompatibleProviderError, match="response read exceeded"):
        openai_compatible.request_json(
            "https://llm.example.invalid/v1",
            method="POST",
            path="/chat/completions",
            payload={"ok": True},
            timeout=1.0,
        )
