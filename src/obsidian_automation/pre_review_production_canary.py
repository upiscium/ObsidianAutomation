from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import shutil
import stat
import sys
import time
from pathlib import Path
from typing import Mapping, Sequence

from .artifact_lifecycle import ArtifactLifecycleError
from .context_bundle import ContextBundle, store_context_bundle
from .evaluator_contract import (
    EVALUATOR_PROMPT_TEMPLATE_VERSION,
    prompt_template_sha256 as evaluator_prompt_sha256,
)
from .generator_contract import (
    PROMPT_TEMPLATE_VERSION,
    prompt_template_sha256 as generator_prompt_sha256,
)
from .openai_compatible import (
    DEFAULT_OPTIONS,
    JSONTransport,
    OpenAICompatibleProviderError,
    PROVIDER_NAME,
    identifier_revision,
)
from .openai_evaluator import (
    ADAPTER_VERSION as EVALUATOR_ADAPTER_VERSION,
    EVALUATION_STRATEGY,
)
from .openai_generator import ADAPTER_VERSION as GENERATOR_ADAPTER_VERSION
from .pre_review_job import (
    PreReviewJobError,
    claim_next_attempt,
    complete_attempt,
    job_status,
    parse_recipe,
    stage_output,
    submit_job,
)
from .pre_review_status import build_status
from .pre_review_worker import (
    run_evaluator_worker,
    run_generator_worker,
    run_reader_worker,
    run_validator_worker,
)
from .production_io import mirror_read_lock


PRODUCTION_ROOTS = (
    Path("/var/lib/obsidian-ai/state"),
    Path("/var/lib/obsidian-ai/vault"),
)
ALLOWED_SCRATCH_PARENTS = (Path("/tmp"), Path("/var/tmp"))


class PreReviewCanaryError(RuntimeError):
    """Raised when a disposable pre-review acceptance canary fails."""


def _safe_scratch_root(path: Path) -> Path:
    absolute = path.absolute()
    if absolute in ALLOWED_SCRATCH_PARENTS:
        raise PreReviewCanaryError("scratch root must be a child directory")
    if not any(absolute.is_relative_to(parent) for parent in ALLOWED_SCRATCH_PARENTS):
        raise PreReviewCanaryError("scratch root must be below /tmp or /var/tmp")
    for production in PRODUCTION_ROOTS:
        if absolute == production or absolute.is_relative_to(production):
            raise PreReviewCanaryError("scratch root must not overlap production state")
    if os.path.lexists(absolute):
        raise PreReviewCanaryError("scratch root must not already exist")
    parent = absolute.parent
    try:
        info = parent.lstat()
    except FileNotFoundError as exc:
        raise PreReviewCanaryError("scratch parent does not exist") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise PreReviewCanaryError("scratch parent is unsafe")
    absolute.mkdir(mode=0o700)
    return absolute


def _area(root: Path, name: str) -> tuple[Path, Path]:
    area = root / name
    vault = area / "vault"
    state = area / "state"
    (vault / "11-Knowledge").mkdir(parents=True)
    state.mkdir()
    for stage in (
        "00-Untrusted",
        "04-Index",
        "05-Context",
        "10-Validation",
        "12-Evaluation-Request",
        "14-Evaluation-Context",
        "15-Evaluation",
        "20-Review",
        "25-Execution",
        "27-Transport",
        "30-Receipts",
    ):
        (state / stage).mkdir()
    (state / "24-Locks" / "read-view").mkdir(parents=True)
    return vault, state


def _recipe(
    *,
    deployed_revision: str,
    generator_identifier: str,
    evaluator_identifier: str,
):
    value = {
        "record_version": 1,
        "pipeline": "knowledge-pre-review-v0",
        "generator": {
            "implementation_revision": deployed_revision,
            "prompt_template_version": PROMPT_TEMPLATE_VERSION,
            "prompt_template_sha256": generator_prompt_sha256(),
            "provider": PROVIDER_NAME,
            "model_identifier": generator_identifier,
            "model_revision": identifier_revision(generator_identifier),
            "model_config": {
                "adapter_version": GENERATOR_ADAPTER_VERSION,
                "identity_binding": "identifier-only",
                "options": dict(DEFAULT_OPTIONS),
            },
        },
        "validator": {"policy": "knowledge-note-v0"},
        "evaluation_context": {
            "selection_policy": "bm25-topk-recall-v0",
            "top_k": 5,
        },
        "evaluator": {
            "implementation_revision": deployed_revision,
            "prompt_template_version": EVALUATOR_PROMPT_TEMPLATE_VERSION,
            "prompt_template_sha256": evaluator_prompt_sha256(),
            "provider": PROVIDER_NAME,
            "model_identifier": evaluator_identifier,
            "model_revision": identifier_revision(evaluator_identifier),
            "model_config": {
                "adapter_version": EVALUATOR_ADAPTER_VERSION,
                "identity_binding": "identifier-only",
                "strategy": EVALUATION_STRATEGY,
                "options": {"temperature": 0},
            },
        },
    }
    return parse_recipe(
        (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    )


def _context(state: Path, query: str) -> str:
    digest, _ = store_context_bundle(
        state,
        ContextBundle(
            query=query,
            created_at="2026-09-19T00:00:00Z",
            sources=(),
        ),
    )
    return digest


def _synthetic_output(stage: str) -> dict[str, object]:
    generation = {
        "proposal_sha256": "1" * 64,
        "generation_sha256": "2" * 64,
    }
    validation = {
        **generation,
        "mutation_sha256": "3" * 64,
        "request_sha256": "4" * 64,
    }
    evaluation_context = {
        **validation,
        "index_sha256": "5" * 64,
        "evaluation_context_sha256": "6" * 64,
    }
    evaluation = {
        **evaluation_context,
        "evaluation_sha256": "7" * 64,
        "recommendation": "manual_review",
    }
    return {
        "generation": generation,
        "validation": validation,
        "evaluation_context": evaluation_context,
        "evaluation": evaluation,
    }[stage]


def _assert_post_review_stages_empty(state: Path) -> None:
    for stage in ("20-Review", "25-Execution", "27-Transport", "30-Receipts"):
        if any((state / stage).iterdir()):
            raise PreReviewCanaryError(
                f"canary unexpectedly wrote post-review stage: {stage}"
            )


def _run_live_pipeline(
    root: Path,
    *,
    generator_base_url: str,
    evaluator_base_url: str,
    generator_model: str,
    evaluator_model: str,
    deployed_revision: str,
    generator_api_key: str | None,
    evaluator_api_key: str | None,
    transport: JSONTransport | None,
) -> dict[str, object]:
    vault, state = _area(root, "live")
    recipe = _recipe(
        deployed_revision=deployed_revision,
        generator_identifier=generator_model,
        evaluator_identifier=evaluator_model,
    )
    context_sha = _context(
        state,
        (
            "Create one concise Obsidian Knowledge Note explaining that this is a "
            "disposable pre-review production acceptance canary. State clearly "
            "that it has no canonical write authority."
        ),
    )
    first = submit_job(state, context_sha256=context_sha, recipe=recipe)
    second = submit_job(state, context_sha256=context_sha, recipe=recipe)
    if first["created"] is not True or second["created"] is not False:
        raise PreReviewCanaryError("duplicate submit did not resolve to one logical job")
    if first["job_id"] != second["job_id"]:
        raise PreReviewCanaryError("duplicate submit changed logical job identity")

    generated = run_generator_worker(
        state,
        base_url=generator_base_url,
        deployed_revision=deployed_revision,
        api_key=generator_api_key,
        transport=transport,
    )
    validated = run_validator_worker(state, vault)
    read = run_reader_worker(state, vault)
    evaluated = run_evaluator_worker(
        state,
        base_url=evaluator_base_url,
        deployed_revision=deployed_revision,
        api_key=evaluator_api_key,
        transport=transport,
    )
    states = [
        generated.get("state"),
        validated.get("state"),
        read.get("state"),
        evaluated.get("state"),
    ]
    if states != [
        "validating",
        "building_evaluation_context",
        "evaluating",
        "awaiting_human_review",
    ]:
        raise PreReviewCanaryError(f"live canary state progression is invalid: {states}")

    status = job_status(state, str(first["job_id"]))
    if status["current_generation"]["state"] != "awaiting_human_review":
        raise PreReviewCanaryError("live canary did not stop at Human Review")
    projection = build_status(state)
    if projection.states["awaiting_human_review"] != 1:
        raise PreReviewCanaryError("live canary status projection is inconsistent")
    _assert_post_review_stages_empty(state)
    return {
        "duplicate_submit": "passed",
        "final_state": "awaiting_human_review",
        "recommendation": (
            stage_output(
                state,
                str(first["generation_id"]),
                "evaluation",
            )
            or {}
        ).get("recommendation"),
        "post_review_writes": 0,
    }


def _run_crash_resume(root: Path, recipe) -> dict[str, object]:
    _vault, state = _area(root, "crash-resume")
    context_sha = _context(state, "crash resume control-plane canary")
    submitted = submit_job(state, context_sha256=context_sha, recipe=recipe)
    generation_id = str(submitted["generation_id"])

    recovered: dict[str, int] = {}
    for stage in ("generation", "validation", "evaluation_context", "evaluation"):
        orphan = claim_next_attempt(state, stage, recover_running=False)
        if orphan is None:
            raise PreReviewCanaryError(f"cannot create orphan attempt for {stage}")
        resumed = claim_next_attempt(
            state,
            stage,
            max_attempts=3,
            recover_running=True,
        )
        if resumed is None or resumed.attempt_index != orphan.attempt_index + 1:
            raise PreReviewCanaryError(f"crash recovery did not allocate a new {stage} attempt")
        if resumed.attempt_id == orphan.attempt_id:
            raise PreReviewCanaryError("crash recovery reused attempt identity")
        if stage_output(state, generation_id, stage) is not None:
            raise PreReviewCanaryError("crash recovery guessed an unselected artifact")
        complete_attempt(
            state,
            resumed.attempt_id,
            outcome="succeeded",
            output=_synthetic_output(stage),
        )
        recovered[stage] = resumed.attempt_index

    if job_status(state, str(submitted["job_id"]))["current_generation"]["state"] != "awaiting_human_review":
        raise PreReviewCanaryError("crash-resume canary did not reach Human Review")
    return recovered


def _run_provider_failure(
    root: Path,
    *,
    recipe,
    generator_base_url: str,
    deployed_revision: str,
    generator_api_key: str | None,
) -> dict[str, object]:
    _vault, state = _area(root, "provider-failure")
    context_sha = _context(state, "provider failure canary")
    submitted = submit_job(state, context_sha256=context_sha, recipe=recipe)

    def fail_transport(*_args, **_kwargs):
        raise OpenAICompatibleProviderError("injected canary provider failure")

    attempts = 0
    for _ in range(3):
        result = run_generator_worker(
            state,
            base_url=generator_base_url,
            deployed_revision=deployed_revision,
            max_attempts=3,
            api_key=generator_api_key,
            transport=fail_transport,
        )
        if result.get("status") != "retryable_failure":
            raise PreReviewCanaryError("provider failure was not classified retryable")
        attempts += 1
    idle = run_generator_worker(
        state,
        base_url=generator_base_url,
        deployed_revision=deployed_revision,
        max_attempts=3,
        api_key=generator_api_key,
        transport=fail_transport,
    )
    if idle.get("status") != "idle":
        raise PreReviewCanaryError("provider retry limit did not stop automatic claims")
    state_name = job_status(state, str(submitted["job_id"]))["current_generation"]["state"]
    if state_name != "retry_exhausted":
        raise PreReviewCanaryError("provider retry limit did not enter retry_exhausted")
    return {"attempts": attempts, "final_state": state_name}


def _run_backpressure(root: Path, recipe) -> dict[str, object]:
    _vault, state = _area(root, "backpressure")
    for index in range(8):
        context_sha = _context(state, f"review wait {index}")
        submitted = submit_job(state, context_sha256=context_sha, recipe=recipe)
        generation_id = str(submitted["generation_id"])
        for stage in ("generation", "validation", "evaluation_context", "evaluation"):
            attempt = claim_next_attempt(state, stage, recover_running=True)
            if attempt is None or attempt.generation_id != generation_id:
                raise PreReviewCanaryError("cannot advance backpressure fixture")
            complete_attempt(
                state,
                attempt.attempt_id,
                outcome="succeeded",
                output=_synthetic_output(stage),
            )

    queued_sha = _context(state, "must remain queued under backpressure")
    submit_job(state, context_sha256=queued_sha, recipe=recipe)
    claim = claim_next_attempt(
        state,
        "generation",
        max_awaiting_review=8,
    )
    if claim is not None:
        raise PreReviewCanaryError("Human Review backpressure did not stop Generator")
    status = build_status(state)
    if not status.backpressure_active:
        raise PreReviewCanaryError("backpressure status projection is not active")
    return {
        "awaiting_human_review": status.states["awaiting_human_review"],
        "backpressure_active": True,
    }


def _acquire_marker(ai_root: str, marker: str) -> None:
    with mirror_read_lock(Path(ai_root)):
        Path(marker).write_text("acquired\n", encoding="utf-8")


def _run_mirror_conflict(root: Path) -> dict[str, object]:
    _vault, state = _area(root, "mirror-conflict")
    marker = root / "mirror-conflict" / "child-acquired"
    context = multiprocessing.get_context("fork")
    with mirror_read_lock(state):
        process = context.Process(
            target=_acquire_marker,
            args=(str(state), str(marker)),
        )
        process.start()
        time.sleep(0.25)
        if marker.exists():
            process.terminate()
            process.join(timeout=2)
            raise PreReviewCanaryError("mirror read-view lock did not serialize conflict")
    process.join(timeout=5)
    if process.exitcode != 0 or not marker.is_file():
        raise PreReviewCanaryError("mirror read-view waiter did not resume after release")
    return {"serialized": True}


def run_canary(
    *,
    scratch_root: Path,
    generator_base_url: str,
    evaluator_base_url: str,
    generator_model: str,
    evaluator_model: str,
    deployed_revision: str,
    generator_api_key: str | None = None,
    evaluator_api_key: str | None = None,
    transport: JSONTransport | None = None,
) -> dict[str, object]:
    if len(deployed_revision) not in {40, 64} or any(
        ch not in "0123456789abcdef" for ch in deployed_revision
    ):
        raise PreReviewCanaryError("deployed revision must be a full lowercase Git digest")
    root = _safe_scratch_root(scratch_root)
    try:
        recipe = _recipe(
            deployed_revision=deployed_revision,
            generator_identifier=generator_model,
            evaluator_identifier=evaluator_model,
        )

        result = {
            "record_version": 1,
            "event": "pre-review-production-canary",
            "scratch_root": str(root),
            "live_pipeline": _run_live_pipeline(
                root,
                generator_base_url=generator_base_url,
                evaluator_base_url=evaluator_base_url,
                generator_model=generator_model,
                evaluator_model=evaluator_model,
                deployed_revision=deployed_revision,
                generator_api_key=generator_api_key,
                evaluator_api_key=evaluator_api_key,
                transport=transport,
            ),
            "crash_resume": _run_crash_resume(root, recipe),
            "provider_failure": _run_provider_failure(
                root,
                recipe=recipe,
                generator_base_url=generator_base_url,
                deployed_revision=deployed_revision,
                generator_api_key=generator_api_key,
            ),
            "backpressure": _run_backpressure(root, recipe),
            "mirror_conflict": _run_mirror_conflict(root),
            "canonical_write_connected": False,
            "status": "passed",
        }
        return result
    except Exception:
        # Preserve scratch evidence on failure for operator inspection.
        raise


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="obsidian-pre-review-production-canary")
    parser.add_argument("--scratch-root", type=Path, required=True)
    parser.add_argument("--generator-base-url", required=True)
    parser.add_argument("--evaluator-base-url", required=True)
    parser.add_argument("--generator-model", required=True)
    parser.add_argument("--evaluator-model", required=True)
    parser.add_argument("--deployed-revision", required=True)
    parser.add_argument("--cleanup-on-success", action="store_true")
    args = parser.parse_args(argv)

    try:
        result = run_canary(
            scratch_root=args.scratch_root,
            generator_base_url=args.generator_base_url,
            evaluator_base_url=args.evaluator_base_url,
            generator_model=args.generator_model,
            evaluator_model=args.evaluator_model,
            deployed_revision=args.deployed_revision,
            generator_api_key=os.environ.get("OPENAI_GENERATOR_API_KEY"),
            evaluator_api_key=os.environ.get("OPENAI_EVALUATOR_API_KEY"),
        )
    except (ArtifactLifecycleError, PreReviewJobError, OpenAICompatibleProviderError, PreReviewCanaryError, OSError) as exc:
        print(
            json.dumps(
                {
                    "event": "pre-review-production-canary",
                    "status": "failed",
                    "message": str(exc),
                    "scratch_root": str(args.scratch_root),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1

    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if args.cleanup_on_success:
        shutil.rmtree(args.scratch_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
