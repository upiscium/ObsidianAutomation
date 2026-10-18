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


def test_title_update_no_generation_change(tmp_path):
    data = conversation()
    first = run(archive(tmp_path, [data]))
    data["title"] = "new title"
    data["update_time"] = 983223.0
    second = run(archive(tmp_path, [data]))
    assert inventory_delta(first, second).unchanged == 1
    assert first.conversations[0].source_revision_sha256 == second.conversations[0].source_revision_sha256


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


def test_message_parts_delimiters_distinct(tmp_path):
    data = conversation()
    data["mapping"]["last"]["message"]["content"]["parts"] = ["a", "b"]
    a = run(archive(tmp_path, [data])).conversations[0].messages[-1].text_sha256
    data["mapping"]["last"]["message"]["content"]["parts"] = ["a\nb"]
    b = run(archive(tmp_path, [data])).conversations[0].messages[-1].text_sha256
    assert a != b


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
