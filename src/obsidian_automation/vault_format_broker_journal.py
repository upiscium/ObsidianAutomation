"""#472 D1/B1/B1 — UNWIRED Broker-private durable state mechanics.

This is NOT a Broker service, credential issuer, authenticated Owner RPC,
Provider finality verifier, Writer/PUT/HTTP path, or executable security
boundary. Only trusted Broker-local control flow may use its mutators.
External OS/Owner authority and Broker-exclusive egress are separately NYI.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
from typing import Iterator
from urllib.parse import quote

from .vault_format_broker_observation import (
    BrokerCredentialObservation, BrokerFinalityObservation, _attempt_key,
)
from .vault_format_effect_journal import (
    EffectAttempt, _FD_DISCOVERY, _process_regular_fds,
    _private_directory, _VerifiedSqliteConnection, JournalDenied,
)

_SHA = frozenset("0123456789abcdef")
_STATES = frozenset(("pre_dispatch", "dispatch_possible", "unconfirmed", "verified_no_effect"))


class BrokerJournalDenied(ValueError):
    """Fixed error codes without filesystem/credential/provider details."""


def _digest(v: object) -> str:
    return hashlib.sha256(json.dumps(
        v, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")).hexdigest()


def _sha(v: object) -> bool:
    return type(v) is str and len(v) == 64 and all(c in _SHA for c in v)


def _check_name(v: object, length: int) -> bool:
    return type(v) is str and len(v) == length and all(c in _SHA for c in v)


class BrokerPrivateJournal:
    """Broker UID-local SQL state. Untrusted RPC access is NOT provided.

    A private 0700 directory must already exist. Bootstrap explicitly creates
    separate pinned 0600 DB/identity files. This class cannot prove that a
    real Broker holds Provider credentials or that Owner/Worker are isolated.
    """

    FILE = "broker-operation-state.sqlite3"
    IDENTITY = "broker-operation-state.identity"

    def __init__(self, state_dir: Path, *, bootstrap: bool = False) -> None:
        if os.geteuid() == 0:
            raise BrokerJournalDenied("broker_root_disallowed")
        self._pid = os.getpid()
        self._uid = os.geteuid()
        self._boot_epoch: int | None = None
        self._state_dir = state_dir
        try:
            self._state_inode = _private_directory(state_dir)
        except JournalDenied:
            raise BrokerJournalDenied("broker_state_unsafe") from None
        self._db_path = state_dir / self.FILE
        self._identity_path = state_dir / self.IDENTITY
        if bootstrap:
            self._bootstrap_new()
        self._pin_existing()

    def _require_process(self) -> None:
        if os.getpid() != self._pid or os.geteuid() != self._uid:
            raise BrokerJournalDenied("broker_process_mismatch")

    def _file(self, p: Path) -> tuple[int, int]:
        try:
            st = p.lstat()
        except FileNotFoundError:
            raise BrokerJournalDenied("broker_file_missing") from None
        except OSError:
            raise BrokerJournalDenied("broker_file_unsafe") from None
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != self._uid
                or st.st_nlink != 1 or stat.S_IMODE(st.st_mode) != 0o600):
            raise BrokerJournalDenied("broker_file_unsafe")
        return (st.st_dev, st.st_ino)

    def _directory(self) -> None:
        try:
            inode = _private_directory(self._state_dir)
        except JournalDenied:
            raise BrokerJournalDenied("broker_state_unsafe") from None
        if inode != self._state_inode:
            raise BrokerJournalDenied("broker_state_unsafe")

    @staticmethod
    def _schema(db: sqlite3.Connection) -> None:
        db.execute("""CREATE TABLE broker_identity (
            id INTEGER PRIMARY KEY CHECK(id=1), binding TEXT NOT NULL)""")
        db.execute("""CREATE TABLE broker_gate (
            id INTEGER PRIMARY KEY CHECK(id=1),
            boot_epoch INTEGER NOT NULL CHECK(boot_epoch>=0),
            boot_reconciled INTEGER NOT NULL CHECK(boot_reconciled IN (0,1)),
            state TEXT NOT NULL CHECK(state IN ('closed','open')),
            reason TEXT NOT NULL CHECK(reason IN ('initial','restart','off')),
            receipt_seq INTEGER NOT NULL CHECK(receipt_seq>=0))""")
        db.execute("""INSERT INTO broker_gate VALUES(1,0,0,'closed','initial',0)""")
        db.execute("""CREATE TABLE broker_account (
            account TEXT PRIMARY KEY,
            generation TEXT NOT NULL,
            credential_epoch INTEGER NOT NULL CHECK(credential_epoch>=0),
            revision TEXT NOT NULL,
            phase TEXT NOT NULL CHECK(phase IN ('ready','rotation')))""")
        db.execute("""CREATE TABLE broker_attempt (
            run_id TEXT NOT NULL, operation_id TEXT NOT NULL,
            account TEXT NOT NULL, target TEXT NOT NULL,
            attempt_digest TEXT NOT NULL, credential_generation TEXT NOT NULL,
            credential_epoch INTEGER NOT NULL, broker_boot_epoch INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN
               ('pre_dispatch','dispatch_possible','unconfirmed','verified_no_effect')),
            receipt_seq INTEGER, receipt_digest TEXT,
            PRIMARY KEY(run_id,operation_id),
            FOREIGN KEY(account) REFERENCES broker_account(account))""")
        db.execute("""CREATE TABLE broker_resource (
            account TEXT NOT NULL, target TEXT NOT NULL,
            run_id TEXT NOT NULL, operation_id TEXT NOT NULL,
            PRIMARY KEY(account,target),
            FOREIGN KEY(run_id,operation_id)
                REFERENCES broker_attempt(run_id,operation_id))""")
        db.execute("""CREATE TABLE broker_account_quarantine (
            account TEXT PRIMARY KEY,
            reason TEXT NOT NULL CHECK(reason='dispatch_may_have_occurred'))""")

    def _bootstrap_new(self) -> None:
        self._require_process()
        self._directory()
        if any(p.exists() or p.is_symlink() for p in
               (self._identity_path, self._db_path)):
            raise BrokerJournalDenied("broker_already_provisioned")
        token = secrets.token_hex(32)
        try:
            for p, data in ((self._identity_path, (token + "\n").encode()),
                            (self._db_path, b"")):
                fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
                try:
                    if data:
                        if os.write(fd, data) != len(data):
                            raise OSError("short metadata write")
                    os.fsync(fd)
                finally:
                    os.close(fd)
            self._fsync_dir()
            db = sqlite3.connect(self._db_path, isolation_level=None, timeout=1)
            try:
                db.execute("PRAGMA synchronous=FULL")
                if db.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                    raise BrokerJournalDenied("broker_journal_mode_unsafe")
                db.execute("BEGIN IMMEDIATE")
                self._schema(db)
                db.execute("INSERT INTO broker_identity VALUES(1,?)", (token,))
                db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise
            finally:
                db.close()
            self._fsync_dir()
        except (OSError, sqlite3.Error):
            raise BrokerJournalDenied("broker_state_unavailable") from None

    def _fsync_dir(self) -> None:
        fd = os.open(self._state_dir, os.O_RDONLY | os.O_DIRECTORY
                     | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _pin_existing(self) -> None:
        with _FD_DISCOVERY:
            self._directory()
            self._identity_inode = self._file(self._identity_path)
            self._db_inode = self._file(self._db_path)
            fd = -1
            try:
                fd = os.open(self._identity_path,
                             os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
                if (os.fstat(fd).st_dev, os.fstat(fd).st_ino) != self._identity_inode:
                    raise BrokerJournalDenied("broker_identity_changed")
                raw = os.read(fd, 128)
                if (len(raw) != 65 or raw[-1:] != b"\n"
                        or not _sha(raw[:-1].decode("ascii"))):
                    raise BrokerJournalDenied("broker_identity_invalid")
                self._binding = raw[:-1].decode("ascii")
            except (OSError, UnicodeError):
                raise BrokerJournalDenied("broker_identity_invalid") from None
            finally:
                if fd >= 0:
                    os.close(fd)
            conn = self._open()
            conn.close()

    def _verify_fd(self, db: sqlite3.Connection) -> None:
        fd = getattr(db, "_journal_fd", None)
        if type(fd) is not int:
            raise BrokerJournalDenied("broker_fd_unverifiable")
        try:
            st = os.fstat(fd)
        except OSError:
            raise BrokerJournalDenied("broker_fd_unverifiable") from None
        if not stat.S_ISREG(st.st_mode) or (st.st_dev, st.st_ino) != self._db_inode:
            raise BrokerJournalDenied("broker_open_inode_mismatch")
        self._directory()
        if (self._file(self._db_path) != self._db_inode
                or self._file(self._identity_path) != self._identity_inode):
            raise BrokerJournalDenied("broker_identity_changed")

    def _open(self) -> sqlite3.Connection:
        self._require_process()
        db = None
        try:
            self._directory()
            if (self._file(self._db_path) != self._db_inode
                    or self._file(self._identity_path) != self._identity_inode):
                raise BrokerJournalDenied("broker_identity_changed")
            uri = "file:" + quote(str(self._db_path), safe="/") + "?mode=rw"
            with _FD_DISCOVERY:
                try:
                    before = _process_regular_fds()
                except JournalDenied:
                    raise BrokerJournalDenied("broker_fd_unverifiable") from None
                db = sqlite3.connect(uri, uri=True, timeout=1,
                                     isolation_level=None,
                                     factory=_VerifiedSqliteConnection)
                db.execute("PRAGMA busy_timeout=1000")
                db.execute("PRAGMA synchronous=FULL")
                if db.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                    raise BrokerJournalDenied("broker_journal_mode_unsafe")
                db.execute("PRAGMA foreign_keys=ON")
                row = db.execute("SELECT binding FROM broker_identity WHERE id=1").fetchone()
                if row is None or row[0] != self._binding:
                    raise BrokerJournalDenied("broker_identity_changed")
                try:
                    after = _process_regular_fds()
                except JournalDenied:
                    raise BrokerJournalDenied("broker_fd_unverifiable") from None
                if any(fd in after and after[fd] != inode
                       for fd, inode in before.items()):
                    raise BrokerJournalDenied("broker_fd_unverifiable")
                new_fds = [(fd, inode) for fd, inode in after.items()
                           if fd not in before]
                candidates = []
                for fd, inode in new_fds:
                    try:
                        name = os.readlink(f"/proc/self/fd/{fd}")
                    except OSError:
                        raise BrokerJournalDenied("broker_fd_unverifiable") from None
                    if name in (str(self._db_path) + "-journal",
                                str(self._db_path) + "-journal (deleted)"):
                        continue
                    candidates.append((fd, inode))
                if len(candidates) != 1:
                    raise BrokerJournalDenied("broker_fd_unverifiable")
                fd, inode = candidates[0]
                db._journal_fd = fd
                if inode != self._db_inode:
                    raise BrokerJournalDenied("broker_open_inode_mismatch")
            self._verify_fd(db)
            return db
        except BrokerJournalDenied:
            if db is not None:
                db.close()
            raise
        except (OSError, sqlite3.Error):
            if db is not None:
                db.close()
            raise BrokerJournalDenied("broker_state_unavailable") from None

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        db = self._open()
        try:
            self._verify_fd(db)
            db.execute("BEGIN IMMEDIATE")
            self._verify_fd(db)
            yield db
            self._verify_fd(db)
            db.execute("COMMIT")
            self._verify_fd(db)
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
        gate = db.execute("""SELECT boot_epoch,boot_reconciled,state,reason,
            receipt_seq FROM broker_gate WHERE id=1""").fetchone()
        if gate is None:
            raise BrokerJournalDenied("broker_state_unavailable")
        return gate

    def _current_gate(self, db: sqlite3.Connection) -> tuple:
        epoch, reconciled, state, reason, receipt_seq = self._gate(db)
        if self._boot_epoch is None or epoch != self._boot_epoch:
            raise BrokerJournalDenied("broker_boot_stale")
        return epoch, reconciled, state, reason, receipt_seq

    def start_boot(self) -> int:
        """Trusted Broker-local boot barrier, never called on instantiation."""
        self._require_process()
        try:
            with self._transaction() as db:
                epoch, reconciled, state, reason, _ = self._gate(db)
                if epoch > 0 and reconciled != 1:
                    raise BrokerJournalDenied("broker_boot_pending")
                reason = "off" if reason == "off" else "restart"
                db.execute("""UPDATE broker_gate SET boot_epoch=?,boot_reconciled=0,
                    state='closed',reason=? WHERE id=1""", (epoch + 1, reason))
                db.execute("""UPDATE broker_attempt SET status='unconfirmed'
                    WHERE status='dispatch_possible'""")
            self._boot_epoch = epoch + 1
            return self._boot_epoch
        except sqlite3.Error:
            raise BrokerJournalDenied("broker_state_unavailable") from None

    def reconcile_boot(self) -> bool:
        """Only local state; externally authenticated provider scan is NYI."""
        try:
            with self._transaction() as db:
                epoch, reconciled, state, reason, _ = self._current_gate(db)
                outstanding = db.execute("""SELECT count(*) FROM broker_attempt
                    WHERE status IN ('dispatch_possible','unconfirmed')""").fetchone()[0]
                unknown = db.execute("SELECT count(*) FROM broker_account_quarantine").fetchone()[0]
                if outstanding or unknown:
                    return False
                if reason == "off":
                    db.execute("UPDATE broker_gate SET boot_reconciled=1 WHERE id=1")
                    return False
                if reason != "restart":
                    return False
                db.execute("""UPDATE broker_gate SET boot_reconciled=1,
                    state='open' WHERE id=1""")
                return True
        except sqlite3.Error:
            raise BrokerJournalDenied("broker_state_unavailable") from None

    def provision_generation_metadata(self, account: str, generation: str) -> None:
        """Explicit synthetic metadata registration, NOT credential install."""
        if not _sha(account) or not _sha(generation):
            raise BrokerJournalDenied("broker_generation_invalid")
        try:
            with self._transaction() as db:
                _, reconciled, state, reason, _ = self._current_gate(db)
                if state != "open" or reconciled != 1:
                    raise BrokerJournalDenied("broker_admission_closed")
                rev = _digest(["broker-generation-metadata-v1", self._binding,
                               account, generation, 0])
                try:
                    db.execute("""INSERT INTO broker_account
                        VALUES(?,?,0,?,'ready')""", (account, generation, rev))
                except sqlite3.IntegrityError:
                    raise BrokerJournalDenied("broker_account_exists") from None
        except sqlite3.Error:
            raise BrokerJournalDenied("broker_state_unavailable") from None

    def observe_credential(self, account: str) -> BrokerCredentialObservation:
        if not _sha(account):
            raise BrokerJournalDenied("broker_account_invalid")
        db = self._open()
        try:
            db.execute("BEGIN")
            row = db.execute("""SELECT generation,credential_epoch,revision
                FROM broker_account WHERE account=?""", (account,)).fetchone()
            self._verify_fd(db)
            db.execute("COMMIT")
            if row is None:
                raise BrokerJournalDenied("broker_account_unknown")
            return BrokerCredentialObservation(
                account_binding_sha256=account, credential_generation=row[0],
                credential_epoch=row[1], broker_ledger_revision=row[2],
            )
        except sqlite3.Error:
            raise BrokerJournalDenied("broker_state_unavailable") from None
        finally:
            db.close()

    @staticmethod
    def _checked_attempt(attempt: EffectAttempt) -> tuple[EffectAttempt, str]:
        try:
            return _attempt_key(attempt)
        except Exception:
            raise BrokerJournalDenied("broker_attempt_invalid") from None

    @staticmethod
    def _row(db: sqlite3.Connection, attempt: EffectAttempt) -> tuple | None:
        return db.execute("""SELECT account,target,attempt_digest,
            credential_generation,credential_epoch,broker_boot_epoch,
            status,receipt_seq,receipt_digest FROM broker_attempt
            WHERE run_id=? AND operation_id=?""",
            (attempt.run_id, attempt.operation_id)).fetchone()

    @staticmethod
    def _match(row: tuple | None, record: EffectAttempt, digest: str) -> None:
        if row is None:
            raise BrokerJournalDenied("broker_attempt_unknown")
        if (row[0] != record.account_binding_sha256 or row[1] != record.target_path
                or row[2] != digest or row[3] != record.credential_generation
                or row[4] != record.claim_credential_epoch):
            raise BrokerJournalDenied("broker_attempt_mismatch")

    def fence_pre_dispatch(self, attempt: EffectAttempt) -> None:
        """One-shot SQL fence only; NOT an Owner-authorized dispatch permit."""
        record, sha = self._checked_attempt(attempt)
        try:
            with self._transaction() as db:
                epoch, reconciled, state, _, _ = self._current_gate(db)
                if not reconciled or state != "open":
                    raise BrokerJournalDenied("broker_admission_closed")
                account = db.execute("""SELECT generation,credential_epoch,phase
                    FROM broker_account WHERE account=?""",
                    (record.account_binding_sha256,)).fetchone()
                if account is None or account[2] != "ready":
                    raise BrokerJournalDenied("broker_account_unavailable")
                if (account[0] != record.credential_generation
                        or account[1] != record.claim_credential_epoch):
                    raise BrokerJournalDenied("broker_generation_stale")
                if db.execute("SELECT 1 FROM broker_account_quarantine WHERE account=?",
                              (record.account_binding_sha256,)).fetchone():
                    raise BrokerJournalDenied("broker_account_unconfirmed")
                if self._row(db, record) is not None:
                    raise BrokerJournalDenied("broker_attempt_spent")
                if db.execute("""SELECT 1 FROM broker_resource WHERE account=?
                    AND target=?""", (record.account_binding_sha256,
                                     record.target_path)).fetchone():
                    raise BrokerJournalDenied("broker_target_conflict")
                db.execute("""INSERT INTO broker_attempt VALUES
                    (?,?,?,?,?,?,?,?,'pre_dispatch',NULL,NULL)""",
                    (record.run_id, record.operation_id, record.account_binding_sha256,
                     record.target_path, sha, record.credential_generation,
                     record.claim_credential_epoch, epoch))
                db.execute("""INSERT INTO broker_resource VALUES(?,?,?,?)""",
                           (record.account_binding_sha256, record.target_path,
                            record.run_id, record.operation_id))
        except sqlite3.Error:
            raise BrokerJournalDenied("broker_state_unavailable") from None

    def mark_dispatch_possible(self, attempt: EffectAttempt) -> None:
        """Durably quarantine BEFORE any future provider transport is allowed.

        This method does NOT send a request, create credentials, grant a
        permit or prove the Owner's admission/fenced state. Actual dispatch
        must be a separate approved phase with Broker-only network egress.
        """
        record, sha = self._checked_attempt(attempt)
        try:
            with self._transaction() as db:
                epoch, reconciled, state, _, _ = self._current_gate(db)
                row = self._row(db, record)
                self._match(row, record, sha)
                if not reconciled or state != "open" or row[6] != "pre_dispatch":
                    raise BrokerJournalDenied("broker_dispatch_denied")
                if row[5] != epoch:
                    raise BrokerJournalDenied("broker_attempt_stale_boot")
                acct = db.execute("""SELECT generation,credential_epoch,phase
                    FROM broker_account WHERE account=?""",
                    (record.account_binding_sha256,)).fetchone()
                if (acct is None or acct[2] != "ready"
                        or acct[0] != record.credential_generation
                        or acct[1] != record.claim_credential_epoch):
                    raise BrokerJournalDenied("broker_generation_stale")
                # An earlier pre-fenced operation on a different target may
                # have already made this whole account uncertain. Recheck
                # under the SAME BEGIN IMMEDIATE transaction as the state
                # transition; the earlier fence-time check is insufficient.
                if db.execute("""SELECT 1 FROM broker_account_quarantine
                    WHERE account=?""",
                    (record.account_binding_sha256,)).fetchone():
                    raise BrokerJournalDenied("broker_account_unconfirmed")
                # No OR IGNORE: a competing/quarantined account may never
                # silently authorize a second possible dispatch.
                db.execute("""INSERT INTO broker_account_quarantine
                    VALUES(?,'dispatch_may_have_occurred')""",
                    (record.account_binding_sha256,))
                db.execute("""UPDATE broker_attempt SET status='dispatch_possible'
                    WHERE run_id=? AND operation_id=? AND status='pre_dispatch'""",
                    (record.run_id, record.operation_id))
        except sqlite3.Error:
            raise BrokerJournalDenied("broker_state_unavailable") from None

    def cancel_never_dispatched(self, attempt: EffectAttempt) -> BrokerFinalityObservation:
        """SQL proof of no dispatch *through this source-only state machine*.

        Cannot cancel if dispatch_possible ever committed. A real Broker
        needs exclusive provider egress and a durable OS session guarantee.
        """
        record, sha = self._checked_attempt(attempt)
        try:
            with self._transaction() as db:
                epoch, _, _, _, seq = self._current_gate(db)
                row = self._row(db, record)
                self._match(row, record, sha)
                if row[6] != "pre_dispatch":
                    raise BrokerJournalDenied("broker_no_dispatch_unverified")
                seq += 1
                if seq >= 1 << 62:
                    raise BrokerJournalDenied("broker_sequence_exhausted")
                proof = _digest(["broker-never-dispatched-v1", self._binding,
                                 record.run_id, record.operation_id, sha, seq])
                db.execute("UPDATE broker_gate SET receipt_seq=? WHERE id=1",
                           (seq,))
                db.execute("""UPDATE broker_attempt SET status='verified_no_effect',
                    receipt_seq=?,receipt_digest=?
                    WHERE run_id=? AND operation_id=? AND status='pre_dispatch'""",
                    (seq, proof, record.run_id, record.operation_id))
                res = db.execute("""DELETE FROM broker_resource WHERE account=?
                    AND target=? AND run_id=? AND operation_id=?""",
                    (record.account_binding_sha256, record.target_path,
                     record.run_id, record.operation_id))
                if res.rowcount != 1:
                    raise BrokerJournalDenied("broker_resource_unavailable")
            return BrokerFinalityObservation(
                run_id=record.run_id, operation_id=record.operation_id,
                account_binding_sha256=record.account_binding_sha256,
                attempt_sha256=sha, disposition="verified_no_effect",
                broker_journal_seq=seq, receipt_sha256=proof,
            )
        except sqlite3.Error:
            raise BrokerJournalDenied("broker_state_unavailable") from None

    def mark_uncertain(self, attempt: EffectAttempt) -> None:
        """An uncertain effect stays quarantined permanently without proof."""
        record, sha = self._checked_attempt(attempt)
        try:
            with self._transaction() as db:
                self._current_gate(db)
                row = self._row(db, record)
                self._match(row, record, sha)
                if row[6] not in ("dispatch_possible", "unconfirmed"):
                    raise BrokerJournalDenied("broker_uncertainty_denied")
                db.execute("""UPDATE broker_attempt SET status='unconfirmed'
                    WHERE run_id=? AND operation_id=?""",
                    (record.run_id, record.operation_id))
        except sqlite3.Error:
            raise BrokerJournalDenied("broker_state_unavailable") from None

    def observe_finality(self, attempt: EffectAttempt) -> BrokerFinalityObservation:
        """Return a read-only A0-compatible report; NOT a Writer permit."""
        record, sha = self._checked_attempt(attempt)
        db = self._open()
        try:
            db.execute("BEGIN")
            row = self._row(db, record)
            self._verify_fd(db)
            db.execute("COMMIT")
            self._match(row, record, sha)
            pending = row[6] != "verified_no_effect"
            return BrokerFinalityObservation(
                run_id=record.run_id, operation_id=record.operation_id,
                account_binding_sha256=record.account_binding_sha256,
                attempt_sha256=sha, disposition=("pending" if pending
                                                 else "verified_no_effect"),
                broker_journal_seq=(None if pending else row[7]),
                receipt_sha256=(None if pending else row[8]),
            )
        except sqlite3.Error:
            raise BrokerJournalDenied("broker_state_unavailable") from None
        finally:
            db.close()

    def request_off(self) -> str:
        """Commit global CLOSED, never ACK stopped for unresolved attempt."""
        try:
            with self._transaction() as db:
                self._current_gate(db)
                db.execute("UPDATE broker_gate SET state='closed',reason='off' WHERE id=1")
                active = db.execute("""SELECT count(*) FROM broker_attempt
                    WHERE status='dispatch_possible'""").fetchone()[0]
                unknown = db.execute("""SELECT count(*) FROM broker_attempt
                    WHERE status='unconfirmed'""").fetchone()[0]
                quarantined = db.execute("SELECT count(*) FROM broker_account_quarantine").fetchone()[0]
                return ("pending_inflight" if active else
                        "pending_unconfirmed" if unknown or quarantined else
                        "complete")
        except sqlite3.Error:
            raise BrokerJournalDenied("broker_state_unavailable") from None

    def freeze_generation(self, account: str) -> str:
        """Close account for separately approved rotation; NO rotation ACK."""
        if not _sha(account):
            raise BrokerJournalDenied("broker_account_invalid")
        try:
            with self._transaction() as db:
                self._current_gate(db)
                r = db.execute("UPDATE broker_account SET phase='rotation' WHERE account=?",
                               (account,))
                if r.rowcount != 1:
                    raise BrokerJournalDenied("broker_account_unknown")
                active = db.execute("""SELECT count(*) FROM broker_attempt
                    WHERE account=? AND status='dispatch_possible'""",
                    (account,)).fetchone()[0]
                unknown = db.execute("""SELECT count(*) FROM broker_account_quarantine
                    WHERE account=?""", (account,)).fetchone()[0]
                return ("pending_inflight" if active else
                        "pending_unconfirmed" if unknown else "ready_for_separate_rotation")
        except sqlite3.Error:
            raise BrokerJournalDenied("broker_state_unavailable") from None
