from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import obsidian_automation.knowledge_production as production
from obsidian_automation.artifact_lifecycle import (
    _canonical_json_bytes,
    ensure_artifact_layout,
    store_review_record,
)
from obsidian_automation.context_bundle import ContextBundle, store_context_bundle
from obsidian_automation.pre_review_job import (
    complete_attempt,
    job_status,
    parse_recipe,
    start_attempt,
    submit_job,
)
from obsidian_automation.pre_review_reconcile import reconcile_post_review
from obsidian_automation.production_orchestrator import ProductionOrchestrationError


REV = "a" * 40
PROMPT_SHA = "b" * 64
MUTATION = "3" * 64
EVALUATION = "7" * 64


def _recipe():
    value = {
        "record_version": 1,
        "pipeline": "knowledge-pre-review-v0",
        "generator": {
            "implementation_revision": REV,
            "prompt_template_version": "knowledge-note-generator-v0",
            "prompt_template_sha256": PROMPT_SHA,
            "provider": "openai-compatible",
            "model_identifier": "generator",
            "model_revision": "identifier:generator",
            "model_config": {
                "adapter_version": "openai-chat-completions-json-schema-v1",
                "identity_binding": "identifier-only",
                "options": {"temperature": 0},
            },
        },
        "validator": {"policy": "knowledge-note-v0"},
        "evaluation_context": {
            "selection_policy": "bm25-topk-recall-v0",
            "top_k": 5,
        },
        "evaluator": {
            "implementation_revision": REV,
            "prompt_template_version": "knowledge-note-evaluator-v3",
            "prompt_template_sha256": PROMPT_SHA,
            "provider": "openai-compatible",
            "model_identifier": "evaluator",
            "model_revision": "identifier:evaluator",
            "model_config": {
                "adapter_version": "openai-evaluator-chat-completions-json-schema-v1",
                "identity_binding": "identifier-only",
                "strategy": "groundedness-plus-pairwise-candidates-v0",
                "options": {"temperature": 0},
            },
        },
    }
    return parse_recipe(
        (json.dumps(value, separators=(",", ":")) + "\n").encode()
    )


def _stage_output(stage: str):
    generation = {
        "proposal_sha256": "1" * 64,
        "generation_sha256": "2" * 64,
    }
    validation = {
        **generation,
        "mutation_sha256": MUTATION,
        "request_sha256": "4" * 64,
    }
    context = {
        **validation,
        "index_sha256": "5" * 64,
        "evaluation_context_sha256": "6" * 64,
    }
    evaluation = {
        **context,
        "evaluation_sha256": EVALUATION,
        "recommendation": "proceed",
    }
    return {
        "generation": generation,
        "validation": validation,
        "evaluation_context": context,
        "evaluation": evaluation,
    }[stage]


def _awaiting(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir()
    ensure_artifact_layout(state)
    (state / "05-Context").mkdir(exist_ok=True)

    context_sha, _ = store_context_bundle(
        state,
        ContextBundle(
            query="post review fixture",
            created_at="2026-09-21T00:00:00Z",
            sources=(),
        ),
    )
    submitted = submit_job(
        state,
        context_sha256=context_sha,
        recipe=_recipe(),
    )
    generation = str(submitted["generation_id"])

    for stage in ("generation", "validation", "evaluation_context", "evaluation"):
        attempt = start_attempt(state, generation, stage)
        complete_attempt(
            state,
            str(attempt["attempt_id"]),
            outcome="succeeded",
            output=_stage_output(stage),
        )

    return state, str(submitted["job_id"]), generation


def _write_v2_review(state: Path, decision: str) -> None:
    path = state / "20-Review" / f"{MUTATION}.approval.json"
    path.write_bytes(
        _canonical_json_bytes(
            {
                "record_version": 2,
                "mutation_sha256": MUTATION,
                "evaluation_sha256": EVALUATION,
                "decision": decision,
                "decided_at": "2026-09-21T00:01:00Z",
                "approver": "human",
            }
        )
    )


def _write_receipt(state: Path) -> None:
    path = state / "30-Receipts" / f"{MUTATION}.receipt.json"
    path.write_bytes(
        _canonical_json_bytes(
            {
                "mutation_id": "post-review-test",
                "mutation_sha256": MUTATION,
                "target_path": "11-Knowledge/example.md",
                "content_sha256": "8" * 64,
                "executed_at": "2026-09-21T00:02:00Z",
                "result": "success",
            }
        )
    )


def test_reconcile_reject_is_terminal(tmp_path: Path) -> None:
    state, job_id, _generation = _awaiting(tmp_path)
    _write_v2_review(state, "reject")

    result = reconcile_post_review(state)

    assert result["human_rejected"] == 1
    assert job_status(state, job_id)["current_generation"]["state"] == "human_rejected"


def test_reconcile_approve_tracks_execution_then_completion(tmp_path: Path) -> None:
    state, job_id, _generation = _awaiting(tmp_path)
    _write_v2_review(state, "approve")

    first = reconcile_post_review(state)
    assert first["approved_pending_execution"] == 1
    assert (
        job_status(state, job_id)["current_generation"]["state"]
        == "approved_pending_execution"
    )

    _write_receipt(state)
    second = reconcile_post_review(state)

    assert second["completed"] == 1
    assert job_status(state, job_id)["current_generation"]["state"] == "completed"


def test_executor_dispatch_skips_reject_and_advances_approve(
    monkeypatch,
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    layout = ensure_artifact_layout(state)
    vault = tmp_path / "vault"
    (vault / "11-Knowledge").mkdir(parents=True)

    store_review_record(
        state,
        mutation_sha256="1" * 64,
        decision="reject",
        approver="human",
        decided_at="2026-09-21T00:00:00Z",
    )
    store_review_record(
        state,
        mutation_sha256="2" * 64,
        decision="approve",
        approver="human",
        decided_at="2026-09-21T00:00:00Z",
    )

    called = []

    def advance(ai_root, vault_root, digest, **_kwargs):
        called.append(digest)
        return SimpleNamespace(status="transport_pending", reason=None)

    monkeypatch.setattr(production, "advance_production_executor", advance)

    result = production.dispatch_pending_executor(state, vault)

    assert called == ["2" * 64]
    assert result["rejected"] == 1
    assert result["transport_pending"] == 1
    assert layout.review.is_dir()


def test_transport_dispatch_fails_closed_on_ambiguous_remote_result(
    monkeypatch,
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    ensure_artifact_layout(state)
    (state / "25-Execution").mkdir()
    (state / "27-Transport").mkdir()
    (state / "24-Locks").mkdir()
    (state / "25-Execution" / f"{MUTATION}.transport-request.json").write_text(
        "{}\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        production,
        "_load_context",
        lambda *_args, **_kwargs: (
            b"",
            SimpleNamespace(
                content="# Example\n",
                target_path="11-Knowledge/example.md",
            ),
            b"",
            SimpleNamespace(decision="approve"),
        ),
    )
    monkeypatch.setattr(production, "validate_knowledge_note_v0", lambda _mutation: None)

    class Lock:
        def __enter__(self):
            return None

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(production, "canonical_io_lock", lambda _root: Lock())
    monkeypatch.setattr(
        production,
        "process_transport_request",
        lambda *_args, **_kwargs: SimpleNamespace(result="target_exists_matching"),
    )

    with pytest.raises(ProductionOrchestrationError, match="Human recovery"):
        production.dispatch_pending_transport(
            state,
            base_url="https://nextcloud.example/dav/Vault",
            username="sync",
            password="secret",
        )
