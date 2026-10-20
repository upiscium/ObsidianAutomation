"""Private Source Reader acceptance; synthetic records ONLY, no model or Vault."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import zipfile

import pytest

from obsidian_automation.chatgpt_export_intake import (
    inventory_ready,
    publish_archive,
)
from obsidian_automation.chatgpt_export_reader import (
    READER_VERSION,
    PrivateSourceReaderError,
    read_admitted_source_page,
)
from obsidian_automation.chatgpt_export_source import (
    SourceCursor,
    validate_source_context_page,
)

SECRET = "PRIVATE_SENTINEL_NO_PUBLIC_VALUES"
SCOPE = "synthetic-only"
CONV_ID = "private-synthetic-conversation-ID"


def conversation(
    *,
    identity=CONV_ID,
    text="A synthetic confirmed possibility.",
    children=True,
    omit_user=False,
    title=SECRET,
):
    base = {
        "root": {"id": "root", "parent": None, "message": None},
        "user": {
            "id": "user",
            "parent": "root",
            "message": {
                "id": "synthetic_user_message",
                "author": {"role": "user"},
                "content": {
                    "content_type": "text",
                    "parts": ["A future plan, not approved."],
                },
            },
        },
        "assistant": {
            "id": "assistant",
            "parent": "user",
            "message": {
                "id": "synthetic_assistant_message",
                "author": {"role": "assistant"},
                "content": {"content_type": "text", "parts": [text]},
            },
        },
    }
    if children:
        base["root"]["children"] = ["user"]
        base["user"]["children"] = ["assistant"]
        base["assistant"]["children"] = []
    if omit_user:
        del base["user"]
        del base["assistant"]
        base["root"]["children"] = []
    return {
        "id": identity,
        "title": title,
        "mapping": base,
        "current_node": "root" if omit_user else "assistant",
    }


def archive(path, records):
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as target:
        target.writestr("conversations.json", json.dumps(records, ensure_ascii=False))
    os.chmod(path, 0o600)
    return path


def _latest(root, scope=SCOPE):
    with sqlite3.connect(root / "state" / "journal.sqlite3") as conn:
        rows = conn.execute(
            "SELECT identity_hmac,content_sha,revision_sha FROM latest WHERE scope=?",
            (scope,),
        ).fetchall()
    assert len(rows) == 1
    return rows[0]


def admitted(root, *, scope=SCOPE, expected=None, **kwargs):
    digest, content, revision = _latest(root, scope)
    if expected is not None:
        content, revision = expected
    return read_admitted_source_page(
        root,
        scope=scope,
        source_hmac=digest,
        expected_content_sha256=content,
        expected_revision_sha256=revision,
        **kwargs,
    )


def _initialize(tmp_path, *conversations):
    root = tmp_path / "private-root"
    original = archive(tmp_path / "synthetic.zip", list(conversations))
    publish_archive(root, original)
    inventory_ready(root, scope=SCOPE, commit=True)
    return root, original


def test_source_reader_is_private_and_read_only(tmp_path):
    root, original = _initialize(tmp_path, conversation())
    original_contents = original.read_bytes()
    db = root / "state" / "journal.sqlite3"
    state_mtime = db.stat().st_mtime_ns
    ready_files = set((root / "ready").iterdir())
    rec = admitted(root)
    assert rec.reader_version == READER_VERSION
    assert rec.inventory_status == "inventory_only"
    assert rec.source_hmac == _latest(root)[0]
    assert [s.role for unit in rec.page.units for s in unit.evidence] == [
        "user",
        "assistant",
    ]
    assert rec.page.complete
    assert not rec.generation_authorized
    assert not rec.approved_for_human_review
    assert rec.page.generation_authorized is False
    validate_source_context_page(
        rec.page,
        rec.conversation,
        scope=SCOPE,
        source_hmac=rec.source_hmac,
    )
    assert SECRET not in repr(rec)
    assert CONV_ID not in repr(rec)
    assert original.read_bytes() == original_contents
    assert db.stat().st_mtime_ns == state_mtime
    assert set((root / "ready").iterdir()) == ready_files
    assert stat.S_IMODE(root.stat().st_mode) == 0o700


def test_unicode_pagination_bound_exactly(tmp_path):
    root, _ = _initialize(tmp_path, conversation(text="日本語🎉" * 65))
    cursor = SourceCursor(1, 0)
    all_text = ""
    for _ in range(150):
        rec = admitted(
            root,
            cursor=cursor,
            max_units=1,
            max_unit_bytes=16,
            max_page_bytes=16,
            max_segments=1,
        )
        assert rec.page.text_bytes <= 16
        for unit in rec.page.units:
            for evidence in unit.evidence:
                all_text += evidence.text
        if rec.page.complete:
            break
        assert rec.page.next_cursor != cursor
        cursor = rec.page.next_cursor
    else:
        pytest.fail("reader failed to advance")
    assert all_text == "日本語🎉" * 65


def test_review_required_source_remains_untrusted(tmp_path):
    root, _ = _initialize(tmp_path, conversation(children=False), {})
    rec = admitted(root)
    assert rec.inventory_status == "inventory_review_required"
    assert rec.page.unverified_child_links > 0
    assert rec.page.generation_authorized is False
    assert not rec.approved_for_human_review


def test_new_ready_archive_requires_reinventory(tmp_path):
    root, _ = _initialize(tmp_path, conversation())
    archive2 = archive(
        tmp_path / "second.zip",
        [
            conversation(text="Updated but NOT committed to journal"),
        ],
    )
    publish_archive(root, archive2)
    with pytest.raises(
        PrivateSourceReaderError,
        match="newer_unprocessed_or_rejected_archive",
    ):
        admitted(root)
    assert _latest(root)[0] == _latest(root)[0]


def test_committed_new_revision_invalidates_old_expected_digests(tmp_path):
    root, _ = _initialize(tmp_path, conversation())
    previous = _latest(root)[1:]
    archive2 = archive(
        tmp_path / "second.zip",
        [
            conversation(text="A different synthetic claim."),
        ],
    )
    publish_archive(root, archive2)
    (new_receipt,) = inventory_ready(root, scope=SCOPE, commit=True)
    assert new_receipt.changed == 1
    with pytest.raises(PrivateSourceReaderError, match="source_revision_is_stale"):
        admitted(root, expected=previous)
    current = admitted(root)
    assert current.content_sha256 != previous[0]


def test_absent_not_deleted_conversation_is_not_current_evidence(tmp_path):
    root, _ = _initialize(tmp_path, conversation())
    digest, content, revision = _latest(root)
    archive2 = archive(
        tmp_path / "other.zip",
        [
            conversation(identity="different-synthetic-conversation"),
        ],
    )
    publish_archive(root, archive2)
    (receipt,) = inventory_ready(root, scope=SCOPE, commit=True)
    assert receipt.absent_not_deleted == 1
    # The old HMAC remains in Latest for no-delete, but is never current evidence.
    with pytest.raises(PrivateSourceReaderError, match="source_absent_from_latest_run"):
        read_admitted_source_page(
            root,
            scope=SCOPE,
            source_hmac=digest,
            expected_content_sha256=content,
            expected_revision_sha256=revision,
        )


def test_wrong_selector_wrong_scope_and_unknown_hmac_rejected(tmp_path):
    root, _ = _initialize(tmp_path, conversation())
    hmac, content, revision = _latest(root)
    with pytest.raises(
        PrivateSourceReaderError, match="reader_scope_contract_mismatch"
    ):
        read_admitted_source_page(
            root,
            scope=SCOPE,
            source_hmac=hmac,
            expected_content_sha256=content,
            expected_revision_sha256=revision,
            members=["conversations.json"],
        )
    with pytest.raises(PrivateSourceReaderError, match="source_not_in_latest"):
        read_admitted_source_page(
            root,
            scope="unrelated-scope",
            source_hmac=hmac,
            expected_content_sha256=content,
            expected_revision_sha256=revision,
        )
    with pytest.raises(PrivateSourceReaderError, match="source_not_in_latest"):
        read_admitted_source_page(
            root,
            scope=SCOPE,
            source_hmac="a" * 64,
            expected_content_sha256=content,
            expected_revision_sha256=revision,
        )


def test_changed_salt_rejected_without_new_journal_identity(tmp_path):
    root, _ = _initialize(tmp_path, conversation())
    salt = root / "state" / "identity-salt.bin"
    salt.write_bytes(os.urandom(32))
    with pytest.raises(PrivateSourceReaderError, match="identity_salt_changed"):
        admitted(root)
    assert salt.stat().st_size == 32


def test_missing_journal_or_root_never_creates_private_state(tmp_path):
    nonexisting = tmp_path / "never-created"
    with pytest.raises(PrivateSourceReaderError):
        read_admitted_source_page(
            nonexisting,
            scope=SCOPE,
            source_hmac="a" * 64,
            expected_content_sha256="a" * 64,
            expected_revision_sha256="a" * 64,
        )
    assert not nonexisting.exists()
    root, _ = _initialize(tmp_path, conversation())
    digest, content, revision = _latest(root)
    db = root / "state" / "journal.sqlite3"
    db.unlink()
    with pytest.raises(PrivateSourceReaderError, match="private_journal_unavailable"):
        read_admitted_source_page(
            root,
            scope=SCOPE,
            source_hmac=digest,
            expected_content_sha256=content,
            expected_revision_sha256=revision,
        )
    assert not db.exists()


def test_private_root_permissions_and_symlinks_refused(tmp_path):
    root, _ = _initialize(tmp_path, conversation())
    root.chmod(0o755)
    with pytest.raises(PrivateSourceReaderError, match="private_root_permissions"):
        admitted(root)
    root.chmod(0o700)
    symlink = tmp_path / "private-symlink"
    symlink.symlink_to(root, target_is_directory=True)
    with pytest.raises(PrivateSourceReaderError, match="private_ancestor_symlink"):
        read_admitted_source_page(
            symlink,
            scope=SCOPE,
            source_hmac="a" * 64,
            expected_content_sha256="a" * 64,
            expected_revision_sha256="a" * 64,
        )


def test_corrupted_ready_archive_is_not_used(tmp_path):
    root, _ = _initialize(tmp_path, conversation())
    (ready_file,) = (root / "ready").iterdir()
    ready_file.write_bytes(b"not really a valid zip")
    with pytest.raises(PrivateSourceReaderError, match="source_archive_invalid"):
        admitted(root)


def test_missing_or_corrupt_source_revision_rejected(tmp_path):
    root, _ = _initialize(tmp_path, conversation())
    hmac, _, revision = _latest(root)
    with pytest.raises(PrivateSourceReaderError, match="invalid_expected_content_sha"):
        read_admitted_source_page(
            root,
            scope=SCOPE,
            source_hmac=hmac,
            expected_content_sha256=SECRET,
            expected_revision_sha256=revision,
        )


def test_archive_must_match_intake_journal_not_just_json(tmp_path):
    root, _ = _initialize(tmp_path, conversation())
    original = admitted(root)
    archive2 = archive(tmp_path / "newer.zip", [conversation(title="updated")])
    publish_archive(root, archive2)
    inventory_ready(root, scope=SCOPE, commit=True)
    current = admitted(root)
    assert current.content_sha256 == original.content_sha256
    assert current.revision_sha256 != original.revision_sha256
    assert current.run_id != original.run_id


def test_no_newer_rejected_archive_is_silently_skipped(tmp_path):
    root, _ = _initialize(tmp_path, conversation())
    second = archive(tmp_path / "unknown.zip", [{"mapping": {"root": {}}}])
    publish_archive(root, second)
    (result,) = inventory_ready(root, scope=SCOPE, commit=True)
    assert result.status == "normalization_rejected"
    with pytest.raises(
        PrivateSourceReaderError, match="newer_unprocessed_or_rejected_archive"
    ):
        admitted(root)


def test_run_item_and_revision_journal_invariants_fail_closed(tmp_path):
    root, _ = _initialize(tmp_path, conversation())
    hmac, _, _ = _latest(root)
    path = root / "state" / "journal.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM run_items WHERE identity_hmac=?", (hmac,))
    with pytest.raises(PrivateSourceReaderError, match="source_run_item_missing"):
        admitted(root)

    # Separate root to demonstrate revision records are also checked.
    other = tmp_path / "revision-check"
    other.mkdir()
    root2, _ = _initialize(other, conversation())
    hmac2, _, revision2 = _latest(root2)
    with sqlite3.connect(root2 / "state" / "journal.sqlite3") as db:
        db.execute(
            "DELETE FROM revisions WHERE scope=? AND identity_hmac=? AND revision_sha=?",
            (SCOPE, hmac2, revision2),
        )
    with pytest.raises(
        PrivateSourceReaderError, match="source_revision_record_missing"
    ):
        admitted(root2)


def test_identity_salt_missing_and_unreadable_rejected_without_reinitialization(
    tmp_path,
):
    root, _ = _initialize(tmp_path, conversation())
    path = root / "state" / "identity-salt.bin"
    path.unlink()
    with pytest.raises(PrivateSourceReaderError, match="identity_salt_unavailable"):
        admitted(root)
    assert not path.exists()


@pytest.mark.parametrize("invalid", ["UNSAFE", "a" * 63, "AA" * 32, None])
def test_invalid_hmac_fails_before_touching_disk(tmp_path, invalid):
    root = tmp_path / "never-created"
    with pytest.raises(PrivateSourceReaderError, match="invalid_source_hmac"):
        read_admitted_source_page(
            root,
            scope=SCOPE,
            source_hmac=invalid,
            expected_content_sha256="a" * 64,
            expected_revision_sha256="a" * 64,
        )
    assert not root.exists()


def test_only_explicit_selected_archive_scope_can_be_read(tmp_path):
    original = archive(tmp_path / "explicit.zip", [conversation()])
    root = tmp_path / "private"
    publish_archive(root, original)
    (_,) = inventory_ready(
        root,
        scope=SCOPE,
        commit=True,
        members=["conversations.json"],
    )
    key, content, revision = _latest(root)
    with pytest.raises(
        PrivateSourceReaderError, match="reader_scope_contract_mismatch"
    ):
        read_admitted_source_page(
            root,
            scope=SCOPE,
            source_hmac=key,
            expected_content_sha256=content,
            expected_revision_sha256=revision,
        )
    exact = read_admitted_source_page(
        root,
        scope=SCOPE,
        source_hmac=key,
        expected_content_sha256=content,
        expected_revision_sha256=revision,
        members=["conversations.json"],
    )
    assert exact.page.complete
    assert exact.generation_authorized is False
