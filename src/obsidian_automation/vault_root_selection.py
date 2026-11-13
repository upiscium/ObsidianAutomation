"""#303 V1/C: read-only Vault root candidate picker and preview service.

Disabled unless the Dashboard is constructed with an instance of this class.
Activation, rollback and enrollment exist only when ``actions`` is supplied
(``vault_root_webui``); this class never touches credentials itself. Browsers receive only
folder labels, short-lived opaque handles and the redacted V1/A preview.
Account binding, resource IDs and revisions stay on the server.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import secrets
import threading
import time
from typing import Callable

from .vault_root_dav_verifier import (
    CURRENT_PROVIDER_PROOF, DavTransport, ProviderProof, TrustedDavAccount,
    list_candidate_directories, verify_root_candidate,
)
from .vault_root_preview import RootSelection

HANDLE_TTL_S = 300.0
RATE_WINDOW_S = 60.0
RATE_LIMIT = 30
MAX_HANDLES = 2048
MAX_CANDIDATES = 200


@dataclass(frozen=True)
class TrustedRootContext:
    """Built server-side from the saved shared Nextcloud binding (V1/D)."""
    account: TrustedDavAccount
    active: str
    active_resource_id: str
    container_resource_id: str
    source_revision: str
    jobs_in_flight: bool


@dataclass(frozen=True)
class _Handle:
    path: str
    resource_id: str
    revision: str
    expires: float
    previewed: bool = False  # set only by a candidate_valid Preview


class VaultRootSelectionService:
    def __init__(
        self, *,
        context: Callable[[], TrustedRootContext | None],
        transport: DavTransport | None,
        proof: ProviderProof = CURRENT_PROVIDER_PROOF,
        clock: Callable[[], float] = time.monotonic,
        restricted_coherence: bool = False,
        actions=None,
        revision: Callable[[], object] | None = None,
    ):
        self._actions = actions
        self._revision_source = revision
        self._context = context
        self._restricted = restricted_coherence is True
        self._requests: list[float] = []
        self._transport = transport
        self._proof = proof
        self._clock = clock
        self._lock = threading.Lock()
        self._handles: dict[str, _Handle] = {}

    def _current(self) -> TrustedRootContext | None:
        try:
            ctx = self._context()
        except Exception:
            return None
        return ctx if type(ctx) is TrustedRootContext else None

    def _revision(self) -> object:
        if self._revision_source is not None:
            try:
                return self._revision_source()
            except Exception:
                return None
        ctx = self._current()
        return ctx.source_revision if ctx else None

    def _admitted(self) -> bool:
        return (type(self._proof) is ProviderProof
                and self._proof.admits(restricted_coherence=self._restricted))

    def _rate_limited(self) -> bool:
        # Bounds provider reads triggered from the UI (double-clicks, loops).
        now = self._clock()
        with self._lock:
            self._requests = [t for t in self._requests if now - t < RATE_WINDOW_S]
            if len(self._requests) >= RATE_LIMIT:
                return True
            self._requests.append(now)
            return False

    def _ready(self) -> tuple[TrustedRootContext | None, str | None]:
        if not self._admitted():
            return None, "provider_unproven"
        if self._rate_limited():
            return None, "rate_limited"
        ctx = self._current()
        if ctx is None or self._transport is None:
            return None, "binding_unavailable"
        return ctx, None

    def status(self) -> dict:
        out = {
            "version": 1, "enabled": True,
            # "restricted": C (tree coherence) is not guaranteed; the UI says so.
            "provider": ("unproven" if not self._admitted() else
                         "restricted" if self._restricted and not self._proof.all_verified()
                         else "verified"),
            "activation": "not_available", "writes_vault": False,
        }
        if self._actions is None:
            out["binding"] = "available" if self._current() else "unavailable"
            return out
        # With actions, status is local only: no provider read on a GET.
        try:
            enrolled = self._actions.enrolled() is True
            label = self._actions.active_label()
            can_rollback = self._actions.can_rollback() is True
        except Exception:
            enrolled, label, can_rollback = False, None, False
        out.update({
            "binding": "available" if enrolled else "unavailable",
            "enrolled": enrolled,
            "active_label": label if type(label) is str else None,
            "activation": "available" if self._admitted() and enrolled else "not_available",
            "can_rollback": can_rollback,
        })
        return out

    def _no_actions(self) -> tuple[int, dict] | None:
        if self._actions is None:
            return 405, {"error": "activation_not_available"}
        return None

    @staticmethod
    def _confirmed(payload: object, keys: set) -> bool:
        return (type(payload) is dict and set(payload) == keys | {"confirm"}
                and payload["confirm"] is True)

    def enroll(self, payload: object) -> tuple[int, dict]:
        if (refused := self._no_actions()) is not None:
            return refused
        if type(payload) is not dict or payload:
            return 400, {"error": "invalid_request"}
        if not self._admitted():
            return 200, {"version": 1, "status": "blocked", "reason": "provider_unproven"}
        if self._rate_limited():
            return 200, {"version": 1, "status": "blocked", "reason": "rate_limited"}
        reason = self._actions.enroll()
        return 200, {"version": 1, "status": "enrolled" if reason == "enrolled" else "blocked",
                     "reason": reason if type(reason) is str else "enrollment_failed"}

    def activate(self, payload: object) -> tuple[int, dict]:
        """Switch ``vault_folder`` to a previewed candidate. Requires
        ``confirm: true``; the activation itself re-walks and re-pins."""
        if (refused := self._no_actions()) is not None:
            return refused
        if not self._confirmed(payload, {"candidate"}):
            return 400, {"error": "confirmation_required"}
        ctx, reason = self._ready()
        if ctx is None:
            return 200, {"version": 1, "status": "blocked", "reason": reason,
                         "writes_vault": False}
        with self._lock:
            found = self._resolve(payload["candidate"], self._clock())
        if found is None:
            return 200, {"version": 1, "status": "blocked", "reason": "handle_expired",
                         "writes_vault": False}
        if found.revision != ctx.source_revision:
            return 200, {"version": 1, "status": "blocked", "reason": "stale_source",
                         "writes_vault": False}
        if found.previewed is not True:
            # Server-side: only a folder that passed Preview can be switched to.
            return 200, {"version": 1, "status": "blocked", "reason": "preview_required",
                         "writes_vault": False}
        result = self._actions.activate(found.path, found.resource_id, found.revision, ctx)
        with self._lock:
            self._handles.clear()  # every handle predates the new binding
        return 200, {"version": 1, "status": result.status, "reason": result.reason,
                     "writes_vault": False}

    def rollback(self, payload: object) -> tuple[int, dict]:
        if (refused := self._no_actions()) is not None:
            return refused
        if not self._confirmed(payload, set()):
            return 400, {"error": "confirmation_required"}
        if self._rate_limited():
            return 200, {"version": 1, "status": "blocked", "reason": "rate_limited",
                         "writes_vault": False}
        result = self._actions.rollback()
        with self._lock:
            self._handles.clear()
        return 200, {"version": 1, "status": result.status, "reason": result.reason,
                     "writes_vault": False}

    def _resolve(self, handle: object, now: float) -> _Handle | None:
        if type(handle) is not str or not 1 <= len(handle) <= 64:
            return None
        found = self._handles.get(handle)
        if found is None or found.expires <= now:
            self._handles.pop(handle, None)
            return None
        return found

    def _issue(self, path: str, resource_id: str, revision: str, now: float) -> str:
        expired = [k for k, h in self._handles.items() if h.expires <= now]
        for key in expired:
            del self._handles[key]
        while len(self._handles) >= MAX_HANDLES:
            del self._handles[next(iter(self._handles))]
        token = secrets.token_urlsafe(24)
        self._handles[token] = _Handle(path, resource_id, revision, now + HANDLE_TTL_S)
        return token

    def candidates(self, payload: object) -> tuple[int, dict]:
        if type(payload) is not dict or set(payload) - {"parent"}:
            return 400, {"error": "invalid_request"}
        ctx, reason = self._ready()
        if ctx is None:
            return 200, {"version": 1, "status": "blocked", "reason": reason,
                         "candidates": []}
        now = self._clock()
        parent_handle = payload.get("parent")
        with self._lock:
            if parent_handle is None:
                parent, parent_id = "", ctx.container_resource_id
            else:
                found = self._resolve(parent_handle, now)
                if found is None:
                    return 200, {"version": 1, "status": "blocked",
                                 "reason": "handle_expired", "candidates": []}
                if found.revision != ctx.source_revision:
                    return 200, {"version": 1, "status": "blocked",
                                 "reason": "stale_source", "candidates": []}
                parent, parent_id = found.path, found.resource_id
        listing = list_candidate_directories(
            ctx.account, self._transport, parent=parent,
            parent_resource_id=parent_id, expected_revision=ctx.source_revision,
            source_revision=self._revision, proof=self._proof, clock=self._clock,
            restricted_coherence=self._restricted,
        )
        if listing.status != "ok":
            return 200, {"version": 1, "status": "blocked", "reason": listing.reason,
                         "candidates": []}
        rows = listing.directories[:MAX_CANDIDATES]
        with self._lock:
            out = [{"handle": self._issue(
                        name if not parent else parent + "/" + name,
                        resource_id, ctx.source_revision, now),
                    "label": name}
                   for name, resource_id in rows]
        return 200, {"version": 1, "status": "ok", "reason": "listed",
                     "candidates": out,
                     "truncated": len(listing.directories) > MAX_CANDIDATES}

    def preview(self, payload: object) -> tuple[int, dict]:
        if type(payload) is not dict or set(payload) != {"candidate"}:
            return 400, {"error": "invalid_request"}
        ctx, reason = self._ready()
        if ctx is None:
            return 200, {"version": 1, "status": "blocked", "reason": reason,
                         "writes_vault": False, "activation_allowed": False,
                         "requires_explicit_activation": True}
        with self._lock:
            found = self._resolve(payload["candidate"], self._clock())
        if found is None:
            return 200, {"version": 1, "status": "blocked", "reason": "handle_expired",
                         "writes_vault": False, "activation_allowed": False,
                         "requires_explicit_activation": True}
        selection = RootSelection(
            requested=found.path, active=ctx.active,
            active_resource_id=ctx.active_resource_id,
            expected_revision=found.revision, current_revision=ctx.source_revision,
            namespace=ctx.account.instance_id,
            container_resource_id=ctx.container_resource_id,
            jobs_in_flight=ctx.jobs_in_flight,
        )
        result = verify_root_candidate(
            selection, ctx.account, self._transport,
            source_revision=self._revision, proof=self._proof, clock=self._clock,
            expected_resource_id=found.resource_id,
            restricted_coherence=self._restricted,
        )
        if result.status == "candidate_valid":
            with self._lock:
                if self._handles.get(payload["candidate"]) is found:
                    self._handles[payload["candidate"]] = replace(found, previewed=True)
        return 200, asdict(result)
