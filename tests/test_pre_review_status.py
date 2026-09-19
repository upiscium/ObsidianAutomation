from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from obsidian_automation.context_bundle import ContextBundle, store_context_bundle
from obsidian_automation.evaluator_contract import (
    EVALUATOR_PROMPT_TEMPLATE_VERSION,
    prompt_template_sha256 as evaluator_prompt_sha256,
)
from obsidian_automation.generator_contract import (
    PROMPT_TEMPLATE_VERSION,
    prompt_template_sha256 as generator_prompt_sha256,
)
from obsidian_automation.ollama_evaluator import (
    ADAPTER_VERSION as EVALUATOR_ADAPTER_VERSION,
    EVALUATION_STRATEGY,
)
from obsidian_automation.ollama_generator import ADAPTER_VERSION as GENERATOR_ADAPTER_VERSION
from obsidian_automation.pre_review_job import (
    complete_attempt,
    parse_recipe,
    start_attempt,
    submit_job,
)
from obsidian_automation.pre_review_status import (
    PreReviewStatusError,
    build_status,
    load_status,
    parse_status,
    store_status,
)


REVISION = "a" * 40


def _recipe():
    value = {
        "record_version": 1,
        "pipeline": "knowledge-pre-review-v0",
        "generator": {
            "implementation_revision": REVISION,
            "prompt_template_version": PROMPT_TEMPLATE_VERSION,
            "prompt_template_sha256": generator_prompt_sha256(),
            "provider": "ollama",
            "model_identifier": "generator",
            "model_revision": "generator-revision",
            "model_config": {
                "adapter_version": GENERATOR_ADAPTER_VERSION,
                "think": False,
                "options": {"temperature": 0},
            },
        },
        "validator": {"policy": "knowledge-note-v0"},
        "evaluation_context": {
            "selection_policy": "bm25-topk-recall-v0",
            "top_k": 5,
        },
        "evaluator": {
            "implementation_revision": REVISION,
            "prompt_template_version": EVALUATOR_PROMPT_TEMPLATE_VERSION,
            "prompt_template_sha256": evaluator_prompt_sha256(),
            "provider": "ollama",
            "model_identifier": "evaluator",
            "model_revision": "evaluator-revision",
            "model_config": {
                "adapter_version": EVALUATOR_ADAPTER_VERSION,
                "think": False,
                "strategy": EVALUATION_STRATEGY,
                "options": {"temperature": 0},
            },
        },
    }
    return parse_recipe((json.dumps(value, separators=(",", ":")) + "\n").encode())


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "state"
    root.mkdir()
    (root / "05-Context").mkdir()
    return root


def _submit(root: Path, query: str, timestamp: str):
    context = ContextBundle(query=query, created_at=timestamp, sources=())
    context_sha, _ = store_context_bundle(root, context)
    return submit_job(root, context_sha256=context_sha, recipe=_recipe())


def _outputs(stage: str) -> dict[str, object]:
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


def _advance_to_review(root: Path, generation_id: str) -> None:
    for stage in ("generation", "validation", "evaluation_context", "evaluation"):
        attempt = start_attempt(root, generation_id, stage)
        complete_attempt(
            root,
            str(attempt["attempt_id"]),
            outcome="succeeded",
            output=_outputs(stage),
        )


def test_status_projection_contains_aggregate_metadata_only(tmp_path: Path) -> None:
    root = _root(tmp_path)
    submitted = _submit(root, "one", "2026-09-19T00:00:00Z")

    status = build_status(
        root,
        now=datetime(2026, 9, 19, 1, 0, tzinfo=timezone.utc),
    )
    data = status.to_json_bytes()
    value = json.loads(data)

    assert value["pipeline_health"] == "OK"
    assert value["current_jobs"] == 1
    assert value["states"]["queued"] == 1
    assert value["authority"] == "orchestration_status_projection_only"

    text = data.decode("utf-8")
    assert str(submitted["job_id"]) not in text
    assert str(submitted["context_sha256"]) not in text
    assert "job_id" not in text
    assert "context_sha256" not in text
    assert "recipe_sha256" not in text


def test_review_reminder_is_separate_from_pipeline_health(tmp_path: Path) -> None:
    root = _root(tmp_path)
    submitted = _submit(root, "review", "2026-09-17T00:00:00Z")
    _advance_to_review(root, str(submitted["generation_id"]))

    # Force an old Human Review wait timestamp without changing semantic state.
    import sqlite3

    db = root / "02-Orchestration" / "pre-review-jobs.sqlite3"
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "UPDATE generations SET updated_at = ? WHERE generation_id = ?",
            ("2026-09-17T00:00:00Z", str(submitted["generation_id"])),
        )
        conn.commit()
    finally:
        conn.close()

    status = build_status(
        root,
        now=datetime(2026, 9, 19, 0, 0, tzinfo=timezone.utc),
    )

    assert status.pipeline_health == "OK"
    assert status.review_reminder_due is True
    assert status.oldest_review_wait_seconds == 2 * 24 * 60 * 60


def test_retryable_is_warning_and_blocked_is_critical(tmp_path: Path) -> None:
    root = _root(tmp_path)
    first = _submit(root, "retry", "2026-09-19T00:00:00Z")
    second = _submit(root, "blocked", "2026-09-19T00:01:00Z")

    attempt = start_attempt(root, str(first["generation_id"]), "generation")
    complete_attempt(
        root,
        str(attempt["attempt_id"]),
        outcome="retryable_failure",
        reason_code="provider_timeout",
    )

    status = build_status(root)
    assert status.pipeline_health == "WARNING"
    assert status.reasons == ("retryable_failures",)

    attempt = start_attempt(root, str(second["generation_id"]), "generation")
    complete_attempt(
        root,
        str(attempt["attempt_id"]),
        outcome="blocked",
        reason_code="runtime_mismatch",
    )

    status = build_status(root)
    assert status.pipeline_health == "CRITICAL"
    assert "blocked_generations" in status.reasons


def test_backpressure_projection_matches_review_threshold(tmp_path: Path) -> None:
    root = _root(tmp_path)
    for index in range(2):
        submitted = _submit(
            root,
            f"review-{index}",
            f"2026-09-19T00:0{index}:00Z",
        )
        _advance_to_review(root, str(submitted["generation_id"]))

    status = build_status(root, backpressure_threshold=2)
    assert status.backpressure_active is True
    assert status.states["awaiting_human_review"] == 2


def test_store_and_load_status_round_trip(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _submit(root, "one", "2026-09-19T00:00:00Z")
    status = build_status(root)

    directory = tmp_path / "status"
    directory.mkdir()
    path = directory / "pre-review-status.json"

    store_status(path, status)
    assert load_status(path) == status
    assert parse_status(path.read_bytes()) == status
    assert path.stat().st_mode & 0o777 == 0o640


def test_status_store_rejects_symlink_destination(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _submit(root, "one", "2026-09-19T00:00:00Z")
    status = build_status(root)

    directory = tmp_path / "status"
    directory.mkdir()
    target = tmp_path / "outside"
    target.write_text("PRIVATE", encoding="utf-8")
    linked = directory / "pre-review-status.json"
    linked.symlink_to(target)

    with pytest.raises(PreReviewStatusError, match="must not be a symlink"):
        store_status(linked, status)

    assert target.read_text(encoding="utf-8") == "PRIVATE"
