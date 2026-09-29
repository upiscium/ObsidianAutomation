from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .artifact_lifecycle import (
    ArtifactLifecycleError,
    _canonical_json_bytes,
    _decode_json_object,
)
from .openai_compatible import (
    DEFAULT_TIMEOUT_SECONDS as OPENAI_DEFAULT_TIMEOUT_SECONDS,
    JSONTransport as OpenAIJSONTransport,
    OpenAICompatibleProviderError,
    PROVIDER_NAME as OPENAI_PROVIDER_NAME,
    chat_content,
    identifier_revision,
    validated_base_url,
    validated_implementation_revision,
    validated_options,
    validated_timeout,
)
from .openai_generator import provider_model_config as openai_model_config
from .ollama_generator import (
    DEFAULT_TIMEOUT_SECONDS as OLLAMA_DEFAULT_TIMEOUT_SECONDS,
    JSONTransport as OllamaJSONTransport,
    OllamaProviderError,
    PROVIDER_NAME as OLLAMA_PROVIDER_NAME,
    _provider_model_config as ollama_model_config,
    _request_json as ollama_request_json,
    _validated_base_url as validated_ollama_base_url,
    _validated_implementation_revision as validated_ollama_revision,
    _validated_options as validated_ollama_options,
    _validated_timeout as validated_ollama_timeout,
    resolve_ollama_model,
)
from .semantic_objective_identity import (
    OBJECTIVE_OLLAMA_ADAPTER_VERSION,
    OBJECTIVE_OPENAI_ADAPTER_VERSION,
)
from .semantic_objective import (
    MAX_CANDIDATE_BYTES,
    ObjectiveOutput,
    SemanticObjectiveError,
    build_objective_generation,
    load_and_render_objective_prompt,
    load_objective_context,
    parse_objective_output,
    store_objective_candidate,
    store_objective_generation,
)


@dataclass(frozen=True)
class SemanticObjectiveGenerationResult:
    objective_context_sha256: str
    objective_policy: str
    candidate_kind: str
    candidate_sha256: str
    candidate_path: Path
    generation_sha256: str
    generation_path: Path
    model_provider: str
    model_identifier: str
    model_revision: str
    prompt_template_version: str
    prompt_template_sha256: str


def _normalize_crlf(value: object) -> object:
    if isinstance(value, str):
        return value.replace("\r\n", "\n")
    if isinstance(value, list):
        return [_normalize_crlf(item) for item in value]
    if isinstance(value, dict):
        return {key: _normalize_crlf(item) for key, item in value.items()}
    return value


def format_objective_output(data: bytes) -> bytes:
    if len(data) > MAX_CANDIDATE_BYTES:
        raise SemanticObjectiveError("semantic objective output exceeds byte limit")
    value = _decode_json_object(data, label="semantic objective provider output")
    return _canonical_json_bytes(_normalize_crlf(value))


def _store_generated(
    ai_root: Path,
    *,
    objective_context_sha256: str,
    output: ObjectiveOutput,
    implementation_revision: str,
    prompt_template_version: str,
    prompt_template_sha256: str,
    model_provider: str,
    model_identifier: str,
    model_revision: str,
    model_config: Mapping[str, object],
) -> SemanticObjectiveGenerationResult:
    context = load_objective_context(ai_root, objective_context_sha256)
    candidate_sha, candidate_path, _candidate = store_objective_candidate(
        ai_root,
        context_sha256=objective_context_sha256,
        output=output,
    )
    generation = build_objective_generation(
        ai_root,
        objective_context_sha256=objective_context_sha256,
        candidate_sha256=candidate_sha,
        implementation_revision=implementation_revision,
        prompt_template_version=prompt_template_version,
        prompt_template_sha256_value=prompt_template_sha256,
        model_provider=model_provider,
        model_identifier=model_identifier,
        model_revision=model_revision,
        model_config=model_config,
    )
    generation_sha, generation_path = store_objective_generation(
        ai_root,
        generation,
    )
    return SemanticObjectiveGenerationResult(
        objective_context_sha256=objective_context_sha256,
        objective_policy=context.objective_policy,
        candidate_kind=context.candidate_kind,
        candidate_sha256=candidate_sha,
        candidate_path=candidate_path,
        generation_sha256=generation_sha,
        generation_path=generation_path,
        model_provider=model_provider,
        model_identifier=model_identifier,
        model_revision=model_revision,
        prompt_template_version=prompt_template_version,
        prompt_template_sha256=prompt_template_sha256,
    )


def generate_semantic_objective_with_openai_compatible(
    ai_root: Path,
    *,
    objective_context_sha256: str,
    base_url: str,
    model: str,
    implementation_revision: str,
    options: Mapping[str, object] | None = None,
    timeout: float = OPENAI_DEFAULT_TIMEOUT_SECONDS,
    api_key: str | None = None,
    transport: OpenAIJSONTransport | None = None,
) -> SemanticObjectiveGenerationResult:
    revision = validated_implementation_revision(implementation_revision)
    timeout_value = validated_timeout(timeout)
    root = validated_base_url(base_url)
    inference_options = validated_options(options)
    context = load_objective_context(ai_root, objective_context_sha256)
    prompt = load_and_render_objective_prompt(
        ai_root,
        objective_context_sha256,
    )
    identity, data = chat_content(
        root,
        model=model,
        system_prompt=prompt.system,
        user_prompt=prompt.user,
        output_schema=prompt.output_schema,
        schema_name="semantic_objective_generator",
        options=inference_options,
        timeout=timeout_value,
        api_key=api_key,
        transport=transport,
    )
    try:
        output = parse_objective_output(
            format_objective_output(data),
            context=context,
        )
    except (ArtifactLifecycleError, SemanticObjectiveError) as exc:
        raise OpenAICompatibleProviderError(str(exc)) from exc
    model_revision = identifier_revision(identity.identifier)
    config = openai_model_config(inference_options)
    config = dict(config)
    config["objective_adapter_version"] = OBJECTIVE_OPENAI_ADAPTER_VERSION
    return _store_generated(
        ai_root,
        objective_context_sha256=objective_context_sha256,
        output=output,
        implementation_revision=revision,
        prompt_template_version=prompt.template_version,
        prompt_template_sha256=prompt.template_sha256,
        model_provider=OPENAI_PROVIDER_NAME,
        model_identifier=identity.identifier,
        model_revision=model_revision,
        model_config=config,
    )


def _ollama_objective_output(
    base_url: str,
    *,
    model_identifier: str,
    system_prompt: str,
    user_prompt: str,
    output_schema: Mapping[str, object],
    options: Mapping[str, object],
    timeout: float,
    transport: OllamaJSONTransport | None,
) -> bytes:
    request_json = transport or ollama_request_json
    response = request_json(
        base_url,
        method="POST",
        path="/api/chat",
        payload={
            "model": model_identifier,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "think": False,
            "format": dict(output_schema),
            "options": dict(options),
        },
        timeout=timeout,
    )
    if response.get("done") is not True:
        raise OllamaProviderError("Ollama semantic objective response is not complete")
    if response.get("model") != model_identifier:
        raise OllamaProviderError(
            "Ollama semantic objective response model does not match resolved model"
        )
    message = response.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise OllamaProviderError("Ollama semantic objective response message is invalid")
    content = message.get("content")
    if not isinstance(content, str) or not content:
        raise OllamaProviderError("Ollama semantic objective response content is empty")
    try:
        data = content.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise OllamaProviderError(
            "Ollama semantic objective response is not UTF-8 encodable"
        ) from exc
    if len(data) > MAX_CANDIDATE_BYTES:
        raise OllamaProviderError("Ollama semantic objective output exceeds byte limit")
    return data


def generate_semantic_objective_with_ollama(
    ai_root: Path,
    *,
    objective_context_sha256: str,
    base_url: str,
    model: str,
    implementation_revision: str,
    options: Mapping[str, object] | None = None,
    timeout: float = OLLAMA_DEFAULT_TIMEOUT_SECONDS,
    transport: OllamaJSONTransport | None = None,
) -> SemanticObjectiveGenerationResult:
    revision = validated_ollama_revision(implementation_revision)
    timeout_value = validated_ollama_timeout(timeout)
    root = validated_ollama_base_url(base_url)
    inference_options = validated_ollama_options(options)
    context = load_objective_context(ai_root, objective_context_sha256)
    prompt = load_and_render_objective_prompt(
        ai_root,
        objective_context_sha256,
    )
    identity = resolve_ollama_model(
        root,
        model,
        timeout=timeout_value,
        transport=transport,
    )
    data = _ollama_objective_output(
        root,
        model_identifier=identity.identifier,
        system_prompt=prompt.system,
        user_prompt=prompt.user,
        output_schema=prompt.output_schema,
        options=inference_options,
        timeout=timeout_value,
        transport=transport,
    )
    try:
        output = parse_objective_output(
            format_objective_output(data),
            context=context,
        )
    except (ArtifactLifecycleError, SemanticObjectiveError) as exc:
        raise OllamaProviderError(str(exc)) from exc
    config = ollama_model_config(inference_options)
    config = dict(config)
    config["objective_adapter_version"] = OBJECTIVE_OLLAMA_ADAPTER_VERSION
    return _store_generated(
        ai_root,
        objective_context_sha256=objective_context_sha256,
        output=output,
        implementation_revision=revision,
        prompt_template_version=prompt.template_version,
        prompt_template_sha256=prompt.template_sha256,
        model_provider=OLLAMA_PROVIDER_NAME,
        model_identifier=identity.identifier,
        model_revision=identity.digest,
        model_config=config,
    )


def _load_options_file(path: Path | None) -> Mapping[str, object] | None:
    if path is None:
        return None
    data = path.read_bytes()
    return _decode_json_object(data, label="semantic objective options")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="obsidian-semantic-objective-generate"
    )
    parser.add_argument(
        "--provider",
        choices=("ollama", "openai-compatible"),
        required=True,
    )
    parser.add_argument("--ai-root", type=Path, required=True)
    parser.add_argument("--objective-context-sha", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--implementation-revision", required=True)
    parser.add_argument("--options-file", type=Path)
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args(argv)

    try:
        options = _load_options_file(args.options_file)
        if args.provider == "ollama":
            result = generate_semantic_objective_with_ollama(
                args.ai_root,
                objective_context_sha256=args.objective_context_sha,
                base_url=args.base_url,
                model=args.model,
                implementation_revision=args.implementation_revision,
                options=options,
                timeout=args.timeout,
            )
        else:
            result = generate_semantic_objective_with_openai_compatible(
                args.ai_root,
                objective_context_sha256=args.objective_context_sha,
                base_url=args.base_url,
                model=args.model,
                implementation_revision=args.implementation_revision,
                options=options,
                timeout=args.timeout,
                api_key=os.environ.get("OPENAI_API_KEY"),
            )
    except (
        ArtifactLifecycleError,
        SemanticObjectiveError,
        OpenAICompatibleProviderError,
        OllamaProviderError,
        OSError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    from .human_projection import emit_semantic_objective_generation_projection

    projection = emit_semantic_objective_generation_projection(
        args.ai_root,
        case_id=result.generation_sha256,
        objective_generation_sha256=result.generation_sha256,
        candidate_sha256=result.candidate_sha256,
    )
    print(
        json.dumps(
            {
                "event": "semantic-objective-generation",
                "objective_context_sha256": result.objective_context_sha256,
                "objective_policy": result.objective_policy,
                "candidate_kind": result.candidate_kind,
                "candidate_sha256": result.candidate_sha256,
                "candidate_path": str(result.candidate_path),
                "generation_sha256": result.generation_sha256,
                "generation_path": str(result.generation_path),
                "model_provider": result.model_provider,
                "model_identifier": result.model_identifier,
                "model_revision": result.model_revision,
                "prompt_template_version": result.prompt_template_version,
                "prompt_template_sha256": result.prompt_template_sha256,
                "projection_request_sha256": (
                    None if projection is None else projection[0]
                ),
                "projection_request_path": (
                    None if projection is None else str(projection[1])
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
