"""Synthetic-only contract tests. No real private ChatGPT data in this suite."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import zipfile

import pytest

from obsidian_automation.chatgpt_export_inspect import Limits
from obsidian_automation.chatgpt_export_normalize import (
    NormalizeError, inventory_delta, normalize_export,
)

SECRET = "PRIVATE_SENTINEL_MUST_NOT_BE_LOGGED"


def msg(id_: str, role: str, text: str = SECRET, **overrides):
    result = {
        "id": id_, "author": {"role": role},
        "create_time": 1234.0,
        "recipient": "all",
        "content": {"content_type": "text", "parts": [text]},
    }
    result.update(overrides)
    return result


def conversation(id_: str = "conversation1"):
    return {
        "id": id_, "title": SECRET,
        "current_node": "last",
        "mapping": {
            "root": {"id": "root", "parent": None, "children": ["first"], "message": None},
            "first": {"id": "first", "parent": "root", "children": ["second", "alt"],
                      "message": msg("m1", "user", "question")},
            "second": {"id": "second", "parent": "first", "children": ["last"],
                       "message": msg("m2", "assistant", "answer")},
            "last": {"id": "last", "parent": "second", "children": [],
                     "message": msg("m3", "user", SECRET)},
            "alt": {"id": "alt", "parent": "first", "children": [],
                    "message": msg("m4", "assistant", "unselected secret")},
        },
    }


def archive(tmp_path: Path, payload: list | object, extra=()) -> Path:
    path = tmp_path / "private_chatgpt.zip"
    with zipfile.ZipFile(path, "w") as target:
        target.writestr("conversations.json", json.dumps(payload, ensure_ascii=False))
        for name, data in extra:
            target.writestr(name, data)
    return path


def run(path: Path, **kwargs):
    return normalize_export(path, scope="private-chatgpt", **kwargs)


def test_selected_parent_branch_only_and_no_side_effects(tmp_path):
    path = archive(tmp_path, [conversation()], [("account.json", SECRET)])
    before = path.read_bytes()
    files = tuple(tmp_path.iterdir())
    result = run(path)
    assert result.real_schema_accepted is False
    assert result.selected_json_members == 1
    assert result.unselected_json_members == 1
    assert len(result.conversations) == 1
    conv = result.conversations[0]
    assert [message.text for message in conv.messages] == ["question", "answer", SECRET]
    assert [message.node_id for message in conv.messages] == ["first", "second", "last"]
    assert conv.off_branch_nodes == 1
    assert conv.omitted_reasons == (("placeholder", 1),)
    assert result.import_run_sha256 != result.archive_sha256
    assert path.read_bytes() == before
    assert tuple(tmp_path.iterdir()) == files
    assert SECRET not in repr(result)
    assert SECRET not in repr(conv)
    assert SECRET not in repr(conv.messages[2])
    assert run(path).import_run_sha256 == result.import_run_sha256


def test_off_branch_edit_no_content_delta_but_changes_archive(tmp_path):
    data = conversation()
    first = run(archive(tmp_path, [data]))
    data["mapping"]["alt"]["message"]["content"]["parts"] = [SECRET]
    second = run(archive(tmp_path, [data]))
    assert second.archive_sha256 != first.archive_sha256
    assert inventory_delta(first, second).unchanged == 1
    assert first.conversations[0].content_sha256 == second.conversations[0].content_sha256
    assert first.conversations[0].source_revision_sha256 != second.conversations[0].source_revision_sha256


def test_title_update_no_generation_change(tmp_path):
    data = conversation()
    first = run(archive(tmp_path, [data]))
    data["title"] = "new title"
    data["update_time"] = 983223.0
    second = run(archive(tmp_path, [data]))
    assert inventory_delta(first, second).unchanged == 1
    assert first.conversations[0].source_revision_sha256 != second.conversations[0].source_revision_sha256


def test_changed_text_and_changed_branch_detected(tmp_path):
    data = conversation()
    original = run(archive(tmp_path, [data]))
    data["mapping"]["last"]["message"]["content"]["parts"] = ["updated"]
    edited = run(archive(tmp_path, [data]))
    assert inventory_delta(original, edited).changed == 1

    data = conversation()
    data["current_node"] = "alt"
    branched = run(archive(tmp_path, [data]))
    assert [m.text for m in branched.conversations[0].messages] == ["question", "unselected secret"]
    assert inventory_delta(original, branched).changed == 1


def test_same_export_twice_no_op_and_absence_is_not_deletion(tmp_path):
    old = run(archive(tmp_path, [conversation("a"), conversation("b")]))
    assert inventory_delta(old, old).unchanged == 2
    new = run(archive(tmp_path, [conversation("a"), conversation("c")]))
    delta = inventory_delta(old, new)
    assert (delta.added, delta.changed, delta.unchanged, delta.absent_not_deleted) == (1, 0, 1, 1)
    with pytest.raises(NormalizeError, match="incompatible_inventory"):
        inventory_delta(old, replace(old, scope="different"))


def test_selected_roles_channels_and_nontext_not_claimed(tmp_path):
    data = conversation()
    data["mapping"]["first"]["message"]["author"]["role"] = "system"
    data["mapping"]["second"]["message"]["channel"] = "analysis"
    data["mapping"]["last"]["message"]["content"] = {
        "content_type": "multimodal_text", "parts": [SECRET, {"image": SECRET}],
    }
    result = run(archive(tmp_path, [data]))
    conv = result.conversations[0]
    assert not conv.messages
    assert dict(conv.omitted_reasons) == {
        "nonconversation_role": 1, "nonfinal_assistant_message": 1,
        "nontext_parts": 1, "placeholder": 1,
    }
    assert SECRET not in repr(result)


def test_human_visible_flag_and_tool_recipient_excluded(tmp_path):
    data = conversation()
    data["mapping"]["first"]["message"]["metadata"] = {"is_visually_hidden_from_conversation": True}
    data["mapping"]["second"]["message"]["recipient"] = "python"
    conv = run(archive(tmp_path, [data])).conversations[0]
    assert len(conv.messages) == 1
    assert ("hidden_message", 1) in conv.omitted_reasons
    assert ("nonfinal_assistant_message", 1) in conv.omitted_reasons


def test_exact_rendered_text_hash_not_parts_encoding(tmp_path):
    data = conversation()
    data["mapping"]["last"]["message"]["content"]["parts"] = ["a", "b"]
    first = run(archive(tmp_path, [data])).conversations[0]
    data["mapping"]["last"]["message"]["content"]["parts"] = ["a\nb"]
    second = run(archive(tmp_path, [data])).conversations[0]
    assert first.messages[-1].text == second.messages[-1].text
    assert first.messages[-1].text_sha256 == second.messages[-1].text_sha256
    assert first.content_sha256 == second.content_sha256
    assert first.source_revision_sha256 != second.source_revision_sha256


@pytest.mark.parametrize("change,code", [
    (lambda c: c.update(current_node="unknown"), "selected_branch_missing_node"),
    (lambda c: c["mapping"]["second"].update(parent="last"), "selected_branch_cycle"),
    (lambda c: c["mapping"]["first"].update(children=[]), "nonreciprocal_parent_edge"),
    (lambda c: c["mapping"]["last"].update(parent=[]), "invalid_branch_parent"),
    (lambda c: c["mapping"]["last"].update(id="other"), "node_id_mismatch"),
    (lambda c: c["mapping"]["last"].update(message="unexpected"), "invalid_selected_message"),
    (lambda c: c["mapping"]["last"]["message"]["author"].update(role="arbitrary"), "unknown_author_role"),
    (lambda c: c["mapping"]["last"]["message"].update(id="m2"), "duplicate_selected_message_id"),
    (lambda c: c.update(id=None), "invalid_conversation_id"),
    (lambda c: c["mapping"]["last"]["message"].update(create_time=SECRET), "unsupported_timestamp"),
    (lambda c: c["mapping"]["last"]["message"].update(metadata={"is_visually_hidden_from_conversation": SECRET}), "invalid_visibility_metadata"),
])
def test_branch_and_schema_errors_are_fixed_codes(tmp_path, change, code):
    data = conversation()
    change(data)
    with pytest.raises(NormalizeError, match=code) as exc:
        run(archive(tmp_path, [data]))
    assert SECRET not in str(exc.value)


def test_unhashable_content_type_rejected_without_crashing(tmp_path):
    data = conversation()
    data["mapping"]["last"]["message"]["content"]["content_type"] = {"untrusted": SECRET}
    conv = run(archive(tmp_path, [data])).conversations[0]
    assert ("unsupported_content", 1) in conv.omitted_reasons


def test_duplicate_conversation_rejected(tmp_path):
    with pytest.raises(NormalizeError, match="duplicate_conversation_id"):
        run(archive(tmp_path, [conversation(), conversation()]))


def test_invalid_json_zip_security_and_limits(tmp_path):
    path = archive(tmp_path, [conversation()], [("../evil.json", SECRET)])
    with pytest.raises(NormalizeError, match="unsafe_zip_member"):
        run(path)
    path = archive(tmp_path, [conversation()])
    with pytest.raises(NormalizeError, match="input_size_limit"):
        run(path, limits=replace(Limits(), archive_bytes=1))
    with pytest.raises(NormalizeError, match="conversation_limit"):
        run(archive(tmp_path, [conversation("a"), conversation("b")]), limits=replace(Limits(), conversations=1))
    with pytest.raises(NormalizeError, match="node_limit"):
        run(archive(tmp_path, [conversation()]), limits=replace(Limits(), nodes=1))


def test_real_json_mode_and_multi_member_selection(tmp_path):
    path = tmp_path / "private.json"
    path.write_text(json.dumps([conversation()]), encoding="utf-8")
    result = run(path, input_format="json")
    assert len(result.conversations) == 1
    assert result.selected_json_members == 1
    with pytest.raises(NormalizeError, match="invalid_input_options"):
        run(path, input_format="json", members=["something.json"])
    path = archive(tmp_path, [conversation("a")], [("conversations_2.json", json.dumps([conversation("b")]))])
    with pytest.raises(NormalizeError, match="ambiguous_member_selection"):
        run(path)
    result = run(path, members=["conversations_2.json"])
    assert result.selected_json_members == 1
    assert [c.conversation_id for c in result.conversations] == ["b"]


def test_no_unsupported_input_root_assumptions(tmp_path):
    with pytest.raises(NormalizeError, match="unsupported_conversations_root"):
        run(archive(tmp_path, {"unknown_root": SECRET}))
    with pytest.raises(NormalizeError, match="invalid_source_scope"):
        normalize_export(archive(tmp_path, [conversation()]), scope="../unsafe")


def test_symlink_and_fifo_never_followed(tmp_path):
    path = archive(tmp_path, [conversation()])
    link = tmp_path / "input-link.zip"
    link.symlink_to(path)
    with pytest.raises(NormalizeError):
        run(link)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(NormalizeError, match="input_not_regular"):
        run(fifo)


def test_error_display_excludes_untrusted_value_and_path(tmp_path):
    path = archive(tmp_path, [conversation()])
    raw = bytearray(path.read_bytes())
    path.write_bytes(raw[:20])  # bad directory
    with pytest.raises(NormalizeError) as exc:
        run(path)
    assert SECRET not in str(exc.value)
    assert str(path) not in str(exc.value)


def test_real_export_style_mapping_parent_only_and_unverified_edges(tmp_path):
    data = conversation()
    for node in data["mapping"].values():
        del node["children"]
    result = run(archive(tmp_path, [data]))
    conv = result.conversations[0]
    assert [m.text for m in conv.messages] == ["question", "answer", SECRET]
    assert conv.unverified_child_links == 3
    assert result.graph_reciprocity_unverified is True
    assert conv.selected_path_nodes == 4
    assert result.real_schema_accepted is False


def test_empty_export_slot_is_counted_quarantined_not_skipped_silently(tmp_path):
    result = run(archive(tmp_path, [{}, conversation()]))
    assert len(result.conversations) == 1
    assert result.quarantined_conversation_slots == 1
    assert result.quarantine_reasons == (("empty_conversation_slot", 1),)
    assert result.real_schema_accepted is False
    with pytest.raises(NormalizeError, match="conversation_limit"):
        run(archive(tmp_path, [{}, {}, conversation()]), limits=replace(Limits(), conversations=2))


def test_unsupported_nonempty_conversation_still_fails_closed(tmp_path):
    with pytest.raises(NormalizeError, match="invalid_conversation_id"):
        run(archive(tmp_path, [{"mapping": {"root": {}}}, conversation()]))


def test_thoughts_and_reasoning_recap_omitted_from_semantic_hash(tmp_path):
    data = conversation()
    data["mapping"]["second"]["message"]["content"] = {
        "content_type": "thoughts", "thoughts": [{"private": SECRET}],
    }
    first = run(archive(tmp_path, [data])).conversations[0]
    assert [m.text for m in first.messages] == ["question", SECRET]
    assert ("internal_reasoning", 1) in first.omitted_reasons
    data["mapping"]["second"]["message"]["content"] = {
        "content_type": "reasoning_recap", "content": SECRET,
    }
    second = run(archive(tmp_path, [data])).conversations[0]
    assert second.content_sha256 == first.content_sha256
    assert second.source_revision_sha256 != first.source_revision_sha256
    assert ("internal_reasoning", 1) in second.omitted_reasons
    assert SECRET not in repr(second)


def test_opaque_node_and_message_identity_not_in_semantic_content_hash(tmp_path):
    data = conversation()
    original = run(archive(tmp_path, [data])).conversations[0]
    mapping = data["mapping"]
    renamed = {"root": "root-v2", "first": "first-v2",
               "second": "second-v2", "last": "last-v2", "alt": "alt-v2"}
    updated = {}
    for old_id, node in mapping.items():
        node = deepcopy(node)
        node["id"] = renamed[old_id]
        node["parent"] = renamed.get(node["parent"])
        node["children"] = [renamed[c] for c in node["children"]]
        if node["message"] is not None:
            node["message"]["id"] += "-new"
        updated[renamed[old_id]] = node
    data["mapping"] = updated
    data["current_node"] = "last-v2"
    newer = run(archive(tmp_path, [data])).conversations[0]
    assert newer.content_sha256 == original.content_sha256
    assert newer.source_revision_sha256 != original.source_revision_sha256


def test_conflicting_conversation_ids_rejected(tmp_path):
    data = conversation()
    data["conversation_id"] = "different"
    with pytest.raises(NormalizeError, match="conversation_id_mismatch"):
        run(archive(tmp_path, [data]))


def test_missing_children_not_a_license_for_missing_parent(tmp_path):
    data = conversation()
    for node in data["mapping"].values():
        del node["children"]
    data["mapping"]["first"].pop("parent")
    with pytest.raises(NormalizeError, match="missing_branch_parent"):
        run(archive(tmp_path, [data]))


def test_invalid_member_selection_returns_fixed_code_before_archive_access(tmp_path):
    path = archive(tmp_path, [conversation()])
    for selected in ([123], (None,), {"conversations.json": 1}, "conversations.json"):
        with pytest.raises(NormalizeError, match="invalid_member_selection") as err:
            run(path, members=selected)
        assert SECRET not in str(err.value)


def test_inconsistent_internal_selection_fails_closed_without_assert(tmp_path, monkeypatch):
    from obsidian_automation import chatgpt_export_normalize as module

    payload = conversation()
    original = module._select_message

    def broken(node_id, node):
        if node_id == "first":
            return None, ""  # Impossible state; simulate future parser regression
        return original(node_id, node)

    monkeypatch.setattr(module, "_select_message", broken)
    with pytest.raises(NormalizeError, match="selected_message_invariant"):
        run(archive(tmp_path, [payload]))


@pytest.mark.parametrize("reasoning_type", ["thoughts", "reasoning_recap"])
def test_user_assigned_internal_reasoning_type_is_not_silently_omitted(tmp_path, reasoning_type):
    data = conversation()
    data["mapping"]["first"]["message"]["content"] = {
        "content_type": reasoning_type, "content": SECRET,
    }
    normalized = run(archive(tmp_path, [data])).conversations[0]
    assert [m.text for m in normalized.messages] == ["answer", SECRET]
    assert ("unsupported_content", 1) in normalized.omitted_reasons
    assert ("internal_reasoning", 1) not in normalized.omitted_reasons
    assert SECRET not in repr(normalized)
