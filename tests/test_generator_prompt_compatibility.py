from __future__ import annotations

from pathlib import Path

import pytest

import obsidian_automation.ai_input_planner as planner
from obsidian_automation.artifact_lifecycle import (
    ArtifactLifecycleError,
    _canonical_json_bytes,
    sha256_bytes,
)
from obsidian_automation.context_bundle import ContextBundle, store_context_bundle
from obsidian_automation.evaluator_contract import (
    EVALUATOR_PROMPT_TEMPLATE_VERSION,
    prompt_template_sha256 as evaluator_prompt_sha256,
)
from obsidian_automation.generator_contract import (
    PROMPT_TEMPLATE_V0_SHA256,
    PROMPT_TEMPLATE_V0_VERSION,
    PROMPT_TEMPLATE_V1_SHA256,
    PROMPT_TEMPLATE_VERSION,
    OUTPUT_CONTRACT_VERSION,
    parse_generator_output,
    prompt_template_bytes,
    prompt_template_sha256,
)
from obsidian_automation.generator_output_formatter import format_generator_output
from obsidian_automation.pre_review_job import (
    PreReviewJobError,
    parse_recipe,
    submit_job,
)


REVISION = "a" * 40
GENERATOR_MODEL = "gemma4:12b"
EVALUATOR_MODEL = "gemma4:12b-evaluator"
EVALUATOR_PROMPT_VERSION = EVALUATOR_PROMPT_TEMPLATE_VERSION
EVALUATOR_PROMPT_SHA256 = evaluator_prompt_sha256()


def _recipe_value(
    *,
    generator_version: str = PROMPT_TEMPLATE_VERSION,
    generator_sha256: str = PROMPT_TEMPLATE_V1_SHA256,
) -> dict[str, object]:
    return {
        "record_version": 1,
        "pipeline": "knowledge-pre-review-v0",
        "generator": {
            "implementation_revision": REVISION,
            "prompt_template_version": generator_version,
            "prompt_template_sha256": generator_sha256,
            "provider": "openai-compatible",
            "model_identifier": GENERATOR_MODEL,
            "model_revision": f"identifier:{GENERATOR_MODEL}",
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
            "implementation_revision": REVISION,
            "prompt_template_version": EVALUATOR_PROMPT_VERSION,
            "prompt_template_sha256": EVALUATOR_PROMPT_SHA256,
            "provider": "openai-compatible",
            "model_identifier": EVALUATOR_MODEL,
            "model_revision": f"identifier:{EVALUATOR_MODEL}",
            "model_config": {
                "adapter_version": "openai-evaluator-chat-completions-json-schema-v3",
                "identity_binding": "identifier-only",
                "strategy": "groundedness-plus-pairwise-candidates-with-verifier-v1",
                "options": {"temperature": 0},
            },
        },
    }


def _recipe_bytes(**kwargs: object) -> bytes:
    return _canonical_json_bytes(_recipe_value(**kwargs))


def _semantic_output(body: str) -> bytes:
    return _canonical_json_bytes(
        {
            "title": "Prompt compatibility fixture",
            "category": "manual",
            "source_type": "self",
            "body": body,
        }
    )


def test_current_generator_prompt_identity_is_pinned_v1() -> None:
    assert OUTPUT_CONTRACT_VERSION == "knowledge-note-semantic-output-v0"
    assert PROMPT_TEMPLATE_VERSION == "knowledge-note-generator-v1"
    assert prompt_template_sha256() == PROMPT_TEMPLATE_V1_SHA256
    assert sha256_bytes(prompt_template_bytes()) == PROMPT_TEMPLATE_V1_SHA256
    assert PROMPT_TEMPLATE_V1_SHA256 != PROMPT_TEMPLATE_V0_SHA256
    manifest = prompt_template_bytes().decode("utf-8")
    assert "Wire JSON and decoded body representation are distinct" in manifest
    assert "actual LF characters" in manifest


def test_planner_emits_the_current_generator_prompt_pair() -> None:
    recipe = planner._build_recipe(
        deployed_revision=REVISION,
        generator_provider="openai-compatible",
        generator_model=GENERATOR_MODEL,
        generator_model_revision=None,
        evaluator_provider="openai-compatible",
        evaluator_model=EVALUATOR_MODEL,
        evaluator_model_revision=None,
    )

    assert recipe.generator.prompt_template_version == PROMPT_TEMPLATE_VERSION
    assert recipe.generator.prompt_template_sha256 == PROMPT_TEMPLATE_V1_SHA256


def test_historical_v0_recipe_round_trips_without_reinterpretation() -> None:
    raw = _recipe_bytes(
        generator_version=PROMPT_TEMPLATE_V0_VERSION,
        generator_sha256=PROMPT_TEMPLATE_V0_SHA256,
    )

    parsed = parse_recipe(raw)

    assert parsed.generator.prompt_template_version == PROMPT_TEMPLATE_V0_VERSION
    assert parsed.generator.prompt_template_sha256 == PROMPT_TEMPLATE_V0_SHA256
    assert parsed.to_json_bytes() == raw


def test_current_v1_recipe_round_trips_canonically() -> None:
    raw = _recipe_bytes()
    parsed = parse_recipe(raw)

    assert parsed.generator.prompt_template_version == PROMPT_TEMPLATE_VERSION
    assert parsed.generator.prompt_template_sha256 == PROMPT_TEMPLATE_V1_SHA256
    assert parsed.to_json_bytes() == raw
    assert parse_recipe(parsed.to_json_bytes()) == parsed


@pytest.mark.parametrize(
    ("generator_version", "generator_sha256"),
    [
        ("knowledge-note-generator-v9", PROMPT_TEMPLATE_V1_SHA256),
        (PROMPT_TEMPLATE_V0_VERSION, PROMPT_TEMPLATE_V1_SHA256),
        (PROMPT_TEMPLATE_VERSION, PROMPT_TEMPLATE_V0_SHA256),
        (PROMPT_TEMPLATE_VERSION, "f" * 64),
    ],
)
def test_generator_prompt_identity_allowlist_fails_closed(
    generator_version: str,
    generator_sha256: str,
) -> None:
    with pytest.raises(PreReviewJobError, match="prompt_template"):
        parse_recipe(
            _recipe_bytes(
                generator_version=generator_version,
                generator_sha256=generator_sha256,
            )
        )


def test_same_context_and_model_get_distinct_v0_and_v1_job_identity(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    (state / "02-Orchestration" / "recipes").mkdir(parents=True)
    (state / "05-Context").mkdir()
    (state / "24-Locks" / "read-view").mkdir(parents=True)
    context_sha256, _ = store_context_bundle(
        state,
        ContextBundle(
            query="Create a grounded note",
            created_at="2026-09-22T00:00:00Z",
            sources=(),
        ),
    )

    v0 = parse_recipe(
        _recipe_bytes(
            generator_version=PROMPT_TEMPLATE_V0_VERSION,
            generator_sha256=PROMPT_TEMPLATE_V0_SHA256,
        )
    )
    v1 = parse_recipe(_recipe_bytes())
    old_job = submit_job(state, context_sha256=context_sha256, recipe=v0)
    new_job = submit_job(state, context_sha256=context_sha256, recipe=v1)

    assert old_job["recipe_sha256"] != new_job["recipe_sha256"]
    assert old_job["job_id"] != new_job["job_id"]
    assert old_job["generation_id"] != new_job["generation_id"]


def test_json_wire_newline_escape_decodes_to_an_actual_lf() -> None:
    wire = (
        b'{"title":"Wire","category":"manual","source_type":"self",'
        b'"body":"A paragraph.\\n\\n- an item"}\n'
    )

    parsed = parse_generator_output(format_generator_output(wire))

    assert parsed.body == "A paragraph.\n\n- an item"


@pytest.mark.parametrize(
    "body",
    [
        "A paragraph.\\n",
        "A paragraph.\\n\\n- an item",
    ],
)
def test_double_escaped_prose_and_list_boundaries_remain_rejected(body: str) -> None:
    wire = _semantic_output(body)

    with pytest.raises(ArtifactLifecycleError, match="escaped"):
        parse_generator_output(format_generator_output(wire))
