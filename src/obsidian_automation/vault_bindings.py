"""#303 Phase 2 / P2-1: role -> folder mapping record, validation, Preview.

Read-only. Nothing here writes the record, the Vault or a Job: Jobs still
use ``vault_layout`` defaults until P2-2/P2-3, and saving a mapping is P2-4.

Record (``vault_bindings.json`` in the state directory, 0600)::

    {"version": 1,
     "root_pin": ["<oc:id>", ...],          # V1 chain of the active root
     "roles": {"daily": "00-DailyNote", ...},
     "role_ids": {"daily": "<oc:id>", ...}}

A missing record means today's names (``vault_layout`` defaults). A record
that exists but is unreadable or invalid is an error: callers fail closed and
never fall back to the defaults.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import time
from types import MappingProxyType
from typing import Callable, Mapping

from .vault_layout import DEFAULT_ROLE_FOLDERS, ROLES
from .vault_root_activation import RootPin, RootPinRefused, read_private_json
from .vault_root_dav_verifier import (
    CURRENT_PROVIDER_PROOF, DavTransport, ProviderProof, verify_root_chain,
)
from .vault_root_preview import MAX_COMPONENTS, RootSelection, _fold, _identifier, _parts
from .vault_root_selection import TrustedRootContext

BINDINGS_FILE = "vault_bindings.json"
# P2-4: present while a mapping change is being written; every RoleGuard
# refuses until it is gone (vault_bindings_editor).
PENDING_FILE = "vault_bindings.pending"
SCHEMA_VERSION = 1
_KEYS = frozenset({"version", "root_pin", "roles", "role_ids"})
MAX_PATH_CHARS = 100  # the V1 path grammar bound (vault_root_preview._ROOT)


class BindingsError(ValueError):
    """Invalid mapping. ``reason`` is a short code safe to show."""

    def __init__(self, reason: str, role: str | None = None):
        super().__init__(reason if role is None else f"{role}: {reason}")
        self.reason = reason
        self.role = role


@dataclass(frozen=True)
class VaultBindings:
    roles: Mapping[str, str]
    root_pin: tuple[str, ...] | None   # None only for the defaults
    role_ids: Mapping[str, str] | None  # None only for the defaults

    @property
    def is_default(self) -> bool:
        return self.root_pin is None


def default_bindings() -> VaultBindings:
    return VaultBindings(DEFAULT_ROLE_FOLDERS, None, None)


def validate_role_paths(roles: object) -> Mapping[str, str]:
    """Check the shape of a role -> folder mapping (no provider I/O).

    Each folder is relative to the Vault root and uses the V1 new-candidate
    grammar. Folders must differ, must not nest inside each other, and must
    not collide after casefold/NFKC folding.
    """
    if type(roles) is not dict or set(roles) != set(ROLES):
        raise BindingsError("invalid_roles")
    folded: dict[str, tuple[str, ...]] = {}
    for role in ROLES:
        parts = _parts(roles[role], new_candidate=True)
        if parts is None:
            raise BindingsError("invalid_path", role)
        folded[role] = tuple(_fold(part) for part in parts)
    for a in ROLES:
        for b in ROLES:
            if a != b and folded[b][:len(folded[a])] == folded[a]:
                raise BindingsError(
                    "duplicate_folder" if folded[a] == folded[b] else "nested_folder", b)
    return MappingProxyType({role: roles[role] for role in ROLES})


def parse_bindings(raw: object) -> VaultBindings:
    if (type(raw) is not dict or set(raw) != _KEYS
            or type(raw["version"]) is not int or raw["version"] != SCHEMA_VERSION):
        raise BindingsError("invalid_record")
    pin = raw["root_pin"]
    if (type(pin) is not list or not pin or not all(_identifier(c) for c in pin)
            or len(set(pin)) != len(pin)):
        raise BindingsError("invalid_record")
    roles = validate_role_paths(raw["roles"])
    ids = raw["role_ids"]
    if (type(ids) is not dict or set(ids) != set(ROLES)
            or not all(_identifier(ids[r]) for r in ROLES)
            or len({ids[r] for r in ROLES}) != len(ROLES)
            or any(ids[r] in pin for r in ROLES)):
        raise BindingsError("invalid_record")
    return VaultBindings(roles, tuple(pin), MappingProxyType({r: ids[r] for r in ROLES}))


def bindings_json(bindings: VaultBindings) -> dict:
    if bindings.is_default or bindings.role_ids is None:
        raise BindingsError("default_not_stored")
    return {"version": SCHEMA_VERSION, "root_pin": list(bindings.root_pin),
            "roles": dict(bindings.roles), "role_ids": dict(bindings.role_ids)}


def load_bindings(state: Path) -> VaultBindings:
    """The stored mapping, or the defaults when no record exists.

    Raises ``BindingsError("unreadable_record")`` for a record that exists but
    is not a private regular JSON file, and ``invalid_record`` (or a path
    reason) for a bad one. It never falls back to the defaults in that case.
    """
    path = state / BINDINGS_FILE
    try:
        os.lstat(path)
    except FileNotFoundError:
        return default_bindings()
    except OSError:
        raise BindingsError("unreadable_record") from None
    raw = read_private_json(path)
    if raw is None:
        raise BindingsError("unreadable_record")
    return parse_bindings(raw)


def _blocked(reason: str) -> dict:
    return {"version": 1, "status": "blocked", "reason": reason, "roles": {},
            "writes_vault": False, "activation_allowed": False}


def preview_bindings(
    proposed: object,
    *,
    current: VaultBindings,
    context: TrustedRootContext | str | None,
    root_pin: RootPin | None,
    transport: DavTransport | None,
    revision: Callable[[], object],
    proof: ProviderProof = CURRENT_PROVIDER_PROOF,
    clock: Callable[[], float] = time.monotonic,
    restricted_coherence: bool = False,
    repin: frozenset = frozenset(),
) -> tuple[dict, VaultBindings | None]:
    """Check a proposed mapping against the live Vault, read-only.

    ``repin`` (P2-4) lists roles whose recreated folder the owner explicitly
    accepts: for those only, a changed ``oc:id`` is not ``identity_changed``.

    Every role must resolve to an existing plain directory below the pinned
    active root (or inside the same accepted share), by the V1 walk. Returns
    a redacted result for the UI (role names, folder labels and reason codes;
    no IDs, URLs or ETags) and, only when every role is valid, the record a
    later, separately approved save may store. The record grants nothing by
    itself: the save must re-check the pins and the Job fence.

    Jobs may run during a Preview because nothing changes; the save (P2-4)
    takes the V1 Job fence.
    """
    try:
        roles = validate_role_paths(proposed)
    except BindingsError as err:
        result = _blocked(err.reason)
        if err.role is not None:
            result["role"] = err.role
        return result, None
    if transport is None or context is None:
        return _blocked("unavailable"), None
    if type(context) is str:
        return _blocked(context), None
    if root_pin is None or root_pin.chain is None:
        return _blocked("root_unpinned"), None
    if root_pin.path != context.active or root_pin.chain[-1] != context.active_resource_id:
        return _blocked("root_pin_mismatch"), None
    if current.root_pin is not None and current.root_pin != root_pin.chain:
        return _blocked("root_changed"), None  # stored mapping is for another root
    root_parts = _parts(context.active, new_candidate=True)
    if root_parts is None:
        return _blocked("root_path_unsupported"), None
    for role in ROLES:
        combined = f"{context.active}/{roles[role]}"
        if len(combined) > MAX_PATH_CHARS or len(combined.split("/")) > MAX_COMPONENTS:
            result = _blocked("path_too_long")
            result["role"] = role
            return result, None

    pin = root_pin.chain
    statuses: dict[str, dict] = {}
    role_ids: dict[str, str] = {}
    chains: dict[str, tuple[str, ...]] = {}
    for role in ROLES:
        selection = RootSelection(
            requested=f"{context.active}/{roles[role]}", active=context.active,
            active_resource_id=context.active_resource_id,
            expected_revision=context.source_revision,
            current_revision=context.source_revision,
            namespace=context.account.instance_id,
            container_resource_id=context.container_resource_id,
            jobs_in_flight=False,
        )
        # A role may not enter another share below the root: it must stay in
        # the root's own storage (the root's share, if the root is one).
        preview, chain = verify_root_chain(
            selection, context.account, transport, source_revision=revision,
            proof=proof, clock=clock, restricted_coherence=restricted_coherence,
            no_share_root_below=len(root_parts),
        )
        if chain is None:
            reason = preview.reason
        elif chain[:len(pin)] != pin:
            reason = "root_changed"  # the root was recreated or moved
        elif len(chain) <= len(pin):
            reason = "outside_root"
        elif (current.role_ids is not None and role not in repin
              and roles[role] == current.roles[role]
              and chain[-1] != current.role_ids[role]):
            reason = "identity_changed"  # same name, recreated folder
        else:
            reason = "ok"
            role_ids[role] = chain[-1]
            chains[role] = chain[len(pin):]
        statuses[role] = {"folder": roles[role], "status": reason,
                          "changed": roles[role] != current.roles[role]}
    # Distinct names may still be one folder on the server (an alias).
    seen: dict[str, int] = {}
    for rid in role_ids.values():
        seen[rid] = seen.get(rid, 0) + 1
    for role, rid in role_ids.items():
        if seen[rid] > 1:
            statuses[role]["status"] = "duplicate_folder"
            continue
        for other in chains:
            if other != role and rid in chains[other][:-1]:
                # Nested on the server: mark the outer and the inner role.
                statuses[role]["status"] = "nested_folder"
                statuses[other]["status"] = "nested_folder"
    ok = all(s["status"] == "ok" for s in statuses.values())
    result = {"version": 1, "status": "valid" if ok else "blocked",
              "reason": "ok" if ok else "role_unverified", "roles": statuses,
              "changed": any(s["changed"] for s in statuses.values()),
              "writes_vault": False, "activation_allowed": False}
    if not ok:
        return result, None
    return result, VaultBindings(roles, root_pin.chain, MappingProxyType(role_ids))


# --- P2-2: consumers -----------------------------------------------------------
#
# Jobs keep *logical* role paths inside (local mirrors, plans, receipts): the
# default folder names. Only the remote edge (the Nextcloud pull) maps a role
# to its *physical* folder from the record, so stored history never changes.

# The mapping a RoleGuard verified for the guarded call. The pull reads its
# folder from this snapshot, so a record saved between the check and the pull
# cannot redirect it to an unpinned folder.
_VERIFIED: ContextVar[VaultBindings | None] = ContextVar("vault_bindings_verified",
                                                         default=None)


def physical_folder(state: Path, role: str) -> str:
    """The folder that holds ``role`` on the server, relative to the Vault root.

    Inside a ``RoleGuard.wrap`` call it uses the mapping the guard verified;
    otherwise it reads the record. Today's name without a record. Raises
    ``BindingsError`` for an unknown role or a bad record: the caller must
    stop, never fall back to the defaults.
    """
    if role not in ROLES:
        raise BindingsError("invalid_roles")
    verified = _VERIFIED.get()
    return (verified if verified is not None else load_bindings(state)).roles[role]


def record_generation(state: Path) -> str | None:
    """Digest to fold into a Job's source generation; None without a record.

    An unreadable or invalid record still yields a value, so the generation
    changes and receipts admitted under the old mapping stop matching. Inside
    a guarded call it describes the mapping the guard verified (the same
    digest the file gives), so a receipt never names a mapping the pull did
    not use.
    """
    verified = _VERIFIED.get()
    if verified is not None:
        if verified.is_default:
            return None
        return _digest(bindings_json(verified))
    path = state / BINDINGS_FILE
    try:
        os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        return "unreadable"
    raw = read_private_json(path)
    if raw is None:
        return "unreadable"
    return _digest(raw)


def _digest(raw: object) -> str:
    encoded = json.dumps(raw, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def fold_generation(generation: str, extra: str | None) -> str:
    """``generation`` unchanged without a record; otherwise bound to it."""
    if extra is None:
        return generation
    return hashlib.sha256(f"{generation}\n{extra}".encode("utf-8")).hexdigest()


def check_role_pins(state: Path, roles: tuple[str, ...], root_status: str,
                    root_pin: RootPin | None,
                    read_identity: Callable[[str], object],
                    unmapped: tuple[str, ...] = ()) -> str:
    return _check_role_pins(state, roles, root_status, root_pin, read_identity,
                            unmapped)[0]


def _check_role_pins(state, roles, root_status, root_pin, read_identity,
                     unmapped=()) -> tuple[str, VaultBindings | None]:
    """Pre-run guard for the role folders a Job reads.

    Without a record this is the root guard's result unchanged. With one, the
    root must be pinned (``ok``) to the record's root, and each role folder
    must still have its recorded ``oc:id``. Anything else is ``mismatch`` (or
    ``unavailable`` when the provider cannot answer).

    ``unmapped`` lists roles the Job still uses under today's name (its code
    does not follow a mapping yet): a record that moves one of them refuses.
    """
    if root_status not in ("ok", "legacy_unpinned"):
        return root_status, None
    pending = state / PENDING_FILE
    if pending.exists() or pending.is_symlink():
        return "mismatch", None  # a mapping change is unfinished
    if (type(roles) is not tuple or not all(r in ROLES for r in roles)
            or type(unmapped) is not tuple or not all(r in roles for r in unmapped)):
        return "mismatch", None
    try:
        bindings = load_bindings(state)
    except BindingsError:
        return "mismatch", None
    if bindings.is_default:
        return root_status, bindings
    if (root_status != "ok" or root_pin is None or root_pin.chain is None
            or root_pin.chain != bindings.root_pin):
        return "mismatch", None
    if any(bindings.roles[r] != DEFAULT_ROLE_FOLDERS[r] for r in unmapped):
        return "mismatch", None
    for role in roles:
        try:
            observed = read_identity(f"{root_pin.path}/{bindings.roles[role]}")
        except Exception:
            return "unavailable", None
        if type(observed) is not str:
            return "unavailable", None
        if observed != bindings.role_ids[role]:
            return "mismatch", None
    return "ok", bindings


class RoleGuard:
    """A ``JobRootGuard`` that also pins the role folders one Job reads."""

    def __init__(self, root_guard, roles: tuple[str, ...], *,
                 unmapped: tuple[str, ...] = ()):
        self._root = root_guard
        self._roles = roles
        self._unmapped = unmapped

    def _verify(self) -> tuple[str, VaultBindings | None]:
        try:
            before = self._root.pin()
            root_status = self._root.status()
            pin = self._root.pin()
            if pin != before:  # an activation landed during the check
                return "unavailable", None
            return _check_role_pins(self._root.state, self._roles, root_status,
                                    pin, self._root.read_identity, self._unmapped)
        except Exception:
            return "unavailable", None

    def status(self) -> str:
        return self._verify()[0]

    def __call__(self) -> VaultBindings | None:
        status, bindings = self._verify()
        if status not in ("ok", "legacy_unpinned"):
            raise RootPinRefused("vault_root_pin_refused")
        return bindings

    def wrap(self, function: Callable) -> Callable:
        """Run ``function`` with the mapping this check verified."""
        def guarded(*args, **kwargs):
            return run_verified(self(), function, *args, **kwargs)
        return guarded


def run_verified(verified: object, function: Callable, *args, **kwargs):
    """Call ``function`` with ``verified`` (a guard's result) as the mapping its
    pulls use. A plain root guard returns None: the record is read as usual."""
    if not isinstance(verified, VaultBindings):
        return function(*args, **kwargs)
    token = _VERIFIED.set(verified)
    try:
        return function(*args, **kwargs)
    finally:
        _VERIFIED.reset(token)
