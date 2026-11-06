"""#472 R2/B2b: source-only operator-side coordination prototype.

No new credentials, signer, IPC, HTTP route or runtime Writer is created.
The ordinary app still uses FormatterJob(writer=None).  The class is a
*local component* intended to run under a separately trusted owner OS
principal; same-UID synthetic tests do NOT prove that security boundary.
All cooperating effects must hold the same pre-provisioned flock inode.
"""
from __future__ import annotations

from contextlib import contextmanager
import math
import os
from pathlib import Path
import threading
from typing import Callable, Iterator

from .vault_format_run_drain import DurableRunDrain, RunDrainDenied
from .vault_format_run_ledger import (
    DurableRunLedger, RunIntent, _sealed_intent,
)
from .vault_format_signed_run import (
    SignedRunDenied, _secure_directory, receipt_root_digest,
)


class OwnerCoordinationDenied(ValueError):
    """Fixed public reason codes only; no Vault data or credentials."""


class OperatorRunCoordinator:
    """Isolated, explicitly constructed owner-side lease/ledger operations.

    This is NOT a production owner service. There is no process isolation,
    authenticated IPC, delegated issuer, or scheduler binding here. A caller
    must ensure the configured UID is a *different* trusted OS principal from
    the future Worker, and that all actors share the stable lock inode.
    """

    def __init__(self, drain: DurableRunDrain, ledger: DurableRunLedger) -> None:
        if type(drain) is not DurableRunDrain or type(ledger) is not DurableRunLedger:
            raise OwnerCoordinationDenied("owner_components_invalid")
        if not isinstance(drain.receipt_root, Path) or not drain.receipt_root.is_absolute():
            raise OwnerCoordinationDenied("owner_receipt_root_invalid")
        if receipt_root_digest(drain.receipt_root) != drain.verifier.receipt_hash:
            raise OwnerCoordinationDenied("owner_receipt_root_mismatch")
        # Do not accept an arbitrary owner-private SQLite database. The only
        # expected authoritative Ledger is anchored below the SAME external
        # signer/authority root as the shared drain lock. The worker may read
        # approved signatures but must not be able to modify run-ledger/.
        expected_ledger = (
            drain.verifier.authority_root / "run-ledger"
            / "formatter-run-intents.sqlite3"
        )
        if ledger.path != expected_ledger:
            raise OwnerCoordinationDenied("owner_ledger_root_mismatch")
        # Pin what was checked before any externally supplied callbacks can
        # run. A caller may mutate a public ledger.path attribute later.
        self._expected_ledger_path = expected_ledger
        self._expected_ledger_state_inode = ledger._state_inode
        self._bound_ledger = ledger
        self.drain = drain
        self.ledger = ledger
        self._serial = threading.Lock()
        self._require_owner()

    def _require_ledger_binding(self) -> None:
        # Validate both the bound ledger object and its exact canonical file
        # each time owner authority is exercised, not just at construction.
        # The Ledger itself still enforces path ancestry and directory inode
        # on every SQLite open; this is the higher-level binding fence.
        if (self.ledger is not self._bound_ledger
                or type(self.ledger) is not DurableRunLedger
                or self.ledger.path != self._expected_ledger_path
                or self.ledger._state_inode != self._expected_ledger_state_inode):
            raise OwnerCoordinationDenied("owner_ledger_root_mismatch")

    def _require_owner(self) -> None:
        if os.geteuid() != self.drain.verifier.trusted_owner_uid:
            raise OwnerCoordinationDenied("owner_principal_required")

    def _budget(self, timeout: float | None) -> float:
        if timeout is None:
            return self.drain.maximum_stop_seconds
        if type(timeout) not in (int, float) or not math.isfinite(timeout):
            raise OwnerCoordinationDenied("owner_timeout_invalid")
        if timeout < 0 or timeout > self.drain.maximum_stop_seconds:
            raise OwnerCoordinationDenied("owner_timeout_invalid")
        return float(timeout)

    @contextmanager
    def quiesced(self, *, timeout: float | None = None) -> Iterator[None]:
        """Hold the same local exclusive effect lock through owner mutation.

        In particular no credential generation change can be ACKed before
        an in-flight, cooperating Writer yields its existing effect lease.
        The external mutation itself must establish its own durable success.
        """
        self._require_owner()
        self._require_ledger_binding()
        budget = self._budget(timeout)
        if not self._serial.acquire(blocking=False):
            raise OwnerCoordinationDenied("owner_busy")
        root_fd = -1
        lock_fd = -1
        try:
            try:
                root_fd = _secure_directory(
                    self.drain.verifier.authority_root,
                    self.drain.verifier.trusted_owner_uid,
                )
                lock_fd = self.drain._open_lock(root_fd)
                self.drain._acquire(lock_fd, budget)
                self._require_ledger_binding()
            except RunDrainDenied as error:
                if str(error) == "drain_timeout":
                    raise OwnerCoordinationDenied("owner_drain_timeout") from None
                raise OwnerCoordinationDenied("owner_lease_unavailable") from None
            except SignedRunDenied:
                raise OwnerCoordinationDenied("owner_lease_unavailable") from None
            yield
        finally:
            close_failed = False
            try:
                if lock_fd >= 0:
                    try:
                        os.close(lock_fd)
                    except OSError:
                        close_failed = True
                if root_fd >= 0:
                    try:
                        os.close(root_fd)
                    except OSError:
                        close_failed = True
            finally:
                # Even if one close raises unexpectedly, never strand the
                # in-process owner guard behind owner_busy forever.
                self._serial.release()
            if close_failed:
                raise OwnerCoordinationDenied("owner_lease_unavailable") from None

    def recover_after_quiescence(self, *, timeout: float | None = None) -> int:
        """Mark prior claims unconfirmed only after all cooperating effects drain.

        A new ledger instance is OFF until this is called. Never rearm claims.
        """
        with self.quiesced(timeout=timeout):
            try:
                return self.ledger.mark_unconfirmed_after_restart()
            except Exception:
                raise OwnerCoordinationDenied("owner_recovery_unverified") from None

    def _require_signed_intent(self, intent: RunIntent) -> RunIntent:
        sealed = _sealed_intent(intent)
        signed = self.drain.verifier
        # A valid SSHSIG binds policy fields independently of a mutable SQL
        # record. A per-run ledger row or boolean callback cannot replace it.
        if (sealed.receipt_root != str(self.drain.receipt_root)
                or sealed.policy_sha256 != signed.policy_sha256
                or sealed.source_generation != signed.source_generation
                or sealed.account_binding_sha256 != signed.account_binding_sha256
                or sealed.provider_proof_sha256 != signed.provider_proof_sha256):
            raise OwnerCoordinationDenied("owner_intent_binding_mismatch")
        try:
            allowed = signed(
                sealed.run_id, sealed.plan_sha256,
                sealed.policy_sha256, sealed.operation_id,
            )
        except Exception:
            raise OwnerCoordinationDenied("owner_signed_run_unverified") from None
        if allowed is not True:
            raise OwnerCoordinationDenied("owner_signed_run_unverified")
        return sealed

    def register_signed(self, intent: RunIntent, *, timeout: float | None = None) -> None:
        """Record a signed grant; no private signing key/issuer is supplied."""
        with self.quiesced(timeout=timeout):
            if self.ledger._boot_reconciled is not True:
                raise OwnerCoordinationDenied("owner_recovery_required")
            sealed = self._require_signed_intent(intent)
            try:
                # Recheck the *exact* signer data from a detached copy while
                # ledger.register seals its durable tuple.
                self.ledger.register(
                    sealed,
                    verify_external_approval=lambda row: (
                        self._require_signed_intent(row) is not None
                    ),
                )
            except OwnerCoordinationDenied:
                raise
            except Exception:
                raise OwnerCoordinationDenied("owner_registration_unverified") from None

    def claim_signed(self, intent: RunIntent, *, timeout: float | None = None) -> None:
        """Reserve a run once; no effect lease or Writer call is returned."""
        with self.quiesced(timeout=timeout):
            if self.ledger._boot_reconciled is not True:
                raise OwnerCoordinationDenied("owner_recovery_required")
            sealed = self._require_signed_intent(intent)
            try:
                claimed = self.ledger.claim(sealed)
            except Exception:
                raise OwnerCoordinationDenied("owner_claim_unverified") from None
            if claimed is not True:
                raise OwnerCoordinationDenied("owner_claim_denied")

    def change_under_quiescence(
        self, change: Callable[[], bool] | None, *, timeout: float | None = None,
    ) -> None:
        """Synthetic owner mutation barrier, NOT a credential-store adapter.

        External callback must durably confirm its own change and report the
        *exact* boolean True; no caller gets an Off/rotation ACK on failure.
        """
        if not callable(change):
            raise OwnerCoordinationDenied("owner_change_missing")
        with self.quiesced(timeout=timeout):
            try:
                changed = change()
            except Exception:
                raise OwnerCoordinationDenied("owner_change_unverified") from None
            if changed is not True:
                raise OwnerCoordinationDenied("owner_change_unverified")

    def stop_and_revoke(
        self, run_id: str, *, timeout: float | None = None,
    ) -> str:
        """ACK only after shared-lock drain, signed revoke AND ledger revoke.

        Calls an explicit post-drain callback while DurableRunDrain still holds
        the stable lock. An earlier durable signed marker can be retried safely
        if a subsequent ledger update/verification failed.
        """
        self._require_owner()
        self._require_ledger_binding()
        budget = self._budget(timeout)
        if not self._serial.acquire(blocking=False):
            raise OwnerCoordinationDenied("owner_busy")
        try:
            def persisted_revocation() -> bool:
                self._require_ledger_binding()
                revoked_now = self.ledger.revoke(run_id)
                self._require_ledger_binding()
                confirmed = revoked_now is True or self.ledger.is_revoked(run_id) is True
                self._require_ledger_binding()
                return confirmed

            try:
                return self.drain.stop_and_drain(
                    run_id, timeout=budget, on_drained=persisted_revocation,
                )
            except RunDrainDenied as error:
                # Reasons are already static from the pre-existing drain.
                raise OwnerCoordinationDenied(str(error)) from None
        finally:
            self._serial.release()
