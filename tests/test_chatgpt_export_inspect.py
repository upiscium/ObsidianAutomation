from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import stat
import struct
import zipfile

import pytest

from obsidian_automation.chatgpt_export_inspect import InspectionError, Limits, inspect_export, main

SECRET = "PRIVATE_SENTINEL_DO_NOT_OUTPUT"


def conversation():
    message = {"id": SECRET, "author": {"role": "user", "name": SECRET},
               "create_time": 12345, "content": {"content_type": "text", "parts": [SECRET]},
               "metadata": {SECRET: SECRET}, SECRET: SECRET}
    return {"id": SECRET, "title": SECRET, "current_node": "leaf",
            "mapping": {"root": {"parent": None, "children": ["leaf", "alternate"], "message": None},
                        "leaf": {"parent": "root", "children": [], "message": message},
                        "alternate": {"parent": "root", "children": [], "message": None}}, SECRET: SECRET}


def archive(tmp_path, root=None, *, name="conversations.json", others=()):
    path = tmp_path / (SECRET + ".zip")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(name, json.dumps([conversation()] if root is None else root))
        for n, data in others:
            z.writestr(n, data)
    return path


def test_structure_only_and_read_only(tmp_path):
    path = archive(tmp_path, others=[("account.json", SECRET), ("image.png", b"private")])
    before = path.read_bytes()
    listing = sorted(tmp_path.iterdir())
    report = inspect_export(path)
    assert report["status"] == "inspected"
    assert report["inspection_only"] is True
    assert report["import_compatibility_established"] is False
    assert report["counts"]["messages"] == 1
    assert report["counts"]["branching_nodes"] == 1
    assert report["selected_parent_chains"] == {"resolved": 1}
    assert report["known_field_types"]["conversation"]["title"] == {"string": 1}
    assert report["selection"]["unselected_json_members"] == 1
    text = json.dumps(report)
    for private in (SECRET, "account.json", "image.png", "12345", "alternate", "leaf", "root"):
        assert '"' + private + '"' not in text
    assert SECRET not in text
    assert before == path.read_bytes()
    assert listing == sorted(tmp_path.iterdir())
    assert inspect_export(path) == report


def test_cli_and_errors_do_not_echo_private_values(tmp_path, capsys):
    path = archive(tmp_path)
    assert main([str(path)]) == 0
    assert SECRET not in capsys.readouterr().out
    assert main([str(tmp_path / SECRET)]) == 2
    assert SECRET not in capsys.readouterr().out
    assert main(["--" + SECRET]) == 2
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err
    assert "Traceback" not in captured.out + captured.err


@pytest.mark.parametrize("root", [{SECRET: SECRET}, [SECRET], [{"mapping": SECRET}], []])
def test_unknown_structure_not_success(tmp_path, root, capsys):
    path = archive(tmp_path, root)
    assert main([str(path)]) == 3
    assert SECRET not in capsys.readouterr().out


@pytest.mark.parametrize("change,expected", [
    (lambda c: c.update(current_node=None), "missing_current_node"),
    (lambda c: c["mapping"]["leaf"].update(parent="leaf"), "cycle"),
    (lambda c: c["mapping"]["leaf"].update(parent="absent"), "missing_parent"),
    (lambda c: c["mapping"]["leaf"].update(parent=[]), "invalid_parent"),
    (lambda c: c["mapping"].update(leaf="bad"), "invalid_node"),
])
def test_selected_branch_is_not_timestamp_sorted_or_guessed(tmp_path, change, expected):
    conv = conversation()
    change(conv)
    report = inspect_export(archive(tmp_path, [conv]))
    assert report["selected_parent_chains"] == {expected: 1}
    assert report["status"] == "schema_review_required"


def test_unknown_enum_values_are_bucketed_not_printed(tmp_path):
    conv = conversation()
    msg = conv["mapping"]["leaf"]["message"]
    msg["author"]["role"] = SECRET
    msg["content"]["content_type"] = {SECRET: SECRET}
    report = inspect_export(archive(tmp_path, [conv]))
    assert report["status"] == "schema_review_required"
    assert report["roles"] == {"other": 1}
    assert report["content_types"] == {"other": 1}
    assert SECRET not in json.dumps(report)


def test_numbered_candidates_and_explicit_selection(tmp_path):
    path = archive(tmp_path, name="conversations-001.json", others=[("conversations-002.json", "[]")])
    assert inspect_export(path)["selection"]["selected_json_members"] == 2
    path = archive(tmp_path, name="bundle/custom.json")
    with pytest.raises(InspectionError, match="selection_required"):
        inspect_export(path)
    assert inspect_export(path, members=["bundle/custom.json"])["selection"]["mode"] == "explicit_members"
    with pytest.raises(InspectionError, match="missing"):
        inspect_export(path, members=["absent.json"])


def test_mixed_single_and_numbered_requires_explicit_selection(tmp_path):
    path = archive(tmp_path, others=[("conversations_1.json", "[]")])
    with pytest.raises(InspectionError, match="ambiguous"):
        inspect_export(path)


@pytest.mark.parametrize("name", ["../bad.json", "/bad", "C:/bad", "a\\b", "a//b", "a/./b", "bad\x01"])
def test_unsafe_member_even_if_unselected(tmp_path, name):
    path = archive(tmp_path, others=[(name, "private")])
    with pytest.raises(InspectionError, match="unsafe"):
        inspect_export(path)


def test_symlink_and_duplicate_entries(tmp_path):
    path = archive(tmp_path)
    with zipfile.ZipFile(path, "a") as z:
        info = zipfile.ZipInfo("link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        z.writestr(info, "/private")
    with pytest.raises(InspectionError, match="nonregular"):
        inspect_export(path)
    path = archive(tmp_path, others=[("CONVERSATIONS.JSON", "[]")])
    with pytest.raises(InspectionError, match="duplicate_zip"):
        inspect_export(path)


@pytest.mark.parametrize("data,code", [(b'{"x":1,"x":2}', "duplicate_json"),
    (b"[NaN]", "nonfinite"), (b"[1e9999]", "nonfinite"), (b"[", "invalid"), (b"\xff", "invalid"),
    (b"[" * 2000, "invalid_or_overdeep")])
def test_invalid_json_is_fail_closed(tmp_path, data, code):
    path = tmp_path / "input.json"
    path.write_bytes(data)
    with pytest.raises(InspectionError, match=code):
        inspect_export(path, input_format="json")


def test_raw_json_and_limits(tmp_path):
    path = tmp_path / "input.json"
    path.write_text(json.dumps([conversation()]))
    assert inspect_export(path, input_format="json")["status"] == "inspected"
    with pytest.raises(InspectionError, match="input_size"):
        inspect_export(path, input_format="json", limits=replace(Limits(), member_bytes=1))
    path = archive(tmp_path, [conversation(), conversation()])
    for name, value in [("conversations", 1), ("nodes", 1), ("member_bytes", 1),
                        ("directory_bytes", 1), ("total_json_bytes", 1), ("archive_bytes", 1)]:
        with pytest.raises(InspectionError, match="limit"):
            inspect_export(path, limits=replace(Limits(), **{name: value}))


def test_ratio_limit(tmp_path):
    path = tmp_path / "bomb.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("conversations.json", '"' + "a" * 10000 + '"')
    with pytest.raises(InspectionError, match="ratio"):
        inspect_export(path)


def test_directory_preflight_and_corrupt_zip(tmp_path):
    path = archive(tmp_path)
    data = bytearray(path.read_bytes())
    pos = data.rfind(b"PK\x05\x06")
    struct.pack_into("<H", data, pos + 8, 65535)
    struct.pack_into("<H", data, pos + 10, 65535)
    path.write_bytes(data)
    with pytest.raises(InspectionError, match="zip64"):
        inspect_export(path)
    path.write_bytes(SECRET.encode())
    with pytest.raises(InspectionError, match="invalid_zip"):
        inspect_export(path)


def test_input_symlink_and_fifo_not_followed(tmp_path):
    import os
    path = archive(tmp_path)
    link = tmp_path / "link.zip"
    link.symlink_to(path)
    with pytest.raises(InspectionError):
        inspect_export(link)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(InspectionError, match="regular"):
        inspect_export(fifo)


def test_changed_input_detected(tmp_path, monkeypatch):
    import obsidian_automation.chatgpt_export_inspect as module
    path = archive(tmp_path)
    original = module._Profile.consume
    def mutate(self, root):
        original(self, root)
        with path.open("ab") as f:
            f.write(b"changed")
    monkeypatch.setattr(module._Profile, "consume", mutate)
    with pytest.raises(InspectionError, match="changed"):
        inspect_export(path)


def test_zip64_locator_rejected_even_without_sentinel_fields(tmp_path):
    path = archive(tmp_path)
    data = path.read_bytes()
    pos = data.rfind(b"PK\x05\x06")
    locator = b"PK\x06\x07" + b"\0" * 16
    path.write_bytes(data[:pos] + locator + data[pos:])
    with pytest.raises(InspectionError, match="zip64"):
        inspect_export(path)
