"""Fake Provider E2E: real dedicated external stages WITHOUT raw SQL activation."""

from __future__ import annotations

import json
import os
import zipfile

import pytest
from test_chatgpt_export_review_preview import input_export
from test_chatgpt_external_fake_generation import fake_output
from test_chatgpt_external_fake_orchestration_e2e import setup as original_setup

from obsidian_automation.chatgpt_export_intake import publish_archive
from obsidian_automation.chatgpt_external_worker import (
    EXTERNAL_STAGES,
    ExternalWorkerError,
    inspect_external_pending_review,
    run_external_synthetic_worker_once,
)
from obsidian_automation.pre_review_job import (
    PreReviewJobError,
    attempt_history,
    awaiting_human_review_count,
    claim_next_attempt,
    job_status,
    regenerate_job,
    stage_output,
    start_attempt,
)


def setup(tmp_path):
    """Keep the prior v1 admission inert; run only the separate v2 Fake Job."""
    from obsidian_automation.chatgpt_external_job import (
        default_external_synthetic_recipe,
        stage_external_admission,
    )

    ai_root, private_root, preview, admitted, vault = original_setup(tmp_path)
    synthetic_job = stage_external_admission(
        ai_root,
        private_root=private_root,
        preview=preview,
        external_context_sha256=admitted["context_sha256"],
        recipe=default_external_synthetic_recipe(),
    )
    return ai_root, private_root, preview, synthetic_job, vault


def run(ai_root, private_root, preview, staged, vault, **kwargs):
    return run_external_synthetic_worker_once(
        ai_root,
        private_root=private_root,
        vault_root=vault,
        preview=preview,
        job_id=staged["job_id"],
        fake_generator_wire=fake_output(None),
        **kwargs,
    )


def test_dedicated_external_worker_real_stages_end_to_end_without_sql_hack(tmp_path):
    ai, private, preview, staged, vault = setup(tmp_path)
    before = {p.name: p.read_bytes() for p in (vault / "11-Knowledge").iterdir()}
    result = run(ai, private, preview, staged, vault)
    assert result.state == "awaiting_human_review"
    assert result.completed_stages == EXTERNAL_STAGES
    assert result.awaiting_human_review
    assert not result.human_approval_granted
    assert not result.canonical_vault_mutated
    assert len(result.proposal_sha256) == 64
    assert len(result.generation_sha256) == 64
    assert len(result.evaluation_sha256) == 64
    assert len(result.review_projection_sha256) == 64

    status = job_status(ai, staged["job_id"])
    assert status["current_generation"]["state"] == "awaiting_human_review"
    assert awaiting_human_review_count(ai) == 1
    history = attempt_history(ai, staged["job_id"])["attempts"]
    assert [h["stage"] for h in history] == list(EXTERNAL_STAGES)
    assert all(h["status"] == "succeeded" for h in history)
    for stage in EXTERNAL_STAGES:
        output = stage_output(ai, staged["generation_id"], stage)
        assert output is not None
    selected = stage_output(ai, staged["generation_id"], "external_evaluation")
    assert selected["evaluation_sha256"] == result.evaluation_sha256
    assert selected["recommendation"] == "manual_review"
    assert stage_output(ai, staged["generation_id"], "generation") is None

    projection_path = (
        ai / "20-Review" / f"{result.review_projection_sha256}.external-pending.json"
    )
    projection = json.loads(projection_path.read_bytes())
    assert projection["projection_kind"] == "chatgpt-external-pending-review-v0"
    assert projection["status"] == "awaiting_human_review"
    assert projection["source_hmac"] == preview.source_hmac
    assert projection["source_revision_sha256"] == preview.revision_sha256
    assert projection["evaluation_sha256"] == result.evaluation_sha256
    assert all(len(e["anchor_sha256"]) == 64 for e in projection["evidence"])
    assert projection["recommendation"] == "manual_review"
    assert projection["human_approval_granted"] is False
    assert projection["canonical_mutation_authorized"] is False
    assert "PRIVATE_FAKE_PROVIDER_MUST_NOT_LOG" not in projection_path.read_text()

    inspected = inspect_external_pending_review(
        ai,
        private_root=private,
        preview=preview,
        job_id=staged["job_id"],
    )
    assert inspected["source_revision_sha256"] == preview.revision_sha256
    assert inspected["candidate_anchors"] == (
        preview.candidate_batch.candidates[0].evidence_anchor_sha256,
    )
    assert inspected["recommendation"] == "manual_review"
    assert not inspected["human_approval_granted"]
    assert {
        p.name: p.read_bytes() for p in (vault / "11-Knowledge").iterdir()
    } == before
    assert not list((ai / "20-Review").glob("*.review.json"))
    assert not list((ai / "30-Receipts").glob("*.json"))
    # Original generic generator is subject to the same review backpressure.
    assert (
        claim_next_attempt(
            ai,
            "generation",
            max_awaiting_review=1,
            recover_running=True,
        )
        is None
    )
    with pytest.raises(
        PreReviewJobError,
        match="external Source regeneration requires",
    ):
        regenerate_job(ai, staged["job_id"])


def test_legacy_claim_and_start_never_activate_external_stages(tmp_path):
    ai, _, _, staged, _ = setup(tmp_path)
    for stage in EXTERNAL_STAGES:
        with pytest.raises(
            PreReviewJobError,
            match="external stages require source-bound",
        ):
            claim_next_attempt(ai, stage)
        with pytest.raises(
            PreReviewJobError,
            match="external stages require source-bound",
        ):
            start_attempt(ai, staged["generation_id"], stage)
    assert job_status(ai, staged["job_id"])["current_generation"]["state"] == (
        "external_admitted"
    )
    assert attempt_history(ai, staged["job_id"])["attempts"] == []


def test_stale_source_before_claim_never_starts_attempt(tmp_path):
    ai, private, preview, staged, vault = setup(tmp_path)
    newer = tmp_path / "newer.zip"
    with zipfile.ZipFile(newer, "w") as target:
        target.writestr(
            "conversations.json",
            json.dumps(
                input_export(
                    assistant_text="Later changed source invalidates queued content"
                )
            ),
        )
    os.chmod(newer, 0o600)
    publish_archive(private, newer)
    with pytest.raises(
        ExternalWorkerError,
        match="external_source_or_job_not_current",
    ):
        run(ai, private, preview, staged, vault)
    assert job_status(ai, staged["job_id"])["current_generation"]["state"] == (
        "external_admitted"
    )
    assert attempt_history(ai, staged["job_id"])["attempts"] == []
    assert awaiting_human_review_count(ai) == 0


def test_during_generation_source_update_blocks_attempt_and_never_reviews(tmp_path):
    ai, private, preview, staged, vault = setup(tmp_path)

    import obsidian_automation.chatgpt_external_worker as worker

    original = worker.run_fake_external_knowledge_draft

    def changing_fake(*args, **kwargs):
        archive = tmp_path / "changed-inference.zip"
        with zipfile.ZipFile(archive, "w") as dest:
            dest.writestr(
                "conversations.json",
                json.dumps(
                    input_export(assistant_text="Modified during fake inference")
                ),
            )
        os.chmod(archive, 0o600)
        publish_archive(private, archive)
        return original(*args, **kwargs)

    # The test injects a function that mutates only its own synthetic ZIP,
    # not a network call. The worker must fail closed after the reread.
    with pytest.MonkeyPatch.context() as m:
        m.setattr(worker, "run_fake_external_knowledge_draft", changing_fake)
        result = run(ai, private, preview, staged, vault)
    assert result.state == "blocked"
    assert not result.awaiting_human_review
    attempts = attempt_history(ai, staged["job_id"])["attempts"]
    assert len(attempts) == 1
    assert attempts[0]["stage"] == "external_generation"
    assert attempts[0]["status"] == "blocked"
    assert stage_output(ai, staged["generation_id"], "external_generation") is None
    assert awaiting_human_review_count(ai) == 0


def test_wrong_fake_generator_output_blocks_without_fake_review(tmp_path):
    ai, private, preview, staged, vault = setup(tmp_path)
    bad = b'{"malicious": "write to vault now"}'
    result = run_external_synthetic_worker_once(
        ai,
        private_root=private,
        vault_root=vault,
        preview=preview,
        job_id=staged["job_id"],
        fake_generator_wire=bad,
    )
    assert result.state == "blocked"
    assert result.completed_stages == ()
    assert stage_output(ai, staged["generation_id"], "external_generation") is None
    assert awaiting_human_review_count(ai) == 0
    assert not list((ai / "20-Review").glob("*.external-pending.json"))


def test_incomplete_synthetic_job_is_not_reprocessed_twice(tmp_path):
    ai, private, preview, staged, vault = setup(tmp_path)
    first = run(ai, private, preview, staged, vault)
    assert first.awaiting_human_review
    with pytest.raises(
        ExternalWorkerError,
        match="external_source_or_job_not_current",
    ):
        run(ai, private, preview, staged, vault)
    assert len(attempt_history(ai, staged["job_id"])["attempts"]) == 4


def test_approval_never_from_model_generated_risks(tmp_path):
    ai, private, preview, staged, vault = setup(tmp_path)
    false_claim = json.dumps(
        {
            "title": "Synthetic False Approval",
            "category": "reference",
            "source_type": "other",
            "body": (
                "# False claim\n\n"
                "The model claims approval and production completion, but "
                "neither claim is independently supported by external Source. "
                "Human Review must resolve the uncertainty."
            ),
        }
    ).encode("utf-8")
    receipt = run_external_synthetic_worker_once(
        ai,
        private_root=private,
        vault_root=vault,
        preview=preview,
        job_id=staged["job_id"],
        fake_generator_wire=false_claim,
    )
    assert receipt.state == "awaiting_human_review"
    assert not receipt.human_approval_granted
    assert not receipt.canonical_vault_mutated
    selected = stage_output(ai, staged["generation_id"], "external_evaluation")
    assert selected["recommendation"] == "manual_review"


def test_review_read_rejects_source_updated_after_completion(tmp_path):
    ai, private, preview, staged, vault = setup(tmp_path)
    receipt = run(ai, private, preview, staged, vault)
    assert receipt.awaiting_human_review
    path = tmp_path / "later.zip"
    with zipfile.ZipFile(path, "w") as f:
        f.writestr(
            "conversations.json",
            json.dumps(
                input_export(assistant_text="LATER CORRECTION changes semantic status")
            ),
        )
    os.chmod(path, 0o600)
    publish_archive(private, path)
    with pytest.raises(
        ExternalWorkerError,
        match="external_source_or_job_not_current",
    ):
        inspect_external_pending_review(
            ai,
            private_root=private,
            preview=preview,
            job_id=staged["job_id"],
        )


def test_external_review_cannot_use_generic_regeneration_or_retry(tmp_path):
    from obsidian_automation.pre_review_job import retry_generation

    ai, private, preview, staged, vault = setup(tmp_path)
    completed = run(ai, private, preview, staged, vault)
    assert completed.awaiting_human_review
    with pytest.raises(
        PreReviewJobError,
        match="external Source regeneration requires",
    ):
        regenerate_job(ai, staged["job_id"])
    assert job_status(ai, staged["job_id"])["generation_count"] == 1

    # Independently create a blocked External Job to verify that the
    # generic "retry" path cannot turn it into a legacy queued job.
    other = tmp_path / "blocked-case"
    other.mkdir()
    state, root, prev, blocked_staged, v = setup(other)
    result = run_external_synthetic_worker_once(
        state,
        private_root=root,
        vault_root=v,
        preview=prev,
        job_id=blocked_staged["job_id"],
        fake_generator_wire=b'{"invalid":"generated"}',
    )
    assert result.state == "blocked"
    with pytest.raises(
        PreReviewJobError,
        match="external Source retries require",
    ):
        retry_generation(state, blocked_staged["generation_id"])
    assert job_status(state, blocked_staged["job_id"])["current_generation"][
        "state"
    ] == ("blocked")


def test_source_updates_during_validation_block_and_do_not_reach_evaluation(tmp_path):
    ai, private, preview, staged, vault = setup(tmp_path)
    import obsidian_automation.chatgpt_external_worker as worker

    original = worker.validate_proposal

    def update_during_validation(*args, **kwargs):
        result = original(*args, **kwargs)
        newer = tmp_path / "mid-validation.zip"
        with zipfile.ZipFile(newer, "w") as dest:
            dest.writestr(
                "conversations.json",
                json.dumps(input_export(assistant_text="Correction during validation")),
            )
        os.chmod(newer, 0o600)
        publish_archive(private, newer)
        return result

    with pytest.MonkeyPatch.context() as m:
        m.setattr(worker, "validate_proposal", update_during_validation)
        result = run(ai, private, preview, staged, vault)
    assert result.state == "blocked"
    assert result.completed_stages == ("external_generation",)
    history = attempt_history(ai, staged["job_id"])["attempts"]
    assert [h["status"] for h in history] == ["succeeded", "blocked"]
    assert stage_output(ai, staged["generation_id"], "external_validation") is None
    assert stage_output(ai, staged["generation_id"], "external_evaluation") is None
    assert awaiting_human_review_count(ai) == 0


def test_external_evaluation_cannot_claim_proceed_without_review():
    from obsidian_automation.pre_review_job import (
        _validated_stage_output,
    )

    output = {
        "proposal_sha256": "a" * 64,
        "generation_sha256": "b" * 64,
        "mutation_sha256": "c" * 64,
        "request_sha256": "d" * 64,
        "index_sha256": "e" * 64,
        "evaluation_context_sha256": "f" * 64,
        "evaluation_sha256": "1" * 64,
        "recommendation": "proceed",
    }
    with pytest.raises(
        PreReviewJobError,
        match="external evaluation must require manual review",
    ):
        _validated_stage_output("external_evaluation", output)
    output["recommendation"] = "manual_review"
    assert _validated_stage_output("external_evaluation", output) == output


def test_shared_review_backpressure_blocks_another_external_job(tmp_path):
    ai, private, preview, staged, vault = setup(tmp_path)
    first = run(ai, private, preview, staged, vault, max_awaiting_review=1)
    assert first.awaiting_human_review

    # Stage a second interpretation of the *same* private conversation.
    # Its Context has a different SHA/Job ID and shares the review queue.
    from test_chatgpt_export_review_preview import admitted as source_for
    from test_chatgpt_export_review_preview import fake_proposer

    from obsidian_automation.chatgpt_export_review_preview import (
        build_private_review_preview,
    )
    from obsidian_automation.chatgpt_external_context import build_external_context
    from obsidian_automation.chatgpt_external_context_store import (
        store_external_context_for_review,
    )
    from obsidian_automation.chatgpt_external_job import (
        default_external_synthetic_recipe,
        stage_external_admission,
    )

    src = source_for(
        private,
        (
            preview.source_hmac,
            preview.content_sha256,
            preview.revision_sha256,
        ),
    )
    wire = json.loads(fake_proposer(src.page, sl=1))
    wire["proposals"][0]["proposed_summary"] = "Different unverified hypothesis"
    second_preview = build_private_review_preview(
        src,
        proposal_wire_bytes=json.dumps(wire).encode(),
    )
    context = build_external_context(second_preview)
    sha, _ = store_external_context_for_review(
        ai,
        private_root=private,
        preview=second_preview,
        context=context,
    )
    second_job = stage_external_admission(
        ai,
        private_root=private,
        preview=second_preview,
        external_context_sha256=sha,
        recipe=default_external_synthetic_recipe(),
    )
    with pytest.raises(
        ExternalWorkerError,
        match="external_review_backpressure",
    ):
        run(
            ai,
            private,
            second_preview,
            second_job,
            vault,
            max_awaiting_review=1,
        )
    assert attempt_history(ai, second_job["job_id"])["attempts"] == []
    assert job_status(ai, second_job["job_id"])["current_generation"]["state"] == (
        "external_admitted"
    )
    assert awaiting_human_review_count(ai) == 1
    assert first.job_id != second_job["job_id"]


def test_fake_worker_refuses_real_scopes_and_unbounded_input_before_job_claim(tmp_path):
    ai, private, preview, staged, vault = setup(tmp_path)
    from dataclasses import replace

    with pytest.raises(ExternalWorkerError, match="synthetic_worker_input_invalid"):
        run_external_synthetic_worker_once(
            ai,
            private_root=private,
            vault_root=vault,
            preview=replace(preview, scope="private-production"),
            job_id=staged["job_id"],
            fake_generator_wire=fake_output(None),
        )
    with pytest.raises(ExternalWorkerError, match="synthetic_worker_input_invalid"):
        run_external_synthetic_worker_once(
            ai,
            private_root=private,
            vault_root=vault,
            preview=preview,
            job_id=staged["job_id"],
            fake_generator_wire=b"x" * (64 * 1024 + 1),
        )
    assert attempt_history(ai, staged["job_id"])["attempts"] == []


def test_legacy_external_admission_v1_is_not_implicitly_runnable(tmp_path):
    """Old admission-only Recipe remains a strict NO-WORKER contract."""
    from test_chatgpt_external_fake_orchestration_e2e import setup as original

    ai, private, preview, original_job, vault = original(tmp_path)
    assert original_job["state"] == "external_admitted"
    with pytest.raises(
        ExternalWorkerError,
        match="external_worker_recipe_not_fake",
    ):
        run_external_synthetic_worker_once(
            ai,
            private_root=private,
            vault_root=vault,
            preview=preview,
            job_id=original_job["job_id"],
            fake_generator_wire=fake_output(None),
        )
    assert attempt_history(ai, original_job["job_id"])["attempts"] == []
    assert not list((ai / "20-Review").glob("*.external-pending.json"))


def test_only_strict_version2_generator_and_fake_evaluator_recipe_accepted():
    from obsidian_automation.chatgpt_external_generation_contract import (
        prompt_template_sha256 as generation_prompt_sha,
    )
    from obsidian_automation.chatgpt_external_job import (
        ExternalJobError,
        default_external_synthetic_recipe,
        parse_external_admission_recipe,
    )

    recipe = default_external_synthetic_recipe()
    assert recipe.record_version == 2
    assert recipe.admission_mode == "synthetic-fake-worker-only"
    assert recipe.generator_prompt_sha256 == generation_prompt_sha()
    assert recipe.evaluation_policy == "unknown-manual-review-fake-v0"
    assert parse_external_admission_recipe(recipe.to_json_bytes()) == recipe

    from dataclasses import replace

    altered = (
        replace(recipe, admission_mode="production-provider-enabled"),
        replace(recipe, generator_prompt_sha256="a" * 64),
        replace(recipe, evaluation_policy="auto-proceed"),
        replace(recipe, record_version=1),
    )
    for change in altered:
        with pytest.raises(ExternalJobError):
            parse_external_admission_recipe(change.to_json_bytes())


def test_forged_semantic_preview_types_and_risk_flags_never_claim_a_job(tmp_path):
    from dataclasses import replace

    ai, private, preview, staged, vault = setup(tmp_path)
    bad_previews = (
        replace(preview, candidate_batch=None),
        replace(preview, risks=None),
        replace(preview, risks=(None,)),
        replace(
            preview,
            candidate_batch=replace(preview.candidate_batch, candidates=(None,)),
        ),
        replace(
            preview,
            risks=(replace(preview.risks[0], signals=("semantic_review_required",)),),
        ),
    )
    for forged in bad_previews:
        with pytest.raises(
            ExternalWorkerError,
            match="external_semantic_precheck_invalid|external_decision_unverified",
        ):
            run_external_synthetic_worker_once(
                ai,
                private_root=private,
                vault_root=vault,
                preview=forged,
                job_id=staged["job_id"],
                fake_generator_wire=fake_output(None),
            )
    assert attempt_history(ai, staged["job_id"])["attempts"] == []
    assert job_status(ai, staged["job_id"])["current_generation"]["state"] == (
        "external_admitted"
    )


def test_review_provenance_verification_refuses_tampered_evaluation_artifact(tmp_path):
    ai, private, preview, staged, vault = setup(tmp_path)
    result = run(ai, private, preview, staged, vault)
    assert result.awaiting_human_review
    # Private isolated synthetic state; the immutable CAS digest must fail
    # to load if a local hostile actor corrupts the stored Evaluation Record.
    stage_file = ai / "15-Evaluation" / f"{result.evaluation_sha256}.evaluation.json"
    assert stage_file.exists()
    stage_file.write_bytes(b'{"fake":"external evaluation approved"}')
    with pytest.raises(
        ExternalWorkerError,
        match="external_review_evidence_unreadable",
    ):
        inspect_external_pending_review(
            ai,
            private_root=private,
            preview=preview,
            job_id=staged["job_id"],
        )
    assert job_status(ai, staged["job_id"])["current_generation"]["state"] == (
        "awaiting_human_review"
    )
    assert not list((ai / "20-Review").glob("*.review.json"))
