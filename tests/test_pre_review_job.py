from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from obsidian_automation.context_bundle import ContextBundle, store_context_bundle
from obsidian_automation.pre_review_job import (
    PreReviewJobError,
    complete_attempt,
    job_status,
    parse_recipe,
    regenerate_job,
    retry_generation,
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
        "prompt_template_version": "knowledge-note-generator-v1",
        "prompt_template_sha256": PROMPT_SHA,
        "provider": "ollama",
        "model_identifier": generator_model,
        "model_revision": MODEL_REV,
        "model_config": {
            "adapter_version": "ollama-chat-v1",
            "think": False,
            "options": {"temperature": 0},
        },
    }
    evaluator = {
        **component,
        "prompt_template_version": "knowledge-note-evaluator-v3",
        "model_identifier": "gemma3:12b-eval",
    }
    return {
        "record_version": 1,
        "pipeline": "knowledge-pre-review-v0",
        "generator": component,
        "validator": {"policy": "knowledge-note-v0"},
        "evaluation_context": {
            "selection_policy": "evaluation-context-v0",
            "top_k": 8,
        },
        "evaluator": evaluator,
    }


def _parsed_recipe(**kwargs):
    return parse_recipe(
        (json.dumps(_recipe(**kwargs), separators=(",", ":")) + "\n").encode()
    )


def test_recipe_is_bounded_and_excludes_execution_configuration() -> None:
    value = _recipe()
    parsed = _parsed_recipe()
    assert parsed.generator.provider == "ollama"
    assert parsed.validator_policy == "knowledge-note-v0"
    assert parsed.evaluation_context_top_k == 8

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
        root, str(generation_attempt["attempt_id"]), outcome="succeeded"
    )["state"] == "validating"

    validation_attempt = start_attempt(root, generation, "validation")
    assert complete_attempt(
        root, str(validation_attempt["attempt_id"]), outcome="succeeded"
    )["state"] == "building_evaluation_context"

    context_attempt = start_attempt(root, generation, "evaluation_context")
    assert complete_attempt(
        root, str(context_attempt["attempt_id"]), outcome="succeeded"
    )["state"] == "evaluating"

    evaluation_attempt = start_attempt(root, generation, "evaluation")
    assert complete_attempt(
        root, str(evaluation_attempt["attempt_id"]), outcome="succeeded"
    )["state"] == "awaiting_human_review"

    with pytest.raises(PreReviewJobError, match="requires generation state"):
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
    complete_attempt(root, str(first["attempt_id"]), outcome="succeeded")
    validation = start_attempt(root, generation, "validation")
    rejected = complete_attempt(
        root,
        str(validation["attempt_id"]),
        outcome="deterministic_reject",
        reason_code="policy_reject",
    )
    assert rejected["state"] == "deterministic_reject"

    with pytest.raises(PreReviewJobError, match="only retryable_failure"):
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
    with pytest.raises(Exception):
        submit_job(root, context_sha256="d" * 64, recipe=_parsed_recipe())

    path = root / "05-Context" / f"{context_sha}.context.json"
    path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(Exception):
        submit_job(root, context_sha256=context_sha, recipe=_parsed_recipe())


def test_database_is_private_and_schema_is_versioned(tmp_path: Path) -> None:
    root, context_sha = _state(tmp_path)
    submitted = submit_job(root, context_sha256=context_sha, recipe=_parsed_recipe())
    db = root / "02-Jobs" / "pre-review-jobs.sqlite3"

    assert db.is_file()
    assert db.stat().st_mode & 0o777 == 0o600

    conn = sqlite3.connect(db)
    try:
        assert conn.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()[0] == "1"
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM generations").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
    finally:
        conn.close()

    assert len(str(submitted["job_id"])) == 64
