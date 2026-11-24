"""Claude (claude.ai export) support in the shared export tools. Synthetic data only."""
import json
import zipfile
from pathlib import Path

import pytest

from obsidian_automation.chatgpt_export_inspect import inspect_export
from obsidian_automation.chatgpt_export_intake import (
    IntakeError, inventory_ready, publish_archive,
)
from obsidian_automation.chatgpt_export_normalize import (
    PARSER_VERSIONS, NormalizeError, normalize_export,
)


def conv(uuid="c1", messages=None, **extra):
    return {"uuid": uuid, "name": "synthetic", "created_at": "2026-01-02T03:04:05.000Z",
            "updated_at": "2026-01-02T03:05:00.000Z", "account": {"uuid": "a1"},
            "chat_messages": messages if messages is not None else [
                {"uuid": "m1", "sender": "human", "created_at": "2026-01-02T03:04:05.000Z",
                 "content": [{"type": "text", "text": "hello"}]},
                {"uuid": "m2", "sender": "assistant", "created_at": "2026-01-02T03:04:07.000Z",
                 "content": [{"type": "thinking", "thinking": "internal"},
                             {"type": "text", "text": "hi there"}]},
            ], **extra}


def make_zip(tmp_path, payload, name="conversations.json"):
    path = tmp_path / "claude.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(name, json.dumps(payload))
    return path


def test_normalize_claude_keeps_visible_text_and_drops_thinking(tmp_path):
    batch = normalize_export(make_zip(tmp_path, [conv()]), scope="private-claude",
                             provider="claude")
    assert batch.provider == "claude"
    assert batch.parser_version == PARSER_VERSIONS["claude"]
    (c,) = batch.conversations
    assert [(m.role, m.text) for m in c.messages] == [("user", "hello"),
                                                     ("assistant", "hi there")]
    assert c.messages[1].parent_node_id == "m1" and c.messages[0].parent_node_id is None
    assert c.messages[0].created_at == pytest.approx(1767323045.0)
    assert not c.omitted_reasons  # thinking block inside a retained message


def test_flat_text_field_and_omission_reasons(tmp_path):
    messages = [
        {"uuid": "m1", "sender": "human", "text": "flat text"},
        {"uuid": "m2", "sender": "assistant",
         "content": [{"type": "tool_use", "id": "t", "name": "x", "input": {}}]},
        {"uuid": "m3", "sender": "assistant", "content": []},
        {"uuid": "m4", "sender": "system", "text": "ignored"},
    ]
    batch = normalize_export(make_zip(tmp_path, [conv(messages=messages)]),
                             scope="private-claude", provider="claude")
    (c,) = batch.conversations
    assert [m.text for m in c.messages] == ["flat text"]
    assert dict(c.omitted_reasons) == {"empty_text": 2, "nonconversation_role": 1}


@pytest.mark.parametrize("payload,code", [
    ({"not": "a list"}, "unsupported_conversations_root"),
    ([conv(uuid="")], "invalid_conversation_id"),
    ([conv(messages="x")], "invalid_conversation_messages"),
    ([conv(messages=[{"uuid": "m", "sender": "human", "text": "a"},
                     {"uuid": "m", "sender": "human", "text": "b"}])],
     "duplicate_selected_message_id"),
    ([conv(messages=[{"uuid": "m", "sender": 3, "text": "a"}])], "unknown_author_role"),
    ([conv(messages=[{"uuid": "m", "sender": "human", "text": "a",
                      "created_at": "not-a-date"}])], "unsupported_timestamp"),
    ([conv("d"), conv("d")], "duplicate_conversation_id"),
])
def test_claude_strict_fixed_error_codes(tmp_path, payload, code):
    with pytest.raises(NormalizeError, match=code):
        normalize_export(make_zip(tmp_path, payload), scope="private-claude",
                         provider="claude")


def test_provider_must_be_known_and_chatgpt_behaviour_is_default(tmp_path):
    with pytest.raises(NormalizeError, match="unsupported_provider"):
        normalize_export(make_zip(tmp_path, [conv()]), scope="s", provider="other")
    # A Claude export read as ChatGPT is rejected, never silently half-imported.
    with pytest.raises(NormalizeError):
        normalize_export(make_zip(tmp_path, [conv()]), scope="s")


def test_inspect_identifies_claude_without_values(tmp_path):
    report = inspect_export(make_zip(tmp_path, [conv(), {}]))
    assert report["provider"] == "claude"
    assert report["claude"]["senders"] == {"human": 1, "assistant": 1}
    assert report["claude"]["content_block_types"] == {"text": 2, "thinking": 1}
    assert report["counts"]["claude_messages"] == 2
    assert report["status"] == "schema_review_required"  # the empty {} slot
    assert "hello" not in json.dumps(report) and "synthetic" not in json.dumps(report)
    clean = inspect_export(make_zip(tmp_path, [conv()]))
    assert clean["status"] == "inspected"


def test_inspect_flags_unknown_senders_blocks_and_mixed(tmp_path):
    odd = conv(messages=[{"uuid": "m", "sender": "robot", "content": [{"type": "weird"}]}])
    report = inspect_export(make_zip(tmp_path, [odd]))
    assert report["status"] == "schema_review_required"
    gpt = {"id": "g", "mapping": {}, "current_node": None}
    assert inspect_export(make_zip(tmp_path, [conv(), gpt]))["provider"] == "mixed"


def test_intake_inventory_per_provider_scope(tmp_path):
    root = tmp_path / "intake"
    root.mkdir(mode=0o700)
    for sub in ("state", "staging", "ready"):
        (root / sub).mkdir(mode=0o700, exist_ok=True)
    src = make_zip(tmp_path, [conv("a"), conv("b")])
    publish_archive(root, src)
    (receipt,) = inventory_ready(root, scope="private-claude", commit=True,
                                 provider="claude")
    assert (receipt.added, receipt.retained_messages) == (2, 4)
    # Same archive again: already journalled, no new run.
    assert inventory_ready(root, scope="private-claude", commit=True,
                           provider="claude") == ()
    # A scope is bound to one provider contract.
    with pytest.raises(IntakeError, match="scope_contract_changed"):
        inventory_ready(root, scope="private-claude", provider="chatgpt")
    with pytest.raises(IntakeError, match="unsupported_provider"):
        inventory_ready(root, scope="x", provider="nope")


ROOT_PARENT = "00000000-0000-4000-8000-000000000000"


def linked(uuid, parent, sender, text, content=None, **extra):
    message = {"uuid": uuid, "parent_message_uuid": parent, "sender": sender,
               "text": text, "created_at": "2026-01-02T03:04:05.000Z",
               "content": content if content is not None else
               [{"type": "text", "text": text}]}
    message.update(extra)
    return message


def test_top_level_text_is_the_final_reply_not_interim_narration(tmp_path):
    messages = [
        linked("m1", ROOT_PARENT, "human", "question"),
        linked("m2", "m1", "assistant", "final answer", content=[
            {"type": "thinking", "thinking": "hidden"},
            {"type": "text", "text": "let me look that up"},
            {"type": "tool_use", "id": "t", "name": "x", "input": {}},
            {"type": "tool_result", "name": "x", "content": "private result"},
            {"type": "injected_prompt_block", "prompt": "system"},
            {"type": "image", "file_uuid": "f"}, {"type": "document", "title": "d"},
            {"type": "text", "text": "final answer"}]),
    ]
    batch = normalize_export(make_zip(tmp_path, [conv(messages=messages)]),
                             scope="private-claude", provider="claude")
    (c,) = batch.conversations
    assert [m.text for m in c.messages] == ["question", "final answer"]


def test_current_branch_follows_parents_from_the_last_message(tmp_path):
    messages = [
        linked("m1", ROOT_PARENT, "human", "first"),
        linked("m2", "m1", "assistant", "old answer"),      # superseded branch
        linked("m3", "m1", "assistant", "regenerated answer"),
        linked("m4", "m3", "human", "follow up"),
        linked("m5", "m4", "assistant", "last answer"),
    ]
    batch = normalize_export(make_zip(tmp_path, [conv(messages=messages)]),
                             scope="private-claude", provider="claude")
    (c,) = batch.conversations
    assert [m.text for m in c.messages] == ["first", "regenerated answer",
                                            "follow up", "last answer"]
    assert c.selected_path_nodes == 4 and c.off_branch_nodes == 1
    assert c.unverified_child_links == 0
    assert c.messages[1].parent_node_id == "m1" and c.messages[0].parent_node_id is None


def test_missing_parent_links_are_linear_but_flagged_unverified(tmp_path):
    batch = normalize_export(make_zip(tmp_path, [conv()]), scope="private-claude",
                             provider="claude")
    (c,) = batch.conversations
    assert c.unverified_child_links == 2 and c.off_branch_nodes == 0
    assert batch.graph_reciprocity_unverified


@pytest.mark.parametrize("messages,code", [
    ([linked("a", ROOT_PARENT, "human", "x"),
      {"uuid": "b", "sender": "human", "text": "y"}], "inconsistent_parent_links"),
    ([linked("a", "b", "human", "x"), linked("b", "a", "assistant", "y")],
     "selected_branch_cycle"),
    ([linked("a", ROOT_PARENT, "human", "x"), linked("b", 5, "assistant", "y")],
     "invalid_branch_parent"),
])
def test_claude_branch_integrity_fails_closed(tmp_path, messages, code):
    with pytest.raises(NormalizeError, match=code):
        normalize_export(make_zip(tmp_path, [conv(messages=messages)]),
                         scope="private-claude", provider="claude")


def test_inspect_reports_branches_and_knows_the_real_block_types(tmp_path):
    messages = [
        linked("m1", ROOT_PARENT, "human", "q"),
        linked("m2", "m1", "assistant", "a1"), linked("m3", "m1", "assistant", "a2",
            content=[{"type": "injected_prompt_block"}, {"type": "image"},
                     {"type": "document"}, {"type": "text", "text": "a2"}]),
    ]
    report = inspect_export(make_zip(tmp_path, [conv(messages=messages)]))
    assert report["provider"] == "claude" and report["status"] == "inspected"
    assert report["counts"]["claude_branch_points"] == 1
    assert report["counts"]["claude_conversations_with_branches"] == 1
    assert report["claude"]["unknown_field_occurrences"]["message"] == 0
