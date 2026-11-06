"""#472 R2/B2c-C0: opt-in, bounded Owner process *session* (source only).

This is NOT a service entrypoint, supervisor or Writer/credential adapter.
It does not create OS users, spawn processes, bind sockets directly, sign runs,
approve intents, start scheduler jobs, or access Nextcloud/Vault. The already
trusted Owner process must separately construct OwnerUnixListener with pinned
pre-provisioned socket/lock/ledger/authority resources.

Boot recovery completes under the shared effect-drain lock while holding the
exclusive Owner Listener lifetime lease, BEFORE accepting any Worker request.
The socket may be bound/listening during recovery but no request is serviced.
Existing claimed Run IDs become unconfirmed and are NEVER rearmed. Run status
and acknowledgements are NOT Writer leases.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import threading

from .vault_format_owner_listener import OwnerUnixListener


class OwnerSessionDenied(ValueError):
    """Stable, path-free fail-closed reasons; no Vault body or credentials."""


@dataclass(frozen=True, slots=True)
class OwnerSessionResult:
    """Non-authoritative count-only observation of a completed session."""

    accepted: int
    idle_polls: int
    boot_unconfirmed: int
    exit_reason: str


class BoundedOwnerSession:
    """One-use, process-pinned Owner startup/recovery and bounded accept loop.

    A new owner process must construct its OWN instance after it starts.
    An object inherited across fork may not run, because its bound verifier,
    lock and Ledger objects were constructed in the previous PID.

    max_requests and max_idle_polls are cumulative, not reset by activity:
    the loop cannot extend its lifetime indefinitely through trickle traffic.
    Each listener accept has its own B1 bounded timeout, and each IPC command
    has its prior B2c bounds. There is NO general-purpose daemon loop.
    """

    def __init__(self, listener: OwnerUnixListener) -> None:
        if type(listener) is not OwnerUnixListener:
            raise OwnerSessionDenied("owner_session_listener_invalid")
        try:
            listener._require_owner()
        except Exception:
            raise OwnerSessionDenied("owner_session_owner_required") from None
        if listener._listener is not None or listener._cleanup_unverified:
            raise OwnerSessionDenied("owner_session_listener_not_fresh")
        self._listener = listener
        self._bound_handler = listener.handler
        self._bound_owner = listener.handler._owner
        self._pid = os.getpid()
        self._euid = os.geteuid()
        self._consumed = False
        self._first_call = threading.Lock()

    def _require_binding(self) -> None:
        if os.getpid() != self._pid or os.geteuid() != self._euid:
            raise OwnerSessionDenied("owner_session_process_mismatch")
        try:
            self._listener._require_owner()
            if (self._listener.handler is not self._bound_handler
                    or self._bound_handler._owner is not self._bound_owner
                    or self._listener._cleanup_unverified):
                raise OwnerSessionDenied("owner_session_binding_changed")
            self._bound_owner._require_ledger_binding()
        except OwnerSessionDenied:
            raise
        except Exception:
            raise OwnerSessionDenied("owner_session_binding_changed") from None

    def run(
        self, *, max_requests: int = 4, max_idle_polls: int = 4,
    ) -> OwnerSessionResult:
        """Hold lifetime lock, recover boot state, then accept finite requests.

        This method is intentionally blocking and synchronous. It cannot be
        resumed or retried on the same instance, including after failure.
        Any request-response ambiguity is NOT an invitation to retry Claims.
        """
        # A fork-inherited session must never touch an inherited threading
        # lock. All other first-call failures permanently consume this object.
        if os.getpid() != self._pid:
            raise OwnerSessionDenied("owner_session_process_mismatch")
        with self._first_call:
            if self._consumed:
                raise OwnerSessionDenied("owner_session_consumed")
            self._consumed = True
        self._require_binding()
        if (type(max_requests) is not int or not 1 <= max_requests <= 16
                or type(max_idle_polls) is not int
                or not 1 <= max_idle_polls <= 24):
            raise OwnerSessionDenied("owner_session_budget_invalid")
        try:
            # Acquire the stable Owner lifetime lock BEFORE recovery. A
            # competing Owner must never invalidate an active Owner's claims
            # unless it has first obtained exclusive listener ownership.
            # The socket can be connected to, but nothing is accepted until
            # the shared effect-drain lock has reconciled boot state.
            with self._listener:
                self._require_binding()
                if self._bound_owner.ledger._boot_reconciled is not False:
                    raise OwnerSessionDenied("owner_session_boot_not_fresh")
                unconfirmed = self._bound_owner.recover_after_quiescence()
                if type(unconfirmed) is not int or unconfirmed < 0:
                    raise OwnerSessionDenied("owner_session_recovery_unverified")
                self._require_binding()
                accepted = 0
                idle = 0
                while accepted < max_requests and idle < max_idle_polls:
                    self._require_binding()
                    if self._listener.serve_once():
                        accepted += 1
                    else:
                        idle += 1
            return OwnerSessionResult(
                accepted=accepted, idle_polls=idle,
                boot_unconfirmed=unconfirmed,
                exit_reason=(
                    "request_limit" if accepted == max_requests
                    else "idle_limit"
                ),
            )
        except OwnerSessionDenied:
            raise
        except Exception:
            # No raw filesystem, socket, SQLite or credential error details.
            # In particular a failed Listener close is NEVER an OK result.
            raise OwnerSessionDenied("owner_session_unverified") from None
