from __future__ import annotations

import json
from pathlib import Path

import pytest

from obsidian_automation.pre_review_production_canary import (
    PreReviewCanaryError,
    run_canary,
)


GEN_MODEL = "gemma3:12b"
EVAL_MODEL = "gemma3:12b-eval"
REVISION = "a" * 40


def _transport(base_url: str, **kwargs):
    assert base_url == "https://openai.example.invalid/v1"
    assert kwargs["path"] == "/chat/completions"
    assert kwargs["api_key"] is None
    payload = kwargs["payload"]
    model = payload["model"]
    messages = payload["messages"]
    user_payload = json.loads(messages[1]["content"])

    if "dimension" in user_payload:
        content = {
            "assessment": "pass",
            "findings": [],
        }
    else:
        content = {
            "title": "Disposable Pre Review Canary",
            "category": "summary",
            "source_type": "self",
            "body": (
                "# Disposable Pre Review Canary\n\n"
                "This note exists only to verify the pre-review pipeline. "
                "It has no canonical write authority."
            ),
        }

    return {
        "model": model,
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        content,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                }
            }
        ],
    }


def test_disposable_canary_covers_wave_d_acceptance_without_canonical_write(
    tmp_path: Path,
) -> None:
    scratch = tmp_path / "pre-review-canary"

    result = run_canary(
        scratch_root=scratch,
        generator_base_url="https://openai.example.invalid/v1",
        evaluator_base_url="https://openai.example.invalid/v1",
        generator_model=GEN_MODEL,
        evaluator_model=EVAL_MODEL,
        deployed_revision=REVISION,
        transport=_transport,
    )

    assert result["status"] == "passed"
    assert result["canonical_write_connected"] is False
    assert result["live_pipeline"]["duplicate_submit"] == "passed"
    assert result["live_pipeline"]["final_state"] == "awaiting_human_review"
    assert result["live_pipeline"]["post_review_writes"] == 0

    assert result["crash_resume"] == {
        "generation": 2,
        "validation": 2,
        "evaluation_context": 2,
        "evaluation": 2,
    }
    assert result["provider_failure"] == {
        "attempts": 3,
        "final_state": "retry_exhausted",
    }
    assert result["backpressure"]["awaiting_human_review"] == 8
    assert result["backpressure"]["backpressure_active"] is True
    assert result["mirror_conflict"]["serialized"] is True


def test_canary_refuses_production_state_path() -> None:
    with pytest.raises(PreReviewCanaryError, match="below /tmp or /var/tmp|overlap"):
        run_canary(
            scratch_root=Path("/var/lib/obsidian-ai/state/canary"),
            generator_base_url="https://openai.example.invalid/v1",
        evaluator_base_url="https://openai.example.invalid/v1",
            generator_model=GEN_MODEL,
            evaluator_model=EVAL_MODEL,
            deployed_revision=REVISION,
            transport=_transport,
        )


def test_canary_refuses_existing_scratch_root(tmp_path: Path) -> None:
    scratch = tmp_path / "existing"
    scratch.mkdir()

    with pytest.raises(PreReviewCanaryError, match="must not already exist"):
        run_canary(
            scratch_root=scratch,
            generator_base_url="https://openai.example.invalid/v1",
        evaluator_base_url="https://openai.example.invalid/v1",
            generator_model=GEN_MODEL,
            evaluator_model=EVAL_MODEL,
            deployed_revision=REVISION,
            transport=_transport,
        )
