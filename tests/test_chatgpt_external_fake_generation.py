"""Synthetic external Job Admission→Fake draft v3; no Job stage or Review."""

from __future__ import annotations

import json
import os
import zipfile

import pytest
from test_chatgpt_export_review_preview import input_export
from test_chatgpt_external_job import admit, case

from obsidian_automation.artifact_lifecycle import ArtifactLifecycleError
from obsidian_automation.chatgpt_export_intake import publish_archive
from obsidian_automation.chatgpt_external_generation import (
    ExternalDraftError,
    run_fake_external_knowledge_draft,
)
from obsidian_automation.generation_artifact import (
    generation_input_context,
    load_generation_record,
)
from obsidian_automation.pre_review_job import (
    awaiting_human_review_count,
    claim_next_attempt,
    job_status,
    stage_output,
)


def fake_output(_):
    return json.dumps(
        {
            "title": "Synthetic External Evidence Review",
            "category": "reference",
            "source_type": "other",
            "body": (
                "# Synthetic External Evidence\n\n"
                "This is an **unverified** proposition from a synthetic source. "
                "Neither an assistant proposal nor a user report establishes "
                "implementation or human approval. All future actions require "
                "separate semantic evidence evaluation and explicit Human Review.\n"
            ),
        }
    ).encode("utf-8")


def setup(tmp_path):
    ai_root, private_root, preview, ctx_sha = case(tmp_path)
    staged = admit(ai_root, private_root, preview, ctx_sha)
    return ai_root, private_root, preview, staged


def call(ai_root, root, preview, staged, *, transport=fake_output):
    return run_fake_external_knowledge_draft(
        ai_root,
        private_root=root,
        preview=preview,
        job_id=staged["job_id"],
        transport=transport,
        implementation_revision="f" * 40,
    )


def test_real_external_job_db_plus_fake_draft_record_still_requires_real_pipeline(
    tmp_path,
):
    ai_root, root, preview, staged = setup(tmp_path)
    receipt = call(ai_root, root, preview, staged)
    assert receipt.job_id == staged["job_id"]
    assert receipt.context_sha256 == staged["context_sha256"]
    assert len(receipt.generation_sha256) == 64
    assert len(receipt.proposal_sha256) == 64
    assert receipt.state == "external_admitted"
    assert not receipt.real_job_stage_output_selected
    assert not receipt.awaiting_human_review
    assert not receipt.canonical_mutation_authorized
    record = load_generation_record(ai_root, receipt.generation_sha256)
    assert record.record_version == 3
    assert record.context_kind == "chatgpt-external-source"
    assert record.external_source
    assert record.external_source.source_revision_sha256 == (
        receipt.source_revision_sha256
    )
    assert record.proposal_sha256 == receipt.proposal_sha256
    assert record.context_sha256 == receipt.context_sha256
    assert record.model.provider == "synthetic-fake-only"
    assert record.generator.prompt_template_version == (
        "chatgpt-external-knowledge-generator-v0"
    )
    assert job_status(ai_root, receipt.job_id)["current_generation"]["state"] == (
        "external_admitted"
    )
    assert stage_output(ai_root, staged["generation_id"], "generation") is None
    assert claim_next_attempt(ai_root, "generation", recover_running=True) is None
    assert awaiting_human_review_count(ai_root) == 0
    assert not list((ai_root / "20-Review").glob("*.json"))
    # The current evaluator deliberately refuses to invent a Vault source path
    # for this new external source kind until a dedicated adapter exists.
    with pytest.raises(ArtifactLifecycleError):
        generation_input_context(ai_root, record)


def test_fake_generation_is_idempotent_in_cas_without_job_activation(tmp_path):
    ai_root, root, preview, staged = setup(tmp_path)
    first = call(ai_root, root, preview, staged)
    second = call(ai_root, root, preview, staged)
    assert first.proposal_sha256 == second.proposal_sha256
    # Generation artifact can differ on the default generated_at timestamp;
    # the source is exactly the same and no Job stage was selected.
    assert first.source_run_sha256 == second.source_run_sha256
    assert job_status(ai_root, staged["job_id"])["current_generation"]["state"] == (
        "external_admitted"
    )


def test_transport_failure_and_wrong_source_type_do_not_generate(tmp_path):
    ai_root, root, preview, staged = setup(tmp_path)

    def bad_transport(_prompt):
        raise RuntimeError("PRIVATE_FAKE_ERROR_DO_NOT_LEAK")

    with pytest.raises(ExternalDraftError, match="synthetic_provider_failed") as e:
        call(ai_root, root, preview, staged, transport=bad_transport)
    assert "PRIVATE_FAKE_ERROR_DO_NOT_LEAK" not in str(e.value)
    altered = json.loads(fake_output(None))
    altered["source_type"] = "official"
    with pytest.raises(ExternalDraftError, match="synthetic_source_type_must_be_other"):
        call(
            ai_root,
            root,
            preview,
            staged,
            transport=lambda _: json.dumps(altered).encode(),
        )
    assert not list((ai_root / "00-Untrusted").glob("*.proposal.json"))


def test_wrong_generator_contract_refused_without_job_effect(tmp_path):
    ai_root, root, preview, staged = setup(tmp_path)
    with pytest.raises(ExternalDraftError, match="synthetic_generator_output_invalid"):
        call(ai_root, root, preview, staged, transport=lambda _: b'{"not":"generator"}')
    assert stage_output(ai_root, staged["generation_id"], "generation") is None
    assert awaiting_human_review_count(ai_root) == 0


def test_new_ready_archive_during_fake_generation_fails_closed_before_artifact(
    tmp_path,
):
    ai_root, root, preview, staged = setup(tmp_path)
    updated = tmp_path / "updated-source.zip"

    def concurrent_transport(prompt):
        with zipfile.ZipFile(updated, "w") as target:
            target.writestr(
                "conversations.json",
                json.dumps(input_export(assistant_text="changed while generating")),
            )
        os.chmod(updated, 0o600)
        publish_archive(root, updated)
        return fake_output(prompt)

    with pytest.raises(
        ExternalDraftError, match="external_source_changed_during_inference"
    ):
        call(ai_root, root, preview, staged, transport=concurrent_transport)
    assert not list((ai_root / "00-Untrusted").glob("*.proposal.json"))


def test_invalid_fake_generation_metadata_and_missing_transport_rejected(tmp_path):
    ai_root, root, preview, staged = setup(tmp_path)
    with pytest.raises(ExternalDraftError, match="explicit_fake_transport_required"):
        call(ai_root, root, preview, staged, transport=None)
    with pytest.raises(
        ExternalDraftError, match="invalid_fake_implementation_revision"
    ):
        run_fake_external_knowledge_draft(
            ai_root,
            private_root=root,
            preview=preview,
            job_id=staged["job_id"],
            transport=fake_output,
            implementation_revision="improper",
        )
    assert not list((ai_root / "00-Untrusted").glob("*.proposal.json"))
