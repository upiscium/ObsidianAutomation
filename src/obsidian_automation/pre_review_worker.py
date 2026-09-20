from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Mapping, Sequence
from urllib.parse import urlsplit

from .artifact_lifecycle import ArtifactLifecycleError
from .evaluation_artifact import (
    build_evaluation_context,
    create_evaluation_request,
    load_evaluation_context,
    load_evaluation_record,
    store_evaluation_context,
)
from .evaluator_contract import prompt_template_sha256 as evaluator_prompt_sha256
from .generation_artifact import load_generation_record
from .human_projection import (
    emit_evaluation_and_review_projections,
    emit_generation_projection,
    emit_validation_projection,
)
from .generator_contract import prompt_template_sha256 as generator_prompt_sha256
from .knowledge_index import build_knowledge_index, store_knowledge_index
from .knowledge_validator import validate_proposal
from .openai_compatible import (
    DEFAULT_TIMEOUT_SECONDS,
    OpenAICompatibleProviderError,
)
from .openai_evaluator import evaluate_knowledge_note_with_openai_compatible
from .openai_generator import generate_knowledge_note_with_openai_compatible
from .ollama_evaluator import evaluate_knowledge_note_with_ollama
from .ollama_generator import (
    OllamaProviderError,
    generate_knowledge_note_with_ollama,
)
from .pre_review_job import (
    PreReviewJobError,
    RecipeComponent,
    StageWorkItem,
    claim_next_attempt,
    complete_attempt,
    load_recipe,
    stage_output,
)
from .production_io import ProductionIOError, mirror_read_lock


DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_MAX_AWAITING_REVIEW = 8


def _json_result(event: str, **payload: object) -> dict[str, object]:
    return {"event": event, **payload}


def _options(component: RecipeComponent) -> Mapping[str, object]:
    value = component.model_config.get("options")
    if not isinstance(value, dict):
        raise PreReviewJobError("recipe model options are invalid")
    return value


def _ollama_root_from_provider_url(base_url: str) -> str:
    parsed = urlsplit(base_url)
    path = parsed.path.rstrip("/")
    if path not in {"", "/v1"}:
        raise PreReviewJobError(
            "Ollama provider base URL must use the authority root or /v1"
        )
    if parsed.query or parsed.fragment or parsed.username is not None or parsed.password is not None:
        raise PreReviewJobError("Ollama provider base URL is unsafe")
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        raise PreReviewJobError("Ollama provider base URL is invalid")
    authority = parsed.netloc
    return f"{parsed.scheme}://{authority}"


def _component_preflight(
    component: RecipeComponent,
    *,
    deployed_revision: str,
    expected_prompt_sha256: str,
    role: str,
) -> None:
    if deployed_revision != component.implementation_revision:
        raise PreReviewJobError(f"{role} deployed revision does not match recipe")
    if component.prompt_template_sha256 != expected_prompt_sha256:
        raise PreReviewJobError(f"{role} prompt hash does not match recipe")


def _block(
    ai_root: Path,
    work: StageWorkItem,
    *,
    reason_code: str,
) -> dict[str, object]:
    result = complete_attempt(
        ai_root,
        work.attempt_id,
        outcome="blocked",
        reason_code=reason_code,
    )
    return _json_result(
        "pre-review-worker",
        stage=work.stage,
        status="blocked",
        job_id=work.job_id,
        generation_id=work.generation_id,
        attempt_id=work.attempt_id,
        reason_code=reason_code,
        state=result["state"],
    )


def _retry(
    ai_root: Path,
    work: StageWorkItem,
    *,
    reason_code: str,
) -> dict[str, object]:
    result = complete_attempt(
        ai_root,
        work.attempt_id,
        outcome="retryable_failure",
        reason_code=reason_code,
    )
    return _json_result(
        "pre-review-worker",
        stage=work.stage,
        status="retryable_failure",
        job_id=work.job_id,
        generation_id=work.generation_id,
        attempt_id=work.attempt_id,
        reason_code=reason_code,
        state=result["state"],
    )


def _idle(stage: str) -> dict[str, object]:
    return _json_result("pre-review-worker", stage=stage, status="idle")


def run_generator_worker(
    ai_root: Path,
    *,
    base_url: str,
    deployed_revision: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    max_awaiting_review: int = DEFAULT_MAX_AWAITING_REVIEW,
    api_key: str | None = None,
    transport=None,
) -> dict[str, object]:
    work = claim_next_attempt(
        ai_root,
        "generation",
        max_attempts=max_attempts,
        max_awaiting_review=max_awaiting_review,
        recover_running=True,
    )
    if work is None:
        return _idle("generation")

    try:
        recipe = load_recipe(ai_root, work.recipe_sha256)
    except (ArtifactLifecycleError, OSError):
        return _block(ai_root, work, reason_code="generator_recipe_unreadable")
    component = recipe.generator
    try:
        _component_preflight(
            component,
            deployed_revision=deployed_revision,
            expected_prompt_sha256=generator_prompt_sha256(),
            role="generator",
        )
    except PreReviewJobError:
        return _block(ai_root, work, reason_code="generator_recipe_runtime_mismatch")

    try:
        if component.provider == "ollama":
            generated = generate_knowledge_note_with_ollama(
                ai_root,
                context_sha256=work.context_sha256,
                base_url=_ollama_root_from_provider_url(base_url),
                model=component.model_identifier,
                implementation_revision=deployed_revision,
                options=_options(component),
                timeout=timeout,
                transport=transport,
            )
        elif component.provider == "openai-compatible":
            generated = generate_knowledge_note_with_openai_compatible(
                ai_root,
                context_sha256=work.context_sha256,
                base_url=base_url,
                model=component.model_identifier,
                implementation_revision=deployed_revision,
                options=_options(component),
                timeout=timeout,
                api_key=api_key,
                transport=transport,
            )
        else:
            return _block(
                ai_root,
                work,
                reason_code="generator_recipe_runtime_mismatch",
            )
    except (
        OpenAICompatibleProviderError,
        OllamaProviderError,
        ArtifactLifecycleError,
        PreReviewJobError,
        OSError,
    ):
        return _retry(ai_root, work, reason_code="generator_provider_or_output_error")

    try:
        record = load_generation_record(ai_root, generated.generation_sha256)
        if (
            record.context_sha256 != work.context_sha256
            or record.proposal_sha256 != generated.proposal_sha256
            or record.generator.implementation_revision != deployed_revision
            or record.generator.prompt_template_version
            != component.prompt_template_version
            or record.generator.prompt_template_sha256
            != component.prompt_template_sha256
            or record.model.provider != component.provider
            or record.model.identifier != component.model_identifier
            or record.model.revision != component.model_revision
            or dict(record.model_config) != dict(component.model_config)
        ):
            raise PreReviewJobError("generated provenance does not match recipe")
    except (ArtifactLifecycleError, PreReviewJobError, OSError):
        return _block(ai_root, work, reason_code="generator_output_binding_mismatch")

    emit_generation_projection(
        ai_root,
        case_id=work.generation_id,
        generation_sha256=generated.generation_sha256,
        proposal_sha256=generated.proposal_sha256,
    )
    output = {
        "proposal_sha256": generated.proposal_sha256,
        "generation_sha256": generated.generation_sha256,
    }
    completed = complete_attempt(
        ai_root,
        work.attempt_id,
        outcome="succeeded",
        output=output,
    )
    return _json_result(
        "pre-review-worker",
        stage="generation",
        status="completed",
        job_id=work.job_id,
        generation_id=work.generation_id,
        attempt_id=work.attempt_id,
        output=completed["output"],
        state=completed["state"],
    )


def run_validator_worker(
    ai_root: Path,
    vault_root: Path,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> dict[str, object]:
    work = claim_next_attempt(
        ai_root,
        "validation",
        max_attempts=max_attempts,
        recover_running=True,
    )
    if work is None:
        return _idle("validation")

    try:
        selected = stage_output(ai_root, work.generation_id, "generation")
    except (ArtifactLifecycleError, OSError):
        return _block(ai_root, work, reason_code="generation_output_unreadable")
    if selected is None:
        return _block(ai_root, work, reason_code="missing_generation_output")
    proposal_sha = str(selected["proposal_sha256"])

    try:
        validation = validate_proposal(ai_root, vault_root, proposal_sha)
    except OSError:
        return _retry(ai_root, work, reason_code="validator_io_error")
    except ArtifactLifecycleError:
        return _block(ai_root, work, reason_code="validator_binding_error")

    if validation["result"] == "rejected":
        emit_validation_projection(
            ai_root,
            case_id=work.generation_id,
            proposal_sha256=proposal_sha,
        )
        completed = complete_attempt(
            ai_root,
            work.attempt_id,
            outcome="deterministic_reject",
            reason_code="validation_rejected",
        )
        return _json_result(
            "pre-review-worker",
            stage="validation",
            status="deterministic_reject",
            job_id=work.job_id,
            generation_id=work.generation_id,
            attempt_id=work.attempt_id,
            state=completed["state"],
        )

    mutation_sha = validation.get("mutation_sha256")
    if not isinstance(mutation_sha, str):
        return _block(ai_root, work, reason_code="validation_result_binding_error")

    try:
        request_sha, _path, request = create_evaluation_request(ai_root, proposal_sha)
        if (
            request.proposal_sha256 != proposal_sha
            or request.mutation_sha256 != mutation_sha
        ):
            raise PreReviewJobError("evaluation request does not match validation")
    except (ArtifactLifecycleError, PreReviewJobError, OSError):
        return _block(ai_root, work, reason_code="evaluation_request_binding_error")

    emit_validation_projection(
        ai_root,
        case_id=work.generation_id,
        proposal_sha256=proposal_sha,
    )
    output = {
        **selected,
        "mutation_sha256": mutation_sha,
        "request_sha256": request_sha,
    }
    completed = complete_attempt(
        ai_root,
        work.attempt_id,
        outcome="succeeded",
        output=output,
    )
    return _json_result(
        "pre-review-worker",
        stage="validation",
        status="completed",
        job_id=work.job_id,
        generation_id=work.generation_id,
        attempt_id=work.attempt_id,
        output=completed["output"],
        state=completed["state"],
    )


def run_reader_worker(
    ai_root: Path,
    vault_root: Path,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> dict[str, object]:
    work = claim_next_attempt(
        ai_root,
        "evaluation_context",
        max_attempts=max_attempts,
        recover_running=True,
    )
    if work is None:
        return _idle("evaluation_context")

    try:
        recipe = load_recipe(ai_root, work.recipe_sha256)
        selected = stage_output(ai_root, work.generation_id, "validation")
    except (ArtifactLifecycleError, OSError):
        return _block(ai_root, work, reason_code="reader_input_metadata_unreadable")
    if selected is None:
        return _block(ai_root, work, reason_code="missing_validation_output")

    try:
        with mirror_read_lock(ai_root):
            index = build_knowledge_index(vault_root)
        index_sha, _ = store_knowledge_index(ai_root, index)
        context = build_evaluation_context(
            ai_root,
            vault_root,
            request_sha256=str(selected["request_sha256"]),
            index_sha256=index_sha,
            top_k=recipe.evaluation_context_top_k,
        )
        context_sha, _ = store_evaluation_context(ai_root, context)
        loaded = load_evaluation_context(ai_root, context_sha)
        if (
            loaded.proposal_sha256 != selected["proposal_sha256"]
            or loaded.mutation_sha256 != selected["mutation_sha256"]
            or loaded.request_sha256 != selected["request_sha256"]
        ):
            raise PreReviewJobError("evaluation context binding mismatch")
    except (ArtifactLifecycleError, ProductionIOError, PreReviewJobError, OSError):
        return _retry(ai_root, work, reason_code="reader_view_or_binding_error")

    output = {
        **selected,
        "index_sha256": index_sha,
        "evaluation_context_sha256": context_sha,
    }
    completed = complete_attempt(
        ai_root,
        work.attempt_id,
        outcome="succeeded",
        output=output,
    )
    return _json_result(
        "pre-review-worker",
        stage="evaluation_context",
        status="completed",
        job_id=work.job_id,
        generation_id=work.generation_id,
        attempt_id=work.attempt_id,
        output=completed["output"],
        state=completed["state"],
    )


def run_evaluator_worker(
    ai_root: Path,
    *,
    base_url: str,
    deployed_revision: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    api_key: str | None = None,
    transport=None,
) -> dict[str, object]:
    work = claim_next_attempt(
        ai_root,
        "evaluation",
        max_attempts=max_attempts,
        recover_running=True,
    )
    if work is None:
        return _idle("evaluation")

    try:
        recipe = load_recipe(ai_root, work.recipe_sha256)
        selected = stage_output(ai_root, work.generation_id, "evaluation_context")
    except (ArtifactLifecycleError, OSError):
        return _block(ai_root, work, reason_code="evaluator_input_metadata_unreadable")
    component = recipe.evaluator
    if selected is None:
        return _block(ai_root, work, reason_code="missing_evaluation_context_output")

    try:
        _component_preflight(
            component,
            deployed_revision=deployed_revision,
            expected_prompt_sha256=evaluator_prompt_sha256(),
            role="evaluator",
        )
    except PreReviewJobError:
        return _block(ai_root, work, reason_code="evaluator_recipe_runtime_mismatch")

    try:
        if component.provider == "ollama":
            evaluated = evaluate_knowledge_note_with_ollama(
                ai_root,
                proposal_sha256=str(selected["proposal_sha256"]),
                generation_sha256=str(selected["generation_sha256"]),
                evaluation_context_sha256=str(selected["evaluation_context_sha256"]),
                base_url=_ollama_root_from_provider_url(base_url),
                model=component.model_identifier,
                implementation_revision=deployed_revision,
                options=_options(component),
                think=component.model_config.get("think", False),
                timeout=timeout,
                transport=transport,
            )
        elif component.provider == "openai-compatible":
            evaluated = evaluate_knowledge_note_with_openai_compatible(
                ai_root,
                proposal_sha256=str(selected["proposal_sha256"]),
                generation_sha256=str(selected["generation_sha256"]),
                evaluation_context_sha256=str(selected["evaluation_context_sha256"]),
                base_url=base_url,
                model=component.model_identifier,
                implementation_revision=deployed_revision,
                options=_options(component),
                timeout=timeout,
                api_key=api_key,
                transport=transport,
            )
        else:
            return _block(
                ai_root,
                work,
                reason_code="evaluator_recipe_runtime_mismatch",
            )
    except (
        OpenAICompatibleProviderError,
        OllamaProviderError,
        ArtifactLifecycleError,
        PreReviewJobError,
        OSError,
    ):
        return _retry(ai_root, work, reason_code="evaluator_provider_or_output_error")

    try:
        record = load_evaluation_record(ai_root, evaluated.evaluation_sha256)
        if (
            record.proposal_sha256 != selected["proposal_sha256"]
            or record.mutation_sha256 != selected["mutation_sha256"]
            or record.generation_sha256 != selected["generation_sha256"]
            or record.evaluation_context_sha256
            != selected["evaluation_context_sha256"]
            or record.evaluator.implementation_revision != deployed_revision
            or record.evaluator.prompt_template_version
            != component.prompt_template_version
            or record.evaluator.prompt_template_sha256
            != component.prompt_template_sha256
            or record.model.provider != component.provider
            or record.model.identifier != component.model_identifier
            or record.model.revision != component.model_revision
            or dict(record.model_config) != dict(component.model_config)
            or record.assessment.recommendation != evaluated.recommendation
        ):
            raise PreReviewJobError("evaluation provenance does not match recipe")
    except (ArtifactLifecycleError, PreReviewJobError, OSError):
        return _block(ai_root, work, reason_code="evaluator_output_binding_mismatch")

    emit_evaluation_and_review_projections(
        ai_root,
        case_id=work.generation_id,
        evaluation_sha256=evaluated.evaluation_sha256,
    )
    output = {
        **selected,
        "evaluation_sha256": evaluated.evaluation_sha256,
        "recommendation": evaluated.recommendation,
    }
    completed = complete_attempt(
        ai_root,
        work.attempt_id,
        outcome="succeeded",
        output=output,
    )
    return _json_result(
        "pre-review-worker",
        stage="evaluation",
        status="completed",
        job_id=work.job_id,
        generation_id=work.generation_id,
        attempt_id=work.attempt_id,
        output=completed["output"],
        state=completed["state"],
    )


def _print_result(result: Mapping[str, object]) -> int:
    print(json.dumps(dict(result), ensure_ascii=False, sort_keys=True))
    return 0


def generator_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="obsidian-pre-review-generator-worker")
    parser.add_argument("--ai-root", type=Path, required=True)
    parser.add_argument(
        "--provider-base-url",
        "--openai-base-url",
        dest="provider_base_url",
        required=True,
    )
    parser.add_argument("--deployed-revision", required=True)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)
    parser.add_argument(
        "--max-awaiting-review",
        type=int,
        default=DEFAULT_MAX_AWAITING_REVIEW,
    )
    args = parser.parse_args(argv)
    try:
        return _print_result(
            run_generator_worker(
                args.ai_root,
                base_url=args.provider_base_url,
                deployed_revision=args.deployed_revision,
                timeout=args.timeout,
                max_attempts=args.max_attempts,
                max_awaiting_review=args.max_awaiting_review,
                api_key=os.environ.get("OPENAI_API_KEY"),
            )
        )
    except (ArtifactLifecycleError, PreReviewJobError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def validator_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="obsidian-pre-review-validator-worker")
    parser.add_argument("--ai-root", type=Path, required=True)
    parser.add_argument("--vault-root", type=Path, required=True)
    parser.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)
    args = parser.parse_args(argv)
    try:
        return _print_result(
            run_validator_worker(
                args.ai_root,
                args.vault_root,
                max_attempts=args.max_attempts,
            )
        )
    except (ArtifactLifecycleError, PreReviewJobError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def reader_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="obsidian-pre-review-reader-worker")
    parser.add_argument("--ai-root", type=Path, required=True)
    parser.add_argument("--vault-root", type=Path, required=True)
    parser.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)
    args = parser.parse_args(argv)
    try:
        return _print_result(
            run_reader_worker(
                args.ai_root,
                args.vault_root,
                max_attempts=args.max_attempts,
            )
        )
    except (ArtifactLifecycleError, PreReviewJobError, ProductionIOError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def evaluator_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="obsidian-pre-review-evaluator-worker")
    parser.add_argument("--ai-root", type=Path, required=True)
    parser.add_argument(
        "--provider-base-url",
        "--openai-base-url",
        dest="provider_base_url",
        required=True,
    )
    parser.add_argument("--deployed-revision", required=True)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)
    args = parser.parse_args(argv)
    try:
        return _print_result(
            run_evaluator_worker(
                args.ai_root,
                base_url=args.provider_base_url,
                deployed_revision=args.deployed_revision,
                timeout=args.timeout,
                max_attempts=args.max_attempts,
                api_key=os.environ.get("OPENAI_API_KEY"),
            )
        )
    except (ArtifactLifecycleError, PreReviewJobError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
