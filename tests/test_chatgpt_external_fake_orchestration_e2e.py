"""TEST-ONLY isolated real DB state machine E2E for #411 External Source.

The synthetic harness directly advances 'external_admitted' to 'queued'
in a disposable temp SQLite DB. No production activation API exists.
Every stage below uses actual source-bound v3 records, Validator,
fresh Vault retrieval Context and Evaluation Record artifacts; no fake hashes.
Fake Evaluation conservatively returns unknown/manual_review, and there
is no approval or Vault write. This is NOT a deployable worker.
"""

from __future__ import annotations

import sqlite3

import pytest
from test_chatgpt_external_fake_generation import fake_output
from test_chatgpt_external_job import admit, case
from test_pre_review_worker import _knowledge_note

from obsidian_automation.chatgpt_external_generation import (
    run_fake_external_knowledge_draft,
)
from obsidian_automation.chatgpt_external_job import (
    ExternalJobError,
    inspect_external_admission,
)
from obsidian_automation.evaluation_artifact import (
    EVALUATION_CONTEXT_STAGE,
    EVALUATION_REQUEST_STAGE,
    EVALUATION_STAGE,
    build_evaluation_context,
    build_evaluation_record,
    create_evaluation_request,
    load_evaluation_record,
    store_evaluation_context,
    store_evaluation_record,
)
from obsidian_automation.evaluator_contract import (
    EVALUATOR_PROMPT_TEMPLATE_VERSION,
)
from obsidian_automation.evaluator_contract import (
    prompt_template_sha256 as evaluator_prompt_sha256,
)
from obsidian_automation.generation_artifact import load_generation_record
from obsidian_automation.knowledge_validator import validate_proposal
from obsidian_automation.pre_review_job import (
    awaiting_human_review_count,
    claim_next_attempt,
    complete_attempt,
    job_status,
    stage_output,
    start_attempt,
)
from obsidian_automation.pre_review_worker import (
    build_knowledge_index,
    store_knowledge_index,
)

REVISION = "f" * 40


def setup(tmp_path):
    ai_root, private_root, preview, ctx_sha = case(tmp_path)
    # Synthetic mirror only, with a distinct canonical note that must
    # remain intact through the full fake pipeline.
    vault = tmp_path / "vault"
    knowledge = vault / "11-Knowledge"
    knowledge.mkdir(parents=True)
    (knowledge / "Existing.md").write_text(
        _knowledge_note(
            "# Existing\n\nCanonical existing source, never changed by the fake E2E."
        ),
        encoding="utf-8",
    )
    for folder in (
        "04-Index",
        EVALUATION_REQUEST_STAGE,
        EVALUATION_CONTEXT_STAGE,
        EVALUATION_STAGE,
        "24-Locks/read-view",
    ):
        (ai_root / folder).mkdir(parents=True, exist_ok=True)
    staged = admit(ai_root, private_root, preview, ctx_sha)
    return ai_root, private_root, preview, staged, vault


def _test_only_activate_job(ai_root, private_root, preview, staged):
    """Isolated synthetic override; NEVER use against a production database."""
    inspect_external_admission(
        ai_root,
        private_root=private_root,
        preview=preview,
        job_id=staged["job_id"],
    )
    with sqlite3.connect(
        ai_root / "02-Orchestration" / "pre-review-jobs.sqlite3"
    ) as db:
        db.execute("BEGIN IMMEDIATE")
        changed = db.execute(
            "UPDATE generations SET state='queued' "
            "WHERE generation_id=? AND job_id=? AND state='external_admitted'",
            (staged["generation_id"], staged["job_id"]),
        ).rowcount
        assert changed == 1
        db.commit()
    assert (
        job_status(ai_root, staged["job_id"])["current_generation"]["state"] == "queued"
    )


def _complete_stage(ai_root, generation_id, stage, output):
    attempt = start_attempt(ai_root, generation_id, stage)
    assert attempt["stage"] == stage
    finished = complete_attempt(
        ai_root,
        attempt["attempt_id"],
        outcome="succeeded",
        output=output,
    )
    assert finished["output"] == output
    assert stage_output(ai_root, generation_id, stage) == output
    return finished


def test_synthetic_real_orchestration_reaches_awaiting_review_with_actual_artifacts(
    tmp_path,
):
    ai_root, private_root, preview, staged, vault = setup(tmp_path)
    original_files = {
        p.name: p.read_bytes() for p in (vault / "11-Knowledge").iterdir()
    }
    assert awaiting_human_review_count(ai_root) == 0

    # Generate the genuinely bound proposal/v3 record *before* test-only
    # queue activation. In production, the worker will own this path and
    # must re-verify private Source immediately before stage selection.
    draft = run_fake_external_knowledge_draft(
        ai_root,
        private_root=private_root,
        preview=preview,
        job_id=staged["job_id"],
        transport=fake_output,
        implementation_revision=REVISION,
    )
    generation = load_generation_record(ai_root, draft.generation_sha256)
    assert generation.external_source
    assert generation.external_source.source_revision_sha256 == (
        preview.revision_sha256
    )
    assert not draft.real_job_stage_output_selected

    _test_only_activate_job(ai_root, private_root, preview, staged)
    generation_id = staged["generation_id"]
    selected_generation = _complete_stage(
        ai_root,
        generation_id,
        "generation",
        {
            "proposal_sha256": draft.proposal_sha256,
            "generation_sha256": draft.generation_sha256,
        },
    )
    assert selected_generation["state"] == "validating"

    validation = validate_proposal(
        ai_root,
        vault,
        draft.proposal_sha256,
    )
    assert validation["result"] == "accepted"
    mutation_sha = validation["mutation_sha256"]
    request_sha, _, request = create_evaluation_request(
        ai_root,
        draft.proposal_sha256,
    )
    assert request.mutation_sha256 == mutation_sha
    validation_output = {
        **selected_generation["output"],
        "mutation_sha256": mutation_sha,
        "request_sha256": request_sha,
    }
    accepted_validation = _complete_stage(
        ai_root,
        generation_id,
        "validation",
        validation_output,
    )
    assert accepted_validation["state"] == "building_evaluation_context"

    # Actual fresh knowledge-index retrieval against isolated synthetic Vault.
    index = build_knowledge_index(vault)
    index_sha, _ = store_knowledge_index(ai_root, index)
    evaluated_context = build_evaluation_context(
        ai_root,
        vault,
        request_sha256=request_sha,
        index_sha256=index_sha,
        top_k=5,
    )
    evaluated_context_sha, _ = store_evaluation_context(ai_root, evaluated_context)
    context_output = {
        **validation_output,
        "index_sha256": index_sha,
        "evaluation_context_sha256": evaluated_context_sha,
    }
    selected_context = _complete_stage(
        ai_root,
        generation_id,
        "evaluation_context",
        context_output,
    )
    assert selected_context["state"] == "evaluating"

    # Honest conservative Fake Evaluator outcome. No model ever claims the
    # draft is established truth or an approved user project decision.
    result = build_evaluation_record(
        ai_root,
        proposal_sha256=draft.proposal_sha256,
        mutation_sha256=mutation_sha,
        generation_sha256=draft.generation_sha256,
        evaluation_context_sha256=evaluated_context_sha,
        implementation_revision=REVISION,
        prompt_template_version=EVALUATOR_PROMPT_TEMPLATE_VERSION,
        prompt_template_sha256=evaluator_prompt_sha256(),
        model_provider="synthetic-fake-only",
        model_identifier="deterministic-fake-evaluator",
        model_revision="fake-only-no-network",
        model_config={
            "adapter_version": "synthetic-evaluation-no-network",
            "strategy": "conservative-unknown",
            "options": {},
        },
        groundedness="unknown",
        knowledge_quality="unknown",
        epistemic_status="unknown",
        redundancy="none",
        consistency="unknown",
        recommendation="manual_review",
        findings=("External Source user/assistant claims remain unverified",),
        evaluated_at="2026-10-21T01:02:00Z",
    )
    eval_sha, _ = store_evaluation_record(ai_root, result)
    assert load_evaluation_record(ai_root, eval_sha).assessment.recommendation == (
        "manual_review"
    )
    eval_output = {
        **context_output,
        "evaluation_sha256": eval_sha,
        "recommendation": "manual_review",
    }
    evaluated = _complete_stage(
        ai_root,
        generation_id,
        "evaluation",
        eval_output,
    )
    assert evaluated["state"] == "awaiting_human_review"
    assert job_status(ai_root, staged["job_id"])["current_generation"]["state"] == (
        "awaiting_human_review"
    )
    assert awaiting_human_review_count(ai_root) == 1

    # No Human Review decision or Vault execution was produced by any stage.
    assert not list((ai_root / "20-Review").glob("*.review.json"))
    assert not list((ai_root / "30-Receipts").glob("*.json"))
    assert {
        p.name: p.read_bytes() for p in (vault / "11-Knowledge").iterdir()
    } == original_files
    assert not (
        vault / "11-Knowledge" / "Synthetic External Evidence Review.md"
    ).exists()

    # With a review in the queue, a normal generator must obey shared
    # max_awaiting_review backpressure. The external Job has no new stage.
    assert (
        claim_next_attempt(
            ai_root,
            "generation",
            max_awaiting_review=1,
            recover_running=True,
        )
        is None
    )


def test_synthetic_activation_requires_current_private_source(tmp_path):
    ai_root, private_root, preview, staged, _ = setup(tmp_path)
    import json
    import os
    import zipfile

    from test_chatgpt_export_review_preview import input_export

    from obsidian_automation.chatgpt_export_intake import publish_archive

    updated = tmp_path / "later.zip"
    with zipfile.ZipFile(updated, "w") as output:
        output.writestr(
            "conversations.json",
            json.dumps(input_export(assistant_text="Later alternate")),
        )
    os.chmod(updated, 0o600)
    publish_archive(private_root, updated)
    with pytest.raises(ExternalJobError, match="external_source_not_current"):
        _test_only_activate_job(ai_root, private_root, preview, staged)
    assert job_status(ai_root, staged["job_id"])["current_generation"]["state"] == (
        "external_admitted"
    )
    assert awaiting_human_review_count(ai_root) == 0
    assert claim_next_attempt(ai_root, "generation") is None
