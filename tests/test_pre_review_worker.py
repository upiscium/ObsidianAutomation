from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import obsidian_automation.pre_review_worker as worker
from obsidian_automation.artifact_lifecycle import (
    ArtifactLifecycleError,
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
    EVALUATOR_PROMPT_TEMPLATE_V3_SHA256,
    EVALUATOR_PROMPT_TEMPLATE_V3_VERSION,
    EVALUATOR_PROMPT_TEMPLATE_VERSION,
)
from obsidian_automation.evaluator_contract import (
    prompt_template_sha256 as evaluator_prompt_sha256,
)
from obsidian_automation.generation_artifact import (
    build_generation_record,
    store_generation_record,
)
from obsidian_automation.generator_contract import (
    PROMPT_TEMPLATE_V0_SHA256,
    PROMPT_TEMPLATE_V0_VERSION,
    PROMPT_TEMPLATE_VERSION,
)
from obsidian_automation.generator_contract import (
    prompt_template_sha256 as generator_prompt_sha256,
)
from obsidian_automation.human_projection import parse_request
from obsidian_automation.ollama_evaluator import (
    ADAPTER_VERSION as OLLAMA_EVALUATOR_ADAPTER_VERSION,
)
from obsidian_automation.ollama_generator import (
    ADAPTER_VERSION as OLLAMA_GENERATOR_ADAPTER_VERSION,
)
from obsidian_automation.openai_compatible import OpenAICompatibleProviderError
from obsidian_automation.openai_evaluator import (
    ADAPTER_VERSION as EVALUATOR_ADAPTER_VERSION,
)
from obsidian_automation.openai_evaluator import (
    EVALUATION_STRATEGY,
    LEGACY_EVALUATION_STRATEGY,
)
from obsidian_automation.openai_evaluator import (
    LEGACY_ADAPTER_VERSION as LEGACY_EVALUATOR_ADAPTER_VERSION,
)
from obsidian_automation.openai_generator import (
    ADAPTER_VERSION as GENERATOR_ADAPTER_VERSION,
)
from obsidian_automation.pre_review_job import (
    attempt_history,
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


def _historical_evaluator_recipe_bytes() -> bytes:
    value = json.loads(_recipe_bytes())
    value["evaluator"]["prompt_template_version"] = EVALUATOR_PROMPT_TEMPLATE_V3_VERSION
    value["evaluator"]["prompt_template_sha256"] = EVALUATOR_PROMPT_TEMPLATE_V3_SHA256
    value["evaluator"]["model_config"]["adapter_version"] = LEGACY_EVALUATOR_ADAPTER_VERSION
    value["evaluator"]["model_config"]["strategy"] = LEGACY_EVALUATION_STRATEGY
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def _fail_provider_contact(*_args, **_kwargs):
    raise AssertionError("historical evaluator recipe must not reach provider")


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


def test_historical_evaluator_recipe_blocks_without_provider_contact(
    monkeypatch,
    tmp_path: Path,
) -> None:
    state, vault, job_id, _ = _fixture(
        tmp_path,
        recipe_bytes=_historical_evaluator_recipe_bytes(),
    )
    monkeypatch.setattr(worker, "generate_knowledge_note_with_openai_compatible", _fake_generator)
    monkeypatch.setattr(worker, "evaluate_knowledge_note_with_openai_compatible", _fail_provider_contact)

    assert worker.run_generator_worker(
        state,
        base_url="https://openai.example.invalid/v1",
        deployed_revision=REVISION,
    )["status"] == "completed"
    assert worker.run_validator_worker(state, vault)["status"] == "completed"
    assert worker.run_reader_worker(state, vault)["status"] == "completed"

    result = worker.run_evaluator_worker(
        state,
        base_url="https://openai.example.invalid/v1",
        deployed_revision=REVISION,
    )

    assert result["status"] == "blocked"
    assert result["reason_code"] == "evaluator_recipe_runtime_mismatch"
    assert job_status(state, job_id)["current_generation"]["state"] == "blocked"


def test_historical_generator_recipe_is_readable_but_not_executable(
    monkeypatch,
    tmp_path: Path,
) -> None:
    recipe = json.loads(_recipe_bytes())
    recipe["generator"]["prompt_template_version"] = PROMPT_TEMPLATE_V0_VERSION
    recipe["generator"]["prompt_template_sha256"] = PROMPT_TEMPLATE_V0_SHA256
    state, _vault, job_id, _ = _fixture(
        tmp_path,
        recipe_bytes=(json.dumps(recipe, separators=(",", ":")) + "\n").encode(),
    )
    called = False

    def should_not_call(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("historical prompt recipe must not reach the provider")

    monkeypatch.setattr(worker, "generate_knowledge_note_with_openai_compatible", should_not_call)
    result = worker.run_generator_worker(
        state,
        base_url="https://openai.example.invalid/v1",
        deployed_revision=REVISION,
    )

    assert result["status"] == "blocked"
    assert result["reason_code"] == "generator_recipe_runtime_mismatch"
    assert called is False
    assert job_status(state, job_id)["current_generation"]["state"] == "blocked"


@pytest.mark.parametrize(
    ("exception_type", "message", "expected_class", "expected_code"),
    [
        (
            OpenAICompatibleProviderError,
            "OpenAI-compatible HTTP request failed with status 503 secret-token",
            "OpenAICompatibleProviderError",
            "provider_transport",
        ),
        (
            OpenAICompatibleProviderError,
            "evaluator finding detail must not contain control characters secret-token",
            "OpenAICompatibleProviderError",
            "provider_response_contract",
        ),
        (
            ArtifactLifecycleError,
            "evaluation artifact binding failed secret-token",
            "ArtifactLifecycleError",
            "artifact_contract",
        ),
        (
            OSError,
            "local read failed /private/secret-token",
            "OSError",
            "local_io",
        ),
    ],
)
def test_evaluator_retry_persists_only_bounded_diagnostic(
    monkeypatch,
    tmp_path: Path,
    exception_type,
    message: str,
    expected_class: str,
    expected_code: str,
) -> None:
    state, vault, job_id, _ = _fixture(tmp_path)

    monkeypatch.setattr(
        worker,
        "generate_knowledge_note_with_openai_compatible",
        _fake_generator,
    )
    assert worker.run_generator_worker(
        state,
        base_url="https://openai.example.invalid/v1",
        deployed_revision=REVISION,
    )["status"] == "completed"
    assert worker.run_validator_worker(state, vault)["status"] == "completed"
    assert worker.run_reader_worker(state, vault)["status"] == "completed"

    def fail(*_args, **_kwargs):
        raise exception_type(message)

    monkeypatch.setattr(
        worker,
        "evaluate_knowledge_note_with_openai_compatible",
        fail,
    )

    result = worker.run_evaluator_worker(
        state,
        base_url="https://openai.example.invalid/v1",
        deployed_revision=REVISION,
    )

    expected = {
        "exception_class": expected_class,
        "diagnostic_code": expected_code,
    }
    assert result["status"] == "retryable_failure"
    assert result["reason_code"] == "evaluator_provider_or_output_error"
    assert result["diagnostic"] == expected
    assert "secret-token" not in json.dumps(result, sort_keys=True)

    history = attempt_history(state, job_id)
    last = history["attempts"][-1]
    assert last["stage"] == "evaluation"
    assert last["status"] == "retryable_failure"
    assert last["reason_code"] == "evaluator_provider_or_output_error"
    assert last["diagnostic"] == expected
    assert "secret-token" not in json.dumps(history, sort_keys=True)
    assert job_status(state, job_id)["current_generation"]["state"] == "retryable_failure"


def test_evaluator_diagnostics_preserve_three_attempt_exhaustion(
    monkeypatch,
    tmp_path: Path,
) -> None:
    state, vault, job_id, _ = _fixture(tmp_path)

    monkeypatch.setattr(
        worker,
        "generate_knowledge_note_with_openai_compatible",
        _fake_generator,
    )
    assert worker.run_generator_worker(
        state,
        base_url="https://openai.example.invalid/v1",
        deployed_revision=REVISION,
    )["status"] == "completed"
    assert worker.run_validator_worker(state, vault)["status"] == "completed"
    assert worker.run_reader_worker(state, vault)["status"] == "completed"

    def fail(*_args, **_kwargs):
        raise OpenAICompatibleProviderError(
            "evaluator structured output is invalid secret-payload"
        )

    monkeypatch.setattr(
        worker,
        "evaluate_knowledge_note_with_openai_compatible",
        fail,
    )

    for attempt_index in range(1, 4):
        result = worker.run_evaluator_worker(
            state,
            base_url="https://openai.example.invalid/v1",
            deployed_revision=REVISION,
            max_attempts=3,
        )
        assert result["status"] == "retryable_failure"
        assert result["diagnostic"] == {
            "exception_class": "OpenAICompatibleProviderError",
            "diagnostic_code": "provider_response_contract",
        }

    idle = worker.run_evaluator_worker(
        state,
        base_url="https://openai.example.invalid/v1",
        deployed_revision=REVISION,
        max_attempts=3,
    )
    assert idle["status"] == "idle"
    assert job_status(state, job_id)["current_generation"]["state"] == "retry_exhausted"

    history = attempt_history(state, job_id)
    evaluation_attempts = [
        item for item in history["attempts"]
        if item["stage"] == "evaluation"
    ]
    assert len(evaluation_attempts) == 3
    assert all(
        item["diagnostic"] == {
            "exception_class": "OpenAICompatibleProviderError",
            "diagnostic_code": "provider_response_contract",
        }
        for item in evaluation_attempts
    )
    assert "secret-payload" not in json.dumps(history, sort_keys=True)


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
        "cleanup": Path("examples/ai/obsidian-ai-human-projection-cleanup-sync.service").read_text(),
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
    assert "User=obsidian-ai-sync" in units["cleanup"]
    assert "projection-cleanup.env" in units["cleanup"]
    assert "projection-cleanup-password" in units["cleanup"]
    assert "PROJECTION_CLEANUP_BASE_URL" in units["cleanup"]
    assert "PROJECTION_CLEANUP_USERNAME" in units["cleanup"]
    assert "InaccessiblePaths=/etc/obsidian-ai/webdav-password" in units["cleanup"]
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
    assert "Requires=obsidian-ai-post-review-reconcile.service" in units["cleanup"]
    assert "Requires=obsidian-pre-review-evaluator.service" in units["status"]
    assert "obsidian-ai-post-review-reconcile.service" in units["status"]
    assert "obsidian-ai-human-projection-cleanup-sync.service" in units["status"]
    assert "User=obsidian-ai-status" in units["status"]
    assert "Unit=obsidian-pre-review-status.service" in units["timer"]

    assert "PrivateNetwork=true" in units["validator"]
    assert "PrivateNetwork=true" in units["reader"]
    assert "User=root" not in "\n".join(units.values())
    assert "WantedBy=timers.target" in units["timer"]

    # Repository examples never enable/start the timer themselves.
    assert "systemctl enable" not in "\n".join(units.values())


# #411: fully synthetic Export Source -> isolated Context -> actual worker stages.
def _external_source_worker_fixture(tmp_path: Path):
    import sqlite3
    import zipfile

    from obsidian_automation.chatgpt_export_generator import (
        PROMPT_TEMPLATE_VERSION as EXTERNAL_PROMPT,
    )
    from obsidian_automation.chatgpt_export_generator import (
        prompt_template_sha256 as external_prompt_sha,
    )
    from obsidian_automation.chatgpt_export_intake import (
        inventory_ready,
        publish_archive,
    )
    from obsidian_automation.chatgpt_export_job_context import (
        store_external_context,
    )
    from obsidian_automation.chatgpt_export_reader import read_admitted_source_page
    from obsidian_automation.chatgpt_export_review_preview import (
        build_private_review_preview,
    )

    private_source = tmp_path / "private-synthetic-source.zip"
    msg = "This may be useful, but nothing is approved."
    conv = [{
        "id": "PRIVATE_SYNTHETIC_CONVERSATION",
        "title": "PRIVATE_SYNTHETIC_TITLE_DO_NOT_LOG",
        "current_node": "assistant",
        "mapping": {
            "root": {"id": "root", "parent": None, "children": ["user"], "message": None},
            "user": {"id": "user", "parent": "root", "children": ["assistant"],
                     "message": {"id": "user-msg", "author": {"role": "user"},
                                 "content": {"content_type": "text", "parts": [
                                     "We might consider adding an export reader."
                                 ]}}},
            "assistant": {"id": "assistant", "parent": "user", "children": [],
                          "message": {"id": "assistant-msg",
                                      "author": {"role": "assistant"},
                                      "content": {"content_type": "text", "parts": [msg]}}},
        },
    }]
    with zipfile.ZipFile(private_source, "w") as zipfile_out:
        zipfile_out.writestr("conversations.json", json.dumps(conv))
    private_source.chmod(0o600)
    root = tmp_path / "private-source-root"
    publish_archive(root, private_source)
    inventory_ready(root, scope="external-synthetic", commit=True)
    with sqlite3.connect(root / "state" / "journal.sqlite3") as db:
        source_hmac, content_sha, revision_sha = db.execute(
            "SELECT identity_hmac,content_sha,revision_sha FROM latest"
        ).fetchone()
    admitted_source = read_admitted_source_page(
        root, scope="external-synthetic", source_hmac=source_hmac,
        expected_content_sha256=content_sha,
        expected_revision_sha256=revision_sha,
    )
    assistant_slice = admitted_source.page.units[0].evidence[1]
    quote = assistant_slice.text
    proposal_json = json.dumps({
        "contract_version": "chatgpt-evidence-candidate-v0",
        "proposals": [{
            "unit_index": 0, "slice_index": 1, "quote_start": 0,
            "quote_end": len(quote), "quote": quote,
            "proposed_summary": "Assistant suggested a potential future feature.",
            "proposed_kind": "hypothesis_candidate",
        }],
    }).encode()
    preview = build_private_review_preview(
        admitted_source, proposal_wire_bytes=proposal_json,
    )
    vault = tmp_path / "vault"
    (vault / "11-Knowledge").mkdir(parents=True)
    (vault / "11-Knowledge" / "Existing.md").write_text(
        _knowledge_note("# Existing\n\nExisting unrelated synthetic note."),
        encoding="utf-8",
    )
    ai = tmp_path / "ai-state"
    ai.mkdir()
    for stage in (
        "00-Untrusted", "04-Index", "05-Context", "10-Validation",
        EVALUATION_REQUEST_STAGE, EVALUATION_CONTEXT_STAGE,
        EVALUATION_STAGE, "20-Review", "30-Receipts",
    ):
        (ai / stage).mkdir()
    (ai / "24-Locks" / "read-view").mkdir(parents=True)
    external_sha, _ = store_external_context(
        ai, private_root=root, preview=preview, candidate_index=0,
    )
    recipe_raw = json.loads(_recipe_bytes())
    recipe_raw["generator"]["prompt_template_version"] = EXTERNAL_PROMPT
    recipe_raw["generator"]["prompt_template_sha256"] = external_prompt_sha()
    recipe = parse_recipe(json.dumps(recipe_raw).encode())
    job = submit_job(
        ai, context_sha256=external_sha, recipe=recipe,
        external_source_opt_in=True,
    )
    return ai, vault, root, str(job["job_id"]), external_sha, preview


def test_external_source_worker_e2e_to_real_awaiting_human_review(
    monkeypatch, tmp_path: Path,
):
    import obsidian_automation.chatgpt_export_generator as external_gen
    from obsidian_automation.chatgpt_export_generator import (
        PROMPT_TEMPLATE_VERSION as EXTERNAL_PROMPT,
    )
    from obsidian_automation.chatgpt_export_generator import (
        prompt_template_sha256 as external_prompt_sha,
    )
    from obsidian_automation.chatgpt_export_job_context import load_external_context
    from obsidian_automation.generation_artifact import load_generation_record

    state, vault, private_root, job_id, context_sha, preview = (
        _external_source_worker_fixture(tmp_path)
    )
    context = load_external_context(state, context_sha)
    calls = []

    def fake_external_model(_base_url, **kwargs):
        calls.append(kwargs)
        assert "UNTRUSTED" in kwargs["system_prompt"]
        assert context.excerpt in kwargs["user_prompt"]
        assert "untrusted-evidence" in kwargs["user_prompt"]
        assert context.source_hmac not in kwargs["user_prompt"]
        assert context.revision_sha256 not in kwargs["user_prompt"]
        assert context.candidate_id not in kwargs["user_prompt"]
        result = {
            "title": "対話履歴にある未検証の機能候補",
            "category": "summary",
            "source_type": "other",
            "body": ("## 未検証の候補\n\n"
                     "元の対話で提示された仮説であり、採用や実装完了は不明。\n\n"
                     f"原文の引用: {context.excerpt}\n\n"
                     "利用前に本人確認と追加根拠の検証が必要。"),
            "evidence_anchor_sha256": context.anchor_sha256,
            "excerpt_sha256": context.excerpt_sha256,
        }
        return SimpleNamespace(identifier=GEN_MODEL), json.dumps(result, ensure_ascii=False).encode()

    monkeypatch.setattr(external_gen, "chat_content", fake_external_model)
    monkeypatch.setattr(
        worker, "evaluate_knowledge_note_with_openai_compatible",
        _fake_evaluator("manual_review"),
    )

    generated = worker.run_generator_worker(
        state, base_url="https://openai.example.invalid/v1",
        deployed_revision=REVISION, external_source_opt_in=True,
    )
    assert generated["state"] == "validating"
    assert len(calls) == 1
    generation = load_generation_record(state, generated["output"]["generation_sha256"])
    assert generation.record_version == 3
    assert generation.context_kind == "chatgpt-export"
    assert generation.generator.prompt_template_version == EXTERNAL_PROMPT
    assert generation.generator.prompt_template_sha256 == external_prompt_sha()
    assert generation.external_source is not None
    assert generation.external_source.revision_sha256 == preview.revision_sha256
    assert generation.external_source.candidate_id == context.candidate_id
    from dataclasses import replace
    with pytest.raises(ArtifactLifecycleError, match="external Generation Record binding mismatch"):
        store_generation_record(
            state, replace(
                generation,
                external_source=replace(
                    generation.external_source, revision_sha256="b" * 64,
                ),
            ),
        )

    validation = worker.run_validator_worker(state, vault)
    assert validation["state"] == "building_evaluation_context"
    reading = worker.run_reader_worker(state, vault)
    assert reading["state"] == "evaluating"
    from obsidian_automation.generation_artifact import generation_input_context
    from obsidian_automation.evaluator_contract import render_evaluator_prompts
    from obsidian_automation.evaluation_artifact import load_evaluation_context
    assert generation_input_context(state, generation) == context
    evaluation_context = load_evaluation_context(
        state, reading["output"]["evaluation_context_sha256"],
    )
    prompts = render_evaluator_prompts(
        target_path="11-Knowledge/合成評価.md",
        proposal_content=_knowledge_note(
            f"# 未検証の合成候補\n\n{context.excerpt}\n"),
        generation_context=context, evaluation_context=evaluation_context,
    )
    assert prompts
    assert any('"source_kind":"chatgpt-export"' in prompt.user for prompt in prompts)
    assert any(context.excerpt in prompt.user for prompt in prompts)
    assert all(context.source_hmac not in prompt.user for prompt in prompts)
    assert all(context.revision_sha256 not in prompt.user for prompt in prompts)
    assert all(context.candidate_id not in prompt.user for prompt in prompts)
    assert all("11-Knowledge/" not in context.scope for _ in prompts)
    evaluated = worker.run_evaluator_worker(
        state, base_url="https://openai.example.invalid/v1",
        deployed_revision=REVISION, external_source_opt_in=True,
    )
    assert evaluated["state"] == "awaiting_human_review"
    assert evaluated["output"]["recommendation"] == "manual_review"
    assert job_status(state, job_id)["current_generation"]["state"] == "awaiting_human_review"
    assert not list((state / "20-Review").iterdir())
    assert not (vault / "11-Knowledge" / "対話履歴にある未検証の機能候補.md").exists()
    assert list((private_root / "ready").iterdir())
    assert preview.awaiting_human_review is False  # preview itself never changes
    assert preview.job_status == "not_submitted"


def test_external_recipe_and_vault_context_are_not_interchangeable(tmp_path):
    from obsidian_automation.chatgpt_export_generator import (
        PROMPT_TEMPLATE_VERSION as EXTERNAL_PROMPT,
    )
    from obsidian_automation.chatgpt_export_generator import (
        prompt_template_sha256 as external_prompt_sha,
    )
    ai, _, _, _, ext_sha, _ = _external_source_worker_fixture(tmp_path)
    bundle = ContextBundle(
        query="Synthetic vault query", created_at="2026-09-19T00:00:00Z", sources=(),
    )
    legacy_sha, _ = store_context_bundle(ai, bundle)
    external_raw = json.loads(_recipe_bytes())
    external_raw["generator"]["prompt_template_version"] = EXTERNAL_PROMPT
    external_raw["generator"]["prompt_template_sha256"] = external_prompt_sha()
    external_recipe = parse_recipe(json.dumps(external_raw).encode())
    with pytest.raises(ArtifactLifecycleError):
        submit_job(ai, context_sha256=legacy_sha, recipe=external_recipe)
    legacy_recipe = parse_recipe(_recipe_bytes())
    with pytest.raises(ArtifactLifecycleError):
        submit_job(ai, context_sha256=ext_sha, recipe=legacy_recipe)
    external_raw["generator"]["provider"] = "ollama"
    with pytest.raises(Exception, match="external Source Generator v0 requires"):
        parse_recipe(json.dumps(external_raw).encode())


@pytest.mark.parametrize("violation", [
    "wrong_anchor", "missing_quote", "unverified_warning_missing",
    "source_type_self", "invented_key",
])
def test_external_fake_provider_evidence_contract_fail_closed(
    monkeypatch, tmp_path: Path, violation: str,
):
    import obsidian_automation.chatgpt_export_generator as external_gen
    from obsidian_automation.chatgpt_export_job_context import load_external_context

    state, vault, _root, job_id, context_sha, _preview = (
        _external_source_worker_fixture(tmp_path)
    )
    context = load_external_context(state, context_sha)
    def fake_invalid_provider(_url, **_kwargs):
        output = {
            "title": "未検証の情報源",
            "category": "summary",
            "source_type": "other",
            "body": f"## 未検証\n\n引用: {context.excerpt}",
            "evidence_anchor_sha256": context.anchor_sha256,
            "excerpt_sha256": context.excerpt_sha256,
        }
        if violation == "wrong_anchor":
            output["evidence_anchor_sha256"] = "a" * 64
        elif violation == "missing_quote":
            output["body"] = "## 未検証\n\n原文が欠落"
        elif violation == "unverified_warning_missing":
            output["body"] = f"## 採用済み\n\n{context.excerpt}"
        elif violation == "source_type_self":
            output["source_type"] = "self"
        else:
            output["authority"] = "approved"
        return SimpleNamespace(identifier=GEN_MODEL), json.dumps(
            output, ensure_ascii=False,
        ).encode("utf-8")

    monkeypatch.setattr(external_gen, "chat_content", fake_invalid_provider)
    completed = worker.run_generator_worker(
        state, base_url="https://openai.example.invalid/v1",
        deployed_revision=REVISION, max_attempts=1,
        external_source_opt_in=True,
    )
    assert completed["status"] == "retryable_failure"
    assert completed["state"] == "retryable_failure"
    assert worker.run_validator_worker(state, vault)["status"] == "idle"
    assert job_status(state, job_id)["current_generation"]["state"] != "awaiting_human_review"
    assert not list((state / "20-Review").iterdir())


@pytest.mark.parametrize("violation", ["decision_candidate", "result_candidate", "correction_candidate", "coverage_gap"])
def test_external_high_risk_claim_cannot_enter_job_before_semantic_adjudication(
    tmp_path: Path, violation: str,
):
    from dataclasses import replace

    from obsidian_automation.artifact_lifecycle import _store_immutable, sha256_bytes
    from obsidian_automation.chatgpt_export_generator import (
        PROMPT_TEMPLATE_VERSION as EXTERNAL_PROMPT,
    )
    from obsidian_automation.chatgpt_export_generator import (
        prompt_template_sha256 as external_prompt_sha,
    )
    from obsidian_automation.chatgpt_export_job_context import (
        SOURCE_CONTEXT_SUFFIX,
        ExternalContextError,
        load_external_context,
    )

    state, _vault, _private, _job_id, context_sha, _preview = (
        _external_source_worker_fixture(tmp_path)
    )
    context = load_external_context(state, context_sha)
    if violation == "coverage_gap":
        forged = replace(
            context, risks=context.risks + ("source_coverage_incomplete",),
        )
        match = "external_source_coverage_incomplete"
    else:
        forged = replace(context, proposed_kind=violation)
        match = "external_source_semantic_review_required"
    wire = forged.to_json_bytes()
    sha = sha256_bytes(wire)
    path = state / "05-Context" / f"{sha}.{SOURCE_CONTEXT_SUFFIX}.json"
    _store_immutable(path, wire)
    recipe_raw = json.loads(_recipe_bytes())
    recipe_raw["generator"]["prompt_template_version"] = EXTERNAL_PROMPT
    recipe_raw["generator"]["prompt_template_sha256"] = external_prompt_sha()
    recipe = parse_recipe(json.dumps(recipe_raw).encode())
    with pytest.raises(ExternalContextError, match=match):
        submit_job(
            state, context_sha256=sha, recipe=recipe,
            external_source_opt_in=True,
        )
    with pytest.raises(ArtifactLifecycleError):
        # Vault recipes cannot use the external Context kind.
        submit_job(state, context_sha256=sha, recipe=parse_recipe(_recipe_bytes()))


def test_external_submission_is_explicitly_disabled_without_opt_in(tmp_path):
    from obsidian_automation.chatgpt_export_generator import (
        PROMPT_TEMPLATE_VERSION as EXTERNAL_PROMPT,
    )
    from obsidian_automation.chatgpt_export_generator import (
        prompt_template_sha256 as external_prompt_sha,
    )
    from obsidian_automation.pre_review_job import PreReviewJobError

    state, _vault, _private, _job_id, context_sha, _preview = (
        _external_source_worker_fixture(tmp_path)
    )
    raw = json.loads(_recipe_bytes())
    raw["generator"]["prompt_template_version"] = EXTERNAL_PROMPT
    raw["generator"]["prompt_template_sha256"] = external_prompt_sha()
    recipe = parse_recipe(json.dumps(raw).encode())
    with pytest.raises(PreReviewJobError, match="disabled by default"):
        submit_job(state, context_sha256=context_sha, recipe=recipe)


def test_external_queued_job_cannot_call_provider_without_runtime_opt_in(
    monkeypatch, tmp_path,
):
    import obsidian_automation.chatgpt_export_generator as external_gen

    state, _vault, _private, job_id, _sha, _preview = (
        _external_source_worker_fixture(tmp_path)
    )
    contacted = False

    def forbidden_provider(*_args, **_kwargs):
        nonlocal contacted
        contacted = True
        raise AssertionError("default production worker must not invoke external Source model")

    monkeypatch.setattr(external_gen, "chat_content", forbidden_provider)
    result = worker.run_generator_worker(
        state, base_url="https://openai.example.invalid/v1",
        deployed_revision=REVISION,
    )
    assert result["status"] == "blocked"
    assert result["reason_code"] == "external_source_runtime_disabled"
    assert not contacted
    assert job_status(state, job_id)["current_generation"]["state"] == "blocked"
    assert not list((state / "20-Review").iterdir())


def test_external_evaluator_cannot_dispatch_without_separate_opt_in(
    monkeypatch, tmp_path,
):
    import obsidian_automation.chatgpt_export_generator as external_gen
    from obsidian_automation.chatgpt_export_job_context import load_external_context

    state, vault, _private, job_id, context_sha, _preview = (
        _external_source_worker_fixture(tmp_path)
    )
    context = load_external_context(state, context_sha)
    def fake_model(_url, **_kwargs):
        return SimpleNamespace(identifier=GEN_MODEL), json.dumps({
            "title": "仮説に基づく未検証の候補",
            "category": "summary",
            "source_type": "other",
            "body": f"## 未検証\n\n引用: {context.excerpt}",
            "evidence_anchor_sha256": context.anchor_sha256,
            "excerpt_sha256": context.excerpt_sha256,
        }, ensure_ascii=False).encode()

    calls = []
    def fail_if_evaluator_contacted(*_args, **_kwargs):
        calls.append("called")
        raise AssertionError("production evaluator unexpectedly sent private Source")

    monkeypatch.setattr(external_gen, "chat_content", fake_model)
    monkeypatch.setattr(
        worker, "evaluate_knowledge_note_with_openai_compatible",
        fail_if_evaluator_contacted,
    )
    assert worker.run_generator_worker(
        state, base_url="https://openai.example.invalid/v1",
        deployed_revision=REVISION, external_source_opt_in=True,
    )["state"] == "validating"
    assert worker.run_validator_worker(state, vault)["state"] == "building_evaluation_context"
    assert worker.run_reader_worker(state, vault)["state"] == "evaluating"

    result = worker.run_evaluator_worker(
        state, base_url="https://openai.example.invalid/v1",
        deployed_revision=REVISION,
    )
    assert result["state"] == "blocked"
    assert result["reason_code"] == "external_evaluator_runtime_disabled"
    assert not calls
    assert job_status(state, job_id)["current_generation"]["state"] == "blocked"
    assert not list((state / "20-Review").iterdir())
