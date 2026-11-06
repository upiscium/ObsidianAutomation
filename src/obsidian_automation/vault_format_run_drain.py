"""R2/B2a source-only cross-process effect/revocation drain gate.

The trusted operator must pre-provision an authority root and a stable lock
file within it. All participating effect workers AND the stop actor MUST use
this same inode on a local filesystem that supports flock. A stop request is
not acknowledged until the prior effect releases the lock and a persistent
revocation marker is fsync'd. No app/WebUI/Timer wires this class.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re
import stat
import time
from typing import Callable, Iterator

from .vault_format_signed_run import SignedRunVerifier, _secure_directory, receipt_root_digest
from .vault_format_writer import _root, FormatWriteError

_ID = re.compile(r"[a-f0-9]{32}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_LOCK_NAME = "effect-drain.lock"


class RunDrainDenied(ValueError):
    """Redacted fixed reason codes, never a platform exception string."""


def _check_run(run_id: str) -> None:
    if type(run_id) is not str or _ID.fullmatch(run_id) is None:
        raise RunDrainDenied("invalid_run_id")


class DurableRunDrain:
    """One stable shared OS lock for effects and externally requested stop.

    It does not mint/approve a run and cannot be used as a production adapter
    until the source-only gate is wired under separately accepted credentials.
    A successful stop only revokes this signed run ID; new run IDs still
    require independent signatures. The worker-owned claim survives ordinary
    crashes while its directory remains intact, but is NOT tamper-proof:
    a separate trusted issuer/ledger must enforce production one-use state.
    """

    def __init__(
        self, verifier: SignedRunVerifier, *,
        receipt_root: Path,
        maximum_stop_seconds: float = 5.0,
    ) -> None:
        if type(verifier) is not SignedRunVerifier:
            raise RunDrainDenied("run_verifier_required")
        if (not isinstance(maximum_stop_seconds, (int, float))
                or type(maximum_stop_seconds) is bool
                or not 0 < maximum_stop_seconds <= 30):
            raise RunDrainDenied("invalid_stop_budget")
        if receipt_root_digest(receipt_root) != verifier.receipt_hash:
            raise RunDrainDenied("receipt_root_identity_mismatch")
        self.verifier = verifier
        self.receipt_root = receipt_root
        self.maximum_stop_seconds = float(maximum_stop_seconds)
        # Verify existing directories only. Creation/provisioning is the
        # operator's responsibility, not an implicit action on app boot.
        root_fd = _secure_directory(verifier.authority_root, verifier.trusted_owner_uid)
        try:
            fd = self._open_lock(root_fd)
            os.close(fd)
        finally:
            os.close(root_fd)
        try:
            with _root(receipt_root):
                pass
        except FormatWriteError:
            raise RunDrainDenied("unsafe_receipt_root") from None

    def _open_lock(self, directory_fd: int) -> int:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            fd = os.open(_LOCK_NAME, flags, dir_fd=directory_fd)
        except OSError:
            raise RunDrainDenied("missing_drain_lock") from None
        try:
            info = os.fstat(fd)
            current = os.stat(_LOCK_NAME, dir_fd=directory_fd, follow_symlinks=False)
            if (not stat.S_ISREG(info.st_mode)
                    or info.st_uid != self.verifier.trusted_owner_uid
                    or info.st_nlink != 1 or info.st_mode & 0o022
                    or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)
                    or info.st_size > 128):
                raise RunDrainDenied("unsafe_drain_lock")
            return fd
        except (OSError, RunDrainDenied):
            os.close(fd)
            raise RunDrainDenied("unsafe_drain_lock") from None

    @staticmethod
    def _acquire(fd: int, timeout: float) -> None:
        if timeout <= 0:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except (OSError, BlockingIOError):
                raise RunDrainDenied("drain_busy") from None
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RunDrainDenied("drain_timeout") from None
                time.sleep(min(0.025, max(0.0, deadline - time.monotonic())))
            except OSError:
                raise RunDrainDenied("drain_unavailable") from None

    @staticmethod
    def _claim(receipt_fd: int, run_id: str) -> None:
        # Fail-closed crash recovery *while the receipt tree is intact*.
        # This worker-owned claim can be deleted by its owning UID and is not
        # a trusted anti-tamper authority; a separate ledger is still required.
        name = f"recurring-run-{run_id}.claimed"
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=receipt_fd)
        except FileExistsError:
            raise RunDrainDenied("run_already_claimed") from None
        except OSError:
            raise RunDrainDenied("claim_unavailable") from None
        try:
            os.write(fd, b"formatter-run-claimed-v1\n")
            os.fsync(fd)
            os.fsync(receipt_fd)
        except OSError:
            raise RunDrainDenied("claim_not_durable") from None
        finally:
            os.close(fd)

    @contextmanager
    def effect(
        self, run_id: str, plan_sha256: str, policy_sha256: str,
        operation_id: str,
    ) -> Iterator[None]:
        """Exclusive effect lease, caller must hold through the entire Writer.

        The signed grant is checked WHILE holding the same advisory kernel
        lock used by stop_and_drain. A trusted revoker cannot acknowledge stop
        while this operation is in flight. No network call is made here.
        """
        _check_run(run_id)
        if any(type(x) is not str or _SHA.fullmatch(x) is None for x in
               (plan_sha256, policy_sha256)):
            raise RunDrainDenied("invalid_effect_binding")
        if type(operation_id) is not str or _ID.fullmatch(operation_id) is None:
            raise RunDrainDenied("invalid_effect_binding")
        root_fd = _secure_directory(
            self.verifier.authority_root, self.verifier.trusted_owner_uid,
        )
        try:
            lock_fd = self._open_lock(root_fd)
            try:
                self._acquire(lock_fd, 0)
                if not self.verifier(
                    run_id, plan_sha256, policy_sha256, operation_id,
                ):
                    raise RunDrainDenied("signed_run_unverified")
                try:
                    with _root(self.receipt_root) as receipts:
                        self._claim(receipts.fd, run_id)
                except FormatWriteError:
                    raise RunDrainDenied("unsafe_receipt_root") from None
                yield
            finally:
                # close() releases the kernel lock even after BaseException.
                os.close(lock_fd)
        finally:
            os.close(root_fd)

    def stop_and_drain(
        self, run_id: str, *, timeout: float | None = None,
        on_drained: Callable[[], bool] | None = None,
    ) -> str:
        """Privileged operator action: durable revocation after effect drain.

        The authority-root owner must be able to create the marker. A
        nonprivileged effect worker can hold the read-only lock file but must
        not be permitted to add/rewrite signed records or revoke markers.
        """
        _check_run(run_id)
        if on_drained is not None and not callable(on_drained):
            raise RunDrainDenied("invalid_stop_postcondition")

        def require_postcondition() -> None:
            # Called after durable signed revocation, while still holding the
            # SAME exclusive lock. It can durably revoke the separate ledger
            # before we acknowledge Off. A false/failed callback is NOT Off.
            if on_drained is None:
                return
            try:
                confirmed = on_drained()
            except Exception:
                raise RunDrainDenied("stop_postcondition_unverified") from None
            if confirmed is not True:
                raise RunDrainDenied("stop_postcondition_unverified")

        budget = self.maximum_stop_seconds if timeout is None else timeout
        if (type(budget) not in (int, float)
                or not 0 <= budget <= self.maximum_stop_seconds):
            raise RunDrainDenied("invalid_stop_budget")
        root_fd = _secure_directory(
            self.verifier.authority_root, self.verifier.trusted_owner_uid,
        )
        try:
            lock_fd = self._open_lock(root_fd)
            try:
                self._acquire(lock_fd, float(budget))
                name = run_id + ".revoked"
                try:
                    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=root_fd)
                except FileExistsError:
                    # Only a complete, owner-controlled durable marker counts
                    # as an acknowledged earlier stop. A symlink or junk file
                    # still denies execution but is not a verified stop.
                    try:
                        old = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                      dir_fd=root_fd)
                        try:
                            info = os.fstat(old)
                            data = os.read(old, 256)
                            if (not stat.S_ISREG(info.st_mode)
                                    or info.st_uid != self.verifier.trusted_owner_uid
                                    or info.st_nlink != 1 or info.st_mode & 0o022
                                    or data != b"formatter-run-stopped-v1\n"):
                                raise RunDrainDenied("unverified_prior_stop")
                            # A previous stop may have failed its FILE fsync,
                            # despite the expected bytes appearing in cache.
                            # Complete that sync before directory sync/ack.
                            try:
                                os.fsync(old)
                            except OSError:
                                raise RunDrainDenied("revocation_not_durable") from None
                        finally:
                            os.close(old)
                    except OSError:
                        raise RunDrainDenied("unverified_prior_stop") from None
                    # A previous call may have fsync'd the marker file but
                    # failed to persist the containing directory entry.
                    # Never acknowledge an existing marker without resyncing
                    # its directory while holding the same OS drain lock.
                    try:
                        os.fsync(root_fd)
                    except OSError:
                        raise RunDrainDenied("revocation_not_durable") from None
                    require_postcondition()
                    return "already_stopped"
                except OSError:
                    raise RunDrainDenied("revocation_unavailable") from None
                try:
                    payload = b"formatter-run-stopped-v1\n"
                    if os.write(fd, payload) != len(payload):
                        raise RunDrainDenied("revocation_not_durable")
                    os.fsync(fd)
                    os.fsync(root_fd)
                except OSError:
                    raise RunDrainDenied("revocation_not_durable") from None
                finally:
                    os.close(fd)
                require_postcondition()
                return "stopped_after_drain"
            finally:
                os.close(lock_fd)
        finally:
            os.close(root_fd)
