"""Private, durable, local-only ChatGPT Export intake inventory (Issue #62).

Never logs or persists plaintext conversations, titles or identifiers to SQLite.
Archives stay in a private root. No LLM, generation, transport or scheduler.
Experimental API: **not** an approved production importer.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import sys
from typing import Iterator, Sequence

from .chatgpt_export_inspect import InspectionError, Limits, inspect_export
from .chatgpt_export_normalize import (
    NORMALIZER_VERSION, NormalizeError, SelectedBatch, normalize_export,
)

_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_SCOPE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
_READ_BUFFER = 1024 * 1024
_SCHEMA_VERSION = 1


class IntakeError(ValueError):
    """Fixed diagnostics only; never include private values or exception text."""


@dataclass(frozen=True, repr=False)
class PublishReceipt:
    status: str
    bytes: int
    archive_sha256: str


@dataclass(frozen=True, repr=False)
class InventoryReceipt:
    status: str
    added: int = 0
    changed: int = 0
    unchanged: int = 0
    revision_only: int = 0
    absent_not_deleted: int = 0
    quarantined_slots: int = 0
    unverified_child_links: int = 0
    unsupported_parts: int = 0
    retained_messages: int = 0
    # Information only. Does not approve any Generation or canonical write.
    eligible_for_generation: bool = False


def _valid_scope(scope: str) -> None:
    if not isinstance(scope, str) or not _SCOPE.fullmatch(scope):
        raise IntakeError("invalid_scope")


def _check_private(path: Path, *, directory: bool) -> None:
    try:
        s = path.lstat()
    except OSError:
        raise IntakeError("private_path_unreadable") from None
    if s.st_uid != os.geteuid():
        raise IntakeError("private_owner_mismatch")
    if stat.S_ISLNK(s.st_mode) or not (
        stat.S_ISDIR(s.st_mode) if directory else stat.S_ISREG(s.st_mode)
    ):
        raise IntakeError("private_path_type")
    if stat.S_IMODE(s.st_mode) != (0o700 if directory else 0o600):
        raise IntakeError("private_permissions")
    if not directory and s.st_nlink != 1:
        raise IntakeError("private_hardlink")


def _root(root: Path) -> Path:
    if os.geteuid() == 0:
        raise IntakeError("root_identity_forbidden")
    if not root.is_absolute() or ".." in root.parts:
        raise IntakeError("invalid_private_root")
    # Ensure root ancestors are not symlinks; same-UID hostile races are not
    # supported and must be isolated by the future Importer service identity.
    current = Path("/")
    for component in root.parts[1:]:
        current = current / component
        if current.exists() or current.is_symlink():
            try:
                if stat.S_ISLNK(current.lstat().st_mode):
                    raise IntakeError("private_ancestor_symlink")
            except OSError:
                raise IntakeError("private_path_unreadable") from None
    try:
        root.mkdir(mode=0o700, exist_ok=True)
        _check_private(root, directory=True)
        for name in ("staging", "ready", "state"):
            child = root / name
            child.mkdir(mode=0o700, exist_ok=True)
            _check_private(child, directory=True)
    except IntakeError:
        raise
    except OSError:
        raise IntakeError("private_root_creation_failed") from None
    return root


def _fsync_dir(path: Path) -> None:
    try:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        raise IntakeError("directory_sync_failed") from None


@contextmanager
def _locked(root: Path) -> Iterator[None]:
    lock_path = root / "state" / "import.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(lock_path, flags, 0o600)
        try:
            _check_private(lock_path, directory=False)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)
    except IntakeError:
        raise
    except OSError:
        raise IntakeError("intake_lock_failed") from None


def _connect(root: Path) -> sqlite3.Connection:
    path = root / "state" / "journal.sqlite3"
    try:
        if not path.exists() and not path.is_symlink():
            # Re-creating an empty Journal while archived ready ZIPs remain
            # would silently discard the import history and reintroduce all
            # conversations as 'added'. Never auto-reset durable state.
            if any((root / "ready").iterdir()):
                raise IntakeError("journal_missing_with_ready_archives")
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
            os.close(fd)
            _fsync_dir(path.parent)
        _check_private(path, directory=False)
        conn = sqlite3.connect(path, timeout=10.0)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA temp_store=MEMORY")
            conn.execute("PRAGMA trusted_schema=OFF")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, _SCHEMA_VERSION):
                raise IntakeError("unsupported_journal_schema")
            if version == 0:
                conn.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE archives (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        archive_sha TEXT NOT NULL UNIQUE,
                        archive_bytes INTEGER NOT NULL,
                        status TEXT NOT NULL CHECK(status IN ('publishing','ready'))
                    );
                    CREATE TABLE runs (
                        run_id TEXT PRIMARY KEY,
                        scope TEXT NOT NULL,
                        archive_sha TEXT NOT NULL REFERENCES archives(archive_sha),
                        parser_version TEXT NOT NULL,
                        selector_sha TEXT NOT NULL,
                        status TEXT NOT NULL,
                        added INTEGER NOT NULL,
                        changed INTEGER NOT NULL,
                        unchanged INTEGER NOT NULL,
                        revision_only INTEGER NOT NULL,
                        absent_not_deleted INTEGER NOT NULL,
                        quarantined_slots INTEGER NOT NULL,
                        unverified_child_links INTEGER NOT NULL,
                        unsupported_parts INTEGER NOT NULL,
                        retained_messages INTEGER NOT NULL
                    );
                    CREATE INDEX runs_lookup ON runs(scope,archive_sha,parser_version,selector_sha);
                    CREATE TABLE journal_binding (
                        id INTEGER PRIMARY KEY CHECK(id=1),
                        salt_sha256 TEXT NOT NULL
                    );
                    CREATE TABLE latest (
                        scope TEXT NOT NULL,
                        identity_hmac TEXT NOT NULL,
                        content_sha TEXT NOT NULL,
                        revision_sha TEXT NOT NULL,
                        last_run_id TEXT NOT NULL REFERENCES runs(run_id),
                        PRIMARY KEY(scope,identity_hmac)
                    );
                    CREATE TABLE revisions (
                        scope TEXT NOT NULL,
                        identity_hmac TEXT NOT NULL,
                        revision_sha TEXT NOT NULL,
                        content_sha TEXT NOT NULL,
                        first_run_id TEXT NOT NULL REFERENCES runs(run_id),
                        PRIMARY KEY(scope,identity_hmac,revision_sha)
                    );
                    CREATE TABLE rejections (
                        scope TEXT NOT NULL,
                        archive_sha TEXT NOT NULL REFERENCES archives(archive_sha),
                        parser_version TEXT NOT NULL,
                        selector_sha TEXT NOT NULL,
                        error_code TEXT NOT NULL,
                        PRIMARY KEY(scope,archive_sha,parser_version,selector_sha)
                    );
                    CREATE TABLE run_items (
                        run_id TEXT NOT NULL REFERENCES runs(run_id),
                        identity_hmac TEXT NOT NULL,
                        item_status TEXT NOT NULL CHECK(item_status IN
                            ('added','changed','unchanged','revision_only')),
                        PRIMARY KEY(run_id,identity_hmac)
                    );
                    PRAGMA user_version=1;
                    COMMIT;
                """)
            # The DB remains private and only bounded, hashed inventory
            # material is stored here. Never SELECT raw source text.
            if conn.execute("PRAGMA quick_check(1)").fetchone()[0] != "ok":
                raise IntakeError("journal_corrupt")
            return conn
        except BaseException:
            conn.close()
            raise
    except IntakeError:
        raise
    except (OSError, sqlite3.Error):
        raise IntakeError("journal_unavailable") from None


def _salt(root: Path, conn: sqlite3.Connection) -> bytes:
    path = root / "state" / "identity-salt.bin"
    try:
        if not path.exists() and not path.is_symlink():
            if conn.execute("SELECT 1 FROM journal_binding WHERE id=1").fetchone():
                raise IntakeError("identity_salt_missing")
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                os.write(fd, secrets.token_bytes(32))
                os.fsync(fd)
            finally:
                os.close(fd)
            _fsync_dir(path.parent)
        _check_private(path, directory=False)
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            data = os.read(fd, 33)
        finally:
            os.close(fd)
        if len(data) != 32:
            raise IntakeError("invalid_identity_salt")
        return data
    except IntakeError:
        raise
    except OSError:
        raise IntakeError("identity_salt_unavailable") from None


def _bind_identity_salt(conn: sqlite3.Connection, salt: bytes) -> None:
    """Changing an identity key must NEVER produce a fake fresh import."""
    digest = hashlib.sha256(salt).hexdigest()
    try:
        stored = conn.execute(
            "SELECT salt_sha256 FROM journal_binding WHERE id=1"
        ).fetchone()
        if stored is None:
            # Existing inventory with missing binding is a corrupted store,
            # not grounds to silently select new conversation identities.
            prior = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            if prior or conn.execute("SELECT COUNT(*) FROM latest").fetchone()[0]:
                raise IntakeError("identity_binding_missing")
            with conn:
                conn.execute(
                    "INSERT INTO journal_binding(id,salt_sha256) VALUES(1,?)", (digest,)
                )
        elif stored[0] != digest:
            raise IntakeError("identity_salt_changed")
    except IntakeError:
        raise
    except sqlite3.Error:
        raise IntakeError("identity_binding_invalid") from None


def _safe_source(source: Path, limit: int) -> tuple[int, os.stat_result]:
    fd = -1
    try:
        fd = os.open(source, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or
                st.st_size > limit or st.st_nlink != 1):
            raise IntakeError("unsafe_export_source")
        return fd, st
    except (IntakeError, OSError) as exc:
        if fd >= 0:
            os.close(fd)
        if isinstance(exc, IntakeError):
            raise
        raise IntakeError("unreadable_export_source") from None


def _recover_orphaned_staging_link(root: Path, ready: Path) -> None:
    """Repair a hard-link publish interrupted before staged-file cleanup.

    The ready file may temporarily have link count two after os.link(staging,
    ready). Reject unexplained hard-links. Only remove the one verified inode
    alias inside our private staging directory.
    """
    try:
        st = ready.lstat()
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid()
                or stat.S_IMODE(st.st_mode) != 0o600):
            raise IntakeError("ready_archive_permissions")
        if st.st_nlink == 1:
            return
        if st.st_nlink != 2:
            raise IntakeError("unexpected_ready_hardlink")
        matches: list[Path] = []
        for idx, item in enumerate((root / "staging").iterdir()):
            if idx >= 1024:
                raise IntakeError("staging_recovery_limit")
            if not item.name.endswith(".partial"):
                continue
            candidate = item.lstat()
            if (stat.S_ISREG(candidate.st_mode) and candidate.st_uid == st.st_uid
                    and stat.S_IMODE(candidate.st_mode) == 0o600
                    and (candidate.st_ino, candidate.st_dev) == (st.st_ino, st.st_dev)):
                matches.append(item)
        if len(matches) != 1:
            raise IntakeError("unexplained_ready_hardlink")
        matches[0].unlink()
        _fsync_dir(root / "staging")
        if ready.stat().st_nlink != 1:
            raise IntakeError("staging_recovery_incomplete")
    except IntakeError:
        raise
    except OSError:
        raise IntakeError("staging_recovery_failed") from None


def _verify_ready(path: Path, expected_sha: str, expected_size: int) -> None:
    _check_private(path, directory=False)
    try:
        flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as source:
            st = os.fstat(source.fileno())
            if st.st_size != expected_size:
                raise IntakeError("ready_archive_mismatch")
            hasher = hashlib.sha256()
            while block := source.read(_READ_BUFFER):
                hasher.update(block)
            if hasher.hexdigest() != expected_sha:
                raise IntakeError("ready_archive_mismatch")
            new = os.fstat(source.fileno())
            if (st.st_size, st.st_mtime_ns, st.st_ctime_ns) != (
                new.st_size, new.st_mtime_ns, new.st_ctime_ns
            ):
                raise IntakeError("ready_archive_mutated")
    except IntakeError:
        raise
    except OSError:
        raise IntakeError("ready_archive_unreadable") from None


def publish_archive(root: Path, source: Path, *, limits: Limits = Limits()) -> PublishReceipt:
    """Atomically copy a finished ZIP to the private ready directory.

    A ready archive is never overwritten. A pending DB registration can be
    recovered by retrying the same source. No automatic processing is invoked.
    """
    root = _root(root)
    with _locked(root):
        fd, before = _safe_source(source, limits.archive_bytes)
        temp = root / "staging" / (secrets.token_hex(16) + ".partial")
        try:
            try:
                tmp_fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            except OSError:
                os.close(fd)  # source descriptor not yet in fdopen's context
                raise
            try:
                total, hasher = 0, hashlib.sha256()
                with os.fdopen(fd, "rb") as inp, os.fdopen(tmp_fd, "wb") as out:
                    while block := inp.read(_READ_BUFFER):
                        total += len(block)
                        if total > limits.archive_bytes or total > before.st_size:
                            raise IntakeError("export_source_size_changed")
                        out.write(block)
                        hasher.update(block)
                    out.flush()
                    os.fsync(out.fileno())
                    after = os.fstat(inp.fileno())
                    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
                    if total != before.st_size or any(getattr(before, f) != getattr(after, f) for f in fields):
                        raise IntakeError("export_source_changed")
            except BaseException:
                # The with-blocks own both descriptors after successful wraps.
                raise
            # A completed upload must also pass bounded ZIP/JSON structural
            # inspection before becoming visible as a ready input. The
            # status 'schema_review_required' is valid for *quarantined*
            # inventory, but it can never authorize Knowledge generation.
            try:
                report = inspect_export(temp, limits=limits)
                if report["status"] not in ("inspected", "schema_review_required"):
                    raise IntakeError("export_structure_rejected")
            except (InspectionError, MemoryError):
                raise IntakeError("export_structure_rejected") from None
            sha = hasher.hexdigest()
            ready = root / "ready" / (sha + ".zip")
            conn = _connect(root)
            try:
                row = conn.execute("SELECT status,archive_bytes FROM archives WHERE archive_sha=?", (sha,)).fetchone()
                if row is not None and row[1] != total:
                    raise IntakeError("archive_identity_collision")
                if row is None:
                    with conn:
                        conn.execute(
                            "INSERT INTO archives(archive_sha,archive_bytes,status) VALUES(?,?,'publishing')",
                            (sha,total),
                        )
                if ready.exists() or ready.is_symlink():
                    _recover_orphaned_staging_link(root, ready)
                    _verify_ready(ready, sha, total)
                else:
                    # Hard-link publication fails rather than replacing a
                    # concurrent or unexpected ready filename.
                    try:
                        os.link(temp, ready, follow_symlinks=False)
                    except FileExistsError:
                        _recover_orphaned_staging_link(root, ready)
                        _verify_ready(ready, sha, total)
                    _fsync_dir(ready.parent)
                with conn:
                    conn.execute("UPDATE archives SET status='ready' WHERE archive_sha=?", (sha,))
                return PublishReceipt(status="existing" if row is not None and row[0] == "ready" else "published",
                                      bytes=total, archive_sha256=sha)
            finally:
                conn.close()
        except IntakeError:
            raise
        except (OSError, sqlite3.Error):
            raise IntakeError("archive_publish_failed") from None
        finally:
            if temp.exists():
                temp.unlink()
                _fsync_dir(temp.parent)


def _selector_sha(members: Sequence[str]) -> str:
    if not isinstance(members, (tuple, list)) or any(not isinstance(x, str) for x in members):
        raise IntakeError("invalid_member_selection")
    return hashlib.sha256(json.dumps(
        sorted(members) if members else ["auto-selection"], ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _identity(salt: bytes, scope: str, conversation_id: str) -> str:
    return hmac.new(salt, (scope + "\x00" + conversation_id).encode("utf-8"), hashlib.sha256).hexdigest()


def _summary(batch: SelectedBatch) -> tuple[int, int, int]:
    missing_edges = sum(c.unverified_child_links for c in batch.conversations)
    # A text record omitted because it is too large, empty or unsupported
    # is a coverage gap too; never mark the inventory as fully understood.
    # Internal reasoning, placeholders, hidden/nonfinal and nonconversation
    # roles are excluded by policy, not treated as missing Knowledge content.
    unsupported_parts = sum(
        count
        for c in batch.conversations
        for reason, count in c.omitted_reasons
        if reason not in {
            "placeholder", "internal_reasoning", "nonconversation_role",
            "nonfinal_assistant_message", "hidden_message",
        }
    )
    retained = sum(len(c.messages) for c in batch.conversations)
    return missing_edges, unsupported_parts, retained


def _inventory(conn: sqlite3.Connection, batch: SelectedBatch, salt: bytes) -> tuple[
    dict[str, tuple[str, str]], dict[str, str], InventoryReceipt
]:
    rows = {
        key: (content, revision) for key, content, revision
        in conn.execute("SELECT identity_hmac,content_sha,revision_sha FROM latest WHERE scope=?", (batch.scope,))
    }
    entries: dict[str, tuple[str, str]] = {}
    statuses: dict[str, str] = {}
    counter = {"added": 0, "changed": 0, "unchanged": 0, "revision_only": 0}
    for conv in batch.conversations:
        key = _identity(salt, batch.scope, conv.conversation_id)
        if key in entries:
            raise IntakeError("duplicate_private_identity")
        entries[key] = (conv.content_sha256, conv.source_revision_sha256)
        previous = rows.get(key)
        if previous is None:
            status = "added"
        elif previous[0] != conv.content_sha256:
            status = "changed"
        elif previous[1] != conv.source_revision_sha256:
            status = "revision_only"
        else:
            status = "unchanged"
        statuses[key] = status
        counter[status] += 1
    missing_edges, unsupported, retained = _summary(batch)
    needs_review = (not batch.conversations or batch.quarantined_conversation_slots > 0 or
                    missing_edges > 0 or unsupported > 0)
    receipt = InventoryReceipt(
        status="inventory_review_required" if needs_review else "inventory_only",
        absent_not_deleted=len(set(rows) - set(entries)),
        quarantined_slots=batch.quarantined_conversation_slots,
        unverified_child_links=missing_edges,
        unsupported_parts=unsupported,
        retained_messages=retained,
        **counter,
    )
    return entries, statuses, receipt


def inventory_ready(
    root: Path, *, scope: str, members: Sequence[str] = (),
    commit: bool = False, max_archives: int = 1,
) -> tuple[InventoryReceipt, ...]:
    """Process ready archives in publication sequence; never generate notes.

    commit=False returns a read-only inventory preview (no runs/Latest writes).
    commit=True commits an immutable run, hashed latest identities and full
    source revisions; it still queues **zero** LLM jobs and changes no Vault.
    """
    _valid_scope(scope)
    if type(max_archives) is not int or not 1 <= max_archives <= 16:
        raise IntakeError("invalid_inventory_limit")
    if not commit and max_archives != 1:
        # Without a simulated overlay, previewing several successive archives
        # against the same committed Latest would produce misleading deltas.
        raise IntakeError("multi_archive_preview_unsupported")
    selector = _selector_sha(members)
    root = _root(root)
    receipts = []
    with _locked(root):
        conn = _connect(root)
        try:
            salt = _salt(root, conn)
            _bind_identity_salt(conn, salt)
            # Scope selection policy is immutable after the first committed
            # inventory/rejection. Changing selector or parser without a new
            # scope could mix incompatible source identities in Latest.
            contracts = conn.execute(
                "SELECT DISTINCT selector_sha,parser_version FROM runs WHERE scope=? "
                "UNION SELECT DISTINCT selector_sha,parser_version FROM rejections WHERE scope=?",
                (scope, scope),
            ).fetchall()
            if any(s != selector or v != NORMALIZER_VERSION for s, v in contracts):
                raise IntakeError("scope_contract_changed")
            archive_rows = conn.execute(
                "SELECT archive_sha,archive_bytes FROM archives WHERE status='ready' ORDER BY seq ASC"
            ).fetchall()
            checkpoint = conn.execute(
                "SELECT MAX(a.seq) FROM runs r JOIN archives a ON r.archive_sha=a.archive_sha "
                "WHERE r.scope=?", (scope,),
            ).fetchone()[0]
            for archive_sha, size in archive_rows:
                if len(receipts) >= max_archives:
                    break
                if not _HEX64.fullmatch(archive_sha):
                    raise IntakeError("invalid_archive_identity")
                row = conn.execute(
                    "SELECT r.run_id FROM runs r WHERE r.scope=? AND r.archive_sha=? "
                    "AND r.parser_version=? AND r.selector_sha=?",
                    (scope,archive_sha,NORMALIZER_VERSION,selector),
                ).fetchone()
                if row is not None:
                    # Already journalled, never revisit/overwrite the current
                    # Latest with an older snapshot on each invocation.
                    continue
                rejected = conn.execute(
                    "SELECT 1 FROM rejections WHERE scope=? AND archive_sha=? "
                    "AND parser_version=? AND selector_sha=?",
                    (scope,archive_sha,NORMALIZER_VERSION,selector),
                ).fetchone()
                if rejected is not None:
                    continue
                seq = conn.execute(
                    "SELECT seq FROM archives WHERE archive_sha=?", (archive_sha,),
                ).fetchone()[0]
                if checkpoint is not None and seq < checkpoint:
                    raise IntakeError("out_of_order_selection_requires_new_scope")
                path = root / "ready" / (archive_sha + ".zip")
                _recover_orphaned_staging_link(root, path)
                _verify_ready(path, archive_sha, size)
                try:
                    batch = normalize_export(path, scope=scope, members=members)
                except NormalizeError as exc:
                    # A valid ZIP can contain a conversation schema that the
                    # strict normalizer cannot handle. Record the fixed code,
                    # no private values, and do not block later ready archives.
                    if commit:
                        with conn:
                            conn.execute(
                                "INSERT OR IGNORE INTO rejections(scope,archive_sha,parser_version,"
                                "selector_sha,error_code) VALUES(?,?,?,?,?)",
                                (scope,archive_sha,NORMALIZER_VERSION,selector,str(exc)),
                            )
                    receipts.append(InventoryReceipt(status="normalization_rejected"))
                    continue
                if batch.archive_sha256 != archive_sha:
                    raise IntakeError("archive_content_mismatch")
                run_id = hashlib.sha256(
                    (batch.import_run_sha256 + ":" + selector).encode("ascii")
                ).hexdigest()
                entries, statuses, receipt = _inventory(conn, batch, salt)
                receipts.append(receipt)
                if not commit:
                    continue
                with conn:
                    conn.execute(
                        "INSERT INTO runs(run_id,scope,archive_sha,parser_version,selector_sha,status,"
                        "added,changed,unchanged,revision_only,absent_not_deleted,quarantined_slots,"
                        "unverified_child_links,unsupported_parts,retained_messages)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (run_id,scope,archive_sha,NORMALIZER_VERSION,selector,
                         receipt.status,receipt.added,receipt.changed,receipt.unchanged,
                         receipt.revision_only,receipt.absent_not_deleted,
                         receipt.quarantined_slots,receipt.unverified_child_links,
                         receipt.unsupported_parts,receipt.retained_messages),
                    )
                    for key, (content, revision) in entries.items():
                        conn.execute(
                            "INSERT OR IGNORE INTO revisions(scope,identity_hmac,revision_sha,"
                            "content_sha,first_run_id) VALUES(?,?,?,?,?)",
                            (scope,key,revision,content,run_id),
                        )
                        conn.execute(
                            "INSERT INTO run_items(run_id,identity_hmac,item_status) VALUES(?,?,?)",
                            (run_id,key,statuses[key]),
                        )
                        conn.execute(
                            "INSERT INTO latest(scope,identity_hmac,content_sha,revision_sha,last_run_id) "
                            "VALUES(?,?,?,?,?) ON CONFLICT(scope,identity_hmac) DO UPDATE SET "
                            "content_sha=excluded.content_sha,revision_sha=excluded.revision_sha,"
                            "last_run_id=excluded.last_run_id",
                            (scope,key,content,revision,run_id),
                        )
            return tuple(receipts)
        except IntakeError:
            raise
        except sqlite3.Error:
            raise IntakeError("journal_write_failed") from None
        finally:
            conn.close()


class _Parser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise IntakeError("invalid_arguments")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _Parser(description=__doc__)
    parser.add_argument("action", choices=("publish", "inventory"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--scope")
    parser.add_argument("--member", action="append", default=[])
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--max-archives", type=int, default=1)
    try:
        args = parser.parse_args(argv)
        if args.action == "publish":
            if args.source is None or args.scope is not None or args.member or args.commit:
                raise IntakeError("invalid_arguments")
            receipt = publish_archive(args.root,args.source)
            report = {"status":receipt.status, "archive_bytes":receipt.bytes, "production_enabled":False}
        else:
            if args.source is not None or args.scope is None:
                raise IntakeError("invalid_arguments")
            receipts = inventory_ready(args.root,scope=args.scope,members=args.member,
                                       commit=args.commit,max_archives=args.max_archives)
            report = {
                "status":"preview" if not args.commit else "committed_inventory_only",
                "runs":[{
                    "status":r.status, "added":r.added, "changed":r.changed,
                    "unchanged":r.unchanged, "revision_only":r.revision_only,
                    "absent_not_deleted":r.absent_not_deleted,
                    "quarantined_slots":r.quarantined_slots,
                    "unverified_child_links":r.unverified_child_links,
                    "unsupported_parts":r.unsupported_parts,
                    "retained_messages":r.retained_messages,
                } for r in receipts],
                "generation_jobs_queued":0,
                "production_enabled":False,
            }
    except (IntakeError, MemoryError) as exc:
        code = str(exc) if isinstance(exc, IntakeError) else "memory_limit"
        report = {"status":"error","error_code":code,"production_enabled":False}
        print(json.dumps(report,sort_keys=True))
        return 2
    print(json.dumps(report,sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
