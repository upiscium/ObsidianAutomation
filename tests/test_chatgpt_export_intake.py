"""Offline, synthetic-only tests for the private ChatGPT Export intake journal."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import zipfile

import pytest

from obsidian_automation.chatgpt_export_intake import (
    IntakeError, inventory_ready, main, publish_archive,
)

SECRET = "PRIVATE_SENTINEL_NEVER_JOURNAL"
SOURCE_ID = "SECRET_CONVERSATION_ID_NEVER_JOURNAL"


def conversation(*, identity: str = SOURCE_ID, text: str = "First response", title: str = SECRET,
                 parent_only: bool = True):
    nodes = {
        "root": {"id": "root", "parent": None, "message": None},
        "one": {"id": "one", "parent": "root",
                "message": {
                    "id": identity + "-1", "author": {"role": "user"},
                    "content": {"content_type": "text", "parts": ["Question"]},
                }},
        "two": {"id": "two", "parent": "one",
                "message": {
                    "id": identity + "-2", "author": {"role": "assistant"},
                    "content": {"content_type": "text", "parts": [text]},
                }},
        "reason": {"id": "reason", "parent": "one",
                   "message": {
                       "id": identity + "-thought", "author": {"role": "assistant"},
                       "content": {"content_type": "thoughts", "thoughts": [SECRET]},
                   }},
    }
    if not parent_only:
        nodes["root"]["children"] = ["one"]
        nodes["one"]["children"] = ["two", "reason"]
        nodes["two"]["children"] = []
        nodes["reason"]["children"] = []
    return {"id": identity, "title": title, "current_node": "two", "mapping": nodes}


def export(path: Path, conversations: list, *, comment: bytes = b"") -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as zip:
        zip.writestr("conversations.json", json.dumps(conversations, ensure_ascii=False))
        zip.comment = comment
    os.chmod(path, 0o600)
    return path


def db_connect(root: Path):
    return sqlite3.connect(root / "state" / "journal.sqlite3")


def count(root: Path, table: str) -> int:
    with db_connect(root) as db:
        return db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]


def test_publish_is_private_atomic_and_idempotent(tmp_path):
    source = export(tmp_path / "private.zip", [conversation(), {}])
    before = source.read_bytes()
    root = tmp_path / "isolated"
    receipt = publish_archive(root, source)
    assert receipt.status == "published"
    assert receipt.bytes == len(before)
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "ready").stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "state" / "journal.sqlite3").stat().st_mode) == 0o600
    ready = root / "ready" / (receipt.archive_sha256 + ".zip")
    assert stat.S_IMODE(ready.stat().st_mode) == 0o600
    assert ready.read_bytes() == before
    assert source.read_bytes() == before
    assert ready.stat().st_nlink == 1
    assert list((root / "staging").iterdir()) == []
    duplicate = publish_archive(root, source)
    assert duplicate.status == "existing"
    assert count(root, "archives") == 1


def test_preview_and_commit_include_review_required_but_no_generation(tmp_path):
    source = export(tmp_path / "private.zip", [conversation(), {}])
    root = tmp_path / "private"
    publish_archive(root, source)
    prev, = inventory_ready(root, scope="chatgpt-test")
    assert prev.status == "inventory_review_required"
    assert (prev.added, prev.quarantined_slots, prev.retained_messages) == (1, 1, 2)
    assert prev.unverified_child_links == 2
    assert prev.eligible_for_generation is False
    assert count(root, "runs") == 0
    receipt, = inventory_ready(root, scope="chatgpt-test", commit=True)
    assert receipt == prev
    assert count(root, "runs") == 1
    assert count(root, "latest") == 1
    assert count(root, "revisions") == 1
    assert count(root, "run_items") == 1
    with db_connect(root) as db:
        status = db.execute("SELECT status FROM runs").fetchone()[0]
        assert status == "inventory_review_required"
        key = db.execute("SELECT identity_hmac FROM latest").fetchone()[0]
        assert len(key) == 64
    raw_sqlite = (root / "state" / "journal.sqlite3").read_bytes()
    assert SECRET.encode() not in raw_sqlite
    assert SOURCE_ID.encode() not in raw_sqlite
    assert b"First response" not in raw_sqlite


def test_once_processed_same_export_is_not_submitted_again(tmp_path):
    source = export(tmp_path / "in.zip", [conversation()])
    root = tmp_path / "private"
    publish_archive(root, source)
    one, = inventory_ready(root, scope="scope-a", commit=True)
    assert one.added == 1
    # A completed run is not re-submitted to downstream jobs.
    assert inventory_ready(root, scope="scope-a", commit=True) == ()
    assert count(root, "runs") == 1
    two, = inventory_ready(root, scope="scope-b", commit=True)
    assert two.added == 1
    assert count(root, "runs") == 2
    assert count(root, "latest") == 2


def test_archive_publication_order_not_sha_sorting(tmp_path):
    source = tmp_path / "in.zip"
    root = tmp_path / "private"
    a = publish_archive(root, export(source, [conversation(text="A")]))
    first, = inventory_ready(root, scope="scope", commit=True)
    assert first.added == 1
    b = publish_archive(root, export(source, [conversation(text="B")]))
    assert a.archive_sha256 != b.archive_sha256
    second, = inventory_ready(root, scope="scope", commit=True)
    assert (second.changed, second.added) == (1, 0)
    with db_connect(root) as db:
        seqs = db.execute("SELECT archive_sha FROM archives ORDER BY seq").fetchall()
        assert seqs == [(a.archive_sha256,), (b.archive_sha256,)]


def test_metadata_revision_only_no_model_regeneration(tmp_path):
    source = tmp_path / "in.zip"
    root = tmp_path / "private"
    publish_archive(root, export(source, [conversation(title="original")]))
    inventory_ready(root, scope="scope", commit=True)
    publish_archive(root, export(source, [conversation(title="updated")]))
    second, = inventory_ready(root, scope="scope", commit=True)
    assert (second.added, second.changed, second.unchanged, second.revision_only) == (0, 0, 0, 1)
    assert count(root, "revisions") == 2
    assert count(root, "latest") == 1


def test_added_changed_absent_not_deleted(tmp_path):
    source = tmp_path / "in.zip"
    root = tmp_path / "private"
    publish_archive(root, export(source, [
        conversation(identity="A", text="v1"),
        conversation(identity="B", text="same"),
    ]))
    inventory_ready(root, scope="scope", commit=True)
    publish_archive(root, export(source, [
        conversation(identity="A", text="v2"),
        conversation(identity="C", text="new"),
    ]))
    result, = inventory_ready(root, scope="scope", commit=True)
    assert (result.added, result.changed, result.absent_not_deleted) == (1, 1, 1)
    assert count(root, "latest") == 3  # B retained, never DELETE


def test_incomplete_publication_is_retryable(tmp_path, monkeypatch):
    source = export(tmp_path / "in.zip", [conversation()])
    root = tmp_path / "private"
    from obsidian_automation import chatgpt_export_intake as module
    real_link = module.os.link

    def fail_once(*args, **kwargs):
        raise OSError("PRIVATE_TEMP_PATH_MUST_NOT_LEAK")
    monkeypatch.setattr(module.os, "link", fail_once)
    with pytest.raises(IntakeError, match="archive_publish_failed") as exc:
        publish_archive(root, source)
    assert "PRIVATE_TEMP_PATH" not in str(exc.value)
    assert count(root, "archives") == 1
    with db_connect(root) as db:
        assert db.execute("SELECT status FROM archives").fetchone()[0] == "publishing"
    assert list((root / "staging").iterdir()) == []
    monkeypatch.setattr(module.os, "link", real_link)
    assert publish_archive(root, source).status == "published"
    result, = inventory_ready(root, scope="scope", commit=True)
    assert result.added == 1
    assert count(root, "archives") == 1


def test_bad_archive_never_published_or_advanced(tmp_path):
    source = tmp_path / "bad.zip"
    source.write_bytes(b"not a zip")
    os.chmod(source, 0o600)
    root = tmp_path / "private"
    with pytest.raises(IntakeError, match="export_structure_rejected"):
        publish_archive(root, source)
    assert not (root / "state" / "journal.sqlite3").exists()
    assert list((root / "ready").iterdir()) == []
    assert list((root / "staging").iterdir()) == []


def test_reject_symlink_private_root_and_symlink_archive(tmp_path):
    root_real = tmp_path / "private"
    root_real.mkdir(mode=0o700)
    root_link = tmp_path / "private-link"
    root_link.symlink_to(root_real, target_is_directory=True)
    source = export(tmp_path / "in.zip", [conversation()])
    with pytest.raises(IntakeError, match="private_ancestor_symlink"):
        publish_archive(root_link, source)
    link = tmp_path / "source-link.zip"
    link.symlink_to(source)
    with pytest.raises(IntakeError, match="unreadable_export_source"):
        publish_archive(root_real, link)


def test_reject_world_readable_private_root_and_corrupt_ready(tmp_path):
    source = export(tmp_path / "in.zip", [conversation()])
    root = tmp_path / "private"
    receipt = publish_archive(root, source)
    root.chmod(0o755)
    with pytest.raises(IntakeError, match="private_permissions"):
        inventory_ready(root, scope="scope")
    root.chmod(0o700)
    ready = root / "ready" / (receipt.archive_sha256 + ".zip")
    ready.write_bytes(b"tampered bytes")
    with pytest.raises(IntakeError, match="ready_archive_mismatch"):
        inventory_ready(root, scope="scope")


def test_cli_fixed_errors_and_value_free_outputs(tmp_path, capsys):
    source = export(tmp_path / "PRIVATENAME.zip", [conversation()])
    root = tmp_path / "private"
    assert main(["publish", "--root", str(root), "--source", str(source)]) == 0
    assert SECRET not in capsys.readouterr().out
    assert main(["inventory", "--root", str(root), "--scope", "test-scope"]) == 0
    output = capsys.readouterr().out
    assert "generation_jobs_queued" in output
    assert SOURCE_ID not in output
    assert str(source) not in output
    assert main(["inventory", "--root", str(root), "--scope", "../unsafe"]) == 2
    error = capsys.readouterr().out
    assert json.loads(error)["error_code"] == "invalid_scope"
    assert main(["--SENSITIVE_BAD_FLAG_NAME", "yes"]) == 2
    assert "SENSITIVE_BAD_FLAG_NAME" not in capsys.readouterr().out


def test_parallel_publish_is_serialized_by_lock(tmp_path):
    source = export(tmp_path / "in.zip", [conversation()])
    root = tmp_path / "private"
    with ThreadPoolExecutor(max_workers=4) as executor:
        receipts = list(executor.map(lambda _index: publish_archive(root, source), range(4)))
    assert sum(r.status == "published" for r in receipts) == 1
    assert count(root, "archives") == 1
    assert len(list((root / "ready").iterdir())) == 1


def test_no_advance_on_inventory_failure(tmp_path, monkeypatch):
    source = export(tmp_path / "in.zip", [conversation()])
    root = tmp_path / "private"
    publish_archive(root, source)
    from obsidian_automation import chatgpt_export_intake as module
    original = module._inventory

    def fake_fail(*args, **kwargs):
        raise IntakeError("injected_test_failure")
    monkeypatch.setattr(module, "_inventory", fake_fail)
    with pytest.raises(IntakeError, match="injected_test_failure"):
        inventory_ready(root, scope="scope", commit=True)
    assert count(root, "runs") == 0
    monkeypatch.setattr(module, "_inventory", original)
    assert inventory_ready(root, scope="scope", commit=True)[0].added == 1


def test_id_hmac_not_raw_id_and_versioned_scope(tmp_path):
    source = export(tmp_path / "in.zip", [conversation(identity=SOURCE_ID)])
    root = tmp_path / "private"
    publish_archive(root, source)
    inventory_ready(root, scope="one", commit=True)
    inventory_ready(root, scope="two", commit=True)
    with db_connect(root) as db:
        rows = db.execute("SELECT identity_hmac FROM latest ORDER BY scope").fetchall()
        assert rows[0][0] != rows[1][0]
        assert SOURCE_ID not in rows[0][0]
    assert (root / "state" / "identity-salt.bin").stat().st_size == 32


def test_strict_schema_rejection_is_journalled_then_next_archive_runs(tmp_path):
    source = tmp_path / "source.zip"
    root = tmp_path / "private"
    bad = conversation()
    bad["current_node"] = "nonexistent-private-node"
    publish_archive(root, export(source, [bad]))
    preview, = inventory_ready(root, scope="test")
    assert preview.status == "normalization_rejected"
    assert count(root, "rejections") == 0
    failed, = inventory_ready(root, scope="test", commit=True)
    assert failed.status == "normalization_rejected"
    assert count(root, "rejections") == 1
    assert count(root, "runs") == 0
    assert count(root, "latest") == 0
    with db_connect(root) as db:
        code = db.execute("SELECT error_code FROM rejections").fetchone()[0]
    assert code == "selected_branch_missing_node"
    assert SECRET not in code
    assert inventory_ready(root, scope="test", commit=True) == ()
    publish_archive(root, export(source, [conversation()]))
    good, = inventory_ready(root, scope="test", commit=True)
    assert good.added == 1
    assert count(root, "runs") == 1


def test_selector_change_cannot_roll_back_latest_to_older_export(tmp_path):
    source = tmp_path / "source.zip"
    root = tmp_path / "private"
    publish_archive(root, export(source, [conversation(text="older")]))
    inventory_ready(root, scope="scope", commit=True)
    publish_archive(root, export(source, [conversation(text="newer")]))
    inventory_ready(root, scope="scope", commit=True)
    with pytest.raises(IntakeError, match="out_of_order_selection_requires_new_scope"):
        inventory_ready(root, scope="scope", members=["conversations.json"], commit=True)
    assert count(root, "runs") == 2
    assert count(root, "latest") == 1


def test_multi_archive_preview_without_commit_is_disallowed(tmp_path):
    source = export(tmp_path / "source.zip", [conversation()])
    root = tmp_path / "private"
    publish_archive(root, source)
    with pytest.raises(IntakeError, match="multi_archive_preview_unsupported"):
        inventory_ready(root, scope="scope", commit=False, max_archives=2)


def test_parallel_inventory_commits_only_one_run(tmp_path):
    source = export(tmp_path / "source.zip", [conversation()])
    root = tmp_path / "private"
    publish_archive(root, source)
    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(
            lambda _index: inventory_ready(root, scope="scope", commit=True),
            range(3),
        ))
    assert sum(len(x) for x in results) == 1
    assert count(root, "runs") == 1
    assert count(root, "latest") == 1


def test_invalid_zip_members_refused_before_ready_publish(tmp_path):
    source = tmp_path / "source.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("../private.json", json.dumps([conversation()]))
        archive.writestr("conversations.json", json.dumps([conversation()]))
    os.chmod(source, 0o600)
    root = tmp_path / "private"
    with pytest.raises(IntakeError, match="export_structure_rejected"):
        publish_archive(root, source)
    assert not list((root / "ready").iterdir())
    assert not list((root / "staging").iterdir())


def test_archive_hardlink_source_is_refused(tmp_path):
    source = export(tmp_path / "source.zip", [conversation()])
    duplicate = tmp_path / "hard-link.zip"
    os.link(source, duplicate)
    with pytest.raises(IntakeError, match="unsafe_export_source"):
        publish_archive(tmp_path / "private", source)


def test_empty_conversation_export_never_reports_inventory_only(tmp_path):
    source = export(tmp_path / "source.zip", [])
    root = tmp_path / "private"
    publish_archive(root, source)
    result, = inventory_ready(root, scope="scope", commit=True)
    assert result.status == "inventory_review_required"
    assert result.added == 0
    assert result.retained_messages == 0
    assert result.eligible_for_generation is False
    assert count(root, "runs") == 1
