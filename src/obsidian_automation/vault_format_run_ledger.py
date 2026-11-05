"""#472 R2/B2a durable intent registry; source-only, no authority issuer.

A separately controlled operator must verify the exact intent at registration.
The registry is not connected to the running Formatter, Writer or WebUI.
Claim is one-shot, never automatically rearmed after crash or revocation.
"""
from __future__ import annotations

import os
import functools
import re
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .vault_format_writer import FormatWriteError

_ID = re.compile(r"[a-f0-9]{32}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")


@dataclass(frozen=True)
class RunIntent:
    run_id: str
    plan_sha256: str
    policy_sha256: str
    operation_id: str
    source_generation: str
    account_binding_sha256: str
    provider_proof_sha256: str
    receipt_root: str


def _check(intent: RunIntent) -> None:
    if type(intent) is not RunIntent:
        raise FormatWriteError("run_intent_invalid")
    for key in ("run_id", "operation_id"):
        v = getattr(intent, key)
        if type(v) is not str or _ID.fullmatch(v) is None:
            raise FormatWriteError("run_intent_invalid")
    for key in ("plan_sha256", "policy_sha256", "source_generation",
                "account_binding_sha256", "provider_proof_sha256"):
        v = getattr(intent, key)
        if type(v) is not str or _SHA.fullmatch(v) is None:
            raise FormatWriteError("run_intent_invalid")
    root = intent.receipt_root
    if (type(root) is not str or not root.startswith("/") or len(root) > 4096
            or "\x00" in root or ".." in Path(root).parts
            or Path(root).as_posix() != root):
        raise FormatWriteError("run_intent_invalid")


def _sealed_intent(intent: RunIntent) -> RunIntent:
    """Detach caller-owned (nominally frozen) state before any side effect.

    Even a frozen dataclass can be modified via object.__setattr__ while a
    verifier runs or after a row was selected. Never use a caller-owned object
    twice across those boundaries.
    """
    if type(intent) is not RunIntent:
        raise FormatWriteError("run_intent_invalid")
    sealed = RunIntent(
        run_id=intent.run_id, plan_sha256=intent.plan_sha256,
        policy_sha256=intent.policy_sha256, operation_id=intent.operation_id,
        source_generation=intent.source_generation,
        account_binding_sha256=intent.account_binding_sha256,
        provider_proof_sha256=intent.provider_proof_sha256,
        receipt_root=intent.receipt_root,
    )
    _check(sealed)
    return sealed


def _database_errors(method):
    @functools.wraps(method)
    def guarded(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except (OSError, sqlite3.Error):
            raise FormatWriteError("run_ledger_unavailable") from None
    return guarded


def _validate_ancestry(path: Path) -> tuple[int, int]:
    """Reject foreign-replaceable components; same-UID processes are trusted."""
    parts = path.parts
    if not parts or parts[0] != "/":
        raise FormatWriteError("run_ledger_state_unsafe")
    current = Path("/")
    owner = os.geteuid()
    try:
        for item in parts[1:]:
            current = current / item
            info = current.lstat()
            if (not stat.S_ISDIR(info.st_mode)
                    or info.st_uid not in (0, owner)
                    or ((info.st_mode & 0o022)
                        and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX))):
                raise FormatWriteError("run_ledger_state_unsafe")
        last = path.lstat()
        return last.st_dev, last.st_ino
    except OSError:
        raise FormatWriteError("run_ledger_state_unsafe") from None


class DurableRunLedger:
    """Persist run intent; neither register nor claim is Human authorization.

    External approval verifier MUST be controlled separately and never supplied
    by HTTP callers. A permissive callback grants no legitimate authority.
    """

    def __init__(self, state_dir: Path) -> None:
        if not isinstance(state_dir, Path) or not state_dir.is_absolute():
            raise FormatWriteError("run_ledger_state_unsafe")
        try:
            info = state_dir.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o700):
                raise FormatWriteError("run_ledger_state_unsafe")
        except OSError:
            raise FormatWriteError("run_ledger_state_unsafe") from None
        self._state_inode = _validate_ancestry(state_dir)
        self._boot_reconciled = False
        self.path = state_dir / "formatter-run-intents.sqlite3"
        db = self._open()
        try:
            db.execute("""CREATE TABLE IF NOT EXISTS run_intents(
                run_id TEXT PRIMARY KEY, plan_sha TEXT NOT NULL,
                policy_sha TEXT NOT NULL, operation_id TEXT UNIQUE NOT NULL,
                source_gen TEXT NOT NULL, binding TEXT NOT NULL,
                provider_proof TEXT NOT NULL, receipt_root TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN
                    ('approved','claimed','revoked','unconfirmed')))""")
        except sqlite3.Error:
            raise FormatWriteError("run_ledger_unavailable") from None
        finally:
            db.close()

    def _open(self) -> sqlite3.Connection:
        db = None
        try:
            if _validate_ancestry(self.path.parent) != self._state_inode:
                raise FormatWriteError("run_ledger_state_unsafe")
            if self.path.is_symlink():
                raise FormatWriteError("run_ledger_state_unsafe")
            if self.path.exists():
                info = self.path.lstat()
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                        or stat.S_IMODE(info.st_mode) != 0o600):
                    raise FormatWriteError("run_ledger_state_unsafe")
            # Never change process-global umask in a multithreaded app.
            # Create a private database inode atomically before SQLite opens
            # the named database. Owner-private parent guards the path.
            if not self.path.exists():
                try:
                    fd = os.open(self.path, os.O_WRONLY | os.O_CREAT
                                 | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                except FileExistsError:
                    pass
                else:
                    os.close(fd)
            info = self.path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600):
                raise FormatWriteError("run_ledger_state_unsafe")
            db = sqlite3.connect(self.path, timeout=2, isolation_level=None)
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA busy_timeout=2000")
            if _validate_ancestry(self.path.parent) != self._state_inode:
                raise FormatWriteError("run_ledger_state_unsafe")
            return db
        except FormatWriteError:
            if db is not None:
                db.close()
            raise
        except (OSError, sqlite3.Error):
            if db is not None:
                db.close()
            raise FormatWriteError("run_ledger_unavailable") from None

    @staticmethod
    def _fields(intent: RunIntent) -> tuple[str, ...]:
        return (intent.plan_sha256, intent.policy_sha256, intent.operation_id,
                intent.source_generation, intent.account_binding_sha256,
                intent.provider_proof_sha256, intent.receipt_root)

    @staticmethod
    def _row(db: sqlite3.Connection, run_id: str):
        return db.execute(
            "SELECT plan_sha,policy_sha,operation_id,source_gen,binding,"
            "provider_proof,receipt_root,status FROM run_intents WHERE run_id=?",
            (run_id,),
        ).fetchone()

    @_database_errors
    def register(
        self, intent: RunIntent, *,
        verify_external_approval: Callable[[RunIntent], bool] | None,
    ) -> None:
        sealed = _sealed_intent(intent)
        if not callable(verify_external_approval):
            raise FormatWriteError("run_approval_missing")
        try:
            # The verifier must approve the sealed values, but may be external
            # and capable of mutating its argument. Pass a second detached
            # object so it cannot replace the row we subsequently persist.
            accepted = verify_external_approval(_sealed_intent(sealed))
        except Exception:
            raise FormatWriteError("run_approval_unverified") from None
        if accepted is not True:
            raise FormatWriteError("run_approval_unverified")
        db = self._open()
        try:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("INSERT INTO run_intents VALUES(?,?,?,?,?,?,?,?,?)",
                           (sealed.run_id, *self._fields(sealed), "approved"))
            except sqlite3.IntegrityError:
                raise FormatWriteError("run_already_registered") from None
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

    @_database_errors
    def claim(self, intent: RunIntent) -> bool:
        sealed = _sealed_intent(intent)
        db = self._open()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, sealed.run_id)
            if row is None or row[:-1] != self._fields(sealed) or row[-1] != "approved":
                db.execute("ROLLBACK")
                return False
            result = db.execute(
                "UPDATE run_intents SET status='claimed' WHERE run_id=? "
                "AND status='approved'", (sealed.run_id,),
            )
            if result.rowcount != 1:
                db.execute("ROLLBACK")
                return False
            db.execute("COMMIT")
            return True
        except BaseException:
            if db.in_transaction:
                try:
                    db.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise
        finally:
            db.close()

    @_database_errors
    def verify_claimed(self, run_id: str, plan_sha: str,
                       policy_sha: str, operation_id: str) -> bool:
        """Read-only B1 RunVerifier shape; no grant or replay is minted."""
        if (type(run_id) is not str or _ID.fullmatch(run_id) is None
                or type(operation_id) is not str or _ID.fullmatch(operation_id) is None
                or type(plan_sha) is not str or _SHA.fullmatch(plan_sha) is None
                or type(policy_sha) is not str or _SHA.fullmatch(policy_sha) is None):
            return False
        # No restarted process can treat old claims as authority before an
        # explicit recovery barrier. Every fresh ledger instance begins OFF.
        if not self._boot_reconciled:
            return False
        db = self._open()
        try:
            row = self._row(db, run_id)
            return bool(row and row[-1] == "claimed"
                        and row[0] == plan_sha and row[1] == policy_sha
                        and row[2] == operation_id)
        finally:
            db.close()

    @_database_errors
    def revoke(self, run_id: str) -> bool:
        if type(run_id) is not str or _ID.fullmatch(run_id) is None:
            raise FormatWriteError("run_intent_invalid")
        db = self._open()
        try:
            db.execute("BEGIN IMMEDIATE")
            result = db.execute(
                "UPDATE run_intents SET status='revoked' WHERE run_id=? "
                "AND status IN ('approved','claimed')", (run_id,))
            db.execute("COMMIT")
            return result.rowcount == 1
        except BaseException:
            if db.in_transaction:
                try:
                    db.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise
        finally:
            db.close()

    @_database_errors
    def mark_unconfirmed_after_restart(self) -> int:
        """Recovery is fail closed; claimed intent is never reissued."""
        db = self._open()
        try:
            db.execute("BEGIN IMMEDIATE")
            result = db.execute(
                "UPDATE run_intents SET status='unconfirmed' WHERE status='claimed'")
            db.execute("COMMIT")
            self._boot_reconciled = True
            return result.rowcount
        except BaseException:
            if db.in_transaction:
                try:
                    db.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise
        finally:
            db.close()
