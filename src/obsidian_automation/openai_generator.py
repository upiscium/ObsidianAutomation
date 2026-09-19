from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .artifact_lifecycle import ArtifactLifecycleError, _require_sha256
from .generation_artifact import (
    build_generation_record,
    store_generation_record,
    validate_model_config,
)
from .generator_contract import (
    MAX_GENERATOR_OUTPUT_BYTES,
    load_and_render_generator_prompt,
    parse_generator_output,
    store_generator_proposal,
)
from .openai_compatible import (
    DEFAULT_TIMEOUT_SECONDS,
    IDENTITY_BINDING,
    JSONTransport,
    OpenAICompatibleProviderError,
    PROVIDER_NAME,
    chat_content,
    identifier_revision,
    validated_base_url,
    validated_implementation_revision,
    validated_options,
    validated_timeout,
)


ADAPTER_VERSION = "openai-chat-completions-json-schema-v1"


@dataclass(frozen=True)
class OpenAICompatibleGenerationResult:
    context_sha256: str
    proposal_sha256: str
    proposal_path: Path
    generation_sha256: str
    generation_path: Path
    model_identifier: str
    model_revision: str
    prompt_template_version: str
    prompt_template_sha256: str


def provider_model_config(options: Mapping[str, object]) -> dict[str, object]:
    return validate_model_config(
        {
            "adapter_version": ADAPTER_VERSION,
            "identity_binding": IDENTITY_BINDING,
            "options": dict(options),
        }
    )


def generate_knowledge_note_with_openai_compatible(
    ai_root: Path,
    *,
    context_sha256: str,
    base_url: str,
    model: str,
    implementation_revision: str,
    options: Mapping[str, object] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    api_key: str | None = None,
    transport: JSONTransport | None = None,
) -> OpenAICompatibleGenerationResult:
    context_digest = _require_sha256(context_sha256, label="context_sha256")
    revision = validated_implementation_revision(implementation_revision)
    timeout_value = validated_timeout(timeout)
    root = validated_base_url(base_url)
    inference_options = validated_options(options)

    prompt = load_and_render_generator_prompt(ai_root, context_digest)
    identity, data = chat_content(
        root,
        model=model,
        system_prompt=prompt.system,
        user_prompt=prompt.user,
        output_schema=prompt.output_schema,
        schema_name="knowledge_note_generator",
        options=inference_options,
        timeout=timeout_value,
        api_key=api_key,
        transport=transport,
    )
    if len(data) > MAX_GENERATOR_OUTPUT_BYTES:
        raise OpenAICompatibleProviderError(
            "OpenAI-compatible semantic output exceeds generator output limit"
        )
    try:
        output = parse_generator_output(data)
    except ArtifactLifecycleError as exc:
        raise OpenAICompatibleProviderError(str(exc)) from exc

    proposal_sha, proposal_path = store_generator_proposal(
        ai_root,
        context_sha256=context_digest,
        output=output,
    )
    model_config = provider_model_config(inference_options)
    model_revision = identifier_revision(identity.identifier)
    record = build_generation_record(
        ai_root,
        context_sha256=context_digest,
        proposal_sha256=proposal_sha,
        implementation_revision=revision,
        prompt_template_version=prompt.template_version,
        prompt_template_sha256=prompt.template_sha256,
        model_provider=PROVIDER_NAME,
        model_identifier=identity.identifier,
        model_revision=model_revision,
        model_config=model_config,
    )
    generation_sha, generation_path = store_generation_record(ai_root, record)
    return OpenAICompatibleGenerationResult(
        context_sha256=context_digest,
        proposal_sha256=proposal_sha,
        proposal_path=proposal_path,
        generation_sha256=generation_sha,
        generation_path=generation_path,
        model_identifier=identity.identifier,
        model_revision=model_revision,
        prompt_template_version=prompt.template_version,
        prompt_template_sha256=prompt.template_sha256,
    )
