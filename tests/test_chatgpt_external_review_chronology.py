"""Private selected-branch later User context, synthetic Fake Provider E2E."""

from __future__ import annotations

import hashlib
import json
import os
import zipfile
from dataclasses import replace

import pytest
from test_chatgpt_export_review_preview import SCOPE, fake_proposer, input_export
from test_chatgpt_external_fake_generation import fake_output
from test_chatgpt_external_worker_e2e import run, setup
from test_pre_review_worker import _knowledge_note

from obsidian_automation.chatgpt_export_intake import inventory_ready, publish_archive
from obsidian_automation.chatgpt_export_reader import (
    list_private_current_sources,
    read_admitted_source_page,
)
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
from obsidian_automation.chatgpt_external_review_chronology import (
    CHRONOLOGY_VERSION,
    PrivateChronologyError,
    read_private_later_user_context,
)
from obsidian_automation.chatgpt_external_worker import (
    run_external_synthetic_worker_once,
)
from obsidian_automation.evaluation_artifact import (
    EVALUATION_CONTEXT_STAGE,
    EVALUATION_REQUEST_STAGE,
    EVALUATION_STAGE,
)
from obsidian_automation.pre_review_job import job_status


def _with_later_users(tmp_path, later_user_texts):
    """Build the entire synthetic immutable ZIP→Job→Review chain."""
    records = input_export()
    graph = records[0]["mapping"]
    preceding = "assistant"
    for index, text in enumerate(later_user_texts):
        node = f"later-user-{index}"
        graph[preceding]["children"] = [node]
        graph[node] = {
            "id": node,
            "parent": preceding,
            "message": {
                "id": f"later-message-{index}",
                "author": {"role": "user"},
                "content": {"content_type": "text", "parts": [text]},
            },
            "children": [],
        }
        preceding = node
    records[0]["current_node"] = preceding
    zip_path = tmp_path / "synthetic-with-later-users.zip"
    with zipfile.ZipFile(zip_path, "w") as writer:
        writer.writestr(
            "conversations.json",
            json.dumps(records, ensure_ascii=False),
        )
    os.chmod(zip_path, 0o600)
    root = tmp_path / "private"
    publish_archive(root, zip_path)
    inventory_ready(root, scope=SCOPE, commit=True)
    (opaque,) = list_private_current_sources(root, scope=SCOPE).sources
    source = read_admitted_source_page(
        root,
        scope=SCOPE,
        source_hmac=opaque.source_hmac,
        expected_content_sha256=opaque.content_sha256,
        expected_revision_sha256=opaque.revision_sha256,
    )
    assert [m.role for m in source.conversation.messages[:2]] == ["user", "assistant"]
    wire = fake_proposer(source.page, sl=1)
    preview = build_private_review_preview(
        source,
        proposal_wire_bytes=wire,
    )
    ai_root = tmp_path / "ai"
    ai_root.mkdir()
    for directory in (
        "05-Context",
        "04-Index",
        EVALUATION_REQUEST_STAGE,
        EVALUATION_CONTEXT_STAGE,
        EVALUATION_STAGE,
        "24-Locks/read-view",
    ):
        (ai_root / directory).mkdir(parents=True)
    typed = build_external_context(preview)
    context_sha, _ = store_external_context_for_review(
        ai_root,
        private_root=root,
        preview=preview,
        context=typed,
    )
    job = stage_external_admission(
        ai_root,
        private_root=root,
        preview=preview,
        external_context_sha256=context_sha,
        recipe=default_external_synthetic_recipe(),
    )
    vault = tmp_path / "vault" / "11-Knowledge"
    vault.mkdir(parents=True)
    (vault / "Existing.md").write_text(
        _knowledge_note("# Existing\n\nUnmodified synthetic knowledge."),
        encoding="utf-8",
    )
    outcome = run_external_synthetic_worker_once(
        ai_root,
        private_root=root,
        vault_root=vault.parent,
        preview=preview,
        job_id=job["job_id"],
        fake_generator_wire=fake_output(None),
    )
    assert outcome.awaiting_human_review
    return ai_root, root, preview, job, vault


def _review(ai, private, preview, job, **kwargs):
    return read_private_later_user_context(
        ai,
        private_root=private,
        preview=preview,
        job_id=job["job_id"],
        **kwargs,
    )


def test_no_later_user_turn_is_not_proof_of_approval_or_global_completeness(tmp_path):
    ai, private, preview, job, vault = setup(tmp_path)
    output = run(ai, private, preview, job, vault)
    assert output.awaiting_human_review
    packet = _review(ai, private, preview, job)
    assert packet.version == CHRONOLOGY_VERSION
    assert len(packet.candidate_context) == 1
    assert packet.candidate_context[0].later_user_turns == ()
    assert packet.candidate_context[0].more_later_user_turns_unshown is False
    assert packet.semantic_status == "unknown"
    assert packet.user_confirmation_verified is False
    assert packet.complete_cross_conversation_correction_check is False
    assert packet.human_approval_granted is False
    assert packet.canonical_vault_mutated is False


def test_later_user_correction_is_shown_exactly_as_data_not_an_auto_judgment(tmp_path):
    correction = "後で訂正します。Assistantの「実装済み」は誤りで、まだ未完成です。"
    ai, private, preview, job, _ = _with_later_users(tmp_path, [correction])
    initial = set((ai / "20-Review").iterdir())
    packet = _review(ai, private, preview, job)
    (candidate,) = packet.candidate_context
    (followup,) = candidate.later_user_turns
    assert candidate.reference_message_index == 1
    assert followup.selected_message_index == 2
    assert followup.source_role == "user"
    assert followup.message_after_evidence is True
    assert followup.preview_text == correction
    assert followup.text_sha256 == hashlib.sha256(correction.encode()).hexdigest()
    assert followup.truncated is False
    assert followup.semantic_correction_verified is False
    assert followup.user_confirmation_verified is False
    assert packet.semantic_status == "unknown"
    assert candidate.complete_chronological_correction_check is False
    assert candidate.later_selected_branch_only
    assert correction not in repr(packet)
    assert packet.source_revision_sha256 == preview.revision_sha256
    assert len(packet.chronology_sha256) == 64
    assert initial == set((ai / "20-Review").iterdir())
    assert job_status(ai, job["job_id"])["current_generation"]["state"] == (
        "awaiting_human_review"
    )


def test_explicit_user_approval_words_are_not_automatic_authorization(tmp_path):
    ai, private, preview, job, _ = _with_later_users(
        tmp_path,
        ["はい、私が正式に承認しました。適用してください。"],
    )
    packet = _review(ai, private, preview, job)
    assert (
        "承認しました" in packet.candidate_context[0].later_user_turns[0].preview_text
    )
    assert not packet.user_confirmation_verified
    assert not packet.human_approval_granted
    assert (
        not packet.candidate_context[0].later_user_turns[0].user_confirmation_verified
    )
    assert not list((ai / "20-Review").glob("*.approval.json"))


def test_unicode_truncation_respects_byte_limit_and_never_claims_full_text(tmp_path):
    long_text = "訂正🎉" * 200
    ai, private, preview, job, _ = _with_later_users(tmp_path, [long_text])
    packet = _review(ai, private, preview, job, max_snippet_bytes=65)
    (followup,) = packet.candidate_context[0].later_user_turns
    assert followup.truncated is True
    assert 0 < len(followup.preview_text.encode("utf-8")) <= 65
    assert long_text.startswith(followup.preview_text)
    assert followup.text_sha256 == hashlib.sha256(long_text.encode("utf-8")).hexdigest()
    assert packet.complete_cross_conversation_correction_check is False


def test_turn_budget_flags_omitted_later_context_without_silent_completion(tmp_path):
    ai, private, preview, job, _ = _with_later_users(
        tmp_path,
        ["Maybe", "Retracted", "Another update"],
    )
    packet = _review(ai, private, preview, job, max_later_turns=1)
    (context,) = packet.candidate_context
    assert len(context.later_user_turns) == 1
    assert context.later_user_turns[0].preview_text == "Maybe"
    assert context.more_later_user_turns_unshown is True
    assert context.complete_chronological_correction_check is False
    assert not packet.human_approval_granted


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_later_turns": 0},
        {"max_later_turns": True},
        {"max_later_turns": 9},
        {"max_snippet_bytes": 15},
        {"max_snippet_bytes": True},
        {"max_snippet_bytes": 1025},
    ],
)
def test_invalid_later_context_limits_are_rejected(tmp_path, kwargs):
    ai, private, preview, job, _ = setup(tmp_path)
    with pytest.raises(PrivateChronologyError, match="later_user_.*_limit_invalid"):
        _review(ai, private, preview, job, **kwargs)


def test_stale_archive_refuses_review_context(tmp_path):
    ai, private, preview, job, _ = _with_later_users(tmp_path, ["Review later"])
    later = tmp_path / "changed.zip"
    with zipfile.ZipFile(later, "w") as writer:
        writer.writestr(
            "conversations.json",
            json.dumps(input_export(assistant_text="A new private revision")),
        )
    os.chmod(later, 0o600)
    publish_archive(private, later)
    with pytest.raises(
        PrivateChronologyError,
        match="later_user_source_unavailable",
    ):
        _review(ai, private, preview, job)


def test_forged_review_preview_cannot_reveal_later_user_messages(tmp_path):
    ai, private, preview, job, _ = _with_later_users(
        tmp_path,
        ["Sensitive fake user correction"],
    )
    candidate = preview.candidate_batch.candidates[0]
    altered = replace(
        preview,
        candidate_batch=replace(
            preview.candidate_batch,
            candidates=(replace(candidate, excerpt="Invented quote"),),
        ),
    )
    with pytest.raises(PrivateChronologyError):
        _review(ai, private, altered, job)


def test_later_context_digest_stable_and_user_identifiers_never_return(tmp_path):
    ai, private, preview, job, _ = _with_later_users(
        tmp_path,
        ["Unconfirmed correction", "Final?"],
    )
    first = _review(ai, private, preview, job)
    second = _review(ai, private, preview, job)
    assert first == second
    assert first.chronology_sha256 == second.chronology_sha256
    assert preview.source_hmac not in repr(first)
    assert not any(
        hasattr(entry, "message_id")
        for entry in first.candidate_context[0].later_user_turns
    )
