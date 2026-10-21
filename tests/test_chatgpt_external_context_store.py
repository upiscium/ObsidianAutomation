"""Synthetic private Reader→separate external CAS; legacy Vault gate unchanged."""

from __future__ import annotations

import json
import os
import zipfile
from dataclasses import replace

import pytest
from test_chatgpt_export_review_preview import (
    admitted,
    fake_proposer,
    input_export,
    set_up,
)
from test_pre_review_job import _parsed_recipe

from obsidian_automation.artifact_lifecycle import ArtifactLifecycleError
from obsidian_automation.chatgpt_export_intake import (
    inventory_ready,
    publish_archive,
)
from obsidian_automation.chatgpt_external_context import (
    build_external_context,
)
from obsidian_automation.chatgpt_external_context_store import (
    EXTERNAL_CONTEXT_SUFFIX,
    ExternalContextStoreError,
    load_external_context_for_review,
    revalidate_saved_external_context,
    store_external_context_for_review,
)
from obsidian_automation.context_bundle import load_context_bundle
from obsidian_automation.generator_contract import load_and_render_generator_prompt
from obsidian_automation.pre_review_job import submit_job


def fixture(tmp_path, *, empty=False):
    root, row = set_up(tmp_path)
    source = admitted(root, row)
    if empty:
        wire = json.dumps(
            {
                "contract_version": "chatgpt-evidence-candidate-v0",
                "proposals": [],
            }
        ).encode("utf-8")
    else:
        wire = fake_proposer(source.page, sl=1)
    from obsidian_automation.chatgpt_export_review_preview import (
        build_private_review_preview,
    )

    preview = build_private_review_preview(source, proposal_wire_bytes=wire)
    context = build_external_context(preview)
    ai_root = tmp_path / "ai-state"
    ai_root.mkdir()
    (ai_root / "05-Context").mkdir()
    return ai_root, root, preview, context


def publish(ai_root, root, preview, context):
    return store_external_context_for_review(
        ai_root,
        private_root=root,
        preview=preview,
        context=context,
    )


def test_external_context_cas_is_separate_private_and_repeatable(tmp_path):
    ai_root, root, preview, context = fixture(tmp_path)
    pre_state = (root / "state" / "journal.sqlite3").stat().st_mtime_ns
    first_sha, path = publish(ai_root, root, preview, context)
    second_sha, second_path = publish(ai_root, root, preview, context)
    assert first_sha == second_sha
    assert path == second_path
    assert path.name == f"{first_sha}.{EXTERNAL_CONTEXT_SUFFIX}.json"
    assert len(list((ai_root / "05-Context").iterdir())) == 1
    assert path.read_bytes() == context.to_json_bytes()
    loaded = load_external_context_for_review(ai_root, first_sha)
    assert loaded == context
    reloaded = revalidate_saved_external_context(
        ai_root,
        private_root=root,
        preview=preview,
        external_context_sha256=first_sha,
    )
    assert reloaded == context
    assert loaded.generation_authorized is False
    assert loaded.human_review_approved is False
    assert loaded.vault_mutation_authorized is False
    assert (root / "state" / "journal.sqlite3").stat().st_mtime_ns == pre_state
    assert "PRIVATE_FAKE_PROVIDER_MUST_NOT_LOG" not in repr(loaded)


def test_legacy_vault_context_and_job_reject_external_cas(tmp_path):
    ai_root, root, preview, context = fixture(tmp_path)
    sha, _ = publish(ai_root, root, preview, context)
    with pytest.raises(ArtifactLifecycleError):
        load_context_bundle(ai_root, sha)
    with pytest.raises(ArtifactLifecycleError):
        load_and_render_generator_prompt(ai_root, sha)
    with pytest.raises(ArtifactLifecycleError):
        submit_job(ai_root, context_sha256=sha, recipe=_parsed_recipe())
    # Reject before allocating the normal pre-review Job db/recipe.
    assert not (ai_root / "02-Orchestration").exists()


def test_tampered_hash_is_rejected(tmp_path):
    ai_root, root, preview, context = fixture(tmp_path)
    sha, path = publish(ai_root, root, preview, context)
    path.write_bytes(b'{"record_version":1,"tampered":true}')
    with pytest.raises(
        ExternalContextStoreError, match="external_context_digest_mismatch"
    ):
        load_external_context_for_review(ai_root, sha)


def test_symlink_and_hardlink_external_contexts_are_rejected(tmp_path):
    ai_root, root, preview, context = fixture(tmp_path)
    sha, path = publish(ai_root, root, preview, context)
    other = tmp_path / "hardlink"
    os.link(path, other)
    with pytest.raises(
        ExternalContextStoreError, match="external_context_file_invalid"
    ):
        load_external_context_for_review(ai_root, sha)
    other.unlink()
    path.unlink()
    path.symlink_to(tmp_path / "source.zip")
    with pytest.raises(
        ExternalContextStoreError, match="external_context_file_invalid"
    ):
        load_external_context_for_review(ai_root, sha)


def test_future_export_invalidates_saved_context_even_when_cas_is_unchanged(tmp_path):
    ai_root, root, preview, context = fixture(tmp_path)
    sha, path = publish(ai_root, root, preview, context)
    updated = tmp_path / "updated.zip"
    with zipfile.ZipFile(updated, "w") as archive:
        archive.writestr(
            "conversations.json",
            json.dumps(input_export(assistant_text="Updated private Source")),
        )
    os.chmod(updated, 0o600)
    publish_archive(root, updated)
    with pytest.raises(
        ExternalContextStoreError, match="external_source_stale_or_forged"
    ):
        revalidate_saved_external_context(
            ai_root,
            private_root=root,
            preview=preview,
            external_context_sha256=sha,
        )
    inventory_ready(root, scope="fake-e2e", commit=True)
    with pytest.raises(
        ExternalContextStoreError, match="external_source_stale_or_forged"
    ):
        revalidate_saved_external_context(
            ai_root,
            private_root=root,
            preview=preview,
            external_context_sha256=sha,
        )
    # Source CAS still exists but cannot be re-admitted.
    assert path.exists()


def test_no_candidate_must_not_publish_generation_context(tmp_path):
    ai_root, root, preview, context = fixture(tmp_path, empty=True)
    with pytest.raises(
        ExternalContextStoreError, match="external_context_no_candidate"
    ):
        publish(ai_root, root, preview, context)
    assert list((ai_root / "05-Context").iterdir()) == []


def test_wrong_context_kind_and_changed_source_provenance_fail_closed(tmp_path):
    ai_root, root, preview, context = fixture(tmp_path)
    with pytest.raises(
        ExternalContextStoreError, match="invalid_external_context_kind"
    ):
        publish(ai_root, root, preview, replace(context, context_kind="context-bundle"))
    forged = replace(context, source_preview_sha256="a" * 64)
    with pytest.raises(
        ExternalContextStoreError, match="external_source_stale_or_forged"
    ):
        publish(ai_root, root, preview, forged)


def test_store_never_initializes_missing_private_root_or_ai_context(tmp_path):
    ai_root, root, preview, context = fixture(tmp_path)
    missing_ai = tmp_path / "missing-ai-root"
    with pytest.raises(
        ExternalContextStoreError, match="external_context_storage_unavailable"
    ):
        publish(missing_ai, root, preview, context)
    assert not missing_ai.exists()

    missing_private = tmp_path / "missing-private-root"
    with pytest.raises(
        ExternalContextStoreError, match="external_source_stale_or_forged"
    ):
        publish(ai_root, missing_private, preview, context)
    assert not missing_private.exists()


def test_oversized_external_context_file_is_rejected_before_read(tmp_path):
    ai_root, root, preview, context = fixture(tmp_path)
    sha, path = publish(ai_root, root, preview, context)
    path.write_bytes(b"x" * (64 * 1024 + 2))
    with pytest.raises(
        ExternalContextStoreError, match="external_context_file_invalid"
    ):
        load_external_context_for_review(ai_root, sha)


def test_fifo_replacement_refused_without_blocking(tmp_path):
    ai_root, root, preview, context = fixture(tmp_path)
    sha, path = publish(ai_root, root, preview, context)
    path.unlink()
    os.mkfifo(path, 0o600)
    with pytest.raises(
        ExternalContextStoreError, match="external_context_file_invalid"
    ):
        load_external_context_for_review(ai_root, sha)
