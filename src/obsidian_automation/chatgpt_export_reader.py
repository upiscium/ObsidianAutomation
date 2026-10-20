"""Private read-only, Journal-bound Reader of ChatGPT Export Source pages.

No CLI, LLM, credentials, publish, Vault writes, or production Job adapter.
This module is deliberately *not* an approval/canonical mutation boundary.
Only invoke inside a dedicated nonroot Importer/Reader service in production.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import sqlite3
import stat
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from .chatgpt_export_inspect import Limits
from .chatgpt_export_intake import (
    _SCHEMA_VERSION,
    IntakeError,
    _check_private,
    _identity,
    _selector_sha,
    _valid_scope,
    _verify_ready,
)
from .chatgpt_export_normalize import (
    NORMALIZER_VERSION,
    NormalizeError,
    SelectedConversation,
    normalize_export,
)
from .chatgpt_export_source import (
    SourceContextPage,
    SourceContractError,
    SourceCursor,
    build_source_context_page,
    validate_source_context_page,
)

READER_VERSION = "chatgpt-private-source-reader-v0"
_MAX_DB_BYTES = 128 * 1024 * 1024
_MAX_SOURCE_CONVERSATIONS = 50_000
_MAX_READY_ARCHIVE_BYTES = 4 * 1024**3


class PrivateSourceReaderError(ValueError):
    """Only fixed error codes; never interpolate private text/paths/IDs."""


@dataclass(frozen=True, repr=False)
class PrivateAdmittedPage:
    """Private result; never serialize, repr/log or send to shared AI state.

    Verification binds one exact immutable ZIP, one Journal commit, Scope,
    selected conversation HMAC, normalized source and the selected page.
    This is **evidence verification**, not authorization to send to a model.
    """

    reader_version: str
    scope: str
    source_hmac: str
    archive_sha256: str
    content_sha256: str
    revision_sha256: str
    run_id: str
    inventory_status: str
    page: SourceContextPage
    conversation: SelectedConversation
    generation_authorized: bool = False
    approved_for_human_review: bool = False


def _existing_private_root(root: Path) -> Path:
    """Read-only admission; never initialize a private import directory."""
    if os.geteuid() == 0:
        raise PrivateSourceReaderError("root_identity_forbidden")
    if not isinstance(root, Path) or not root.is_absolute() or ".." in root.parts:
        raise PrivateSourceReaderError("invalid_private_root")
    here = Path("/")
    try:
        for segment in root.parts[1:]:
            here = here / segment
            if stat.S_ISLNK(here.lstat().st_mode):
                raise PrivateSourceReaderError("private_ancestor_symlink")
        for path in (root, root / "state", root / "ready", root / "staging"):
            _check_private(path, directory=True)
    except IntakeError:
        raise PrivateSourceReaderError("private_root_permissions") from None
    except OSError:
        raise PrivateSourceReaderError("private_root_unavailable") from None
    return root


@contextmanager
def _read_locked(root: Path) -> Iterator[None]:
    """Shared flock without creating lock/db/root. Writer uses EX lock."""
    lock = root / "state" / "import.lock"
    try:
        _check_private(lock, directory=False)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK
        fd = os.open(lock, flags)
        try:
            st = os.fstat(fd)
            disk = lock.lstat()
            if (
                not stat.S_ISREG(st.st_mode)
                or st.st_uid != os.geteuid()
                or st.st_nlink != 1
                or stat.S_IMODE(st.st_mode) != 0o600
                or (st.st_dev, st.st_ino) != (disk.st_dev, disk.st_ino)
            ):
                raise PrivateSourceReaderError("private_lock_invalid")
            fcntl.flock(fd, fcntl.LOCK_SH)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    except PrivateSourceReaderError:
        raise
    except (IntakeError, OSError):
        raise PrivateSourceReaderError("private_lock_unavailable") from None


@contextmanager
def _open_readonly_journal(root: Path) -> Iterator[sqlite3.Connection]:
    db = root / "state" / "journal.sqlite3"
    try:
        _check_private(db, directory=False)
        st = db.lstat()
        if not 0 < st.st_size <= _MAX_DB_BYTES:
            raise PrivateSourceReaderError("private_journal_size_limit")
        uri = "file:" + quote(str(db), safe="/") + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=3.0)
        try:
            conn.execute("PRAGMA trusted_schema=OFF")
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA foreign_keys=ON")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if type(version) is not int or version != _SCHEMA_VERSION:
                raise PrivateSourceReaderError("journal_version_mismatch")
            if conn.execute("PRAGMA quick_check(1)").fetchone()[0] != "ok":
                raise PrivateSourceReaderError("journal_integrity_error")
            conn.execute("BEGIN")
            yield conn
        finally:
            conn.close()
    except PrivateSourceReaderError:
        raise
    except (IntakeError, OSError, sqlite3.Error):
        raise PrivateSourceReaderError("private_journal_unavailable") from None


def _read_bound_salt(root: Path, conn: sqlite3.Connection) -> bytes:
    """Fail closed on missing/changed HMAC salt; never regenerate it."""
    secret = root / "state" / "identity-salt.bin"
    try:
        _check_private(secret, directory=False)
        fd = os.open(secret, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            st = os.fstat(fd)
            salt = os.read(fd, 33)
            new = os.fstat(fd)
            disk = secret.lstat()
            if (
                not stat.S_ISREG(st.st_mode)
                or st.st_uid != os.geteuid()
                or st.st_nlink != 1
                or stat.S_IMODE(st.st_mode) != 0o600
                or (st.st_dev, st.st_ino) != (disk.st_dev, disk.st_ino)
                or (st.st_size, st.st_mtime_ns, st.st_ctime_ns)
                != (new.st_size, new.st_mtime_ns, new.st_ctime_ns)
            ):
                raise PrivateSourceReaderError("identity_salt_invalid")
        finally:
            os.close(fd)
        if len(salt) != 32:
            raise PrivateSourceReaderError("identity_salt_invalid")
        bindings = conn.execute(
            "SELECT salt_sha256 FROM journal_binding WHERE id=1"
        ).fetchall()
        if len(bindings) != 1 or bindings[0][0] != hashlib.sha256(salt).hexdigest():
            raise PrivateSourceReaderError("identity_salt_changed")
        return salt
    except PrivateSourceReaderError:
        raise
    except (IntakeError, OSError, sqlite3.Error):
        raise PrivateSourceReaderError("identity_salt_unavailable") from None


def _hex(value: object, code: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise PrivateSourceReaderError(code)
    return value


def _lookup_latest(
    conn: sqlite3.Connection,
    *,
    scope: str,
    source_hmac: str,
    selector_sha: str,
) -> tuple[str, str, str, str, str, int]:
    try:
        result = conn.execute(
            "SELECT l.content_sha,l.revision_sha,l.last_run_id,"
            " r.archive_sha,r.status,r.parser_version,r.selector_sha,"
            " a.status,a.archive_bytes,a.seq "
            "FROM latest l JOIN runs r ON r.run_id=l.last_run_id "
            "JOIN archives a ON a.archive_sha=r.archive_sha "
            "WHERE l.scope=? AND l.identity_hmac=? AND r.scope=?",
            (scope, source_hmac, scope),
        ).fetchall()
        if len(result) != 1:
            raise PrivateSourceReaderError("source_not_in_latest")
        (
            content,
            revision,
            run_id,
            archive,
            run_status,
            parser,
            selector,
            archive_status,
            size,
            seq,
        ) = result[0]
        for value, code in (
            (content, "invalid_latest_content_digest"),
            (revision, "invalid_latest_revision_digest"),
            (run_id, "invalid_latest_run"),
            (archive, "invalid_latest_archive"),
        ):
            _hex(value, code)
        if parser != NORMALIZER_VERSION or selector != selector_sha:
            raise PrivateSourceReaderError("reader_scope_contract_mismatch")
        if run_status not in ("inventory_only", "inventory_review_required"):
            raise PrivateSourceReaderError("reader_source_not_accepted_inventory")
        if archive_status != "ready" or type(size) is not int or size < 1:
            raise PrivateSourceReaderError("reader_archive_not_ready")
        if size > _MAX_READY_ARCHIVE_BYTES:
            raise PrivateSourceReaderError("reader_archive_size_limit")
        if type(seq) is not int or seq < 1:
            raise PrivateSourceReaderError("reader_sequence_invalid")
        newest = conn.execute(
            "SELECT MAX(a.seq) FROM runs r JOIN archives a "
            "ON a.archive_sha=r.archive_sha WHERE r.scope=?",
            (scope,),
        ).fetchone()[0]
        if newest != seq:
            # The old row remains in Latest if a later archive omitted it.
            # Reading it as *current* evidence would be an unsafe backfill.
            raise PrivateSourceReaderError("source_absent_from_latest_run")
        items = conn.execute(
            "SELECT item_status FROM run_items WHERE run_id=? AND identity_hmac=?",
            (run_id, source_hmac),
        ).fetchall()
        if len(items) != 1 or items[0][0] not in (
            "added",
            "changed",
            "unchanged",
            "revision_only",
        ):
            raise PrivateSourceReaderError("source_run_item_missing")
        revisions = conn.execute(
            "SELECT content_sha FROM revisions WHERE scope=? AND "
            "identity_hmac=? AND revision_sha=?",
            (scope, source_hmac, revision),
        ).fetchall()
        if len(revisions) != 1 or revisions[0][0] != content:
            raise PrivateSourceReaderError("source_revision_record_missing")
        if (
            conn.execute(
                "SELECT 1 FROM archives WHERE status='ready' AND seq>? LIMIT 1",
                (seq,),
            ).fetchone()
            is not None
        ):
            # Uncommitted/rejected newer archives may revise this source.
            raise PrivateSourceReaderError("newer_unprocessed_or_rejected_archive")
        return content, revision, run_id, archive, run_status, size
    except PrivateSourceReaderError:
        raise
    except sqlite3.Error:
        raise PrivateSourceReaderError("journal_lookup_failed") from None


def read_admitted_source_page(
    root: Path,
    *,
    scope: str,
    source_hmac: str,
    expected_content_sha256: str,
    expected_revision_sha256: str,
    cursor: SourceCursor | None = None,
    members: Sequence[str] = (),
    max_units: int = 4,
    max_unit_bytes: int = 8192,
    max_page_bytes: int = 32768,
    max_segments: int = 8,
) -> PrivateAdmittedPage:
    """Read one exact committed Source revision with bounded selected context.

    The caller must already possess the Source HMAC and expected digests from
    a trusted private inventory snapshot. This capability returns ONLY private
    memory. It never authorizes a model, scheduler, Human Review or mutation.
    """
    try:
        _valid_scope(scope)
        _hex(source_hmac, "invalid_source_hmac")
        _hex(expected_content_sha256, "invalid_expected_content_sha")
        _hex(expected_revision_sha256, "invalid_expected_revision_sha")
        selector_sha = _selector_sha(members)
    except IntakeError:
        raise PrivateSourceReaderError("invalid_source_selection") from None
    root = _existing_private_root(root)
    with _read_locked(root), _open_readonly_journal(root) as conn:
        salt = _read_bound_salt(root, conn)
        content_sha, revision_sha, run_id, archive_sha, status, size = _lookup_latest(
            conn,
            scope=scope,
            source_hmac=source_hmac,
            selector_sha=selector_sha,
        )
        if (
            content_sha != expected_content_sha256
            or revision_sha != expected_revision_sha256
        ):
            raise PrivateSourceReaderError("source_revision_is_stale")
        ready = root / "ready" / (archive_sha + ".zip")
        try:
            _verify_ready(ready, archive_sha, size)
            batch = normalize_export(
                ready,
                scope=scope,
                members=members,
                limits=Limits(archive_bytes=_MAX_READY_ARCHIVE_BYTES),
            )
        except (IntakeError, NormalizeError, OSError, MemoryError):
            raise PrivateSourceReaderError("source_archive_invalid") from None
        if (
            batch.parser_version != NORMALIZER_VERSION
            or batch.archive_sha256 != archive_sha
            or batch.selected_json_members < 1
            or len(batch.conversations) > _MAX_SOURCE_CONVERSATIONS
        ):
            raise PrivateSourceReaderError("source_batch_mismatch")
        verified_run = hashlib.sha256(
            (batch.import_run_sha256 + ":" + selector_sha).encode("ascii")
        ).hexdigest()
        if verified_run != run_id:
            raise PrivateSourceReaderError("source_run_mismatch")
        selected = [
            item
            for item in batch.conversations
            if _identity(salt, scope, item.conversation_id) == source_hmac
        ]
        if len(selected) != 1:
            raise PrivateSourceReaderError("source_conversation_missing")
        conversation = selected[0]
        if (
            conversation.content_sha256 != content_sha
            or conversation.source_revision_sha256 != revision_sha
        ):
            raise PrivateSourceReaderError("source_conversation_stale")
        try:
            page = build_source_context_page(
                conversation,
                scope=scope,
                source_hmac=source_hmac,
                cursor=cursor,
                max_units=max_units,
                max_unit_bytes=max_unit_bytes,
                max_page_bytes=max_page_bytes,
                max_segments=max_segments,
            )
            validate_source_context_page(
                page,
                conversation,
                scope=scope,
                source_hmac=source_hmac,
            )
        except SourceContractError:
            raise PrivateSourceReaderError("source_context_invalid") from None
        return PrivateAdmittedPage(
            reader_version=READER_VERSION,
            scope=scope,
            source_hmac=source_hmac,
            archive_sha256=archive_sha,
            content_sha256=content_sha,
            revision_sha256=revision_sha,
            run_id=run_id,
            inventory_status=status,
            page=page,
            conversation=conversation,
        )
