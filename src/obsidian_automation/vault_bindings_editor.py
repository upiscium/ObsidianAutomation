"""#303 Phase 2 / P2-4: save, reset and roll back the role mapping.

The first slice that writes ``vault_bindings.json``. It follows the V1 root
switch (``vault_root_activation``): every change runs inside the Dashboard
Job fence, behind a pending marker that makes every RoleGuard refuse, and can
be undone one step. Owner decisions (2026-11-14): only ``daily``, ``project``
and ``system`` are editable (D1-A); a V1 root switch is refused while a
record exists (D2-A); folders are typed and checked by Preview (D3-A).
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import secrets
import threading
import time
from typing import Callable

from .snapshot_setup import SetupStore, _atomic
from .vault_bindings import (
    BINDINGS_FILE, PENDING_FILE, BindingsError, VaultBindings, bindings_json, default_bindings,
    load_bindings, parse_bindings, preview_bindings,
)
from .vault_layout import DEFAULT_ROLE_FOLDERS, ROLES
from .vault_root_activation import _read_binding, current_pin, read_private_json
from .vault_root_dav_verifier import CURRENT_PROVIDER_PROOF, ProviderProof

EDITABLE_ROLES = ("daily", "project", "system")      # D1-A
PREVIOUS_FILE = "vault_bindings.previous.json"
HANDLE_TTL_S = 300.0
MAX_HANDLES = 256
RATE_WINDOW_S = 60.0
RATE_LIMIT = 30


@dataclass(frozen=True)
class EditResult:
    status: str   # saved | reset | rolled_back | blocked | uncertain
    reason: str


def _present(path: Path) -> bool:
    try:
        os.lstat(path)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return True   # cannot tell: treat as present (fail-closed)


def change_pending(state: Path) -> bool:
    return _present(state / PENDING_FILE)


def mapping_present(state: Path) -> bool:
    """A record (or an unfinished change) exists: V1 root switches refuse."""
    return _present(state / BINDINGS_FILE) or change_pending(state)


def root_settled(state: Path) -> bool:
    """The V1 root pin is finished (not ``pending_*``). A mapping is saved only
    for a settled root, so an unfinished V1 switch can still be completed."""
    data = _read_binding(state)
    return bool(data) and data.get("outcome") in ("activated", "rolled_back")


def _read_previous(state: Path) -> tuple[bool, VaultBindings | None]:
    """``(present, record)``. ``record`` None with present means "defaults"."""
    path = state / PREVIOUS_FILE
    if not path.exists() and not path.is_symlink():
        return False, None
    raw = read_private_json(path)
    if type(raw) is not dict or set(raw) != {"version", "record"} or raw["version"] != 1:
        raise BindingsError("unreadable_record")
    return True, None if raw["record"] is None else parse_bindings(raw["record"])


def _commit(state: Path, fence, new: VaultBindings | None,
            previous: VaultBindings | None, *, keep_previous: bool | None,
            done: str) -> EditResult:
    """Pending marker first, then the record. A crash leaves the marker, so
    every RoleGuard refuses (fail-closed) until the change is finished.

    ``keep_previous``: True writes ``previous`` as the rollback point, False
    deletes the rollback point, None leaves it as it is (finishing an
    interrupted change must not replace the state it would roll back to)."""
    pending = state / PENDING_FILE
    try:
        _atomic(pending, "1\n")
    except OSError:
        return EditResult("blocked", "pending_unrecorded")
    record = state / BINDINGS_FILE
    try:
        if keep_previous is True:
            _atomic(state / PREVIOUS_FILE, json.dumps({
                "version": 1,
                "record": None if previous is None else bindings_json(previous),
            }, sort_keys=True))
        elif keep_previous is False:
            (state / PREVIOUS_FILE).unlink(missing_ok=True)
    except OSError:
        try:
            pending.unlink()
        except OSError:
            return EditResult("uncertain", "pending_unfinalized")
        return EditResult("blocked", "previous_unrecorded")
    try:
        if new is None:
            record.unlink(missing_ok=True)
        else:
            _atomic(record, json.dumps(bindings_json(new), sort_keys=True))
    except OSError:
        return EditResult("uncertain", "record_write_uncertain")
    try:
        fence.after_change()  # pause schedules, as after a V1 root switch
    except Exception:
        return EditResult("uncertain", "job_pause_failed")
    try:
        pending.unlink()
    except OSError:
        return EditResult("uncertain", "pending_unfinalized")
    return EditResult(done, "requires_job_reenable")


def _current(state: Path) -> VaultBindings | None:
    current = load_bindings(state)
    return None if current.is_default else current


def save_bindings(store: SetupStore, fence, record: VaultBindings,
                  revalidate: Callable[[], VaultBindings | None]) -> EditResult:
    """Store ``record`` (from a valid Preview) after re-checking it live."""
    with fence.quiesced() as busy:
        if busy:
            return EditResult("blocked", "jobs_in_flight")
        if change_pending(store.state):
            return EditResult("blocked", "change_pending")
        try:
            current = _current(store.state)
        except BindingsError:
            return EditResult("blocked", "unreadable_record")
        if not root_settled(store.state):
            return EditResult("blocked", "root_unsettled")
        fresh = revalidate()
        if fresh is None or fresh != record:
            return EditResult("blocked", "stale_preview")
        if dict(record.roles) == dict(DEFAULT_ROLE_FOLDERS) and current is None:
            return EditResult("blocked", "already_default")
        return _commit(store.state, fence, record, current, keep_previous=True,
                       done="saved")


def reset_bindings(store: SetupStore, fence) -> EditResult:
    """Back to today's names (no record). No provider I/O; works after a
    root change, so it is also the way out of ``stale_root``."""
    with fence.quiesced() as busy:
        if busy:
            return EditResult("blocked", "jobs_in_flight")
        if change_pending(store.state):
            # Finish or drop an interrupted change. The rollback point stays
            # what it was before that change, never the unfinished record.
            return _commit(store.state, fence, None, None, keep_previous=None,
                           done="reset")
        if not _present(store.state / BINDINGS_FILE):
            return EditResult("blocked", "already_default")
        try:
            current = _current(store.state)
        except BindingsError:
            current = None   # an unreadable record is dropped, not kept
        return _commit(store.state, fence, None, current,
                       keep_previous=True if current is not None else False,
                       done="reset")


def rollback_bindings(store: SetupStore, fence) -> EditResult:
    """One step back to the state before the last save or reset."""
    with fence.quiesced() as busy:
        if busy:
            return EditResult("blocked", "jobs_in_flight")
        if change_pending(store.state):
            return EditResult("blocked", "change_pending")
        try:
            present, previous = _read_previous(store.state)
        except BindingsError:
            return EditResult("blocked", "unreadable_record")
        if not present:
            return EditResult("blocked", "nothing_to_roll_back")
        pin = current_pin(store.state)
        if previous is not None and not root_settled(store.state):
            return EditResult("blocked", "root_unsettled")
        if previous is not None and (pin is None or pin.chain != previous.root_pin):
            return EditResult("blocked", "root_changed")
        return _commit(store.state, fence, previous, None, keep_previous=False,
                       done="rolled_back")


def bindings_state(state: Path) -> str:
    """``ok`` | ``default`` | ``pending`` | ``stale_root`` | ``unreadable``."""
    if change_pending(state):
        return "pending"
    try:
        current = load_bindings(state)
    except BindingsError:
        return "unreadable"
    if current.is_default:
        return "default"
    pin = current_pin(state)
    return "ok" if pin is not None and pin.chain == current.root_pin else "stale_root"


@dataclass(frozen=True)
class _Handle:
    record: VaultBindings
    roles: dict
    accepted: tuple[str, ...]
    expires: float


class VaultBindingsService:
    """``/api/v1/vault-bindings*``. Built only with ``--vault-bindings-ui``."""

    def __init__(self, store: SetupStore, *, context, transport, fence_factory,
                 proof: ProviderProof = CURRENT_PROVIDER_PROOF,
                 clock: Callable[[], float] = time.monotonic):
        self._store = store
        self._context = context
        self._transport = transport
        self._fence = fence_factory
        self._proof = proof
        self._clock = clock
        self._lock = threading.Lock()
        self._handles: dict[str, _Handle] = {}
        self._requests: list[float] = []

    def _rate_limited(self) -> bool:
        now = self._clock()
        with self._lock:
            self._requests = [t for t in self._requests if now - t < RATE_WINDOW_S]
            if len(self._requests) >= RATE_LIMIT:
                return True
            self._requests.append(now)
            return False

    def status(self) -> dict:
        state = bindings_state(self._store.state)
        try:
            current = load_bindings(self._store.state)
            roles = dict(current.roles)
        except BindingsError:
            roles = None
        try:
            present, _ = _read_previous(self._store.state)
        except BindingsError:
            present = False
        return {"version": 1, "enabled": True, "state": state, "roles": roles,
                "editable_roles": list(EDITABLE_ROLES),
                "defaults": dict(DEFAULT_ROLE_FOLDERS),
                "can_rollback": present and state not in ("pending", "unreadable"),
                "can_reset": state != "default"}

    def _proposal(self, payload: object) -> tuple[dict | None, tuple[str, ...] | None]:
        if (type(payload) is not dict or not set(payload) <= {"roles", "accept_recreated"}
                or "roles" not in payload or type(payload["roles"]) is not dict
                or not set(payload["roles"]) <= set(EDITABLE_ROLES)
                or not all(type(v) is str for v in payload["roles"].values())):
            return None, None
        accepted = payload.get("accept_recreated", [])
        if (type(accepted) is not list or len(set(accepted)) != len(accepted)
                or not all(type(r) is str and r in EDITABLE_ROLES for r in accepted)):
            return None, None
        try:
            current = load_bindings(self._store.state)
        except BindingsError:
            current = default_bindings()   # _evaluate reports unreadable_record
        # D1-A: roles outside EDITABLE_ROLES always keep today's names.
        roles = {r: (payload["roles"].get(r, current.roles[r]) if r in EDITABLE_ROLES
                     else DEFAULT_ROLE_FOLDERS[r]) for r in ROLES}
        return roles, tuple(sorted(accepted))

    def _evaluate(self, roles: dict, accepted: tuple[str, ...]):
        """P2-1 Preview, with an explicit re-pin for ``accepted`` roles only."""
        if change_pending(self._store.state):
            return _blocked_preview("change_pending"), None
        try:
            current = load_bindings(self._store.state)
        except BindingsError:
            return _blocked_preview("unreadable_record"), None
        if not root_settled(self._store.state):
            return _blocked_preview("root_unsettled"), None

        def run(repin: frozenset):
            return preview_bindings(
                roles, current=current, context=self._context(),
                root_pin=current_pin(self._store.state), transport=self._transport,
                revision=self._store.nextcloud_revision, proof=self._proof,
                restricted_coherence=True, repin=repin)
        result, record = run(frozenset())
        if not accepted:
            return result, record
        # A re-pin is allowed only for a role this Preview reports as recreated.
        for role in accepted:
            if result.get("roles", {}).get(role, {}).get("status") != "identity_changed":
                return _blocked_preview("invalid_accept", result.get("roles", {})), None
        result, record = run(frozenset(accepted))
        for role in accepted:
            if role in result.get("roles", {}):
                result["roles"][role]["repinned"] = True
        return result, record

    def preview(self, payload: object) -> tuple[int, dict]:
        roles, accepted = self._proposal(payload)
        if roles is None:
            return 400, {"error": "invalid_request"}
        if self._rate_limited():
            return 200, _blocked_preview("rate_limited")
        result, record = self._evaluate(roles, accepted)
        if record is not None:
            handle = secrets.token_urlsafe(24)
            now = self._clock()
            with self._lock:
                self._handles = {k: h for k, h in self._handles.items() if h.expires > now}
                if len(self._handles) >= MAX_HANDLES:
                    self._handles.pop(next(iter(self._handles)))
                self._handles[handle] = _Handle(record, roles, accepted, now + HANDLE_TTL_S)
            result["handle"] = handle
        return 200, result

    def save(self, payload: object) -> tuple[int, dict]:
        if (type(payload) is not dict or set(payload) != {"handle", "confirm"}
                or type(payload["handle"]) is not str):
            return 400, {"error": "invalid_request"}
        if payload["confirm"] is not True:
            return 400, {"error": "confirmation_required"}
        if self._rate_limited():   # before taking the handle, so it survives
            return 200, _edit(EditResult("blocked", "rate_limited"))
        with self._lock:
            found = self._handles.pop(payload["handle"], None)
        if found is None or found.expires <= self._clock():
            return 200, _edit(EditResult("blocked", "handle_expired"))

        def revalidate():
            return self._evaluate(found.roles, found.accepted)[1]
        result = save_bindings(self._store, self._fence(), found.record, revalidate)
        if result.status == "saved":
            self._drop_handles()   # other tabs must preview the new state again
        return 200, _edit(result)

    def reset(self, payload: object) -> tuple[int, dict]:
        if payload != {"confirm": True}:
            return 400, {"error": "confirmation_required"}
        if self._rate_limited():
            return 200, _edit(EditResult("blocked", "rate_limited"))
        self._drop_handles()
        return 200, _edit(reset_bindings(self._store, self._fence()))

    def rollback(self, payload: object) -> tuple[int, dict]:
        if payload != {"confirm": True}:
            return 400, {"error": "confirmation_required"}
        if self._rate_limited():
            return 200, _edit(EditResult("blocked", "rate_limited"))
        self._drop_handles()
        return 200, _edit(rollback_bindings(self._store, self._fence()))

    def _drop_handles(self) -> None:
        with self._lock:
            self._handles.clear()


def _blocked_preview(reason: str, roles: dict | None = None) -> dict:
    return {"version": 1, "status": "blocked", "reason": reason, "roles": roles or {},
            "writes_vault": False, "activation_allowed": False}


def _edit(result: EditResult) -> dict:
    return {"version": 1, "status": result.status, "reason": result.reason,
            "writes_vault": False}
