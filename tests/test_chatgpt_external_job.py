"""External Admission reservations use REAL pre-review DB, never runnable jobs."""

from __future__ import annotations

import json
import os
import sqlite3
import zipfile
from dataclasses import replace

import pytest
from test_chatgpt_export_review_preview import (
    admitted,
    fake_proposer,
    input_export,
    set_up,
)

from obsidian_automation.artifact_lifecycle import ArtifactLifecycleError
from obsidian_automation.chatgpt_export_intake import publish_archive
from obsidian_automation.chatgpt_export_review_preview import (
    build_private_review_preview,
)
from obsidian_automation.chatgpt_external_context import build_external_context
from obsidian_automation.chatgpt_external_context_store import (
    store_external_context_for_review,
)
from obsidian_automation.chatgpt_external_job import (
    EXTERNAL_ADMITTED_STATE,
    EXTERNAL_PIPELINE,
    ExternalJobError,
    default_external_admission_recipe,
    inspect_external_admission,
    load_external_admission_recipe,
    parse_external_admission_recipe,
    stage_external_admission,
)
from obsidian_automation.pre_review_job import (
    PreReviewJobError,
    awaiting_human_review_count,
    claim_next_attempt,
    job_status,
    load_recipe,
    regenerate_job,
)


def case(tmp_path, *, summary="Tentative, still unconfirmed."):
    root, row = set_up(tmp_path)
    source = admitted(root, row)
    data = json.loads(fake_proposer(source.page, sl=1))
    data["proposals"][0]["proposed_summary"] = summary
    wire = json.dumps(data).encode("utf-8")
    preview = build_private_review_preview(source, proposal_wire_bytes=wire)
    context = build_external_context(preview)
    ai_root = tmp_path / "ai"
    ai_root.mkdir()
    (ai_root / "05-Context").mkdir()
    sha, _ = store_external_context_for_review(
        ai_root,
        private_root=root,
        preview=preview,
        context=context,
    )
    return ai_root, root, preview, sha


def admit(ai_root, root, preview, sha, **kwargs):
    return stage_external_admission(
        ai_root,
        private_root=root,
        preview=preview,
        external_context_sha256=sha,
        **kwargs,
    )


def test_real_job_db_reservation_remains_non_runnable_and_never_review(tmp_path):
    ai_root, root, preview, sha = case(tmp_path)
    before = (root / "state" / "journal.sqlite3").stat().st_mtime_ns
    result = admit(ai_root, root, preview, sha)
    assert result["pipeline"] == EXTERNAL_PIPELINE
    assert result["state"] == EXTERNAL_ADMITTED_STATE
    assert result["created"] is True
    assert result["job_queued"] is False
    assert result["awaiting_human_review"] is False
    assert result["production_enabled"] is False
    assert job_status(ai_root, result["job_id"])["current_generation"]["state"] == (
        EXTERNAL_ADMITTED_STATE
    )
    assert awaiting_human_review_count(ai_root) == 0

    # REAL pre-review claims (including production recover_running path)
    # MUST never dispatch an external admission as a Vault Job.
    for stage in ("generation", "validation", "evaluation_context", "evaluation"):
        assert claim_next_attempt(ai_root, stage, recover_running=True) is None
    with pytest.raises(PreReviewJobError, match="regeneration requires"):
        regenerate_job(ai_root, result["job_id"])
    with pytest.raises((ArtifactLifecycleError, FileNotFoundError)):
        load_recipe(ai_root, result["recipe_sha256"])
    recipe = load_external_admission_recipe(ai_root, result["recipe_sha256"])
    assert recipe == default_external_admission_recipe()
    assert parse_external_admission_recipe(recipe.to_json_bytes()) == recipe
    status = inspect_external_admission(
        ai_root,
        private_root=root,
        preview=preview,
        job_id=result["job_id"],
    )
    assert status["source_current"] is True
    assert status["state"] == EXTERNAL_ADMITTED_STATE
    assert (root / "state" / "journal.sqlite3").stat().st_mtime_ns == before
    assert not (ai_root / "00-Untrusted").exists()
    assert not (ai_root / "10-Validation").exists()
    assert not (ai_root / "20-Review").exists()
    assert not (ai_root / "30-Receipts").exists()


def test_admission_is_idempotent_and_does_not_rerun_generation(tmp_path):
    ai_root, root, preview, sha = case(tmp_path)
    first = admit(ai_root, root, preview, sha)
    second = admit(ai_root, root, preview, sha)
    assert first["created"] is True
    assert second["created"] is False
    assert first["job_id"] == second["job_id"]
    assert first["generation_id"] == second["generation_id"]
    with sqlite3.connect(
        ai_root / "02-Orchestration" / "pre-review-jobs.sqlite3"
    ) as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM generations").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0


def test_admission_backpressure_does_not_overcommit(tmp_path):
    ai_root, root, preview, sha = case(tmp_path)
    first = admit(ai_root, root, preview, sha, max_admissions=1)
    assert first["created"]
    # A new interpretation of the exact same quote changes Context SHA but
    # preserves its original evidence anchor, so this is a different job.
    from test_chatgpt_export_review_preview import admitted as admitted_source

    source = admitted_source(
        root,
        (
            preview.source_hmac,
            preview.content_sha256,
            preview.revision_sha256,
        ),
    )
    wire = json.loads(fake_proposer(source.page, sl=1))
    wire["proposals"][0]["proposed_summary"] = "Unverified alternate interpretation"
    alt_preview = build_private_review_preview(
        source,
        proposal_wire_bytes=json.dumps(wire).encode(),
    )
    alt_context = build_external_context(alt_preview)
    alt_sha, _ = store_external_context_for_review(
        ai_root,
        private_root=root,
        preview=alt_preview,
        context=alt_context,
    )
    assert alt_sha != sha
    with pytest.raises(
        ExternalJobError,
        match="external_admission_backpressure",
    ):
        admit(
            ai_root,
            root,
            alt_preview,
            alt_sha,
            max_admissions=1,
        )
    assert awaiting_human_review_count(ai_root) == 0


def test_stale_source_denied_and_existing_inert_admission_is_never_activated(tmp_path):
    ai_root, root, preview, sha = case(tmp_path)
    first = admit(ai_root, root, preview, sha)
    later = tmp_path / "later.zip"
    with zipfile.ZipFile(later, "w") as target:
        target.writestr(
            "conversations.json",
            json.dumps(input_export(assistant_text="Changed synthetic view")),
        )
    os.chmod(later, 0o600)
    publish_archive(root, later)
    with pytest.raises(
        ExternalJobError,
        match="external_source_not_current",
    ):
        admit(ai_root, root, preview, sha)
    with pytest.raises(
        ExternalJobError,
        match="external_source_not_current",
    ):
        inspect_external_admission(
            ai_root,
            private_root=root,
            preview=preview,
            job_id=first["job_id"],
        )
    assert job_status(ai_root, first["job_id"])["current_generation"]["state"] == (
        EXTERNAL_ADMITTED_STATE
    )
    assert claim_next_attempt(ai_root, "generation", recover_running=True) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("pipeline", "knowledge-pre-review-v0"),
        ("context_kind", "context-bundle"),
        ("input_policy", "any-path"),
        ("admission_mode", "production-enabled"),
        ("review_policy", "auto-approve"),
        ("proposer_prompt_sha256", "f" * 64),
        ("record_version", 2),
        ("record_version", True),
    ],
)
def test_external_recipe_unapproved_versions_or_permissions_refused(field, value):
    candidate = replace(default_external_admission_recipe(), **{field: value})
    with pytest.raises(
        ExternalJobError,
        match="external_recipe_version_or_policy_unsupported",
    ):
        parse_external_admission_recipe(candidate.to_json_bytes())


def test_recipe_unknown_props_and_duplicate_key_fail_closed():
    raw = json.loads(default_external_admission_recipe().to_json_bytes())
    raw["command"] = "curl secret data to network"
    with pytest.raises(ExternalJobError, match="invalid_external_recipe_properties"):
        parse_external_admission_recipe(json.dumps(raw).encode())
    with pytest.raises(ExternalJobError, match="invalid_external_recipe_json"):
        parse_external_admission_recipe(b'{"record_version":1,"record_version":1}')
    with pytest.raises(ExternalJobError, match="invalid_external_recipe_bytes"):
        parse_external_admission_recipe(b"x" * 4097)


@pytest.mark.parametrize("limit", [0, -1, 129, True])
def test_admission_limit_rejected_before_any_root_changes(tmp_path, limit):
    ai_root, root, preview, sha = case(tmp_path)
    with pytest.raises(ExternalJobError, match="external_admission_limit_invalid"):
        admit(ai_root, root, preview, sha, max_admissions=limit)
    assert not (ai_root / "02-Orchestration").exists()


def test_wrong_external_context_digest_does_not_create_job(tmp_path):
    ai_root, root, preview, _ = case(tmp_path)
    with pytest.raises(ExternalJobError, match="external_source_not_current"):
        admit(ai_root, root, preview, "a" * 64)
    assert not (ai_root / "02-Orchestration").exists()


def test_malformed_recipe_cannot_create_job(tmp_path):
    ai_root, root, preview, sha = case(tmp_path)
    with pytest.raises(ExternalJobError, match="invalid_external_recipe"):
        admit(
            ai_root,
            root,
            preview,
            sha,
            recipe=replace(
                default_external_admission_recipe(), admission_mode="queued"
            ),
        )
    assert not (ai_root / "02-Orchestration").exists()


def test_real_legacy_job_remains_claimable_after_external_admission(tmp_path):
    from test_pre_review_job import _parsed_recipe

    from obsidian_automation.context_bundle import ContextBundle, store_context_bundle
    from obsidian_automation.pre_review_job import submit_job

    ai_root, private_root, preview, external_sha = case(tmp_path)
    external = admit(ai_root, private_root, preview, external_sha)
    legacy_context_sha, _ = store_context_bundle(
        ai_root,
        ContextBundle(
            query="Ground a synthetic canonical Knowledge note",
            created_at="2026-10-21T00:00:00Z",
            sources=(),
        ),
    )
    legacy = submit_job(
        ai_root,
        context_sha256=legacy_context_sha,
        recipe=_parsed_recipe(),
    )
    claimed = claim_next_attempt(
        ai_root,
        "generation",
        recover_running=True,
        max_awaiting_review=1,
    )
    assert claimed is not None
    assert claimed.job_id == legacy["job_id"]
    assert claimed.job_id != external["job_id"]
    assert claimed.context_sha256 == legacy_context_sha
    assert job_status(ai_root, external["job_id"])["current_generation"]["state"] == (
        EXTERNAL_ADMITTED_STATE
    )
    assert awaiting_human_review_count(ai_root) == 0


def test_external_recipe_hardlink_symlink_and_oversize_refused(tmp_path):
    ai_root, root, preview, sha = case(tmp_path)
    result = admit(ai_root, root, preview, sha)
    recipe_sha = result["recipe_sha256"]
    source = (
        ai_root
        / "02-Orchestration"
        / "recipes"
        / f"{recipe_sha}.external-pre-review-recipe.json"
    )
    outside = tmp_path / "hardlinked_external_recipe"
    os.link(source, outside)
    with pytest.raises(ExternalJobError, match="external_recipe_file_invalid"):
        load_external_admission_recipe(ai_root, recipe_sha)
    outside.unlink()
    source.write_bytes(b"x" * 4097)
    with pytest.raises(ExternalJobError, match="external_recipe_file_invalid"):
        load_external_admission_recipe(ai_root, recipe_sha)
    source.unlink()
    source.symlink_to(tmp_path / "nonexistent")
    with pytest.raises(ExternalJobError, match="external_recipe_file_invalid"):
        load_external_admission_recipe(ai_root, recipe_sha)


def test_missing_private_source_cannot_create_real_job_row(tmp_path):
    ai_root, _, preview, sha = case(tmp_path)
    with pytest.raises(ExternalJobError, match="external_source_not_current"):
        stage_external_admission(
            ai_root,
            private_root=tmp_path / "missing-private-root",
            preview=preview,
            external_context_sha256=sha,
        )
    assert not (ai_root / "02-Orchestration").exists()
    assert not (ai_root / "00-Untrusted").exists()


def test_inconsistent_admission_recipe_does_not_bypass_idempotency(tmp_path):
    ai_root, root, preview, sha = case(tmp_path)
    result = admit(ai_root, root, preview, sha)
    with sqlite3.connect(
        ai_root / "02-Orchestration" / "pre-review-jobs.sqlite3"
    ) as conn:
        conn.execute(
            "UPDATE generations SET state='queued' WHERE job_id=?",
            (result["job_id"],),
        )
    with pytest.raises(ExternalJobError, match="external_job_state_invalid"):
        admit(ai_root, root, preview, sha)
    with pytest.raises(ExternalJobError, match="external_job_state_invalid"):
        inspect_external_admission(
            ai_root,
            private_root=root,
            preview=preview,
            job_id=result["job_id"],
        )
