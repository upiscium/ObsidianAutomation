from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .artifact_lifecycle import ArtifactLifecycleError, _require_sha256
from .context_bundle import load_context_bundle
from .evaluation_artifact import (
    _load_accepted_mutation,
    build_evaluation_record,
    load_evaluation_context,
    store_evaluation_record,
)
from .evaluator_contract import (
    MAX_EVALUATOR_OUTPUT_BYTES,
    EvaluatorOutput,
    parse_evaluator_output,
    render_evaluator_prompt,
    to_evaluation_assessment,
)
from .generation_artifact import load_generation_record, validate_model_config
from .ollama_generator import (
    DEFAULT_TIMEOUT_SECONDS,
    JSONTransport,
    OllamaModelIdentity,
    OllamaProviderError,
    _request_json,
    _validated_base_url,
    _validated_implementation_revision,
    _validated_options,
    _validated_timeout,
    resolve_ollama_model,
)


PROVIDER_NAME = "ollama"
ADAPTER_VERSION = "ollama-evaluator-chat-structured-v0"
MAX_OPTIONS_BYTES = 12 * 1024


@dataclass(frozen=True)
class OllamaEvaluationResult:
    proposal_sha256: str
    mutation_sha256: str
    generation_sha256: str
    evaluation_context_sha256: str
    evaluation_sha256: str
    evaluation_path: Path
    model_identifier: str
    model_revision: str
    prompt_template_version: str
    prompt_template_sha256: str
    groundedness: str
    redundancy: str
    consistency: str
    recommendation: str
    findings: tuple[str, ...]


def _provider_model_config(options: Mapping[str, object]) -> dict[str, object]:
    return validate_model_config(
        {
            "adapter_version": ADAPTER_VERSION,
            "think": False,
            "options": dict(options),
        }
    )


def _chat_evaluator_output(
    base_url: str,
    *,
    identity: OllamaModelIdentity,
    system_prompt: str,
    user_prompt: str,
    output_schema: Mapping[str, object],
    options: Mapping[str, object],
    timeout: float,
    transport: JSONTransport | None,
) -> EvaluatorOutput:
    request_json = transport or _request_json
    response = request_json(
        base_url,
        method="POST",
        path="/api/chat",
        payload={
            "model": identity.identifier,
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
        raise OllamaProviderError("Ollama evaluator chat response is not complete")
    response_model = response.get("model")
    if not isinstance(response_model, str) or response_model != identity.identifier:
        raise OllamaProviderError(
            "Ollama evaluator chat response model does not match resolved model"
        )
    message = response.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise OllamaProviderError("Ollama evaluator chat response message is invalid")
    content = message.get("content")
    if not isinstance(content, str) or not content:
        raise OllamaProviderError("Ollama evaluator chat response content is empty or invalid")
    try:
        data = content.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise OllamaProviderError("Ollama evaluator chat content is not UTF-8 encodable") from exc
    if len(data) > MAX_EVALUATOR_OUTPUT_BYTES:
        raise OllamaProviderError("Ollama semantic output exceeds evaluator output limit")
    try:
        return parse_evaluator_output(data)
    except ArtifactLifecycleError as exc:
        raise OllamaProviderError(str(exc)) from exc


def evaluate_knowledge_note_with_ollama(
    ai_root: Path,
    *,
    proposal_sha256: str,
    generation_sha256: str,
    evaluation_context_sha256: str,
    base_url: str,
    model: str,
    implementation_revision: str,
    options: Mapping[str, object] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    transport: JSONTransport | None = None,
) -> OllamaEvaluationResult:
    proposal_digest = _require_sha256(proposal_sha256, label="proposal_sha256")
    generation_digest = _require_sha256(generation_sha256, label="generation_sha256")
    evaluation_context_digest = _require_sha256(
        evaluation_context_sha256,
        label="evaluation_context_sha256",
    )
    revision = _validated_implementation_revision(implementation_revision)
    timeout_value = _validated_timeout(timeout)
    root = _validated_base_url(base_url)
    inference_options = _validated_options(options)
    model_config = _provider_model_config(inference_options)

    mutation_digest, target_path, proposal_content = _load_accepted_mutation(
        ai_root,
        proposal_digest,
    )

    generation = load_generation_record(ai_root, generation_digest)
    if generation.proposal_sha256 != proposal_digest:
        raise ArtifactLifecycleError(
            "evaluator generation record is bound to another proposal"
        )
    generation_context = load_context_bundle(ai_root, generation.context_sha256)

    evaluation_context = load_evaluation_context(ai_root, evaluation_context_digest)
    if (
        evaluation_context.proposal_sha256 != proposal_digest
        or evaluation_context.mutation_sha256 != mutation_digest
    ):
        raise ArtifactLifecycleError(
            "evaluator evaluation context is bound to another mutation"
        )

    prompt = render_evaluator_prompt(
        target_path=target_path,
        proposal_content=proposal_content,
        generation_context=generation_context,
        evaluation_context=evaluation_context,
    )

    identity = resolve_ollama_model(
        root,
        model,
        timeout=timeout_value,
        transport=transport,
    )
    output = _chat_evaluator_output(
        root,
        identity=identity,
        system_prompt=prompt.system,
        user_prompt=prompt.user,
        output_schema=prompt.output_schema,
        options=inference_options,
        timeout=timeout_value,
        transport=transport,
    )
    assessment = to_evaluation_assessment(output)

    record = build_evaluation_record(
        ai_root,
        proposal_sha256=proposal_digest,
        mutation_sha256=mutation_digest,
        generation_sha256=generation_digest,
        evaluation_context_sha256=evaluation_context_digest,
        implementation_revision=revision,
        prompt_template_version=prompt.template_version,
        prompt_template_sha256=prompt.template_sha256,
        model_provider=PROVIDER_NAME,
        model_identifier=identity.identifier,
        model_revision=identity.digest,
        model_config=model_config,
        groundedness=assessment.groundedness,
        redundancy=assessment.redundancy,
        consistency=assessment.consistency,
        recommendation=assessment.recommendation,
        findings=assessment.findings,
    )
    evaluation_sha, evaluation_path = store_evaluation_record(ai_root, record)

    return OllamaEvaluationResult(
        proposal_sha256=proposal_digest,
        mutation_sha256=mutation_digest,
        generation_sha256=generation_digest,
        evaluation_context_sha256=evaluation_context_digest,
        evaluation_sha256=evaluation_sha,
        evaluation_path=evaluation_path,
        model_identifier=identity.identifier,
        model_revision=identity.digest,
        prompt_template_version=prompt.template_version,
        prompt_template_sha256=prompt.template_sha256,
        groundedness=assessment.groundedness,
        redundancy=assessment.redundancy,
        consistency=assessment.consistency,
        recommendation=assessment.recommendation,
        findings=assessment.findings,
    )


def _load_options_file(path: Path | None) -> Mapping[str, object] | None:
    if path is None:
        return None
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ArtifactLifecycleError(f"cannot read Ollama evaluator options file: {path}") from exc
    if len(data) > MAX_OPTIONS_BYTES:
        raise ArtifactLifecycleError(
            f"Ollama evaluator options file exceeds {MAX_OPTIONS_BYTES} bytes"
        )
    try:
        value = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ArtifactLifecycleError("Ollama evaluator options file is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ArtifactLifecycleError("Ollama evaluator options file must contain a JSON object")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="obsidian-knowledge-evaluate")
    parser.add_argument("--ai-root", type=Path, required=True)
    parser.add_argument("--proposal-sha256", required=True)
    parser.add_argument("--generation-sha256", required=True)
    parser.add_argument("--evaluation-context-sha256", required=True)
    parser.add_argument("--ollama-base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--implementation-revision", required=True)
    parser.add_argument("--options-file", type=Path)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)

    try:
        result = evaluate_knowledge_note_with_ollama(
            args.ai_root,
            proposal_sha256=args.proposal_sha256,
            generation_sha256=args.generation_sha256,
            evaluation_context_sha256=args.evaluation_context_sha256,
            base_url=args.ollama_base_url,
            model=args.model,
            implementation_revision=args.implementation_revision,
            options=_load_options_file(args.options_file),
            timeout=args.timeout,
        )
    except (ArtifactLifecycleError, OllamaProviderError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "proposal_sha256": result.proposal_sha256,
                "mutation_sha256": result.mutation_sha256,
                "generation_sha256": result.generation_sha256,
                "evaluation_context_sha256": result.evaluation_context_sha256,
                "evaluation_sha256": result.evaluation_sha256,
                "evaluation_path": str(result.evaluation_path),
                "model_identifier": result.model_identifier,
                "model_revision": result.model_revision,
                "prompt_template_version": result.prompt_template_version,
                "prompt_template_sha256": result.prompt_template_sha256,
                "assessment": {
                    "groundedness": result.groundedness,
                    "redundancy": result.redundancy,
                    "consistency": result.consistency,
                    "recommendation": result.recommendation,
                    "findings": list(result.findings),
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0
