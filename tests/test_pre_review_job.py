from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from obsidian_automation.artifact_lifecycle import ArtifactLifecycleError
from obsidian_automation.context_bundle import ContextBundle, store_context_bundle
from obsidian_automation.pre_review_job import (
    PreReviewJobError,
    claim_next_attempt,
    complete_attempt,
    job_status,
    parse_recipe,
    regenerate_job,
    retry_generation,
    stage_output,
    start_attempt,
    submit_job,
)


REV = "a" * 40
PROMPT_SHA = "b" * 64
MODEL_REV = "sha256:" + "c" * 64


def _state(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "state"
    root.mkdir()
    (root / "05-Context").mkdir()
    bundle = ContextBundle(
        query="Build one Knowledge note",
        created_at="2026-09-19T00:00:00Z",
        sources=(),
    )
    context_sha, _ = store_context_bundle(root, bundle)
    return root, context_sha


def _recipe(*, generator_model: str = "gemma3:12b") -> dict[str, object]:
    component = {
        "implementation_revision": REV,
        "prompt_template_version": "knowledge-note-generator-v0",
        "prompt_template_sha256": PROMPT_SHA,
        "provider": "ollama",
        "model_identifier": generator_model,
        "model_revision": MODEL_REV,
        "model_config": {
            "adapter_version": "ollama-chat-structured-v0",
            "think": False,
            "options": {"temperature": 0},
        },
    }
    evaluator = {
        **component,
        "prompt_template_version": "knowledge-note-evaluator-v3",
        "model_identifier": "gemma3:12b-eval",
        "model_config": {
            "adapter_version": "ollama-evaluator-chat-structured-v2",
            "think": False,
            "strategy": "groundedness-plus-pairwise-candidates-v0",
            "options": {"temperature": 0},
        },
    }
    return {
        "record_version": 1,
        "pipeline": "knowledge-pre-review-v0",
        "generator": component,
        "validator": {"policy": "knowledge-note-v0"},
        "evaluation_context": {
            "selection_policy": "bm25-topk-recall-v0",
            "top_k": 5,
        },
        "evaluator": evaluator,
    }


def _parsed_recipe(**kwargs):
    return parse_recipe(
        (json.dumps(_recipe(**kwargs), separators=(",", ":")) + "\n").encode()
    )


def _stage_output(stage: str) -> dict[str, object]:
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


def test_recipe_is_bounded_and_excludes_execution_configuration() -> None:
    value = _recipe()
    parsed = _parsed_recipe()
    assert parsed.generator.provider == "ollama"
    assert parsed.validator_policy == "knowledge-note-v0"
    assert parsed.evaluation_context_top_k == 5

    value["command"] = ["sh", "-c", "danger"]
    with pytest.raises(PreReviewJobError, match="properties do not match contract"):
        parse_recipe((json.dumps(value) + "\n").encode())

    value = _recipe()
    value["generator"]["endpoint"] = "https://private.example"
    with pytest.raises(PreReviewJobError, match="generator properties"):
        parse_recipe((json.dumps(value) + "\n").encode())


def test_recipe_rejects_unknown_provider_and_unbounded_top_k() -> None:
    value = _recipe()
    value["generator"]["provider"] = "shell"
    with pytest.raises(PreReviewJobError, match="provider must be ollama"):
        parse_recipe((json.dumps(value) + "\n").encode())

    value = _recipe()
    value["generator"]["model_config"]["endpoint"] = "https://private.example"
    with pytest.raises(PreReviewJobError, match="model_config properties"):
        parse_recipe((json.dumps(value) + "\n").encode())

    value = _recipe()
    value["generator"]["model_config"]["think"] = True
    with pytest.raises(PreReviewJobError, match="think must be false"):
        parse_recipe((json.dumps(value) + "\n").encode())

    value = _recipe()
    value["evaluation_context"]["top_k"] = 1000
    with pytest.raises(PreReviewJobError, match="top_k"):
        parse_recipe((json.dumps(value) + "\n").encode())


def test_submit_is_idempotent_for_same_context_and_recipe(tmp_path: Path) -> None:
    root, context_sha = _state(tmp_path)
    recipe = _parsed_recipe()

    first = submit_job(root, context_sha256=context_sha, recipe=recipe)
    second = submit_job(root, context_sha256=context_sha, recipe=recipe)

    assert first["created"] is True
    assert second["created"] is False
    assert first["job_id"] == second["job_id"]
    assert first["generation_id"] == second["generation_id"]
    assert first["context_sha256"] == context_sha
    assert first["recipe_sha256"] == second["recipe_sha256"]
    assert Path(first["recipe_path"]).is_file()

    status = job_status(root, str(first["job_id"]))
    assert status["authority"] == "orchestration_metadata_only"
    assert status["generation_count"] == 1
    assert status["current_generation"]["state"] == "queued"


def test_recipe_change_creates_distinct_job(tmp_path: Path) -> None:
    root, context_sha = _state(tmp_path)
    first = submit_job(root, context_sha256=context_sha, recipe=_parsed_recipe())
    second = submit_job(
        root,
        context_sha256=context_sha,
        recipe=_parsed_recipe(generator_model="qwen3:14b"),
    )
    assert first["job_id"] != second["job_id"]
    assert first["generation_id"] != second["generation_id"]


def test_regenerate_is_explicit_and_creates_next_generation(tmp_path: Path) -> None:
    root, context_sha = _state(tmp_path)
    first = submit_job(root, context_sha256=context_sha, recipe=_parsed_recipe())
    generation = str(first["generation_id"])

    for stage in ("generation", "validation", "evaluation_context", "evaluation"):
        attempt = start_attempt(root, generation, stage)
        complete_attempt(
            root,
            str(attempt["attempt_id"]),
            outcome="succeeded",
            output=_stage_output(stage),
        )

    regenerated = regenerate_job(root, str(first["job_id"]))
    assert regenerated["generation_index"] == 2
    assert regenerated["generation_id"] != first["generation_id"]
    assert regenerated["state"] == "queued"

    status = job_status(root, str(first["job_id"]))
    assert status["generation_count"] == 2
    assert status["current_generation"]["generation_id"] == regenerated["generation_id"]


def test_generation_attempts_advance_state_machine_transactionally(tmp_path: Path) -> None:
    root, context_sha = _state(tmp_path)
    submitted = submit_job(root, context_sha256=context_sha, recipe=_parsed_recipe())
    generation = str(submitted["generation_id"])

    with pytest.raises(PreReviewJobError, match="requires generation state"):
        start_attempt(root, generation, "validation")

    generation_attempt = start_attempt(root, generation, "generation")
    with pytest.raises(PreReviewJobError, match="already has a running attempt"):
        start_attempt(root, generation, "generation")
    assert complete_attempt(
        root,
        str(generation_attempt["attempt_id"]),
        outcome="succeeded",
        output=_stage_output("generation"),
    )["state"] == "validating"

    validation_attempt = start_attempt(root, generation, "validation")
    assert complete_attempt(
        root,
        str(validation_attempt["attempt_id"]),
        outcome="succeeded",
        output=_stage_output("validation"),
    )["state"] == "building_evaluation_context"

    context_attempt = start_attempt(root, generation, "evaluation_context")
    assert complete_attempt(
        root,
        str(context_attempt["attempt_id"]),
        outcome="succeeded",
        output=_stage_output("evaluation_context"),
    )["state"] == "evaluating"

    evaluation_attempt = start_attempt(root, generation, "evaluation")
    assert complete_attempt(
        root,
        str(evaluation_attempt["attempt_id"]),
        outcome="succeeded",
        output=_stage_output("evaluation"),
    )["state"] == "awaiting_human_review"

    with pytest.raises(PreReviewJobError, match="selected successful output"):
        start_attempt(root, generation, "generation")


def test_retryable_failure_retries_same_generation_with_new_attempt(tmp_path: Path) -> None:
    root, context_sha = _state(tmp_path)
    submitted = submit_job(root, context_sha256=context_sha, recipe=_parsed_recipe())
    generation = str(submitted["generation_id"])

    first = start_attempt(root, generation, "generation")
    failed = complete_attempt(
        root,
        str(first["attempt_id"]),
        outcome="retryable_failure",
        reason_code="provider_timeout",
    )
    assert failed["state"] == "retryable_failure"

    retry = retry_generation(root, generation)
    assert retry["state"] == "queued"

    second = start_attempt(root, generation, "generation")
    assert second["attempt_id"] != first["attempt_id"]
    assert second["attempt_index"] == 2


def test_deterministic_reject_requires_explicit_new_generation(tmp_path: Path) -> None:
    root, context_sha = _state(tmp_path)
    submitted = submit_job(root, context_sha256=context_sha, recipe=_parsed_recipe())
    generation = str(submitted["generation_id"])

    first = start_attempt(root, generation, "generation")
    complete_attempt(
        root,
        str(first["attempt_id"]),
        outcome="succeeded",
        output=_stage_output("generation"),
    )
    validation = start_attempt(root, generation, "validation")
    rejected = complete_attempt(
        root,
        str(validation["attempt_id"]),
        outcome="deterministic_reject",
        reason_code="policy_reject",
    )
    assert rejected["state"] == "deterministic_reject"

    with pytest.raises(PreReviewJobError, match="only retryable, exhausted, or blocked"):
        retry_generation(root, generation)

    regenerated = regenerate_job(root, str(submitted["job_id"]))
    assert regenerated["generation_index"] == 2
    assert regenerated["state"] == "queued"


def test_regenerate_rejects_active_generation(tmp_path: Path) -> None:
    root, context_sha = _state(tmp_path)
    submitted = submit_job(root, context_sha256=context_sha, recipe=_parsed_recipe())

    with pytest.raises(PreReviewJobError, match="requires the current generation to be stopped"):
        regenerate_job(root, str(submitted["job_id"]))


def test_attempt_identity_is_separate_and_stage_bound(tmp_path: Path) -> None:
    root, context_sha = _state(tmp_path)
    submitted = submit_job(root, context_sha256=context_sha, recipe=_parsed_recipe())
    generation = str(submitted["generation_id"])

    first = start_attempt(root, generation, "generation")
    assert first["attempt_id"] != generation
    assert first["stage"] == "generation"
    assert first["attempt_index"] == 1

    with pytest.raises(PreReviewJobError, match="attempt stage is invalid"):
        start_attempt(root, generation, "shell")


def test_submit_rejects_missing_or_tampered_context(tmp_path: Path) -> None:
    root, context_sha = _state(tmp_path)
    with pytest.raises(ArtifactLifecycleError):
        submit_job(root, context_sha256="d" * 64, recipe=_parsed_recipe())

    path = root / "05-Context" / f"{context_sha}.context.json"
    path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(ArtifactLifecycleError):
        submit_job(root, context_sha256=context_sha, recipe=_parsed_recipe())


def test_database_is_private_and_schema_is_versioned(tmp_path: Path) -> None:
    root, context_sha = _state(tmp_path)
    submitted = submit_job(root, context_sha256=context_sha, recipe=_parsed_recipe())
    db = root / "02-Orchestration" / "pre-review-jobs.sqlite3"

    assert db.is_file()
    assert db.stat().st_mode & 0o777 == 0o660

    conn = sqlite3.connect(db)
    try:
        assert conn.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()[0] == "1"
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM generations").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM stage_outputs").fetchone()[0] == 0
    finally:
        conn.close()

    assert len(str(submitted["job_id"])) == 64


def test_status_on_uninitialized_root_is_non_mutating(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()

    with pytest.raises(ArtifactLifecycleError):
        job_status(root, "e" * 64)

    assert not (root / "02-Orchestration").exists()


def test_completed_attempt_cannot_be_reused(tmp_path: Path) -> None:
    root, context_sha = _state(tmp_path)
    submitted = submit_job(root, context_sha256=context_sha, recipe=_parsed_recipe())
    generation = str(submitted["generation_id"])
    attempt = start_attempt(root, generation, "generation")

    complete_attempt(
        root,
        str(attempt["attempt_id"]),
        outcome="succeeded",
        output=_stage_output("generation"),
    )
    with pytest.raises(PreReviewJobError, match="already completed"):
        complete_attempt(
            root,
            str(attempt["attempt_id"]),
            outcome="succeeded",
            output=_stage_output("generation"),
        )


def test_public_cli_and_json_schemas_are_pinned() -> None:
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    assert (
        'obsidian-pre-review-job = "obsidian_automation.pre_review_job:main"'
        in pyproject
    )

    recipe_schema = json.loads(
        Path("schemas/pre-review-recipe-v0.schema.json").read_text(encoding="utf-8")
    )
    assert recipe_schema["properties"]["pipeline"]["const"] == "knowledge-pre-review-v0"
    assert recipe_schema["additionalProperties"] is False

    status_schema = json.loads(
        Path("schemas/pre-review-job-status-v0.schema.json").read_text(encoding="utf-8")
    )
    assert (
        status_schema["properties"]["authority"]["const"]
        == "orchestration_metadata_only"
    )
    assert status_schema["additionalProperties"] is False


def test_database_symlink_is_rejected_without_touching_target(tmp_path: Path) -> None:
    root, context_sha = _state(tmp_path)
    jobs = root / "02-Orchestration"
    jobs.mkdir()
    (jobs / "recipes").mkdir()
    outside = tmp_path / "outside.db"
    outside.write_bytes(b"PRIVATE-CANARY")
    (jobs / "pre-review-jobs.sqlite3").symlink_to(outside)

    with pytest.raises(PreReviewJobError, match="not a regular file"):
        submit_job(root, context_sha256=context_sha, recipe=_parsed_recipe())

    assert outside.read_bytes() == b"PRIVATE-CANARY"


def test_success_requires_selected_stage_output(tmp_path: Path) -> None:
    root, context_sha = _state(tmp_path)
    submitted = submit_job(root, context_sha256=context_sha, recipe=_parsed_recipe())
    attempt = start_attempt(root, str(submitted["generation_id"]), "generation")

    with pytest.raises(PreReviewJobError, match="requires selected stage output"):
        complete_attempt(root, str(attempt["attempt_id"]), outcome="succeeded")


def test_stage_output_chain_rejects_cross_bound_output(tmp_path: Path) -> None:
    root, context_sha = _state(tmp_path)
    submitted = submit_job(root, context_sha256=context_sha, recipe=_parsed_recipe())
    generation = str(submitted["generation_id"])

    first = start_attempt(root, generation, "generation")
    complete_attempt(
        root,
        str(first["attempt_id"]),
        outcome="succeeded",
        output=_stage_output("generation"),
    )
    validation = start_attempt(root, generation, "validation")
    bad = _stage_output("validation")
    bad["proposal_sha256"] = "9" * 64
    with pytest.raises(PreReviewJobError, match="not bound"):
        complete_attempt(
            root,
            str(validation["attempt_id"]),
            outcome="succeeded",
            output=bad,
        )


def test_stage_output_is_selected_and_read_back_exactly(tmp_path: Path) -> None:
    root, context_sha = _state(tmp_path)
    submitted = submit_job(root, context_sha256=context_sha, recipe=_parsed_recipe())
    generation = str(submitted["generation_id"])
    attempt = start_attempt(root, generation, "generation")
    expected = _stage_output("generation")
    complete_attempt(
        root,
        str(attempt["attempt_id"]),
        outcome="succeeded",
        output=expected,
    )
    assert stage_output(root, generation, "generation") == expected


def test_retry_restores_failed_stage_not_generator(tmp_path: Path) -> None:
    root, context_sha = _state(tmp_path)
    submitted = submit_job(root, context_sha256=context_sha, recipe=_parsed_recipe())
    generation = str(submitted["generation_id"])

    generation_attempt = start_attempt(root, generation, "generation")
    complete_attempt(
        root,
        str(generation_attempt["attempt_id"]),
        outcome="succeeded",
        output=_stage_output("generation"),
    )
    validation = start_attempt(root, generation, "validation")
    complete_attempt(
        root,
        str(validation["attempt_id"]),
        outcome="retryable_failure",
        reason_code="temporary_io",
    )

    retry = retry_generation(root, generation)
    assert retry["stage"] == "validation"
    assert retry["state"] == "validating"
    assert stage_output(root, generation, "generation") == _stage_output("generation")


def test_claim_retries_same_stage_and_exhausts_after_limit(tmp_path: Path) -> None:
    root, context_sha = _state(tmp_path)
    submit_job(root, context_sha256=context_sha, recipe=_parsed_recipe())

    for attempt_index in range(1, 4):
        work = claim_next_attempt(
            root,
            "generation",
            max_attempts=3,
            recover_running=True,
        )
        assert work is not None
        assert work.attempt_index == attempt_index
        complete_attempt(
            root,
            work.attempt_id,
            outcome="retryable_failure",
            reason_code="provider_timeout",
        )

    assert claim_next_attempt(
        root,
        "generation",
        max_attempts=3,
        recover_running=True,
    ) is None

    status = job_status(root, _job_id_from_db(root))
    assert status["current_generation"]["state"] == "retry_exhausted"


def _job_id_from_db(root: Path) -> str:
    conn = sqlite3.connect(root / "02-Orchestration" / "pre-review-jobs.sqlite3")
    try:
        return str(conn.execute("SELECT job_id FROM jobs LIMIT 1").fetchone()[0])
    finally:
        conn.close()


def test_claim_recovers_orphaned_running_attempt_without_guessing_output(
    tmp_path: Path,
) -> None:
    root, context_sha = _state(tmp_path)
    submitted = submit_job(root, context_sha256=context_sha, recipe=_parsed_recipe())
    first = claim_next_attempt(root, "generation", recover_running=False)
    assert first is not None
    assert stage_output(root, str(submitted["generation_id"]), "generation") is None

    second = claim_next_attempt(
        root,
        "generation",
        max_attempts=3,
        recover_running=True,
    )
    assert second is not None
    assert second.attempt_index == 2
    assert second.attempt_id != first.attempt_id
    assert stage_output(root, str(submitted["generation_id"]), "generation") is None


def test_generator_claim_stops_at_human_review_backpressure(tmp_path: Path) -> None:
    root, context_sha = _state(tmp_path)
    first = submit_job(root, context_sha256=context_sha, recipe=_parsed_recipe())
    generation = str(first["generation_id"])
    for stage in ("generation", "validation", "evaluation_context", "evaluation"):
        attempt = start_attempt(root, generation, stage)
        complete_attempt(
            root,
            str(attempt["attempt_id"]),
            outcome="succeeded",
            output=_stage_output(stage),
        )

    other_bundle = ContextBundle(
        query="Another note",
        created_at="2026-09-19T00:01:00Z",
        sources=(),
    )
    other_sha, _ = store_context_bundle(root, other_bundle)
    submit_job(root, context_sha256=other_sha, recipe=_parsed_recipe())

    assert claim_next_attempt(
        root,
        "generation",
        max_awaiting_review=1,
    ) is None
