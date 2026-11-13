"""#472 D1/B0: UNWIRED Owner-private SQLite journal source prototype.

This module persists attempt fences, whole-Owner Off/rotation gates,
conflicting resources, and post-crash terminal outcomes. It does NOT verify
signed plans, Worker identity, Broker finality or OS network separation, and
returns NO Writer permit. Never call from untrusted IPC or activate in the
ordinary Formatter until independently approved Owner/Broker gates exist.
"""
from __future__ import annotations

import os
import re
import secrets
import sqlite3
import stat
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote
from typing import Iterator

from .vault_layout import role_folder

_ID = re.compile(r"[a-f0-9]{32}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_TERMINAL = ("verified_remote_effect", "verified_no_effect")
# sqlite3.Connection exposes no SQLite VFS database FD to Python.
# Linux /proc/self/fd is required; ambiguous or unavailable -> deny.
_FD_DISCOVERY = threading.RLock()


class _VerifiedSqliteConnection(sqlite3.Connection):
    """Captures the actually-opened SQLite DB FD, not a pathname guess."""


def _process_regular_fds() -> dict[int, tuple[int, int]]:
    """Snapshot regular-file FDs of the current process."""
    result = {}
    try:
        for name in os.listdir("/proc/self/fd"):
            if not name.isdecimal():
                continue
            fd = int(name)
            try:
                info = os.fstat(fd)
            except OSError:
                # /proc enumeration can include its own transient FD.
                continue
            if stat.S_ISREG(info.st_mode):
                result[fd] = (info.st_dev, info.st_ino)
    except OSError:
        raise JournalDenied("journal_open_inode_unverifiable") from None
    return result


class JournalDenied(ValueError):
    """Fixed denial codes; do not expose Vault contents or credentials."""


@dataclass(frozen=True, slots=True)
class EffectAttempt:
    """Exact Owner-verified input (verification is NOT implemented here)."""

    run_id: str
    operation_id: str
    plan_sha256: str
    policy_sha256: str
    source_generation: str
    account_binding_sha256: str
    provider_proof_sha256: str
    receipt_root: str
    target_path: str
    precondition_etag: str
    before_sha256: str
    after_sha256: str
    credential_generation: str
    claim_boot_epoch: int
    claim_credential_epoch: int


def _seal(attempt: EffectAttempt) -> EffectAttempt:
    if type(attempt) is not EffectAttempt:
        raise JournalDenied("attempt_invalid")
    # Detach even nominally frozen input objects; validators must not trust
    # caller-owned values after an asynchronous approval callback.
    try:
        record = EffectAttempt(*(getattr(attempt, name) for name in EffectAttempt.__dataclass_fields__))
    except (AttributeError, TypeError):
        raise JournalDenied("attempt_invalid") from None
    for name in ("run_id", "operation_id"):
        val = getattr(record, name)
        if type(val) is not str or _ID.fullmatch(val) is None:
            raise JournalDenied("attempt_invalid")
    for name in ("plan_sha256", "policy_sha256", "source_generation",
                 "account_binding_sha256", "provider_proof_sha256",
                 "before_sha256", "after_sha256", "credential_generation"):
        val = getattr(record, name)
        if type(val) is not str or _SHA.fullmatch(val) is None:
            raise JournalDenied("attempt_invalid")
    for name in ("claim_boot_epoch", "claim_credential_epoch"):
        val = getattr(record, name)
        if type(val) is not int or not 0 <= val < (1 << 62):
            raise JournalDenied("attempt_invalid")
    root = record.receipt_root
    if (type(root) is not str or not root.startswith("/") or len(root) > 4096
            or "\x00" in root or ".." in Path(root).parts
            or str(Path(root)) != root):
        raise JournalDenied("attempt_invalid")
    path = record.target_path
    if (type(path) is not str or not path.startswith(role_folder("system") + "/")
            or len(path) > 1024 or "\\" in path or "\x00" in path
            or "\n" in path or "\r" in path or path.endswith("/")
            or any(x in ("", ".", "..") for x in path.split("/"))):
        raise JournalDenied("attempt_invalid")
    etag = record.precondition_etag
    if (type(etag) is not str or not 1 <= len(etag) <= 256
            or any(ord(ch) < 32 or ord(ch) > 126 for ch in etag)):
        raise JournalDenied("attempt_invalid")
    return record


_FIELDS = tuple(EffectAttempt.__dataclass_fields__)
_ATTEMPT_COLUMNS = (",".join(_FIELDS))


def _private_directory(directory: Path) -> tuple[int, int]:
    if not isinstance(directory, Path) or not directory.is_absolute():
        raise JournalDenied("journal_directory_unsafe")
    # The root/sticky ancestor exception matches the existing durable Ledger.
    owner = os.geteuid()
    current = Path("/")
    try:
        for component in directory.parts[1:]:
            current = current / component
            info = current.lstat()
            if (not stat.S_ISDIR(info.st_mode)
                    or info.st_uid not in (0, owner)
                    or ((info.st_mode & 0o022)
                        and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX))):
                raise JournalDenied("journal_directory_unsafe")
        info = directory.lstat()
    except OSError:
        raise JournalDenied("journal_directory_unsafe") from None
    if info.st_uid != owner or stat.S_IMODE(info.st_mode) != 0o700:
        raise JournalDenied("journal_directory_unsafe")
    return info.st_dev, info.st_ino


class OwnerEffectJournal:
    """Unwired SQL mechanics. Owner auth, Broker finality and flock are NYI.

    The private DB must be explicitly provisioned, never created implicitly
    when a formerly durable path goes missing. A separately pinned identity
    file is bound to this DB; another owned 0600 DB cannot silently replace it.
    Same-UID mutation of *both* private files remains outside B0's threat model.
    """

    FILE = "formatter-effect-admission.sqlite3"
    IDENTITY = "formatter-effect-admission.identity"

    def __init__(self, state_dir: Path, *, bootstrap: bool = False) -> None:
        self._state_dir = state_dir
        self._state_inode = _private_directory(state_dir)
        self._path = state_dir / self.FILE
        self._identity_path = state_dir / self.IDENTITY
        # Bootstrap is explicitly operator-controlled, NEVER startup/recovery.
        # If one member of the pair survives, bootstrap must refuse.
        if bootstrap:
            self._bootstrap_new()
        self._pin_existing()

    @staticmethod
    def _regular_private_file(path: Path) -> tuple[int, int]:
        try:
            info = path.lstat()
        except FileNotFoundError:
            raise JournalDenied("journal_file_missing") from None
        except OSError:
            raise JournalDenied("journal_file_unsafe") from None
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise JournalDenied("journal_file_unsafe")
        return info.st_dev, info.st_ino

    def _bootstrap_new(self) -> None:
        if self._path.exists() or self._path.is_symlink() or (
            self._identity_path.exists() or self._identity_path.is_symlink()
        ):
            raise JournalDenied("journal_already_provisioned")
        token = secrets.token_hex(32)
        # Create both inodes with O_EXCL and O_NOFOLLOW. A partial bootstrap
        # intentionally leaves an incomplete pair which recovery cannot reset.
        try:
            for path, data in (
                (self._identity_path, (token + "\n").encode()),
                (self._path, b""),
            ):
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
                try:
                    if data:
                        os.write(fd, data)
                    os.fsync(fd)
                finally:
                    os.close(fd)
            dirfd = os.open(self._state_dir, os.O_RDONLY | os.O_DIRECTORY
                            | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                os.fsync(dirfd)
            finally:
                os.close(dirfd)
            db = sqlite3.connect(self._path, isolation_level=None, timeout=1)
            try:
                db.execute("PRAGMA synchronous=FULL")
                if db.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                    raise JournalDenied("journal_mode_unsafe")
                db.execute("BEGIN IMMEDIATE")
                self._schema(db)
                db.execute("INSERT INTO journal_binding VALUES (1,?)", (token,))
                db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    try:
                        db.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                raise
            finally:
                db.close()
            dirfd = os.open(self._state_dir, os.O_RDONLY | os.O_DIRECTORY
                            | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                os.fsync(dirfd)
            finally:
                os.close(dirfd)
        except (OSError, sqlite3.Error):
            raise JournalDenied("journal_unavailable") from None

    def _pin_existing(self) -> None:
        # Include identity-file opens in the same process-wide FD inventory
        # window; concurrent same-process Owner handles cannot misattribute
        # each other's transient identity descriptors to SQLite.
        with _FD_DISCOVERY:
            self._pin_existing_locked()

    def _pin_existing_locked(self) -> None:
        if _private_directory(self._state_dir) != self._state_inode:
            raise JournalDenied("journal_directory_unsafe")
        self._identity_inode = self._regular_private_file(self._identity_path)
        self._db_inode = self._regular_private_file(self._path)
        fd = -1
        try:
            fd = os.open(self._identity_path, os.O_RDONLY
                         | os.O_NOFOLLOW | os.O_CLOEXEC)
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) != self._identity_inode:
                raise JournalDenied("journal_identity_changed")
            data = os.read(fd, 128)
            if len(data) != 65 or data[-1:] != b"\n":
                raise JournalDenied("journal_identity_invalid")
            token = data[:64].decode("ascii")
            if _SHA.fullmatch(token) is None:
                raise JournalDenied("journal_identity_invalid")
            self._binding = token
        except (UnicodeError, OSError):
            raise JournalDenied("journal_identity_invalid") from None
        finally:
            if fd >= 0:
                os.close(fd)
        db = self._connect()
        db.close()

    def _connect(self) -> sqlite3.Connection:
        db = None
        try:
            if _private_directory(self._state_dir) != self._state_inode:
                raise JournalDenied("journal_directory_unsafe")
            if (self._regular_private_file(self._identity_path) != self._identity_inode
                    or self._regular_private_file(self._path) != self._db_inode):
                raise JournalDenied("journal_identity_changed")
            uri = "file:" + quote(str(self._path), safe="/") + "?mode=rw"
            # Path lstat misses swap -> SQLite open alternate -> swap back.
            # Observe the ACTUAL new regular-file FD SQLite opened.
            with _FD_DISCOVERY:
                before = _process_regular_fds()
                db = sqlite3.connect(
                    uri, uri=True, timeout=1, isolation_level=None,
                    factory=_VerifiedSqliteConnection,
                )
                db.execute("PRAGMA busy_timeout=1000")
                db.execute("PRAGMA synchronous=FULL")
                if db.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                    raise JournalDenied("journal_mode_unsafe")
                db.execute("PRAGMA foreign_keys=ON")
                row = db.execute("SELECT binding FROM journal_binding WHERE id=1").fetchone()
                if row is None or row[0] != self._binding:
                    raise JournalDenied("journal_identity_changed")
                after = _process_regular_fds()
                if any(fd in after and after[fd] != inode
                       for fd, inode in before.items()):
                    raise JournalDenied("journal_open_inode_unverifiable")
                opened = [(fd, inode) for fd, inode in after.items()
                          if fd not in before]
                main_files = []
                for fd, inode in opened:
                    try:
                        target = os.readlink(f"/proc/self/fd/{fd}")
                    except OSError:
                        raise JournalDenied("journal_open_inode_unverifiable") from None
                    # Another current Owner transaction can transiently hold
                    # the *separate* SQLite rollback-journal FD. Ignore only
                    # its exactly named -journal companion, never an unknown
                    # regular FD or a second candidate main DB.
                    if target in (str(self._path) + "-journal",
                                  str(self._path) + "-journal (deleted)"):
                        continue
                    main_files.append((fd, inode))
                if len(main_files) != 1:
                    raise JournalDenied("journal_open_inode_unverifiable")
                fd, inode = main_files[0]
                db._journal_fd = fd
                if inode != self._db_inode:
                    raise JournalDenied("journal_open_inode_mismatch")
            self._verify_connection_file(db)
            return db
        except JournalDenied:
            if db is not None:
                db.close()
            raise
        except (OSError, sqlite3.Error):
            if db is not None:
                db.close()
            raise JournalDenied("journal_unavailable") from None

    def _verify_connection_file(self, db: sqlite3.Connection) -> None:
        """Check actual opened SQLite FD and pinned canonical names.

        Repeat before/after each SQL transaction. A compromised same-UID
        process replacing all of the trusted state remains outside B0.
        """
        fd = getattr(db, "_journal_fd", None)
        if type(fd) is not int:
            raise JournalDenied("journal_open_inode_unverifiable")
        try:
            info = os.fstat(fd)
        except OSError:
            raise JournalDenied("journal_open_inode_unverifiable") from None
        if (not stat.S_ISREG(info.st_mode)
                or (info.st_dev, info.st_ino) != self._db_inode):
            raise JournalDenied("journal_open_inode_mismatch")
        if (_private_directory(self._state_dir) != self._state_inode
                or self._regular_private_file(self._identity_path) != self._identity_inode
                or self._regular_private_file(self._path) != self._db_inode):
            raise JournalDenied("journal_identity_changed")

    @staticmethod
    def _schema(db: sqlite3.Connection) -> None:
        try:
            db.execute("""CREATE TABLE journal_binding (
                id INTEGER PRIMARY KEY CHECK(id=1),
                binding TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS admission_gate (
                id INTEGER PRIMARY KEY CHECK(id=1),
                state TEXT NOT NULL CHECK(state IN ('open','closed')),
                reason TEXT NOT NULL CHECK(reason IN
                    ('initial','restart','off','rotation')),
                boot_epoch INTEGER NOT NULL CHECK(boot_epoch>=0),
                boot_reconciled INTEGER NOT NULL CHECK(boot_reconciled IN (0,1)),
                credential_generation TEXT NOT NULL,
                credential_epoch INTEGER NOT NULL CHECK(credential_epoch>=0),
                last_rotation_epoch INTEGER NOT NULL,
                rotation_target TEXT)""")
            db.execute("""INSERT OR IGNORE INTO admission_gate VALUES
                (1,'closed','initial',0,0,?,0,-1,NULL)""", ("0" * 64,))
            db.execute(f"""CREATE TABLE IF NOT EXISTS effect_attempt (
                {", ".join(f"{field} TEXT NOT NULL" if field not in ("claim_boot_epoch","claim_credential_epoch") else f"{field} INTEGER NOT NULL" for field in _FIELDS)},
                status TEXT NOT NULL CHECK(status IN
                    ('active','unconfirmed','verified_remote_effect','verified_no_effect')),
                PRIMARY KEY(run_id,operation_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS resource_conflict (
                account_binding_sha256 TEXT NOT NULL,
                target_path TEXT NOT NULL,
                run_id TEXT NOT NULL,
                operation_id TEXT NOT NULL,
                PRIMARY KEY(account_binding_sha256,target_path))""")
            db.execute("""CREATE TABLE IF NOT EXISTS account_quarantine (
                account_binding_sha256 TEXT PRIMARY KEY,
                reason TEXT NOT NULL CHECK(reason='unconfirmed_remote_effect'))""")
        except sqlite3.Error:
            raise JournalDenied("journal_unavailable") from None

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        db = self._connect()
        try:
            self._verify_connection_file(db)
            db.execute("BEGIN IMMEDIATE")
            self._verify_connection_file(db)
            yield db
            self._verify_connection_file(db)
            db.execute("COMMIT")
            # If postcommit verification fails, acknowledge NO success.
            self._verify_connection_file(db)
        except BaseException:
            if db.in_transaction:
                try:
                    db.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise
        finally:
            db.close()

    @staticmethod
    def _gate(db: sqlite3.Connection) -> tuple:
        row = db.execute("""SELECT state,reason,boot_epoch,boot_reconciled,
                credential_generation,credential_epoch,last_rotation_epoch,
                rotation_target FROM admission_gate WHERE id=1""").fetchone()
        if row is None:
            raise JournalDenied("journal_unavailable")
        return row

    @staticmethod
    def _counts(db: sqlite3.Connection) -> tuple[int, int]:
        active = db.execute("SELECT count(*) FROM effect_attempt WHERE status='active'").fetchone()[0]
        pending = db.execute("""SELECT count(*) FROM effect_attempt WHERE status='unconfirmed'""").fetchone()[0]
        resources = db.execute("SELECT count(*) FROM resource_conflict").fetchone()[0]
        accounts = db.execute("SELECT count(*) FROM account_quarantine").fetchone()[0]
        return active, pending + resources + accounts

    @staticmethod
    def _pending(active: int, unresolved: int) -> str:
        return ("pending_inflight" if active else
                "pending_unconfirmed" if unresolved else "complete")

    def status(self) -> dict:
        db = self._connect()
        try:
            db.execute("BEGIN")
            row = self._gate(db)
            active, unresolved = self._counts(db)
            self._verify_connection_file(db)
            db.execute("COMMIT")
            self._verify_connection_file(db)
            return dict(state=row[0], reason=row[1], boot_epoch=row[2],
                        boot_reconciled=bool(row[3]),
                        credential_generation=row[4], credential_epoch=row[5],
                        last_rotation_epoch=row[6], rotation_target=row[7],
                        active=active, outstanding=unresolved)
        except sqlite3.Error:
            raise JournalDenied("journal_unavailable") from None
        finally:
            db.close()

    def owner_start_boot(self) -> int:
        """Explicit Owner lifecycle boundary. NOT called on handle creation."""
        try:
            with self._transaction() as db:
                _, reason, epoch, reconciled, *_ = self._gate(db)
                if epoch > 0 and reconciled != 1:
                    raise JournalDenied("boot_already_pending")
                reason = reason if reason in ("off", "rotation") else "restart"
                db.execute("""UPDATE admission_gate SET state='closed',
                    reason=?,boot_epoch=?,boot_reconciled=0 WHERE id=1""",
                    (reason, epoch + 1))
                db.execute("""UPDATE effect_attempt SET status='unconfirmed'
                    WHERE status='active'""")
                return epoch + 1
        except sqlite3.Error:
            raise JournalDenied("journal_unavailable") from None

    def record_begin(self, intent: EffectAttempt) -> None:
        """One-shot durable fence only, never a permit or verified grant.

        Caller must independently establish current Claim, SSHSIG, plan,
        approved bytes, broker credential generation, Owner-held effect lock
        and credential scope. NO such checks are available in D1/B0.
        """
        record = _seal(intent)
        try:
            with self._transaction() as db:
                state, _, boot_epoch, boot_ok, cred_gen, cred_epoch, watermark, _ = self._gate(db)
                if state != "open" or boot_ok != 1:
                    raise JournalDenied("admission_closed")
                if record.claim_boot_epoch != boot_epoch:
                    raise JournalDenied("claim_stale_boot")
                if (record.claim_credential_epoch != cred_epoch
                        or record.claim_credential_epoch <= watermark
                        or record.credential_generation != cred_gen):
                    raise JournalDenied("claim_stale_generation")
                if db.execute("SELECT 1 FROM account_quarantine WHERE account_binding_sha256=?",
                              (record.account_binding_sha256,)).fetchone():
                    raise JournalDenied("account_unconfirmed")
                if db.execute("SELECT 1 FROM effect_attempt WHERE run_id=? AND operation_id=?",
                              (record.run_id, record.operation_id)).fetchone():
                    raise JournalDenied("attempt_already_fenced")
                if db.execute("""SELECT 1 FROM resource_conflict
                    WHERE account_binding_sha256=? AND target_path=?""",
                    (record.account_binding_sha256, record.target_path)).fetchone():
                    raise JournalDenied("resource_unconfirmed")
                db.execute(f"""INSERT INTO effect_attempt ({_ATTEMPT_COLUMNS},status)
                    VALUES ({",".join("?" for _ in _FIELDS)},'active')""",
                    tuple(getattr(record, field) for field in _FIELDS))
                db.execute("""INSERT INTO resource_conflict
                    VALUES (?,?,?,?)""",
                    (record.account_binding_sha256, record.target_path,
                     record.run_id, record.operation_id))
        except sqlite3.IntegrityError:
            raise JournalDenied("attempt_already_fenced") from None
        except sqlite3.Error:
            raise JournalDenied("journal_unavailable") from None

    def mark_uncertain(self, intent: EffectAttempt) -> None:
        """Fail-closed uncertain attempt; NEVER rearm a terminal outcome."""
        record = _seal(intent)
        try:
            with self._transaction() as db:
                result = db.execute("""UPDATE effect_attempt SET status='unconfirmed'
                    WHERE run_id=? AND operation_id=? AND status IN ('active','unconfirmed')
                    AND """ + " AND ".join(f"{field}=?" for field in _FIELDS[2:]),
                    (record.run_id, record.operation_id,
                     *(getattr(record, key) for key in _FIELDS[2:])))
                if result.rowcount != 1:
                    raise JournalDenied("attempt_not_pending")
        except sqlite3.Error:
            raise JournalDenied("journal_unavailable") from None

    def record_verified_outcome(self, intent: EffectAttempt, *,
                                disposition: str) -> None:
        """Owner journal mechanics only; authenticated Broker proof is NYI.

        This method cannot validate a provider's finality. The caller MUST
        provide separately audited, durable operation-specific evidence
        BEFORE calling. It MUST NOT be exposed over Worker IPC.
        """
        record = _seal(intent)
        if type(disposition) is not str or disposition not in _TERMINAL:
            raise JournalDenied("disposition_invalid")
        try:
            with self._transaction() as db:
                row = db.execute("""SELECT status FROM effect_attempt
                    WHERE run_id=? AND operation_id=? AND """
                    + " AND ".join(f"{field}=?" for field in _FIELDS[2:]),
                    (record.run_id, record.operation_id,
                     *(getattr(record, key) for key in _FIELDS[2:]))).fetchone()
                if row is None or row[0] not in ("active", "unconfirmed"):
                    raise JournalDenied("attempt_not_pending")
                key = (record.account_binding_sha256, record.target_path,
                       record.run_id, record.operation_id)
                result = db.execute("""DELETE FROM resource_conflict
                    WHERE account_binding_sha256=? AND target_path=?
                    AND run_id=? AND operation_id=?""", key)
                if result.rowcount != 1:
                    raise JournalDenied("resource_missing")
                db.execute("""UPDATE effect_attempt SET status=?
                    WHERE run_id=? AND operation_id=?""",
                    (disposition, record.run_id, record.operation_id))
        except sqlite3.Error:
            raise JournalDenied("journal_unavailable") from None

    def quarantine_account(self, account_binding_sha256: str) -> None:
        if type(account_binding_sha256) is not str or _SHA.fullmatch(account_binding_sha256) is None:
            raise JournalDenied("account_invalid")
        try:
            with self._transaction() as db:
                db.execute("""INSERT OR IGNORE INTO account_quarantine
                    VALUES (?,'unconfirmed_remote_effect')""", (account_binding_sha256,))
        except sqlite3.Error:
            raise JournalDenied("journal_unavailable") from None

    def release_account_after_verified_finality(self, account_binding_sha256: str) -> None:
        """Persistence mechanism only. Account-scope finality verifier NYI."""
        if type(account_binding_sha256) is not str or _SHA.fullmatch(account_binding_sha256) is None:
            raise JournalDenied("account_invalid")
        try:
            with self._transaction() as db:
                if db.execute("""SELECT 1 FROM resource_conflict
                    WHERE account_binding_sha256=?""", (account_binding_sha256,)).fetchone():
                    raise JournalDenied("account_unresolved")
                result = db.execute("""DELETE FROM account_quarantine
                    WHERE account_binding_sha256=?""", (account_binding_sha256,))
                if result.rowcount != 1:
                    raise JournalDenied("account_not_quarantined")
        except sqlite3.Error:
            raise JournalDenied("journal_unavailable") from None

    def request_off(self) -> str:
        """Durably close admissions first, then report pending vs complete."""
        try:
            with self._transaction() as db:
                db.execute("""UPDATE admission_gate
                    SET state='closed', reason='off', rotation_target=NULL
                    WHERE id=1""")
                return self._pending(*self._counts(db))
        except sqlite3.Error:
            raise JournalDenied("journal_unavailable") from None

    def request_rotation(self, new_generation: str) -> str:
        if type(new_generation) is not str or _SHA.fullmatch(new_generation) is None:
            raise JournalDenied("generation_invalid")
        try:
            with self._transaction() as db:
                state, reason, _, reconciled, current, epoch, watermark, target = self._gate(db)
                if reason == "off":
                    raise JournalDenied("off_precedes_rotation")
                if new_generation == current:
                    raise JournalDenied("generation_reused")
                if reason == "rotation" and target not in (None, new_generation):
                    raise JournalDenied("rotation_already_pending")
                db.execute("""UPDATE admission_gate SET state='closed',
                    reason='rotation',rotation_target=? WHERE id=1""", (new_generation,))
                active, unresolved = self._counts(db)
                if active or unresolved or reconciled != 1:
                    return self._pending(active, unresolved) if active or unresolved else "pending_boot"
                db.execute("""UPDATE admission_gate SET credential_generation=?,
                    credential_epoch=?,last_rotation_epoch=?,
                    rotation_target=NULL WHERE id=1""",
                    (new_generation, epoch + 1, epoch))
                return "complete"
        except sqlite3.Error:
            raise JournalDenied("journal_unavailable") from None

    def complete_boot_recovery(self) -> bool:
        """Caller-proven Owner-wide boot scan is required but NOT implemented.

        B0 only checks durable state; no external Owner/Broker proof here.
        The operation must not re-open an Off/Rotation gate.
        """
        try:
            with self._transaction() as db:
                state, reason, epoch, reconciled, *_ = self._gate(db)
                active, unresolved = self._counts(db)
                if active or unresolved or epoch == 0:
                    return False
                # Verifying the Owner journal boot does NOT imply a permission
                # to reopen an explicit Off or pending Rotation gate.
                if reason in ("off", "rotation"):
                    db.execute("""UPDATE admission_gate
                        SET boot_reconciled=1 WHERE id=1""")
                    return False
                if reason != "restart":
                    return False
                db.execute("""UPDATE admission_gate SET state='open',
                    reason='restart',boot_reconciled=1 WHERE id=1""")
                return True
        except sqlite3.Error:
            raise JournalDenied("journal_unavailable") from None
