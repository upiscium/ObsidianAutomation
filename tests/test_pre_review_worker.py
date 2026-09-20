from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import obsidian_automation.pre_review_worker as worker
from obsidian_automation.artifact_lifecycle import (
    store_untrusted_proposal,
)
from obsidian_automation.context_bundle import ContextBundle, store_context_bundle
from obsidian_automation.evaluation_artifact import (
    EVALUATION_CONTEXT_STAGE,
    EVALUATION_REQUEST_STAGE,
    EVALUATION_STAGE,
    build_evaluation_record,
    store_evaluation_record,
)
from obsidian_automation.evaluator_contract import (
    EVALUATOR_PROMPT_TEMPLATE_VERSION,
    prompt_template_sha256 as evaluator_prompt_sha256,
)
from obsidian_automation.generation_artifact import (
    build_generation_record,
    store_generation_record,
)
from obsidian_automation.human_projection import parse_request
from obsidian_automation.generator_contract import (
    PROMPT_TEMPLATE_VERSION,
    prompt_template_sha256 as generator_prompt_sha256,
)
from obsidian_automation.openai_compatible import OpenAICompatibleProviderError
from obsidian_automation.openai_evaluator import (
    ADAPTER_VERSION as EVALUATOR_ADAPTER_VERSION,
    EVALUATION_STRATEGY,
)
from obsidian_automation.openai_generator import (
    ADAPTER_VERSION as GENERATOR_ADAPTER_VERSION,
)
from obsidian_automation.ollama_evaluator import (
    ADAPTER_VERSION as OLLAMA_EVALUATOR_ADAPTER_VERSION,
)
from obsidian_automation.ollama_generator import (
    ADAPTER_VERSION as OLLAMA_GENERATOR_ADAPTER_VERSION,
)
from obsidian_automation.pre_review_job import (
    job_status,
    parse_recipe,
    stage_output,
    submit_job,
)


REVISION = "a" * 40
GEN_MODEL = "gemma3:12b"
GEN_REVISION = f"identifier:{GEN_MODEL}"
EVAL_MODEL = "gemma3:12b-eval"
EVAL_REVISION = f"identifier:{EVAL_MODEL}"
OLLAMA_MODEL = "gemma4:12b"
OLLAMA_DIGEST = "d" * 64


def _knowledge_note(body: str) -> str:
    return (
        "---\n"
        "type: knowledge-note\n"
        "status: active\n"
        "category: manual\n"
        "maturity: draft\n"
        "source_type: self\n"
        "---\n\n"
        f"{body}\n"
    )


def _proposal_bytes() -> bytes:
    value = {
        "contract_version": 1,
        "operation": "create_note",
        "mutation_id": "pre-review-worker-test",
        "target": {"path": "11-Knowledge/Generated Worker Note.md"},
        "content": _knowledge_note(
            "# Generated Worker Note\n\nThis draft is grounded in the supplied Context."
        ),
    }
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def _recipe_bytes() -> bytes:
    value = {
        "record_version": 1,
        "pipeline": "knowledge-pre-review-v0",
        "generator": {
            "implementation_revision": REVISION,
            "prompt_template_version": PROMPT_TEMPLATE_VERSION,
            "prompt_template_sha256": generator_prompt_sha256(),
            "provider": "openai-compatible",
            "model_identifier": GEN_MODEL,
            "model_revision": GEN_REVISION,
            "model_config": {
                "adapter_version": GENERATOR_ADAPTER_VERSION,
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
            "implementation_revision": REVISION,
            "prompt_template_version": EVALUATOR_PROMPT_TEMPLATE_VERSION,
            "prompt_template_sha256": evaluator_prompt_sha256(),
            "provider": "openai-compatible",
            "model_identifier": EVAL_MODEL,
            "model_revision": EVAL_REVISION,
            "model_config": {
                "adapter_version": EVALUATOR_ADAPTER_VERSION,
                "identity_binding": "identifier-only",
                "strategy": EVALUATION_STRATEGY,
                "options": {"temperature": 0},
            },
        },
    }
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def _ollama_recipe_bytes() -> bytes:
    value = json.loads(_recipe_bytes())
    value["generator"] = {
        "implementation_revision": REVISION,
        "prompt_template_version": PROMPT_TEMPLATE_VERSION,
        "prompt_template_sha256": generator_prompt_sha256(),
        "provider": "ollama",
        "model_identifier": OLLAMA_MODEL,
        "model_revision": OLLAMA_DIGEST,
        "model_config": {
            "adapter_version": OLLAMA_GENERATOR_ADAPTER_VERSION,
            "think": False,
            "options": {"temperature": 0},
        },
    }
    value["evaluator"] = {
        "implementation_revision": REVISION,
        "prompt_template_version": EVALUATOR_PROMPT_TEMPLATE_VERSION,
        "prompt_template_sha256": evaluator_prompt_sha256(),
        "provider": "ollama",
        "model_identifier": OLLAMA_MODEL,
        "model_revision": OLLAMA_DIGEST,
        "model_config": {
            "adapter_version": OLLAMA_EVALUATOR_ADAPTER_VERSION,
            "think": "low",
            "strategy": EVALUATION_STRATEGY,
            "options": {"temperature": 0},
        },
    }
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def _fixture(
    tmp_path: Path,
    *,
    recipe_bytes: bytes | None = None,
) -> tuple[Path, Path, str, str]:
    vault = tmp_path / "vault"
    knowledge = vault / "11-Knowledge"
    knowledge.mkdir(parents=True)
    (knowledge / "Existing.md").write_text(
        _knowledge_note(
            "# Existing\n\nThis draft is grounded in the supplied Context and may overlap."
        ),
        encoding="utf-8",
    )

    state = tmp_path / "state"
    state.mkdir()
    for stage in (
        "00-Untrusted",
        "04-Index",
        "05-Context",
        "10-Validation",
        EVALUATION_REQUEST_STAGE,
        EVALUATION_CONTEXT_STAGE,
        EVALUATION_STAGE,
        "20-Review",
        "30-Receipts",
    ):
        (state / stage).mkdir()
    (state / "24-Locks" / "read-view").mkdir(parents=True)

    bundle = ContextBundle(
        query="Create a grounded worker note",
        created_at="2026-09-19T00:00:00Z",
        sources=(),
    )
    context_sha, _ = store_context_bundle(state, bundle)
    recipe = parse_recipe(_recipe_bytes() if recipe_bytes is None else recipe_bytes)
    submitted = submit_job(state, context_sha256=context_sha, recipe=recipe)
    return state, vault, str(submitted["job_id"]), context_sha



def _enable_human_projection(state: Path) -> None:
    root = state / "16-Human-Projection"
    root.mkdir()
    for role in ("reader", "generator", "validator", "evaluator", "reviewer", "executor", "sync"):
        (root / role).mkdir()
    (state / "17-Human-Projection-Result").mkdir()


def _projection_requests(state: Path, role: str):
    paths = sorted((state / "16-Human-Projection" / role).glob("*.projection.json"))
    return [parse_request(path.read_bytes()) for path in paths]

def _fake_generator(ai_root: Path, *, context_sha256: str, **_kwargs):
    proposal_sha, proposal_path = store_untrusted_proposal(ai_root, _proposal_bytes())
    record = build_generation_record(
        ai_root,
        context_sha256=context_sha256,
        proposal_sha256=proposal_sha,
        implementation_revision=REVISION,
        prompt_template_version=PROMPT_TEMPLATE_VERSION,
        prompt_template_sha256=generator_prompt_sha256(),
        model_provider="openai-compatible",
        model_identifier=GEN_MODEL,
        model_revision=GEN_REVISION,
        model_config={
            "adapter_version": GENERATOR_ADAPTER_VERSION,
            "identity_binding": "identifier-only",
            "options": {"temperature": 0},
        },
        generated_at="2026-09-19T00:01:00Z",
    )
    generation_sha, generation_path = store_generation_record(ai_root, record)
    return SimpleNamespace(
        context_sha256=context_sha256,
        proposal_sha256=proposal_sha,
        proposal_path=proposal_path,
        generation_sha256=generation_sha,
        generation_path=generation_path,
        model_identifier=GEN_MODEL,
        model_revision=GEN_REVISION,
        prompt_template_version=PROMPT_TEMPLATE_VERSION,
        prompt_template_sha256=generator_prompt_sha256(),
    )


def _fake_ollama_generator(ai_root: Path, *, context_sha256: str, **_kwargs):
    proposal_sha, proposal_path = store_untrusted_proposal(ai_root, _proposal_bytes())
    record = build_generation_record(
        ai_root,
        context_sha256=context_sha256,
        proposal_sha256=proposal_sha,
        implementation_revision=REVISION,
        prompt_template_version=PROMPT_TEMPLATE_VERSION,
        prompt_template_sha256=generator_prompt_sha256(),
        model_provider="ollama",
        model_identifier=OLLAMA_MODEL,
        model_revision=OLLAMA_DIGEST,
        model_config={
            "adapter_version": OLLAMA_GENERATOR_ADAPTER_VERSION,
            "think": False,
            "options": {"temperature": 0},
        },
        generated_at="2026-09-19T00:01:00Z",
    )
    generation_sha, generation_path = store_generation_record(ai_root, record)
    return SimpleNamespace(
        context_sha256=context_sha256,
        proposal_sha256=proposal_sha,
        proposal_path=proposal_path,
        generation_sha256=generation_sha,
        generation_path=generation_path,
        model_identifier=OLLAMA_MODEL,
        model_revision=OLLAMA_DIGEST,
        prompt_template_version=PROMPT_TEMPLATE_VERSION,
        prompt_template_sha256=generator_prompt_sha256(),
    )


def _fake_ollama_evaluator(ai_root: Path, *, proposal_sha256: str, generation_sha256: str, evaluation_context_sha256: str, **kwargs):
    assert kwargs["think"] == "low"
    context = worker.load_evaluation_context(ai_root, evaluation_context_sha256)
    record = build_evaluation_record(
        ai_root,
        proposal_sha256=proposal_sha256,
        mutation_sha256=context.mutation_sha256,
        generation_sha256=generation_sha256,
        evaluation_context_sha256=evaluation_context_sha256,
        implementation_revision=REVISION,
        prompt_template_version=EVALUATOR_PROMPT_TEMPLATE_VERSION,
        prompt_template_sha256=evaluator_prompt_sha256(),
        model_provider="ollama",
        model_identifier=OLLAMA_MODEL,
        model_revision=OLLAMA_DIGEST,
        model_config={
            "adapter_version": OLLAMA_EVALUATOR_ADAPTER_VERSION,
            "think": "low",
            "strategy": EVALUATION_STRATEGY,
            "options": {"temperature": 0},
        },
        groundedness="pass",
        redundancy="none",
        consistency="pass",
        recommendation="proceed",
        findings=[],
        evaluated_at="2026-09-19T00:02:00Z",
    )
    evaluation_sha, evaluation_path = store_evaluation_record(ai_root, record)
    return SimpleNamespace(
        proposal_sha256=proposal_sha256,
        mutation_sha256=context.mutation_sha256,
        generation_sha256=generation_sha256,
        evaluation_context_sha256=evaluation_context_sha256,
        evaluation_sha256=evaluation_sha,
        evaluation_path=evaluation_path,
        model_identifier=OLLAMA_MODEL,
        model_revision=OLLAMA_DIGEST,
        prompt_template_version=EVALUATOR_PROMPT_TEMPLATE_VERSION,
        prompt_template_sha256=evaluator_prompt_sha256(),
        groundedness="pass",
        redundancy="none",
        consistency="pass",
        recommendation="proceed",
        findings=(),
    )


def _fake_evaluator(recommendation: str):
    def run(
        ai_root: Path,
        *,
        proposal_sha256: str,
        generation_sha256: str,
        evaluation_context_sha256: str,
        **_kwargs,
    ):
        if recommendation == "do_not_proceed":
            groundedness, redundancy, consistency = "pass", "likely", "pass"
        elif recommendation == "proceed":
            groundedness, redundancy, consistency = "pass", "none", "pass"
        else:
            groundedness, redundancy, consistency = "unknown", "possible", "unknown"

        # The accepted mutation is already bound by the Evaluation Context.
        context = worker.load_evaluation_context(ai_root, evaluation_context_sha256)
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
                "options": {"temperature": 0},
            },
            groundedness=groundedness,
            redundancy=redundancy,
            consistency=consistency,
            recommendation=recommendation,
            findings=[],
            evaluated_at="2026-09-19T00:02:00Z",
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
            groundedness=groundedness,
            redundancy=redundancy,
            consistency=consistency,
            recommendation=recommendation,
            findings=(),
        )

    return run


def test_native_ollama_worker_chain_dispatches_false_and_low_thinking(
    monkeypatch,
    tmp_path: Path,
) -> None:
    state, vault, job_id, _ = _fixture(
        tmp_path,
        recipe_bytes=_ollama_recipe_bytes(),
    )

    openai_generator_called = False
    openai_evaluator_called = False

    def reject_openai_generator(*_args, **_kwargs):
        nonlocal openai_generator_called
        openai_generator_called = True
        raise AssertionError("OpenAI generator must not be called")

    def reject_openai_evaluator(*_args, **_kwargs):
        nonlocal openai_evaluator_called
        openai_evaluator_called = True
        raise AssertionError("OpenAI evaluator must not be called")

    monkeypatch.setattr(worker, "generate_knowledge_note_with_openai_compatible", reject_openai_generator)
    monkeypatch.setattr(worker, "evaluate_knowledge_note_with_openai_compatible", reject_openai_evaluator)
    monkeypatch.setattr(worker, "generate_knowledge_note_with_ollama", _fake_ollama_generator)
    monkeypatch.setattr(worker, "evaluate_knowledge_note_with_ollama", _fake_ollama_evaluator)

    generated = worker.run_generator_worker(
        state,
        base_url="https://ollama.arc.upiscium.dev/v1",
        deployed_revision=REVISION,
    )
    assert generated["state"] == "validating"

    validated = worker.run_validator_worker(state, vault)
    assert validated["state"] == "building_evaluation_context"

    read = worker.run_reader_worker(state, vault)
    assert read["state"] == "evaluating"

    evaluated = worker.run_evaluator_worker(
        state,
        base_url="https://ollama.arc.upiscium.dev/v1",
        deployed_revision=REVISION,
    )
    assert evaluated["state"] == "awaiting_human_review"
    assert openai_generator_called is False
    assert openai_evaluator_called is False
    assert job_status(state, job_id)["current_generation"]["state"] == "awaiting_human_review"


@pytest.mark.parametrize("recommendation", ["proceed", "do_not_proceed"])
def test_identity_worker_chain_stops_at_human_review_without_review_artifact(
    monkeypatch,
    tmp_path: Path,
    recommendation: str,
) -> None:
    state, vault, job_id, _ = _fixture(tmp_path)
    monkeypatch.setattr(worker, "generate_knowledge_note_with_openai_compatible", _fake_generator)
    monkeypatch.setattr(
        worker,
        "evaluate_knowledge_note_with_openai_compatible",
        _fake_evaluator(recommendation),
    )

    generated = worker.run_generator_worker(
        state,
        base_url="https://openai.example.invalid/v1",
        deployed_revision=REVISION,
    )
    assert generated["status"] == "completed"
    assert generated["state"] == "validating"

    validated = worker.run_validator_worker(state, vault)
    assert validated["status"] == "completed"
    assert validated["state"] == "building_evaluation_context"

    read = worker.run_reader_worker(state, vault)
    assert read["status"] == "completed"
    assert read["state"] == "evaluating"

    evaluated = worker.run_evaluator_worker(
        state,
        base_url="https://openai.example.invalid/v1",
        deployed_revision=REVISION,
    )
    assert evaluated["status"] == "completed"
    assert evaluated["state"] == "awaiting_human_review"

    status = job_status(state, job_id)
    assert status["current_generation"]["state"] == "awaiting_human_review"
    generation_id = status["current_generation"]["generation_id"]
    final = stage_output(state, generation_id, "evaluation")
    assert final is not None
    assert final["recommendation"] == recommendation

    assert list((state / "20-Review").iterdir()) == []



def test_worker_chain_emits_role_scoped_human_projection_requests(
    monkeypatch,
    tmp_path: Path,
) -> None:
    state, vault, job_id, _ = _fixture(tmp_path)
    _enable_human_projection(state)
    monkeypatch.setattr(worker, "generate_knowledge_note_with_openai_compatible", _fake_generator)
    monkeypatch.setattr(
        worker,
        "evaluate_knowledge_note_with_openai_compatible",
        _fake_evaluator("proceed"),
    )

    assert worker.run_generator_worker(
        state,
        base_url="https://openai.example.invalid/v1",
        deployed_revision=REVISION,
    )["status"] == "completed"
    assert worker.run_validator_worker(state, vault)["status"] == "completed"
    assert worker.run_reader_worker(state, vault)["status"] == "completed"
    assert worker.run_evaluator_worker(
        state,
        base_url="https://openai.example.invalid/v1",
        deployed_revision=REVISION,
    )["status"] == "completed"

    generation = _projection_requests(state, "generator")
    validation = _projection_requests(state, "validator")
    evaluation = _projection_requests(state, "evaluator")
    assert [item.stage for item in generation] == ["generation"]
    assert [item.stage for item in validation] == ["validation"]
    assert sorted(item.stage for item in evaluation) == ["evaluation", "review"]
    assert all(item.case_id == job_status(state, job_id)["current_generation"]["generation_id"]
               for item in [*generation, *validation, *evaluation])
    assert "review_request:" in next(item.content for item in evaluation if item.stage == "review")
    assert list((state / "20-Review").iterdir()) == []

def test_generator_revision_mismatch_blocks_without_provider_contact(
    monkeypatch,
    tmp_path: Path,
) -> None:
    state, _vault, job_id, _ = _fixture(tmp_path)
    called = False

    def should_not_call(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("provider must not be called")

    monkeypatch.setattr(worker, "generate_knowledge_note_with_openai_compatible", should_not_call)
    result = worker.run_generator_worker(
        state,
        base_url="https://openai.example.invalid/v1",
        deployed_revision="d" * 40,
    )

    assert result["status"] == "blocked"
    assert result["reason_code"] == "generator_recipe_runtime_mismatch"
    assert called is False
    assert job_status(state, job_id)["current_generation"]["state"] == "blocked"


def test_generator_provider_failure_is_bounded_to_three_attempts(
    monkeypatch,
    tmp_path: Path,
) -> None:
    state, _vault, job_id, _ = _fixture(tmp_path)

    def fail(*_args, **_kwargs):
        raise OpenAICompatibleProviderError("temporary provider failure")

    monkeypatch.setattr(worker, "generate_knowledge_note_with_openai_compatible", fail)

    for index in range(1, 4):
        result = worker.run_generator_worker(
            state,
            base_url="https://openai.example.invalid/v1",
            deployed_revision=REVISION,
            max_attempts=3,
        )
        assert result["status"] == "retryable_failure"
        assert result["attempt_id"]

    idle = worker.run_generator_worker(
        state,
        base_url="https://openai.example.invalid/v1",
        deployed_revision=REVISION,
        max_attempts=3,
    )
    assert idle["status"] == "idle"
    assert job_status(state, job_id)["current_generation"]["state"] == "retry_exhausted"


def test_worker_recovers_orphaned_attempt_without_selecting_old_artifact(
    monkeypatch,
    tmp_path: Path,
) -> None:
    state, _vault, job_id, _ = _fixture(tmp_path)
    first = worker.claim_next_attempt(
        state,
        "generation",
        recover_running=False,
    )
    assert first is not None

    monkeypatch.setattr(worker, "generate_knowledge_note_with_openai_compatible", _fake_generator)
    result = worker.run_generator_worker(
        state,
        base_url="https://openai.example.invalid/v1",
        deployed_revision=REVISION,
    )

    assert result["status"] == "completed"
    assert result["attempt_id"] != first.attempt_id
    status = job_status(state, job_id)
    assert status["current_generation"]["state"] == "validating"


def test_systemd_chain_uses_distinct_fixed_identities_and_stays_disabled_by_default() -> None:
    units = {
        "planner": Path("examples/ai/obsidian-ai-input-planner.service").read_text(),
        "generator": Path("examples/ai/obsidian-pre-review-generator.service").read_text(),
        "validator": Path("examples/ai/obsidian-pre-review-validator.service").read_text(),
        "reader": Path("examples/ai/obsidian-pre-review-reader.service").read_text(),
        "evaluator": Path("examples/ai/obsidian-pre-review-evaluator.service").read_text(),
        "projection": Path("examples/ai/obsidian-ai-human-projection-sync.service").read_text(),
        "status": Path("examples/ai/obsidian-pre-review-status.service").read_text(),
        "timer": Path("examples/ai/obsidian-pre-review.timer").read_text(),
    }

    assert "User=obsidian-ai-reader" in units["planner"]
    assert "PrivateNetwork=true" in units["planner"]
    assert "ConditionPathExists=/etc/obsidian-ai/pre-review-input.env" in units["planner"]
    assert "User=obsidian-ai-generator" in units["generator"]
    assert "User=obsidian-ai-validator" in units["validator"]
    assert "User=obsidian-ai-reader" in units["reader"]
    assert "User=obsidian-ai-evaluator" in units["evaluator"]
    assert "User=obsidian-ai-sync" in units["projection"]
    assert "/var/lib/obsidian-ai/state/16-Human-Projection/reader" in units["planner"]
    assert "/var/lib/obsidian-ai/state/16-Human-Projection/generator" in units["generator"]
    assert "/var/lib/obsidian-ai/state/16-Human-Projection/validator" in units["validator"]
    assert "/var/lib/obsidian-ai/state/16-Human-Projection/evaluator" in units["evaluator"]
    assert "ConditionPathExists=/etc/obsidian-ai/human-projection.env" in units["projection"]

    assert "Requires=obsidian-ai-input-planner.service" in units["generator"]
    assert "After=network-online.target obsidian-ai-input-planner.service" in units["generator"]
    assert "Requires=obsidian-pre-review-generator.service" in units["validator"]
    assert "Requires=obsidian-pre-review-validator.service" in units["reader"]
    assert "Requires=obsidian-pre-review-reader.service" in units["evaluator"]
    assert "Requires=obsidian-pre-review-evaluator.service" in units["projection"]
    assert "Requires=obsidian-pre-review-evaluator.service" in units["status"]
    assert "Wants=obsidian-ai-human-projection-sync.service" in units["status"]
    assert "User=obsidian-ai-status" in units["status"]
    assert "Unit=obsidian-pre-review-status.service" in units["timer"]

    assert "PrivateNetwork=true" in units["validator"]
    assert "PrivateNetwork=true" in units["reader"]
    assert "User=root" not in "\n".join(units.values())
    assert "WantedBy=timers.target" in units["timer"]

    # Repository examples never enable/start the timer themselves.
    assert "systemctl enable" not in "\n".join(units.values())
