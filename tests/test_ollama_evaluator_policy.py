from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import obsidian_automation.ai_input_planner as ai_input_planner
import obsidian_automation.ollama_generator as ollama_generator
import obsidian_automation.pre_review_worker as pre_review_worker
from obsidian_automation.artifact_lifecycle import (
    sha256_bytes,
    store_untrusted_proposal,
)
from obsidian_automation.context_bundle import (
    ContextBundle,
    build_context_bundle,
    store_context_bundle,
)
from obsidian_automation.evaluation_artifact import (
    EVALUATION_CONTEXT_STAGE,
    EVALUATION_REQUEST_STAGE,
    EVALUATION_STAGE,
    build_evaluation_context,
    create_evaluation_request,
    load_evaluation_record,
    store_evaluation_context,
)
from obsidian_automation.evaluator_contract import (
    EVALUATOR_PROMPT_TEMPLATE_VERSION,
    prompt_template_sha256 as evaluator_prompt_sha256,
)
from obsidian_automation.generation_artifact import (
    build_generation_record,
    store_generation_record,
)
from obsidian_automation.generator_contract import (
    PROMPT_TEMPLATE_VERSION,
    prompt_template_sha256 as generator_prompt_sha256,
)
from obsidian_automation.knowledge_index import build_knowledge_index, store_knowledge_index
from obsidian_automation.knowledge_validator import validate_proposal
from obsidian_automation.ollama_evaluator import (
    ADAPTER_VERSION as OLLAMA_EVALUATOR_ADAPTER_VERSION,
    EVALUATION_STRATEGY,
    OllamaProviderError,
    evaluate_knowledge_note_with_ollama,
)
from obsidian_automation.pre_review_job import (
    PreReviewJobError,
    StageWorkItem,
    parse_recipe,
    submit_job,
)


REVISION = "a" * 40
MODEL_DIGEST = "b" * 64
GENERATOR_MODEL = "gemma4:12b"
EVALUATOR_MODEL = "gemma4:12b-eval"


def _planner_ollama_recipe():
    return ai_input_planner._build_recipe(
        deployed_revision=REVISION,
        generator_provider="ollama",
        generator_model=GENERATOR_MODEL,
        generator_model_revision=MODEL_DIGEST,
        evaluator_provider="ollama",
        evaluator_model=EVALUATOR_MODEL,
        evaluator_model_revision=MODEL_DIGEST,
    )


def _recipe_bytes(think: object) -> bytes:
    value = json.loads(_planner_ollama_recipe().to_json_bytes())
    assert isinstance(value, dict)
    evaluator = value["evaluator"]
    assert isinstance(evaluator, dict)
    model_config = evaluator["model_config"]
    assert isinstance(model_config, dict)
    model_config["think"] = think
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def _recipe(think: object):
    return parse_recipe(_recipe_bytes(think))


def test_planner_uses_false_for_native_ollama_evaluator_and_generator() -> None:
    recipe = _planner_ollama_recipe()

    assert recipe.generator.provider == "ollama"
    assert recipe.generator.model_config["think"] is False
    assert recipe.evaluator.provider == "ollama"
    assert recipe.evaluator.model_config["think"] is False


@pytest.mark.parametrize("think", [False, "low"], ids=["false", "historical-low"])
def test_current_and_historical_ollama_think_values_canonical_round_trip(
    think: bool | str,
) -> None:
    parsed = parse_recipe(_recipe_bytes(think))

    assert parsed.generator.model_config["think"] is False
    assert parsed.evaluator.model_config["think"] == think
    assert type(parsed.evaluator.model_config["think"]) is type(think)

    canonical = parsed.to_json_bytes()
    reparsed = parse_recipe(canonical)
    assert reparsed == parsed
    assert reparsed.to_json_bytes() == canonical


@pytest.mark.parametrize("think", ["medium", "high", "max", "xhigh"])
def test_unsupported_ollama_evaluator_think_values_are_rejected(think: str) -> None:
    with pytest.raises(PreReviewJobError):
        parse_recipe(_recipe_bytes(think))


def _context_state(tmp_path: Path) -> tuple[Path, str]:
    state = tmp_path / "state"
    state.mkdir()
    (state / "05-Context").mkdir()
    context = ContextBundle(
        query="Ollama evaluator policy",
        created_at="2026-09-21T00:00:00Z",
        sources=(),
    )
    context_sha, _ = store_context_bundle(state, context)
    return state, context_sha


def test_low_and_false_recipes_have_distinct_canonical_and_job_identity(
    tmp_path: Path,
) -> None:
    low = _recipe("low")
    current = _recipe(False)

    low_value = json.loads(low.to_json_bytes())
    current_value = json.loads(current.to_json_bytes())
    low_value["evaluator"]["model_config"]["think"] = False
    assert low_value == current_value

    low_bytes = low.to_json_bytes()
    current_bytes = current.to_json_bytes()
    assert low_bytes != current_bytes
    assert sha256_bytes(low_bytes) != sha256_bytes(current_bytes)

    state, context_sha = _context_state(tmp_path)
    low_submission = submit_job(state, context_sha256=context_sha, recipe=low)
    current_submission = submit_job(state, context_sha256=context_sha, recipe=current)

    assert low_submission["recipe_sha256"] == sha256_bytes(low_bytes)
    assert current_submission["recipe_sha256"] == sha256_bytes(current_bytes)
    assert low_submission["recipe_sha256"] != current_submission["recipe_sha256"]
    assert low_submission["job_id"] != current_submission["job_id"]
    assert low_submission["generation_id"] != current_submission["generation_id"]


@pytest.mark.parametrize("think", [False, "low"], ids=["false", "historical-low"])
def test_evaluator_worker_passes_recipe_think_value_exactly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    think: bool | str,
) -> None:
    recipe = _recipe(think)
    work = StageWorkItem(
        job_id="1" * 64,
        generation_id="2" * 64,
        generation_index=1,
        context_sha256="3" * 64,
        recipe_sha256="4" * 64,
        attempt_id="5" * 64,
        attempt_index=1,
        stage="evaluation",
    )
    selected = {
        "proposal_sha256": "6" * 64,
        "generation_sha256": "7" * 64,
        "mutation_sha256": "8" * 64,
        "request_sha256": "9" * 64,
        "index_sha256": "a" * 64,
        "evaluation_context_sha256": "b" * 64,
    }
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        pre_review_worker,
        "claim_next_attempt",
        lambda *_args, **_kwargs: work,
    )
    monkeypatch.setattr(pre_review_worker, "load_recipe", lambda *_args: recipe)
    monkeypatch.setattr(pre_review_worker, "stage_output", lambda *_args: selected)

    def fake_evaluate(*_args: object, **kwargs: object):
        calls.append(dict(kwargs))
        return SimpleNamespace(evaluation_sha256="c" * 64, recommendation="proceed")

    monkeypatch.setattr(
        pre_review_worker,
        "evaluate_knowledge_note_with_ollama",
        fake_evaluate,
    )
    monkeypatch.setattr(
        pre_review_worker,
        "load_evaluation_record",
        lambda *_args: SimpleNamespace(
            proposal_sha256=selected["proposal_sha256"],
            mutation_sha256=selected["mutation_sha256"],
            generation_sha256=selected["generation_sha256"],
            evaluation_context_sha256=selected["evaluation_context_sha256"],
            evaluator=SimpleNamespace(
                implementation_revision=REVISION,
                prompt_template_version=EVALUATOR_PROMPT_TEMPLATE_VERSION,
                prompt_template_sha256=evaluator_prompt_sha256(),
            ),
            model=SimpleNamespace(
                provider="ollama",
                identifier=EVALUATOR_MODEL,
                revision=MODEL_DIGEST,
            ),
            model_config=dict(recipe.evaluator.model_config),
            assessment=SimpleNamespace(recommendation="proceed"),
        ),
    )
    monkeypatch.setattr(
        pre_review_worker,
        "emit_evaluation_and_review_projections",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        pre_review_worker,
        "complete_attempt",
        lambda *_args, **kwargs: {
            "state": "awaiting_human_review",
            "output": kwargs["output"],
        },
    )

    result = pre_review_worker.run_evaluator_worker(
        tmp_path,
        base_url="https://ollama.example.test/v1",
        deployed_revision=REVISION,
    )

    assert result["status"] == "completed"
    assert len(calls) == 1
    assert calls[0]["think"] == think
    assert type(calls[0]["think"]) is type(think)
    assert calls[0]["options"] == {"temperature": 0}


def _note(body: str) -> str:
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


def _evaluator_fixture(tmp_path: Path) -> tuple[Path, str, str, str]:
    vault = tmp_path / "vault"
    knowledge = vault / "11-Knowledge"
    knowledge.mkdir(parents=True)
    existing_path = "11-Knowledge/Existing.md"
    (vault / existing_path).write_text(
        _note("# Existing policy\n\nOllama evaluator policy is grounded in the supplied context."),
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
    ):
        (state / stage).mkdir()
    (state / "24-Locks" / "read-view").mkdir(parents=True)

    proposal = {
        "contract_version": 1,
        "operation": "create_note",
        "mutation_id": "ollama-evaluator-policy-test",
        "target": {"path": "11-Knowledge/Generated.md"},
        "content": _note(
            "# Generated policy\n\nOllama evaluator policy is grounded in the supplied context."
        ),
    }
    proposal_sha, _ = store_untrusted_proposal(
        state,
        (json.dumps(proposal, ensure_ascii=False, separators=(",", ":")) + "\n").encode(),
    )
    assert validate_proposal(state, vault, proposal_sha)["result"] == "accepted"

    request_sha, _, _ = create_evaluation_request(state, proposal_sha)
    index_sha, _ = store_knowledge_index(state, build_knowledge_index(vault))
    evaluation_context = build_evaluation_context(
        state,
        vault,
        request_sha256=request_sha,
        index_sha256=index_sha,
        created_at="2026-09-21T00:01:00Z",
    )
    evaluation_context_sha, _ = store_evaluation_context(state, evaluation_context)

    generation_context = build_context_bundle(
        vault,
        query="Ollama evaluator policy",
        source_paths=[existing_path],
        created_at="2026-09-21T00:02:00Z",
    )
    generation_context_sha, _ = store_context_bundle(state, generation_context)
    generation = build_generation_record(
        state,
        context_sha256=generation_context_sha,
        proposal_sha256=proposal_sha,
        implementation_revision=REVISION,
        prompt_template_version=PROMPT_TEMPLATE_VERSION,
        prompt_template_sha256=generator_prompt_sha256(),
        model_provider="ollama",
        model_identifier=GENERATOR_MODEL,
        model_revision=MODEL_DIGEST,
        model_config={"adapter_version": "fixture", "think": False},
        generated_at="2026-09-21T00:03:00Z",
    )
    generation_sha, _ = store_generation_record(state, generation)
    return state, proposal_sha, generation_sha, evaluation_context_sha


def _evaluator_transport(calls: list[dict[str, object]]):
    def transport(base_url: str, **kwargs: object) -> dict[str, object]:
        calls.append({"base_url": base_url, **kwargs})
        if kwargs["path"] == "/api/tags":
            return {
                "models": [
                    {
                        "name": EVALUATOR_MODEL,
                        "model": EVALUATOR_MODEL,
                        "digest": MODEL_DIGEST,
                    }
                ]
            }

        payload = kwargs["payload"]
        assert isinstance(payload, dict)
        messages = payload["messages"]
        assert isinstance(messages, list)
        user = json.loads(messages[1]["content"])
        dimension = user["dimension"]
        assessment = {
            "groundedness": "pass",
            "redundancy": "none",
            "consistency": "pass",
        }[dimension]
        return {
            "model": EVALUATOR_MODEL,
            "done": True,
            "message": {
                "role": "assistant",
                "content": json.dumps(
                    {
                        "assessment": assessment,
                        "findings": [],
                        **({"conflicts": []} if dimension == "consistency" else {}),
                    },
                    separators=(",", ":"),
                ),
            },
        }

    return transport


@pytest.mark.parametrize("think", [False, "low"], ids=["false", "historical-low"])
def test_ollama_evaluator_preserves_think_in_payload_and_record(
    tmp_path: Path,
    think: bool | str,
) -> None:
    state, proposal_sha, generation_sha, evaluation_context_sha = _evaluator_fixture(tmp_path)
    calls: list[dict[str, object]] = []

    result = evaluate_knowledge_note_with_ollama(
        state,
        proposal_sha256=proposal_sha,
        generation_sha256=generation_sha,
        evaluation_context_sha256=evaluation_context_sha,
        base_url="https://ollama.example.test",
        model=EVALUATOR_MODEL,
        implementation_revision=REVISION,
        think=think,
        transport=_evaluator_transport(calls),
    )

    record = load_evaluation_record(state, result.evaluation_sha256)
    assert record.model_config == {
        "adapter_version": OLLAMA_EVALUATOR_ADAPTER_VERSION,
        "think": think,
        "strategy": EVALUATION_STRATEGY,
        "options": {"temperature": 0},
    }
    chat_calls = calls[1:]
    assert chat_calls
    assert [call["payload"]["think"] for call in chat_calls] == [think] * len(chat_calls)
    assert all(type(call["payload"]["think"]) is type(think) for call in chat_calls)


def test_native_ollama_transport_enforces_total_response_read_timeout(monkeypatch) -> None:
    class Response:
        fp = None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, size: int) -> bytes:
            assert size == 1
            return b"x"

    class Opener:
        def open(self, _request, timeout: float):
            assert timeout == 1.0
            return Response()

    clock = iter((0.0, 0.5, 2.0))
    monkeypatch.setattr(ollama_generator.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(ollama_generator, "_direct_opener", lambda: Opener())

    with pytest.raises(OllamaProviderError, match="response read exceeded"):
        ollama_generator._request_json(
            "https://ollama.example.test",
            method="POST",
            path="/api/chat",
            payload={"ok": True},
            timeout=1.0,
        )
