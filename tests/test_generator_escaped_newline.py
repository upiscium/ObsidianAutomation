from __future__ import annotations

import json
from pathlib import Path

import pytest

import obsidian_automation.pre_review_worker as worker
from obsidian_automation.artifact_lifecycle import ArtifactLifecycleError
from obsidian_automation.context_bundle import ContextBundle, store_context_bundle
from obsidian_automation.evaluator_contract import (
    EVALUATOR_PROMPT_TEMPLATE_VERSION,
    prompt_template_sha256 as evaluator_prompt_sha256,
)
from obsidian_automation.generator_contract import (
    PROMPT_TEMPLATE_VERSION,
    parse_generator_output,
    prompt_template_sha256 as generator_prompt_sha256,
)
from obsidian_automation.generator_output_formatter import format_generator_output
from obsidian_automation.openai_compatible import identifier_revision
from obsidian_automation.openai_evaluator import (
    ADAPTER_VERSION as OPENAI_EVALUATOR_ADAPTER_VERSION,
    EVALUATION_STRATEGY,
)
from obsidian_automation.openai_generator import (
    ADAPTER_VERSION as OPENAI_GENERATOR_ADAPTER_VERSION,
)
from obsidian_automation.pre_review_job import (
    job_status,
    parse_recipe,
    submit_job,
)


REVISION = "a" * 40
GENERATOR_MODEL = "issue-155-generator"
EVALUATOR_MODEL = "issue-155-evaluator"


def _semantic_output(body: str) -> bytes:
    """Encode the semantic response as a provider would, including JSON escapes."""
    return (
        json.dumps(
            {
                "title": "Issue 155 fixture",
                "category": "manual",
                "source_type": "self",
                "body": body,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _parse_provider_output(body: str):
    return parse_generator_output(format_generator_output(_semantic_output(body)))


@pytest.mark.parametrize(
    "body",
    [
        "A first paragraph.\n\nA second paragraph.\n",
        "A checklist.\n\n- first item\n- second item\n",
    ],
)
def test_proper_lf_prose_and_list_output_is_accepted(body: str) -> None:
    parsed = _parse_provider_output(body)

    assert parsed.body == body


@pytest.mark.parametrize(
    "body",
    [
        "A paragraph ending at a boundary.\\n",
        "A first paragraph.\\nA second paragraph.",
        "A first paragraph.\\n\\nA second paragraph.",
        "A checklist.\\n- first item\\n- second item",
        "Checklist: \\n- first item",
        "Intro\n\\n- an accidentally escaped list item",
        "\\n- an escaped list item at the start",
        "`A sentence.\\n- item``",
        "The protocol's result. A sentence.\\nNext user's note.",
        "The users' data. A sentence.\\nNext 'literal'.",
        "-```text\nA sentence.\\nNext sentence.",
        "```text\n\" string\n```\nA sentence.\\nNext\"",
        "```text\nprotocol\n```\n'A sentence.\\nNext'",
        'He wrote "Done."\\nNext paragraph.',
    ],
)
def test_production_literal_newline_boundaries_are_rejected_without_repair(
    body: str,
) -> None:
    raw = _semantic_output(body)

    try:
        formatted = format_generator_output(raw)
    except ArtifactLifecycleError:
        # Rejecting at the formatting boundary is also non-repairing.
        pass
    else:
        assert json.loads(formatted)["body"] == body
        with pytest.raises(ArtifactLifecycleError):
            parse_generator_output(formatted)

    # The semantic response is not rewritten in place, and the literal escape
    # remains visible in the rejected provider payload.
    assert json.loads(raw)["body"] == body


def test_literal_newline_inside_fenced_and_inline_code_is_preserved() -> None:
    body = (
        "```text\n"
        "literal \\n\\n- text inside the fenced example\n"
        "```\n\n"
        "Inline `\\n\\n-` remains a literal escaped boundary.\n"
    )

    parsed = _parse_provider_output(body)

    assert parsed.body == body


def test_literal_newline_inside_inline_code_before_a_backslash_is_preserved() -> None:
    body = "Inline `A sentence.\\nNext\\` remains code."

    parsed = _parse_provider_output(body)

    assert parsed.body == body


def test_literal_newline_inside_a_root_fence_is_not_closed_by_blockquote_text() -> None:
    body = "```text\n> ```\nliteral \\n- text remains fenced\n```\n"

    parsed = _parse_provider_output(body)

    assert parsed.body == body


def test_root_fence_does_not_close_at_four_space_indentation() -> None:
    body = "```text\n    ```\nliteral \\n- text remains fenced\n```\n"

    parsed = _parse_provider_output(body)

    assert parsed.body == body


def test_literal_newline_inside_blockquoted_fenced_code_is_preserved() -> None:
    body = "> ```text\n> literal \\n- text inside the example\n> ```\n"

    parsed = _parse_provider_output(body)

    assert parsed.body == body


@pytest.mark.parametrize(
    "body",
    [
        ">     literal \\n- text inside indented code\n",
        ">  \tliteral.\\nNext\n",
        "- ```text\n  literal \\n- text inside a list fence\n  ```\n",
        "  - ```text\n    literal \\n- text inside a nested list fence\n    ```\n",
        "- > ```text\n  > A sentence.\\nNext\n  > ```\n",
        "> ```text\n> > ```\n> A sentence.\\n- item\n> ```\n",
    ],
)
def test_literal_newline_inside_nested_indented_code_is_preserved(body: str) -> None:
    parsed = _parse_provider_output(body)

    assert parsed.body == body


def test_technical_prose_can_discuss_literal_backslash_n_without_rewriting() -> None:
    body = (
        r'The string "\n" denotes a line-feed escape in this technical example.'
        "\n"
        r'The protocol token "\n-" is a literal marker.'
        "\n"
        r'The JSON string is "done.\nnext".'
        "\n"
        r'The JSON string is "done. `example`.\nnext".'
        "\n"
        r'The quoted example is "JSON string: done.\nnext".'
        "\n"
        r"The protocol says 'A user's result.\nNext'."
        "\n"
        r"The escape sequence:\n"
    )
    raw = _semantic_output(body)

    formatted = format_generator_output(raw)
    assert json.loads(formatted)["body"] == body
    assert parse_generator_output(formatted).body == body


def test_crlf_formatter_behavior_is_unchanged() -> None:
    body = "A paragraph.\r\n\r\n- an item\r\nThe token \\n remains literal.\r\n"
    formatted = format_generator_output(_semantic_output(body))
    expected = body.replace("\r\n", "\n")

    assert json.loads(formatted)["body"] == expected
    assert parse_generator_output(formatted).body == expected


def test_formatter_rejects_unencodable_json_as_a_lifecycle_error() -> None:
    raw = json.dumps(
        {
            "title": "Issue 155 fixture",
            "category": "manual",
            "source_type": "self",
            "body": "\ud800",
        },
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")

    with pytest.raises(ArtifactLifecycleError):
        format_generator_output(raw)


def _recipe_bytes() -> bytes:
    generator = {
        "implementation_revision": REVISION,
        "prompt_template_version": PROMPT_TEMPLATE_VERSION,
        "prompt_template_sha256": generator_prompt_sha256(),
        "provider": "openai-compatible",
        "model_identifier": GENERATOR_MODEL,
        "model_revision": identifier_revision(GENERATOR_MODEL),
        "model_config": {
            "adapter_version": OPENAI_GENERATOR_ADAPTER_VERSION,
            "identity_binding": "identifier-only",
            "options": {"temperature": 0},
        },
    }
    evaluator = {
        "implementation_revision": REVISION,
        "prompt_template_version": EVALUATOR_PROMPT_TEMPLATE_VERSION,
        "prompt_template_sha256": evaluator_prompt_sha256(),
        "provider": "openai-compatible",
        "model_identifier": EVALUATOR_MODEL,
        "model_revision": identifier_revision(EVALUATOR_MODEL),
        "model_config": {
            "adapter_version": OPENAI_EVALUATOR_ADAPTER_VERSION,
            "identity_binding": "identifier-only",
            "strategy": EVALUATION_STRATEGY,
            "options": {"temperature": 0},
        },
    }
    return (
        json.dumps(
            {
                "record_version": 1,
                "pipeline": "knowledge-pre-review-v0",
                "generator": generator,
                "validator": {"policy": "knowledge-note-v0"},
                "evaluation_context": {
                    "selection_policy": "bm25-topk-recall-v0",
                    "top_k": 5,
                },
                "evaluator": evaluator,
            },
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


@pytest.fixture
def production_fixture(tmp_path: Path) -> tuple[Path, Path, str]:
    """Create only the temporary state and Vault roots used by the worker chain."""
    state = tmp_path / "state"
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
        "30-Receipts",
    ):
        (state / stage).mkdir()

    vault = tmp_path / "vault"
    (vault / "11-Knowledge").mkdir(parents=True)

    context = ContextBundle(
        query="Create a grounded Knowledge Note",
        created_at="2026-09-21T00:00:00Z",
        sources=(),
    )
    context_sha256, _ = store_context_bundle(state, context)
    recipe = parse_recipe(_recipe_bytes())
    submitted = submit_job(state, context_sha256=context_sha256, recipe=recipe)
    return state, vault, str(submitted["job_id"])


def _provider_transport(body: str, calls: list[tuple[str, str]] | None = None):
    def transport(
        _base_url: str,
        *,
        method: str,
        path: str,
        payload: object,
        timeout: float,
        api_key: str | None = None,
    ) -> dict[str, object]:
        del payload, timeout, api_key
        if calls is not None:
            calls.append((method, path))
        return {
            "model": GENERATOR_MODEL,
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": _semantic_output(body).decode("utf-8"),
                    }
                }
            ],
        }

    return transport


def _run_generator_worker(
    state: Path,
    *,
    body: str,
    calls: list[tuple[str, str]] | None = None,
) -> dict[str, object]:
    return worker.run_generator_worker(
        state,
        base_url="https://provider.example.invalid/v1",
        deployed_revision=REVISION,
        transport=_provider_transport(body, calls),
        max_attempts=3,
    )


def test_invalid_generator_output_uses_the_bounded_retry_path(
    production_fixture: tuple[Path, Path, str],
) -> None:
    state, _vault, job_id = production_fixture
    calls: list[tuple[str, str]] = []
    invalid_body = "A paragraph.\\n\\n- an accidentally escaped list item"

    for _ in range(3):
        result = _run_generator_worker(state, body=invalid_body, calls=calls)
        assert result["status"] == "retryable_failure"
        assert result["reason_code"] == "generator_provider_or_output_error"

    assert calls == [("POST", "/chat/completions")] * 3
    assert not list((state / "00-Untrusted").glob("*.proposal.json"))

    idle = _run_generator_worker(state, body=invalid_body, calls=calls)
    assert idle["status"] == "idle"
    assert job_status(state, job_id)["current_generation"]["state"] == "retry_exhausted"
    assert len(calls) == 3


def test_valid_generator_to_validator_path_remains_green(
    production_fixture: tuple[Path, Path, str],
) -> None:
    state, vault, _job_id = production_fixture
    body = "A grounded paragraph.\n\n- a valid list item\n"

    generated = _run_generator_worker(state, body=body)
    assert generated["status"] == "completed"
    assert generated["state"] == "validating"

    validated = worker.run_validator_worker(state, vault)

    assert validated["status"] == "completed"
    assert validated["state"] == "building_evaluation_context"

    output = generated["output"]
    assert isinstance(output, dict)
    proposal_sha256 = output["proposal_sha256"]
    assert isinstance(proposal_sha256, str)
    proposal = json.loads(
        (state / "00-Untrusted" / f"{proposal_sha256}.proposal.json").read_bytes()
    )
    assert "A grounded paragraph.\n\n- a valid list item\n" in proposal["content"]
