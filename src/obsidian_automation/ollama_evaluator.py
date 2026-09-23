from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .artifact_lifecycle import ArtifactLifecycleError, _decode_json_object, _require_sha256
from .context_bundle import load_context_bundle
from .evaluation_artifact import (
    _load_accepted_mutation,
    build_evaluation_record,
    load_evaluation_context,
    store_evaluation_record,
)
from .evaluator_contract import (
    EVALUATOR_STRATEGY_V4,
    EVALUATOR_STRATEGY_V5,
    EVALUATOR_STRATEGY_VERSION,
    MAX_EVALUATOR_WALL_SECONDS,
    MAX_EVALUATOR_OUTPUT_BYTES,
    CandidateEvaluatorOutput,
    ConsistencyVerification,
    DimensionEvaluatorOutput,
    aggregate_evaluator_outputs,
    bind_candidate_output,
    bind_consistency_proposals,
    finalize_consistency_candidate,
    evaluator_call_timeout,
    parse_consistency_verifier_output,
    parse_dimension_evaluator_output,
    render_consistency_verifier_prompt,
    render_evaluator_prompts,
    to_evaluation_assessment,
)
from .evaluator_conflict import ConsistencyConflict
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
ADAPTER_VERSION = "ollama-evaluator-chat-structured-v5"
PREVIOUS_ADAPTER_VERSION = "ollama-evaluator-chat-structured-v4"
V5_ADAPTER_VERSION = "ollama-evaluator-chat-structured-v3"
LEGACY_ADAPTER_VERSION = "ollama-evaluator-chat-structured-v2"
EVALUATION_STRATEGY = EVALUATOR_STRATEGY_VERSION
PREVIOUS_EVALUATION_STRATEGY = EVALUATOR_STRATEGY_V5
LEGACY_EVALUATION_STRATEGY = EVALUATOR_STRATEGY_V4
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
    conflicts: tuple[ConsistencyConflict, ...] = ()


def _validated_think(value: object) -> bool | str:
    if value is False:
        return False
    if isinstance(value, str) and value in {"low", "medium", "high", "max", "xhigh"}:
        return value
    raise ArtifactLifecycleError(
        "Ollama evaluator think must be false or a supported bounded reasoning level"
    )


def _provider_model_config(
    options: Mapping[str, object],
    *,
    think: bool | str,
) -> dict[str, object]:
    return validate_model_config(
        {
            "adapter_version": ADAPTER_VERSION,
            "think": _validated_think(think),
            "strategy": EVALUATION_STRATEGY,
            "options": dict(options),
        }
    )


def _chat_dimension_output(
    base_url: str,
    *,
    dimension: str,
    identity: OllamaModelIdentity,
    system_prompt: str,
    user_prompt: str,
    output_schema: Mapping[str, object],
    options: Mapping[str, object],
    think: bool | str,
    timeout: float,
    transport: JSONTransport | None,
) -> DimensionEvaluatorOutput:
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
            "think": _validated_think(think),
            "format": dict(output_schema),
            "options": dict(options),
        },
        timeout=timeout,
    )
    if response.get("done") is not True:
        raise OllamaProviderError(
            f"Ollama {dimension} evaluator chat response is not complete"
        )
    response_model = response.get("model")
    if not isinstance(response_model, str) or response_model != identity.identifier:
        raise OllamaProviderError(
            f"Ollama {dimension} evaluator chat response model does not match resolved model"
        )
    message = response.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise OllamaProviderError(
            f"Ollama {dimension} evaluator chat response message is invalid"
        )
    content = message.get("content")
    if not isinstance(content, str) or not content:
        raise OllamaProviderError(
            f"Ollama {dimension} evaluator chat response content is empty or invalid"
        )
    try:
        data = content.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise OllamaProviderError(
            f"Ollama {dimension} evaluator chat content is not UTF-8 encodable"
        ) from exc
    if len(data) > MAX_EVALUATOR_OUTPUT_BYTES:
        raise OllamaProviderError(
            f"Ollama {dimension} semantic output exceeds evaluator output limit"
        )
    try:
        return parse_dimension_evaluator_output(data, dimension=dimension)
    except ArtifactLifecycleError as exc:
        raise OllamaProviderError(str(exc)) from exc


def _chat_verifier_output(
    base_url: str,
    *,
    identity: OllamaModelIdentity,
    system_prompt: str,
    user_prompt: str,
    output_schema: Mapping[str, object],
    options: Mapping[str, object],
    think: bool | str,
    timeout: float,
    transport: JSONTransport | None,
) -> ConsistencyVerification:
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
            "think": _validated_think(think),
            "format": dict(output_schema),
            "options": dict(options),
        },
        timeout=timeout,
    )
    if response.get("done") is not True:
        raise OllamaProviderError(
            "Ollama consistency verifier chat response is not complete"
        )
    response_model = response.get("model")
    if not isinstance(response_model, str) or response_model != identity.identifier:
        raise OllamaProviderError(
            "Ollama consistency verifier response model does not match resolved model"
        )
    message = response.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise OllamaProviderError(
            "Ollama consistency verifier response message is invalid"
        )
    content = message.get("content")
    if not isinstance(content, str) or not content:
        raise OllamaProviderError(
            "Ollama consistency verifier response content is empty or invalid"
        )
    try:
        data = content.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise OllamaProviderError(
            "Ollama consistency verifier content is not UTF-8 encodable"
        ) from exc
    if len(data) > MAX_EVALUATOR_OUTPUT_BYTES:
        raise OllamaProviderError(
            "Ollama consistency verifier output exceeds evaluator output limit"
        )
    try:
        return parse_consistency_verifier_output(data)
    except ArtifactLifecycleError as exc:
        raise OllamaProviderError(str(exc)) from exc


def _expected_prompt_order(evaluation_context: object) -> tuple[tuple[str, str | None], ...]:
    candidates = getattr(evaluation_context, "candidates", None)
    if not isinstance(candidates, tuple):
        raise ArtifactLifecycleError("evaluator context candidates are invalid")
    expected: list[tuple[str, str | None]] = [("groundedness", None)]
    for candidate in candidates:
        path = getattr(candidate, "path", None)
        if not isinstance(path, str):
            raise ArtifactLifecycleError("evaluator context candidate path is invalid")
        expected.extend(
            (("redundancy", path), ("consistency_proposer", path))
        )
    return tuple(expected)


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
    think: bool | str = False,
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
    think_value = _validated_think(think)
    model_config = _provider_model_config(inference_options, think=think_value)
    deadline = time.monotonic() + MAX_EVALUATOR_WALL_SECONDS

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
    actual_order = tuple((prompt.pass_kind, prompt.candidate_path) for prompt in prompts)
    if actual_order != _expected_prompt_order(evaluation_context):
        raise ArtifactLifecycleError("evaluator prompt pass order is invalid")

    identity = resolve_ollama_model(
        root,
        model,
        timeout=evaluator_call_timeout(deadline, timeout_value),
        transport=transport,
    )

    candidate_contents = {candidate.path: candidate.content for candidate in evaluation_context.candidates}
    groundedness_output: DimensionEvaluatorOutput | None = None
    redundancy_pairs: list[CandidateEvaluatorOutput] = []
    consistency_pairs: list[CandidateEvaluatorOutput] = []
    used_prompts = list(prompts)

    for prompt in prompts:
        dimension_output = _chat_dimension_output(
            root,
            dimension=prompt.dimension,
            identity=identity,
            system_prompt=prompt.system,
            user_prompt=prompt.user,
            output_schema=prompt.output_schema,
            options=inference_options,
            think=think_value,
            timeout=evaluator_call_timeout(deadline, timeout_value),
            transport=transport,
        )
        if prompt.pass_kind == "groundedness":
            if prompt.candidate_path is not None or groundedness_output is not None:
                raise ArtifactLifecycleError("groundedness evaluator pass is invalid")
            groundedness_output = dimension_output
            continue

        if prompt.candidate_path is None:
            raise ArtifactLifecycleError("pairwise evaluator pass is missing candidate path")
        if prompt.pass_kind == "redundancy":
            bound = bind_candidate_output(
                dimension_output,
                candidate_path=prompt.candidate_path,
            )
            redundancy_pairs.append(bound)
            continue
        if prompt.pass_kind != "consistency_proposer":
            raise ArtifactLifecycleError("pairwise evaluator dimension is invalid")
        candidate_content = candidate_contents.get(prompt.candidate_path)
        if candidate_content is None:
            raise ArtifactLifecycleError("evaluator candidate content is unavailable")
        proposal_output = bind_consistency_proposals(
            dimension_output,
            candidate_path=prompt.candidate_path,
            proposal_content=proposal_content,
            candidate_content=candidate_content,
        )
        verifications: list[ConsistencyVerification] = []
        for proposal in proposal_output.proposals:
            verifier_prompt = render_consistency_verifier_prompt(
                candidate_path=prompt.candidate_path,
                proposal=proposal,
            )
            used_prompts.append(verifier_prompt)
            verification = _chat_verifier_output(
                root,
                identity=identity,
                system_prompt=verifier_prompt.system,
                user_prompt=verifier_prompt.user,
                output_schema=verifier_prompt.output_schema,
                options=inference_options,
                think=think_value,
                timeout=evaluator_call_timeout(deadline, timeout_value),
                transport=transport,
            )
            verifications.append(verification)
        consistency_pairs.append(
            finalize_consistency_candidate(proposal_output, tuple(verifications))
        )

    if groundedness_output is None:
        raise ArtifactLifecycleError("groundedness evaluator pass is missing")

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
        for item in used_prompts[1:]
    ):
        raise ArtifactLifecycleError("evaluator prompt provenance is inconsistent")

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
        conflicts=assessment.conflicts,
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
        conflicts=assessment.conflicts,
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
    return _decode_json_object(data, label="Ollama evaluator options file")


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
                    "conflicts": [
                        {
                            "candidate_path": conflict.candidate_path,
                            "proposal_claim": conflict.proposal_claim,
                            "candidate_claim": conflict.candidate_claim,
                            "incompatibility": conflict.incompatibility,
                        }
                        for conflict in result.conflicts
                    ],
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0
