"""Source-aware input for the ORIGINAL evaluator schema/aggregation.

Synthetic ZIP, Fake Provider and disposable Vault only: no real user data,
live LLM transport, new authoritative Review record, or canonical mutation.
"""
from __future__ import annotations

import json
import os
import zipfile
from dataclasses import replace

import pytest
from test_chatgpt_export_review_preview import input_export
from test_chatgpt_external_worker_e2e import run, setup

from obsidian_automation.artifact_lifecycle import ArtifactLifecycleError
from obsidian_automation.chatgpt_export_intake import publish_archive
from obsidian_automation.chatgpt_external_evaluator_input import (
    ExternalEvaluationInputError,
    read_external_evaluation_source,
)
from obsidian_automation.evaluation_artifact import load_evaluation_context
from obsidian_automation.evaluator_contract import (
    EXTERNAL_EVALUATOR_PROMPT_VERSION,
    DimensionEvaluatorOutput,
    aggregate_evaluator_outputs,
    external_evaluator_prompt_sha256,
    output_schema,
    render_evaluator_prompts,
    to_evaluation_assessment,
)
from obsidian_automation.pre_review_job import stage_output


def test_worker_routes_synthetic_outputs_through_native_aggregation(tmp_path, monkeypatch):
    import obsidian_automation.chatgpt_external_worker as worker
    from obsidian_automation.evaluation_artifact import load_evaluation_record
    from obsidian_automation.evaluator_contract import aggregate_evaluator_outputs

    ai, private, preview, staged, vault = setup(tmp_path)
    calls = []

    def observing_aggregate(**outputs):
        result = aggregate_evaluator_outputs(**outputs)
        calls.append(result)
        return result

    monkeypatch.setattr(worker, "aggregate_evaluator_outputs", observing_aggregate)
    completed = run(ai, private, preview, staged, vault)
    assert completed.awaiting_human_review
    assert len(calls) == 1
    record = load_evaluation_record(ai, completed.evaluation_sha256)
    assert record.assessment.knowledge_quality == calls[0].knowledge_quality


def test_worker_persists_changed_fake_dimension_and_pairwise_results(tmp_path, monkeypatch):
    import obsidian_automation.chatgpt_external_worker as worker
    from obsidian_automation.evaluation_artifact import load_evaluation_record

    ai, private, preview, staged, vault = setup(tmp_path)
    original = worker._synthetic_evaluator_wire
    seen = []
    aggregated = []
    native = worker.aggregate_evaluator_outputs

    def observing_aggregate(**outputs):
        result = native(**outputs)
        aggregated.append(result)
        return result

    def alternate(prompt):
        if prompt.dimension == "knowledge_quality":
            return b'{"assessment":"unknown","findings":[{"detail":"Synthetic reusability unverified"}]}'
        if prompt.dimension == "redundancy":
            seen.append(prompt.candidate_path)
            return b'{"assessment":"possible","findings":[{"detail":"Synthetic overlap"}]}'
        return original(prompt)

    monkeypatch.setattr(worker, "_synthetic_evaluator_wire", alternate)
    monkeypatch.setattr(worker, "aggregate_evaluator_outputs", observing_aggregate)
    completed = run(ai, private, preview, staged, vault)
    assert completed.awaiting_human_review
    assert seen  # A real fresh-retrieval candidate was compared pairwise.
    assert len(aggregated) == 1
    assert aggregated[0].redundancy == "possible"
    assert aggregated[0].knowledge_quality == "unknown"
    assessment = load_evaluation_record(ai, completed.evaluation_sha256).assessment
    assert assessment.knowledge_quality == "unknown"
    assert assessment.redundancy == "possible"
    assert "knowledge_quality: Synthetic reusability unverified" in assessment.findings
    assert any("redundancy: [11-Knowledge/" in item and "Synthetic overlap" in item
               for item in assessment.findings)
    assert assessment.findings[:-1] == aggregated[0].findings
    assert assessment.groundedness == "unknown"
    assert assessment.epistemic_status == "unknown"
    assert assessment.recommendation == "manual_review"
    assert not completed.human_approval_granted
    assert not completed.canonical_vault_mutated


def test_worker_fake_concern_blocks_instead_of_selecting_review(tmp_path, monkeypatch):
    import obsidian_automation.chatgpt_external_worker as worker

    ai, private, preview, staged, vault = setup(tmp_path)
    original = worker._synthetic_evaluator_wire
    clamp = worker.conservative_external_assessment
    assessments = []

    def observing_clamp(output, source):
        result = clamp(output, source)
        assessments.append(result)
        return result

    def concerned(prompt):
        if prompt.dimension == "groundedness":
            return b'{"assessment":"concern","findings":[{"detail":"Synthetic unsupported result"}]}'
        return original(prompt)

    monkeypatch.setattr(worker, "_synthetic_evaluator_wire", concerned)
    monkeypatch.setattr(worker, "conservative_external_assessment", observing_clamp)
    completed = run(ai, private, preview, staged, vault)
    assert len(assessments) == 1
    assert assessments[0].groundedness == "concern"
    assert assessments[0].recommendation == "do_not_proceed"
    assert completed.state == "blocked"
    assert completed.completed_stages == (
        "external_generation", "external_validation", "external_evaluation_context",
    )
    assert stage_output(ai, staged["generation_id"], "external_evaluation") is None


def _snapshot(tmp_path):
    ai, private, preview, staged, vault = setup(tmp_path)
    result = run(ai, private, preview, staged, vault)
    assert result.awaiting_human_review
    source = read_external_evaluation_source(private, preview)
    selected = stage_output(
        ai, staged["generation_id"], "external_evaluation_context",
    )
    context = load_evaluation_context(ai, selected["evaluation_context_sha256"])
    return ai, private, preview, source, context, result


def _render(source, context):
    return render_evaluator_prompts(
        target_path="11-Knowledge/Synthetic External Evidence.md",
        proposal_content="# Candidate\n\nA tentative unverified claim.\n",
        generation_context=source,
        evaluation_context=context,
    )


def test_reuses_existing_evaluator_dimensions_and_schema_with_typed_source(tmp_path):
    _, _, preview, source, context, _ = _snapshot(tmp_path)
    prompts = _render(source, context)
    assert len(prompts) >= 3
    assert [p.dimension for p in prompts[:3]] == [
        "groundedness", "knowledge_quality", "epistemic_status",
    ]
    assert all(p.template_version == EXTERNAL_EVALUATOR_PROMPT_VERSION
               for p in prompts)
    assert all(p.template_sha256 == external_evaluator_prompt_sha256()
               for p in prompts)
    assert len({p.template_sha256 for p in prompts}) == 1
    assert all(p.output_schema == output_schema(p.dimension) for p in prompts)
    payload = json.loads(prompts[0].user)
    evidence = payload["generation_input"]
    assert evidence["input_kind"] == "chatgpt-external-source"
    assert evidence["source_truth_verified"] is False
    assert evidence["user_confirmation_verified"] is False
    assert evidence["correction_chronology_verified"] is False
    assert "query" not in evidence
    assert "sources" not in evidence
    assert source.source_revision_sha256 == preview.revision_sha256
    assert evidence["source_revision_sha256"] == preview.revision_sha256
    assert evidence["source_run_sha256"] == preview.source_run_id
    assert evidence["source_preview_sha256"] == preview.preview_digest
    quote, = evidence["source_excerpts"]
    original, = preview.candidate_batch.candidates
    assert quote["source_exact_quote"] == original.excerpt
    assert quote["speaker_role"] == "assistant"
    assert quote["attribution"] == "assistant-claim-unverified"
    assert quote["quote_sha256"] == original.excerpt_sha256
    assert quote["anchor_sha256"] == original.evidence_anchor_sha256
    assert quote["semantic_truth_verified"] is False
    assert quote["correction_chronology_verified"] is False
    assert quote["user_confirmation_verified"] is False
    assert "A quoted assistant claim is not evidence of user confirmation" in prompts[0].system
    assert preview.source_hmac not in prompts[0].user
    assert "PRIVATE_FAKE_PROVIDER_MUST_NOT_LOG" not in prompts[0].user
    assert original.excerpt not in repr(source)


def test_reused_deterministic_evaluator_is_conservative_without_semantic_truth(tmp_path):
    _, _, _, source, context, _ = _snapshot(tmp_path)
    _render(source, context)
    result = to_evaluation_assessment(aggregate_evaluator_outputs(
        groundedness=DimensionEvaluatorOutput(
            "groundedness", "unknown", (),
        ),
        knowledge_quality=DimensionEvaluatorOutput(
            "knowledge_quality", "unknown", (),
        ),
        epistemic_status=DimensionEvaluatorOutput(
            "epistemic_status", "unknown", (),
        ),
        redundancy_pairs=(),
        consistency_pairs=(),
    ))
    assert result.recommendation == "manual_review"
    assert result.groundedness == "unknown"
    assert result.epistemic_status == "unknown"


@pytest.mark.parametrize("field", [
    "source_truth_verified",
    "user_confirmation_verified",
    "correction_chronology_verified",
])
def test_external_evaluator_input_cannot_claim_verification_not_done(
    tmp_path, field,
):
    _, _, _, source, context, _ = _snapshot(tmp_path)
    forged = replace(source, **{field: True})
    with pytest.raises(
        ArtifactLifecycleError,
        match="external evaluator input claims unverified authority",
    ):
        _render(forged, context)


def test_no_fabricated_vault_context_bundle_for_external_source(tmp_path):
    _, _, _, source, context, _ = _snapshot(tmp_path)
    payload = json.loads(_render(source, context)[0].user)
    assert "sources" not in payload["generation_input"]
    assert "source_excerpts" in payload["generation_input"]
    assert all("path" not in item
               for item in payload["generation_input"]["source_excerpts"])
    # A pairwise pass compares a current Knowledge index candidate, not
    # the original ChatGPT transcript relabeled as a canonical Vault note.
    for prompt in _render(source, context)[3:]:
        pair = json.loads(prompt.user)
        assert "generation_input" not in pair
        assert pair["evaluation_candidate"]["path"].startswith("11-Knowledge/")


def test_forged_quote_contract_must_fail_before_render(tmp_path):
    _, _, _, source, context, _ = _snapshot(tmp_path)
    original = source.quotes[0]
    bad = replace(source, quotes=(replace(original, exact_quote="x" * 5000),))
    with pytest.raises(
        ArtifactLifecycleError, match="external evaluator quote contract invalid",
    ):
        _render(bad, context)
    # A syntactically short replacement quote is also rejected unless it
    # has the exact original Source-bound SHA.
    forged = replace(source, quotes=(replace(original, exact_quote="forged"),))
    with pytest.raises(
        ArtifactLifecycleError, match="external evaluator quote contract invalid",
    ):
        _render(forged, context)
    forged_role = replace(
        source, quotes=(replace(original, speaker_role="user"),),
    )
    with pytest.raises(
        ArtifactLifecycleError, match="external evaluator quote contract invalid",
    ):
        _render(forged_role, context)
    bad = replace(source, quotes=())
    with pytest.raises(ArtifactLifecycleError):
        _render(bad, context)


def test_changed_committed_source_is_rejected_by_private_adapter(tmp_path):
    _, private, preview, _, _, _ = _snapshot(tmp_path)
    next_zip = tmp_path / "updated-during-evaluation.zip"
    with zipfile.ZipFile(next_zip, "w") as new:
        new.writestr(
            "conversations.json",
            json.dumps(input_export(
                assistant_text="Later correction: the project is NOT finished",
            )),
        )
    os.chmod(next_zip, 0o600)
    publish_archive(private, next_zip)
    with pytest.raises(
        ExternalEvaluationInputError, match="external_evaluator_source_stale",
    ):
        read_external_evaluation_source(private, preview)


def test_private_adapter_rejects_real_source_scope_before_any_llm(tmp_path):
    _, private, preview, _, _, _ = _snapshot(tmp_path)
    with pytest.raises(
        ExternalEvaluationInputError, match="external_evaluator_scope_not_authorized",
    ):
        read_external_evaluation_source(
            private, replace(preview, scope="real-user-export"),
        )


def test_legacy_generation_input_rejects_external_when_context_type_missing(tmp_path):
    _, _, _, _, context, _ = _snapshot(tmp_path)
    with pytest.raises(
        ArtifactLifecycleError, match="unsupported evaluator generation input",
    ):
        render_evaluator_prompts(
            target_path="11-Knowledge/Fake.md",
            proposal_content="# Test\n\nBody",
            generation_context=None,
            evaluation_context=context,
        )



def test_overconfident_five_dimension_pass_never_enables_external_approval(tmp_path):
    from obsidian_automation.chatgpt_external_evaluator_input import (
        conservative_external_assessment,
    )
    from obsidian_automation.evaluator_contract import EvaluatorOutput

    _, _, _, source, _, _ = _snapshot(tmp_path)
    all_pass = EvaluatorOutput(
        groundedness="pass",
        knowledge_quality="pass",
        epistemic_status="pass",
        redundancy="none",
        consistency="pass",
        findings=(),
    )
    decision = conservative_external_assessment(all_pass, source)
    assert decision.recommendation == "manual_review"
    assert decision.groundedness == "unknown"
    assert decision.epistemic_status == "unknown"
    assert decision.knowledge_quality == "pass"
    assert decision.consistency == "pass"
    assert "user approval" in decision.findings[-1]


def test_external_evaluation_concern_preserves_do_not_proceed(tmp_path):
    from obsidian_automation.chatgpt_external_evaluator_input import (
        conservative_external_assessment,
    )
    from obsidian_automation.evaluator_contract import EvaluatorOutput

    _, _, _, source, _, _ = _snapshot(tmp_path)
    model = EvaluatorOutput(
        groundedness="concern",
        knowledge_quality="unknown",
        epistemic_status="unknown",
        redundancy="none",
        consistency="unknown",
        findings=("groundedness: Proposed implementation is not evidenced",),
    )
    decision = conservative_external_assessment(model, source)
    assert decision.recommendation == "do_not_proceed"
    assert decision.groundedness == "concern"
    assert decision.epistemic_status == "unknown"


def test_forged_external_user_confirmation_does_not_override_policy(tmp_path):
    from obsidian_automation.chatgpt_external_evaluator_input import (
        ExternalEvaluationInputError,
        conservative_external_assessment,
    )
    from obsidian_automation.evaluator_contract import EvaluatorOutput

    _, _, _, source, _, _ = _snapshot(tmp_path)
    model = EvaluatorOutput(
        groundedness="pass",
        knowledge_quality="pass",
        epistemic_status="pass",
        redundancy="none",
        consistency="pass",
        findings=(),
    )
    with pytest.raises(
        ExternalEvaluationInputError, match="external_assessment_authority_invalid",
    ):
        conservative_external_assessment(
            model, replace(source, user_confirmation_verified=True),
        )
    with pytest.raises(
        ExternalEvaluationInputError, match="external_assessment_output_invalid",
    ):
        conservative_external_assessment({"groundedness": "pass"}, source)


def test_worker_evaluation_records_external_template_identity(tmp_path):
    from obsidian_automation.evaluation_artifact import load_evaluation_record

    ai, private, preview, staged, vault = setup(tmp_path)
    completed = run(ai, private, preview, staged, vault)
    assert completed.awaiting_human_review
    record = load_evaluation_record(ai, completed.evaluation_sha256)
    assert record.evaluator.prompt_template_version == EXTERNAL_EVALUATOR_PROMPT_VERSION
    assert record.evaluator.prompt_template_sha256 == external_evaluator_prompt_sha256()
    assert record.model.provider == "synthetic-fake-only"
    assert record.assessment.recommendation == "manual_review"
    assert record.assessment.groundedness == "unknown"
    assert record.assessment.epistemic_status == "unknown"
    assert not record.assessment.conflicts



def test_large_external_generation_evidence_input_has_strict_prompt_budget(tmp_path):
    import hashlib

    _, _, _, source, context, _ = _snapshot(tmp_path)
    entry = source.quotes[0]
    oversized_quote = "x" * 3950
    extended = replace(
        entry,
        exact_quote=oversized_quote,
        quote_sha256=hashlib.sha256(oversized_quote.encode()).hexdigest(),
        proposed_summary="y" * 3950,
    )
    many = replace(source, quotes=(extended,) * 16)
    with pytest.raises(
        ArtifactLifecycleError,
        match="external evaluator prompt exceeds private input budget",
    ):
        _render(many, context)
