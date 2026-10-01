from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import obsidian_automation.pre_review_worker as worker
from obsidian_automation.ai_input_planner import (
    INPUT_MODE_LEGACY,
    INPUT_MODE_SEMANTIC_DEEP,
    plan_once,
)
from obsidian_automation.artifact_lifecycle import ArtifactLifecycleError
from obsidian_automation.evaluation_artifact import (
    build_evaluation_record,
    load_evaluation_context,
    store_evaluation_record,
)
from obsidian_automation.evaluator_contract import (
    EVALUATOR_PROMPT_TEMPLATE_VERSION,
    prompt_template_sha256 as evaluator_prompt_sha256,
)
from obsidian_automation.generation_artifact import (
    generation_input_context,
    load_generation_record,
)
from obsidian_automation.openai_evaluator import (
    ADAPTER_VERSION as EVALUATOR_ADAPTER_VERSION,
    EVALUATION_STRATEGY,
)
from obsidian_automation.planner_cadence import load_cadence_state
from obsidian_automation.pre_review_job import (
    job_status,
    stage_output,
)
from obsidian_automation.semantic_corpus import (
    build_semantic_corpus,
    store_semantic_corpus_manifest,
)
from obsidian_automation.semantic_retrieval import RetrievalFilter
from obsidian_automation.semantic_index import (
    EmbeddingResult,
    EmbeddingResultSet,
    EmbeddingResultSetEntry,
    finalize_semantic_index,
    load_embedding_request,
    prepare_semantic_embedding_plan,
    store_embedding_result,
    store_embedding_result_set,
)
from obsidian_automation.semantic_objective import (
    DEEP_KNOWLEDGE,
    load_objective_context,
)
from obsidian_automation.semantic_objective_generation import (
    OBJECTIVE_OPENAI_ADAPTER_VERSION,
)


REVISION = "a" * 40
GEN_MODEL = "generator-model"
GEN_REVISION = f"identifier:{GEN_MODEL}"
EVAL_MODEL = "evaluator-model"
EVAL_REVISION = f"identifier:{EVAL_MODEL}"
EMBED_MODEL = "embedding-model:latest"
EMBED_DIGEST = "b" * 64


def _note(frontmatter: str, body: str) -> str:
    return f"---\n{frontmatter}---\n{body}\n"


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    for root in ("00-DailyNote", "05-Idea", "10-Project", "11-Knowledge"):
        (vault / root).mkdir(parents=True)

    daily = vault / "00-DailyNote" / "2026" / "09"
    daily.mkdir(parents=True)
    (daily / "2026-09-29.md").write_text(
        _note(
            "type: daily-review\n",
            "# Note\nQR labels lose storage provenance when cables move.",
        ),
        encoding="utf-8",
    )

    (vault / "05-Idea" / "Inventory.md").write_text(
        _note(
            "type: idea\n"
            "title: Stable inventory identity\n"
            "created: 2026-09-29\n"
            "workspace: '[[03-Workspace/Lab/Lab|Lab]]'\n"
            "status: active\n",
            "# Idea\nUse one stable identifier for labels and inventory records.",
        ),
        encoding="utf-8",
    )

    project = vault / "10-Project" / "Inventory"
    project.mkdir()
    (project / "Inventory.md").write_text(
        _note(
            "type: project\n"
            "workspace: '[[03-Workspace/Lab/Lab|Lab]]'\n"
            "status: running\n",
            "# Project Summary\nTrack physical cables with QR labels.",
        ),
        encoding="utf-8",
    )
    (project / "Design.md").write_text(
        _note(
            "type: project-note\n"
            "project: '[[10-Project/Inventory/Inventory|Inventory]]'\n"
            "workspace: '[[03-Workspace/Lab/Lab|Lab]]'\n"
            "category: design\n"
            "lifecycle: active\n",
            "# Design\nKeep asset identity stable while location and stock state change.",
        ),
        encoding="utf-8",
    )

    (vault / "11-Knowledge" / "Provenance.md").write_text(
        _note(
            "type: knowledge-note\n"
            "status: active\n"
            "category: summary\n"
            "maturity: stable\n"
            "source_type: self\n",
            "# Provenance\nStable identifiers help preserve traceability across state changes.",
        ),
        encoding="utf-8",
    )
    return vault


VECTORS = {
    "00-DailyNote/2026/09/2026-09-29.md": (0.92, 0.08, 0.0),
    "05-Idea/Inventory.md": (0.88, 0.12, 0.0),
    "10-Project/Inventory/Inventory.md": (0.97, 0.03, 0.0),
    "10-Project/Inventory/Design.md": (1.0, 0.0, 0.0),
    "11-Knowledge/Provenance.md": (0.55, 0.83, 0.0),
}


def _state(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    for path in (
        "00-Untrusted",
        "02-Orchestration/recipes",
        "02-Orchestration/semantic-selections",
        "04-Index",
        "05-Context",
        "10-Validation",
        "12-Evaluation-Request",
        "14-Evaluation-Context",
        "15-Evaluation",
        "20-Review",
        "30-Receipts",
        "24-Locks/read-view",
    ):
        (state / path).mkdir(parents=True)
    return state


def _semantic_index(tmp_path: Path) -> tuple[Path, Path, str]:
    vault = _vault(tmp_path)
    state = _state(tmp_path)
    corpus = build_semantic_corpus(vault)
    corpus_sha, _ = store_semantic_corpus_manifest(state, corpus)
    plan_sha, _, plan = prepare_semantic_embedding_plan(
        state,
        vault,
        corpus_manifest_sha256=corpus_sha,
        model_identifier=EMBED_MODEL,
        model_revision=EMBED_DIGEST,
    )
    result_entries: list[EmbeddingResultSetEntry] = []
    for entry in plan.requests:
        request = load_embedding_request(state, entry.request_sha256)
        result = EmbeddingResult(
            request_sha256=entry.request_sha256,
            provider=plan.provider,
            adapter_version=plan.adapter_version,
            model_identifier=plan.model_identifier,
            model_revision=plan.model_revision,
            vector=VECTORS[request.source_path],
        )
        result_sha, _ = store_embedding_result(state, result)
        result_entries.append(
            EmbeddingResultSetEntry(
                request_sha256=entry.request_sha256,
                result_sha256=result_sha,
            )
        )
    result_set = EmbeddingResultSet(
        plan_sha256=plan_sha,
        vector_dimension=3,
        vector_encoding="json-number-finite-v0",
        results=tuple(result_entries),
    )
    result_set_sha, _ = store_embedding_result_set(state, result_set)
    index_sha, _, _ = finalize_semantic_index(
        state,
        vault,
        plan_sha256=plan_sha,
        result_set_sha256=result_set_sha,
    )
    return vault, state, index_sha


def _deep_provider_transport(base_url: str, **kwargs):
    assert kwargs["path"] == "/chat/completions"
    payload = kwargs["payload"]
    assert (
        payload["response_format"]["json_schema"]["schema"]["properties"]
        ["objective_policy"]["const"]
        == DEEP_KNOWLEDGE
    )
    content = json.dumps(
        {
            "objective_policy": DEEP_KNOWLEDGE,
            "candidate_kind": "knowledge_candidate",
            "candidate": {
                "title": "安定した資産識別子による来歴管理",
                "category": "summary",
                "source_type": "self",
                "body": (
                    "# 中心概念\n\n"
                    "物理ラベルと在庫記録で同じ識別子を維持すると，"
                    "保管場所や在庫状態が変化しても同じ資産として追跡できる。\n\n"
                    "# 仕組み\n\n"
                    "識別子を不変にし，可変な場所・状態を別属性として更新する。\n\n"
                    "# 制約\n\n"
                    "識別子の再利用を避け，ラベルと記録の対応を維持する必要がある。\n"
                ),
            },
        },
        ensure_ascii=False,
    )
    return {
        "model": GEN_MODEL,
        "choices": [{"message": {"role": "assistant", "content": content}}],
    }



def _no_candidate_transport(base_url: str, **kwargs):
    assert kwargs["path"] == "/chat/completions"
    content = json.dumps(
        {
            "objective_policy": DEEP_KNOWLEDGE,
            "candidate_kind": "knowledge_candidate",
            "candidate": {
                "status": "no_candidate",
                "reason": "insufficient_evidence",
            },
        },
        ensure_ascii=False,
    )
    return {
        "model": GEN_MODEL,
        "choices": [{"message": {"role": "assistant", "content": content}}],
    }


def _fake_evaluator(
    ai_root: Path,
    *,
    proposal_sha256: str,
    generation_sha256: str,
    evaluation_context_sha256: str,
    **_kwargs,
):
    generation = load_generation_record(ai_root, generation_sha256)
    grounding = generation_input_context(ai_root, generation)
    assert any(source.path.startswith("00-DailyNote/") for source in grounding.sources)
    assert any(source.path.startswith("10-Project/") for source in grounding.sources)

    context = load_evaluation_context(ai_root, evaluation_context_sha256)
    record = build_evaluation_record(
        ai_root,
        proposal_sha256=proposal_sha256,
        mutation_sha256=context.mutation_sha256,
        generation_sha256=generation_sha256,
        evaluation_context_sha256=evaluation_context_sha256,
        implementation_revision=REVISION,
        prompt_template_version=EVALUATOR_PROMPT_TEMPLATE_VERSION,
        prompt_template_sha256=evaluator_prompt_sha256(),
        model_provider="openai-compatible",
        model_identifier=EVAL_MODEL,
        model_revision=EVAL_REVISION,
        model_config={
            "adapter_version": EVALUATOR_ADAPTER_VERSION,
            "identity_binding": "identifier-only",
            "strategy": EVALUATION_STRATEGY,
            "options": {"temperature": 0, "reasoning_effort": "low"},
        },
        groundedness="pass",
        redundancy="none",
        consistency="pass",
        recommendation="proceed",
        findings=[],
        evaluated_at="2026-09-29T04:05:00Z",
    )
    evaluation_sha, evaluation_path = store_evaluation_record(ai_root, record)
    return SimpleNamespace(
        proposal_sha256=proposal_sha256,
        mutation_sha256=context.mutation_sha256,
        generation_sha256=generation_sha256,
        evaluation_context_sha256=evaluation_context_sha256,
        evaluation_sha256=evaluation_sha,
        evaluation_path=evaluation_path,
        model_identifier=EVAL_MODEL,
        model_revision=EVAL_REVISION,
        prompt_template_version=EVALUATOR_PROMPT_TEMPLATE_VERSION,
        prompt_template_sha256=evaluator_prompt_sha256(),
        groundedness="pass",
        redundancy="none",
        consistency="pass",
        recommendation="proceed",
        findings=(),
    )


def test_semantic_deep_mode_reaches_human_review_through_existing_chain(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    vault, state, index_sha = _semantic_index(tmp_path)
    now = datetime(2026, 9, 29, 4, 0, tzinfo=timezone.utc)

    planned = plan_once(
        state,
        vault,
        deployed_revision=REVISION,
        input_mode=INPUT_MODE_SEMANTIC_DEEP,
        semantic_index_sha256=index_sha,
        semantic_selection_policy="semantic-project-distill-v0",
        generator_model=GEN_MODEL,
        evaluator_model=EVAL_MODEL,
        now=now,
    )
    assert planned["status"] == "submitted"
    assert planned["input_mode"] == INPUT_MODE_SEMANTIC_DEEP
    assert planned["objective_policy"] == DEEP_KNOWLEDGE
    assert planned["semantic_index_sha256"] == index_sha

    objective_context = load_objective_context(
        state,
        str(planned["context_sha256"]),
    )
    assert objective_context.selection_sha256 == planned["selection_sha256"]
    assert objective_context.semantic_index_sha256 == index_sha
    assert any(item.source_kind == "daily" for item in objective_context.sources)
    assert any(item.source_kind == "project-note" for item in objective_context.sources)

    generated = worker.run_generator_worker(
        state,
        base_url="http://127.0.0.1:8000/v1",
        deployed_revision=REVISION,
        transport=_deep_provider_transport,
    )
    assert generated["state"] == "validating"
    generation = load_generation_record(
        state,
        str(generated["output"]["generation_sha256"]),
    )
    assert generation.record_version == 2
    assert generation.context_kind == "semantic-objective"
    assert generation.semantic_objective is not None
    assert generation.semantic_objective.selection_sha256 == planned["selection_sha256"]
    assert generation.semantic_objective.semantic_index_sha256 == index_sha

    validated = worker.run_validator_worker(state, vault)
    assert validated["state"] == "building_evaluation_context"
    read = worker.run_reader_worker(state, vault)
    assert read["state"] == "evaluating"

    monkeypatch.setattr(
        worker,
        "evaluate_knowledge_note_with_openai_compatible",
        _fake_evaluator,
    )
    evaluated = worker.run_evaluator_worker(
        state,
        base_url="http://127.0.0.1:8000/v1",
        deployed_revision=REVISION,
    )
    assert evaluated["state"] == "awaiting_human_review"
    assert job_status(state, str(planned["job_id"]))["current_generation"]["state"] == (
        "awaiting_human_review"
    )



def test_semantic_deep_provider_no_candidate_is_deterministic_reject(
    tmp_path: Path,
) -> None:
    vault, state, index_sha = _semantic_index(tmp_path)
    planned = plan_once(
        state,
        vault,
        deployed_revision=REVISION,
        input_mode=INPUT_MODE_SEMANTIC_DEEP,
        semantic_index_sha256=index_sha,
        semantic_selection_policy="semantic-project-distill-v0",
        generator_model=GEN_MODEL,
        evaluator_model=EVAL_MODEL,
        now=datetime(2026, 9, 29, 4, 0, tzinfo=timezone.utc),
    )
    assert planned["status"] == "submitted"

    generated = worker.run_generator_worker(
        state,
        base_url="http://127.0.0.1:8000/v1",
        deployed_revision=REVISION,
        transport=_no_candidate_transport,
    )
    assert generated["status"] == "deterministic_reject"
    assert generated["state"] == "deterministic_reject"
    assert generated["reason_code"] == (
        "semantic_objective_insufficient_evidence"
    )
    generation_id = str(
        job_status(state, str(planned["job_id"]))["current_generation"]["generation_id"]
    )
    assert stage_output(state, generation_id, "generation") is None
    assert job_status(state, str(planned["job_id"]))[
        "current_generation"
    ]["state"] == "deterministic_reject"


def test_semantic_novelty_skip_does_not_create_job_or_submission_clock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    vault, state, index_sha = _semantic_index(tmp_path)
    from obsidian_automation.semantic_selection import (
        NoveltyObservation,
        SemanticSelectionRecord,
    )
    import obsidian_automation.ai_input_planner as planner

    selection = SemanticSelectionRecord(
        selection_policy="semantic-project-distill-v0",
        semantic_index_sha256=index_sha,
        corpus_manifest_sha256="c" * 64,
        metadata_filters=RetrievalFilter().payload(),
        retrieval_mode="hybrid",
        lexical_weight=0.6,
        source_kind_weights={
            "daily": 1.0,
            "idea": 1.0,
            "project": 1.0,
            "project-note": 1.0,
            "knowledge": 1.0,
        },
        anchors=(),
        selected=(),
        novelty=NoveltyObservation(
            decision="skipped",
            skip_reason="recent_context_too_similar",
            cluster_coherence=0.9,
            recent_context_max_similarity=0.99,
            knowledge_max_similarity=0.5,
            recent_contexts=(),
            recent_context_unmatched_count=0,
            thresholds={"recent_context_skip": 0.94},
        ),
        policy_observations={},
    )
    # build_semantic_selection normally returns a fully parsed record. The
    # planner only needs the skip decision before Objective Context creation.
    monkeypatch.setattr(planner, "build_semantic_selection", lambda *a, **k: selection)
    monkeypatch.setattr(
        planner,
        "store_semantic_selection",
        lambda *a, **k: ("d" * 64, state / "02-Orchestration" / "semantic-selections" / "fixture.json"),
    )

    result = plan_once(
        state,
        vault,
        deployed_revision=REVISION,
        input_mode=INPUT_MODE_SEMANTIC_DEEP,
        semantic_index_sha256=index_sha,
        semantic_selection_policy="semantic-project-distill-v0",
        generator_model=GEN_MODEL,
        evaluator_model=EVAL_MODEL,
        now=datetime(2026, 9, 29, 4, 0, tzinfo=timezone.utc),
    )
    assert result["status"] == "skipped_novelty"
    assert result["skip_reason"] == "recent_context_too_similar"
    assert not (state / "02-Orchestration" / "pre-review-jobs.sqlite3").exists()
    cadence = load_cadence_state(state)
    assert cadence.last_submission_at is None
    assert cadence.last_novelty_skip_reason == (
        "semantic-project-distill-v0:recent_context_too_similar"
    )


def test_semantic_mode_missing_or_stale_exact_index_never_submits(
    tmp_path: Path,
) -> None:
    vault, state, index_sha = _semantic_index(tmp_path)
    with pytest.raises(ArtifactLifecycleError):
        plan_once(
            state,
            vault,
            deployed_revision=REVISION,
            input_mode=INPUT_MODE_SEMANTIC_DEEP,
            semantic_index_sha256="f" * 64,
            semantic_selection_policy="semantic-project-distill-v0",
            generator_model=GEN_MODEL,
            evaluator_model=EVAL_MODEL,
        )
    assert not (state / "02-Orchestration" / "pre-review-jobs.sqlite3").exists()

    source = vault / "10-Project" / "Inventory" / "Design.md"
    source.write_text(
        source.read_text(encoding="utf-8") + "\nChanged after indexing.\n",
        encoding="utf-8",
    )
    with pytest.raises(ArtifactLifecycleError):
        plan_once(
            state,
            vault,
            deployed_revision=REVISION,
            input_mode=INPUT_MODE_SEMANTIC_DEEP,
            semantic_index_sha256=index_sha,
            semantic_selection_policy="semantic-project-distill-v0",
            generator_model=GEN_MODEL,
            evaluator_model=EVAL_MODEL,
        )
    assert not (state / "02-Orchestration" / "pre-review-jobs.sqlite3").exists()


def test_legacy_mode_remains_default_contract() -> None:
    unit = Path("examples/ai/obsidian-ai-input-planner.service").read_text()
    assert "Environment=AI_INPUT_MODE=legacy" in unit
    assert "Environment=AI_INPUT_SEMANTIC_INDEX_SHA=disabled" in unit
    assert "--input-mode ${AI_INPUT_MODE}" in unit
    assert "--semantic-index-sha ${AI_INPUT_SEMANTIC_INDEX_SHA}" in unit

def test_semantic_pending_recovery_is_idempotent_after_projection_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    vault, state, index_sha = _semantic_index(tmp_path)
    import obsidian_automation.ai_input_planner as planner
    import sqlite3

    calls = 0
    original = planner.emit_semantic_objective_context_projection

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("fixture projection failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(
        planner,
        "emit_semantic_objective_context_projection",
        fail_once,
    )
    now = datetime(2026, 9, 29, 4, 0, tzinfo=timezone.utc)

    with pytest.raises(OSError, match="fixture projection failure"):
        plan_once(
            state,
            vault,
            deployed_revision=REVISION,
            input_mode=INPUT_MODE_SEMANTIC_DEEP,
            semantic_index_sha256=index_sha,
            semantic_selection_policy="semantic-project-distill-v0",
            generator_model=GEN_MODEL,
            evaluator_model=EVAL_MODEL,
            now=now,
        )

    pending_path = state / "02-Orchestration" / "input-planner-pending.json"
    pending = json.loads(pending_path.read_text(encoding="utf-8"))
    assert pending["phase"] == "submitted"
    assert pending["input_mode"] == INPUT_MODE_SEMANTIC_DEEP
    assert pending["semantic_index_sha256"] == index_sha
    first_job = pending["job_id"]

    conn = sqlite3.connect(
        state / "02-Orchestration" / "pre-review-jobs.sqlite3"
    )
    try:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    finally:
        conn.close()

    recovered = plan_once(
        state,
        vault,
        deployed_revision=REVISION,
        input_mode=INPUT_MODE_SEMANTIC_DEEP,
        semantic_index_sha256=index_sha,
        semantic_selection_policy="semantic-project-distill-v0",
        generator_model=GEN_MODEL,
        evaluator_model=EVAL_MODEL,
        now=now,
    )
    assert recovered["status"] == "recovered_pending_submission"
    assert recovered["job_id"] == first_job
    assert not pending_path.exists()

    conn = sqlite3.connect(
        state / "02-Orchestration" / "pre-review-jobs.sqlite3"
    )
    try:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    finally:
        conn.close()


def test_semantic_pending_recovery_rejects_index_configuration_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    vault, state, index_sha = _semantic_index(tmp_path)
    import obsidian_automation.ai_input_planner as planner

    monkeypatch.setattr(
        planner,
        "emit_semantic_objective_context_projection",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            OSError("fixture projection failure")
        ),
    )
    with pytest.raises(OSError):
        plan_once(
            state,
            vault,
            deployed_revision=REVISION,
            input_mode=INPUT_MODE_SEMANTIC_DEEP,
            semantic_index_sha256=index_sha,
            semantic_selection_policy="semantic-project-distill-v0",
            generator_model=GEN_MODEL,
            evaluator_model=EVAL_MODEL,
            now=datetime(2026, 9, 29, 4, 0, tzinfo=timezone.utc),
        )

    with pytest.raises(ArtifactLifecycleError, match="semantic index"):
        plan_once(
            state,
            vault,
            deployed_revision=REVISION,
            input_mode=INPUT_MODE_SEMANTIC_DEEP,
            semantic_index_sha256="e" * 64,
            semantic_selection_policy="semantic-project-distill-v0",
            generator_model=GEN_MODEL,
            evaluator_model=EVAL_MODEL,
            now=datetime(2026, 9, 29, 4, 1, tzinfo=timezone.utc),
        )

