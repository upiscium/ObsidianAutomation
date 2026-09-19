from __future__ import annotations

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
    CandidateEvaluatorOutput,
    DimensionEvaluatorOutput,
    aggregate_evaluator_outputs,
    bind_candidate_output,
    parse_dimension_evaluator_output,
    render_evaluator_prompts,
    to_evaluation_assessment,
)
from .generation_artifact import load_generation_record, validate_model_config
from .openai_compatible import (
    DEFAULT_TIMEOUT_SECONDS,
    IDENTITY_BINDING,
    JSONTransport,
    OpenAICompatibleProviderError,
    PROVIDER_NAME,
    chat_content,
    identifier_revision,
    validated_base_url,
    validated_options,
    validated_timeout,
)
from .ollama_evaluator import EVALUATION_STRATEGY, _expected_prompt_order
from .ollama_generator import _validated_implementation_revision


ADAPTER_VERSION = "openai-evaluator-chat-completions-json-v0"


@dataclass(frozen=True)
class OpenAICompatibleEvaluationResult:
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


def provider_model_config(options: Mapping[str, object]) -> dict[str, object]:
    return validate_model_config(
        {
            "adapter_version": ADAPTER_VERSION,
            "identity_binding": IDENTITY_BINDING,
            "strategy": EVALUATION_STRATEGY,
            "options": dict(options),
        }
    )


def _chat_dimension_output(
    base_url: str,
    *,
    dimension: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    options: Mapping[str, object],
    timeout: float,
    api_key: str | None,
    transport: JSONTransport | None,
) -> tuple[str, DimensionEvaluatorOutput]:
    identity, data = chat_content(
        base_url,
        model=model,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        options=options,
        timeout=timeout,
        api_key=api_key,
        transport=transport,
    )
    if len(data) > MAX_EVALUATOR_OUTPUT_BYTES:
        raise OpenAICompatibleProviderError(
            f"OpenAI-compatible {dimension} output exceeds evaluator output limit"
        )
    try:
        output = parse_dimension_evaluator_output(data, dimension=dimension)
    except ArtifactLifecycleError as exc:
        raise OpenAICompatibleProviderError(str(exc)) from exc
    return identity.identifier, output


def evaluate_knowledge_note_with_openai_compatible(
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
    api_key: str | None = None,
    transport: JSONTransport | None = None,
) -> OpenAICompatibleEvaluationResult:
    proposal_digest = _require_sha256(proposal_sha256, label="proposal_sha256")
    generation_digest = _require_sha256(generation_sha256, label="generation_sha256")
    evaluation_context_digest = _require_sha256(
        evaluation_context_sha256,
        label="evaluation_context_sha256",
    )
    revision = _validated_implementation_revision(implementation_revision)
    timeout_value = validated_timeout(timeout)
    root = validated_base_url(base_url)
    inference_options = validated_options(options)
    model_config = provider_model_config(inference_options)

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

    prompts = render_evaluator_prompts(
        target_path=target_path,
        proposal_content=proposal_content,
        generation_context=generation_context,
        evaluation_context=evaluation_context,
    )
    actual_order = tuple((prompt.dimension, prompt.candidate_path) for prompt in prompts)
    if actual_order != _expected_prompt_order(evaluation_context):
        raise ArtifactLifecycleError("evaluator prompt pass order is invalid")

    groundedness_output: DimensionEvaluatorOutput | None = None
    redundancy_pairs: list[CandidateEvaluatorOutput] = []
    consistency_pairs: list[CandidateEvaluatorOutput] = []
    response_model: str | None = None

    for prompt in prompts:
        model_identifier, dimension_output = _chat_dimension_output(
            root,
            dimension=prompt.dimension,
            model=model,
            system_prompt=prompt.system,
            user_prompt=prompt.user,
            options=inference_options,
            timeout=timeout_value,
            api_key=api_key,
            transport=transport,
        )
        if response_model is None:
            response_model = model_identifier
        elif response_model != model_identifier:
            raise OpenAICompatibleProviderError(
                "OpenAI-compatible evaluator returned inconsistent model identities"
            )

        if prompt.dimension == "groundedness":
            if prompt.candidate_path is not None or groundedness_output is not None:
                raise ArtifactLifecycleError("groundedness evaluator pass is invalid")
            groundedness_output = dimension_output
            continue
        if prompt.candidate_path is None:
            raise ArtifactLifecycleError("pairwise evaluator pass is missing candidate path")
        bound = bind_candidate_output(
            dimension_output,
            candidate_path=prompt.candidate_path,
        )
        if prompt.dimension == "redundancy":
            redundancy_pairs.append(bound)
        elif prompt.dimension == "consistency":
            consistency_pairs.append(bound)
        else:
            raise ArtifactLifecycleError("pairwise evaluator dimension is invalid")

    if groundedness_output is None or response_model is None:
        raise ArtifactLifecycleError("evaluator output is incomplete")

    output = aggregate_evaluator_outputs(
        groundedness=groundedness_output,
        redundancy_pairs=redundancy_pairs,
        consistency_pairs=consistency_pairs,
    )
    assessment = to_evaluation_assessment(output)
    prompt = prompts[0]
    if any(
        item.template_version != prompt.template_version
        or item.template_sha256 != prompt.template_sha256
        for item in prompts[1:]
    ):
        raise ArtifactLifecycleError("evaluator prompt provenance is inconsistent")

    model_revision = identifier_revision(response_model)
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
        model_identifier=response_model,
        model_revision=model_revision,
        model_config=model_config,
        groundedness=assessment.groundedness,
        redundancy=assessment.redundancy,
        consistency=assessment.consistency,
        recommendation=assessment.recommendation,
        findings=assessment.findings,
    )
    evaluation_sha, evaluation_path = store_evaluation_record(ai_root, record)
    return OpenAICompatibleEvaluationResult(
        proposal_sha256=proposal_digest,
        mutation_sha256=mutation_digest,
        generation_sha256=generation_digest,
        evaluation_context_sha256=evaluation_context_digest,
        evaluation_sha256=evaluation_sha,
        evaluation_path=evaluation_path,
        model_identifier=response_model,
        model_revision=model_revision,
        prompt_template_version=prompt.template_version,
        prompt_template_sha256=prompt.template_sha256,
        groundedness=assessment.groundedness,
        redundancy=assessment.redundancy,
        consistency=assessment.consistency,
        recommendation=assessment.recommendation,
        findings=assessment.findings,
    )
