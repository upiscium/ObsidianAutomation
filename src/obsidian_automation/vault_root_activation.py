"""#303 V1/D: explicit Vault root activation under the restricted contract.

Owner-approved policy (2026-11-12): C (tree coherence) stays unproven, so the
guarantee is limited to identity pinning. An activation:

1. quiesces Snapshot, Daily, Formatter and B3 in the Dashboard lock order and
   refuses while any run is queued or running;
2. re-walks the selected root read-only and requires the same leaf ``oc:id``
   the operator previewed;
3. changes only ``vault_folder`` through a local revision CAS;
4. records the ``oc:id`` chain in a private binding file, keeping the previous
   binding for one-step rollback.

Not guaranteed: edits by other Nextcloud clients between the final read and
the CAS, or after it. Each Job must call ``check_root_pin`` before acting
(not yet wired into the Jobs). Nothing here reads, moves, writes or deletes
Vault contents. Disabled unless ``enabled=True`` is passed explicitly, and
blocked with zero provider I/O while the proof register is unproven.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import dataclass
import json
from pathlib import Path
import stat
import time
from typing import Callable, ContextManager, Iterator, Protocol

from .snapshot_setup import SetupStore, _atomic
from .vault_root_dav_verifier import (
    CURRENT_PROVIDER_PROOF, DavTransport, ProviderProof, TrustedDavAccount,
    verify_root_chain,
)
from .vault_root_preview import RootSelection, _identifier, _parts

BINDING_FILE = "vault_root_binding.json"
# Sticky: once any activation has started, a missing binding never falls back
# to "legacy". Back up and restore it together with the binding file.
PINNING_MARKER = "vault_root_pinning.enabled"
_BUSY = "SELECT 1 FROM runs WHERE status IN ('queued','running') LIMIT 1"


@dataclass(frozen=True)
class ActivationResult:
    status: str  # activated | rolled_back | blocked | uncertain
    reason: str
    writes_vault: bool = False


@dataclass(frozen=True)
class RootPin:
    path: str
    chain: tuple[str, ...] | None  # None: legacy root that was never pinned
    instance_id: str | None


class JobFence(Protocol):
    def quiesced(self) -> ContextManager[bool]: ...
    def after_change(self) -> None: ...


class DashboardJobFence:
    """Same lock order as ``POST /api/v1/nextcloud/connections``:
    Formatter write gate -> Snapshot -> Daily -> Formatter -> B3 (last)."""

    def __init__(self, store: SetupStore, *, snapshot_job, daily_job=None,
                 formatter_job=None, b3_observer_job=None):
        # Snapshot is mandatory, as for the shared-connection save. Pass every
        # other Job the Dashboard was built with; an omitted Job is unfenced.
        if snapshot_job is None:
            raise ValueError("snapshot_job_required")
        self._store = store
        self._snapshot = snapshot_job
        self._daily = daily_job
        self._formatter = formatter_job
        self._b3 = b3_observer_job

    @contextmanager
    def quiesced(self) -> Iterator[bool]:
        jobs = [j for j in (self._snapshot, self._daily, self._formatter, self._b3) if j]
        with ExitStack() as stack:
            stack.enter_context(self._formatter._write_gate if self._formatter
                                else nullcontext())
            for job in jobs:
                stack.enter_context(job._lock)
            yield any(job.db.execute(_BUSY).fetchone() for job in jobs)

    def after_change(self) -> None:
        # Mirror the shared-connection save: pause schedules; the new root
        # is never used by a run that was admitted under the old one.
        if self._daily:
            settings = self._daily.settings()
            self._daily.update({"enabled": False,
                                "interval_seconds": settings["interval_seconds"]})
        if self._formatter:
            self._formatter.connections_saved()
        if self._snapshot and self._store.load() is not None:
            self._snapshot.connections_saved(self._store.generation())


def _read_binding(state: Path) -> dict | None:
    path = state / BINDING_FILE
    try:
        meta = path.lstat()
        if not stat.S_ISREG(meta.st_mode) or stat.S_IMODE(meta.st_mode) != 0o600:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if type(data) is dict and data.get("version") == 1 else None


def _pin(value: object) -> RootPin | None:
    if type(value) is not dict or type(value.get("path")) is not str:
        return None
    chain = value.get("chain")
    if chain is not None and (type(chain) is not list
                              or not all(_identifier(c) for c in chain)):
        return None
    instance = value.get("instance_id")
    if instance is not None and not _identifier(instance):
        return None
    if (chain is not None) != (instance is not None) or (
            chain is not None and (not chain or not all(c.endswith(instance) for c in chain))):
        return None  # oc:id embeds the instance ID; a foreign chain is invalid
    return RootPin(value["path"], tuple(chain) if chain is not None else None, instance)


def _pin_json(pin: RootPin | None) -> dict | None:
    if pin is None:
        return None
    return {"path": pin.path, "chain": list(pin.chain) if pin.chain is not None else None,
            "instance_id": pin.instance_id}


def _pin_for(state: Path, folder: str) -> RootPin | None:
    """Pin that describes ``folder``. After an uncertain write the config may
    still hold the *previous* root, so keep its identity rather than
    downgrading it to an unpinned legacy root."""
    data = _read_binding(state)
    for key in ("current", "previous"):
        pin = _pin(data.get(key)) if data else None
        if pin is not None and pin.path == folder:
            return pin
    return None


def current_pin(state: Path) -> RootPin | None:
    data = _read_binding(state)
    return _pin(data.get("current")) if data else None


def _write_binding(state: Path, current: RootPin, previous: RootPin | None,
                   outcome: str, clock: Callable[[], float]) -> bool:
    try:
        _atomic(state / BINDING_FILE, json.dumps({
            "version": 1, "current": _pin_json(current),
            "previous": _pin_json(previous), "outcome": outcome,
            "recorded_at": int(clock()),
        }, sort_keys=True))
        return True
    except OSError:
        return False


def _restore_binding(state: Path, old: bytes | None) -> bool:
    path = state / BINDING_FILE
    try:
        if old is None:
            path.unlink(missing_ok=True)
        else:
            _atomic(path, old.decode("utf-8"))
        return True
    except (OSError, UnicodeError):
        return False


def _binding_bytes(state: Path) -> bytes | None:
    try:
        return (state / BINDING_FILE).read_bytes()
    except FileNotFoundError:
        return None


def _commit(store: SetupStore, fence: JobFence, new: RootPin, previous: RootPin | None,
            folder: str, revision: str, done: str, clock: Callable[[], float],
            ) -> ActivationResult:
    """Pin first, then CAS. Every crash point leaves the Jobs fail-closed:
    a pin that names a folder the config does not hold is a ``mismatch``."""
    marker = store.state / PINNING_MARKER
    try:
        old = _binding_bytes(store.state)
        had_marker = marker.exists() or marker.is_symlink()
        _atomic(marker, "1\n")
    except OSError:
        return ActivationResult("blocked", "pin_unrecorded")

    def undo(reason: str) -> ActivationResult:
        # A "blocked" result must leave Jobs exactly as before, including
        # the sticky marker on a first-ever attempt.
        if not _restore_binding(store.state, old):
            return ActivationResult("uncertain", "pin_restore_failed")
        if not had_marker:
            try:
                marker.unlink(missing_ok=True)
            except OSError:
                return ActivationResult("uncertain", "pin_restore_failed")
        return ActivationResult("blocked", reason)

    if not _write_binding(store.state, new, previous, "pending", clock):
        return undo("pin_unrecorded")
    outcome = store.replace_vault_folder(folder, revision)
    if outcome == "uncertain":
        return ActivationResult("uncertain", "config_write_uncertain")
    if outcome != "replaced":
        return undo(outcome)
    # The pin stays "pending" (Jobs refuse) until the schedules are paused.
    try:
        fence.after_change()
    except Exception:
        return ActivationResult("uncertain", "job_pause_failed")
    if not _write_binding(store.state, new, previous, done, clock):
        return ActivationResult("uncertain", "pin_unfinalized")
    return ActivationResult(done, "requires_job_reenable")


def activate_vault_root(
    *,
    enabled: bool,
    store: SetupStore,
    fence: JobFence,
    account: TrustedDavAccount,
    transport: DavTransport,
    requested: str,
    expected_leaf_id: str,
    expected_revision: str,
    container_resource_id: str,
    active_resource_id: str,
    proof: ProviderProof = CURRENT_PROVIDER_PROOF,
    clock: Callable[[], float] = time.time,
) -> ActivationResult:
    if enabled is not True:
        return ActivationResult("blocked", "activation_disabled")
    if type(proof) is not ProviderProof or not proof.admits(restricted_coherence=True):
        return ActivationResult("blocked", "provider_unproven")
    if (type(requested) is not str or _parts(requested, new_candidate=True) is None
            or not all(_identifier(v) for v in (expected_leaf_id, expected_revision,
                                                container_resource_id,
                                                active_resource_id))):
        return ActivationResult("blocked", "invalid_request")
    with fence.quiesced() as busy:
        if busy:
            return ActivationResult("blocked", "jobs_in_flight")
        if store.nextcloud_revision() != expected_revision:
            return ActivationResult("blocked", "stale_source")
        saved = store.load_nextcloud()
        if saved is None:
            return ActivationResult("blocked", "binding_unavailable")
        selection = RootSelection(
            requested=requested, active=saved["vault_folder"],
            active_resource_id=active_resource_id,
            expected_revision=expected_revision, current_revision=expected_revision,
            namespace=account.instance_id,
            container_resource_id=container_resource_id, jobs_in_flight=False,
        )
        preview, chain = verify_root_chain(
            selection, account, transport, source_revision=store.nextcloud_revision,
            proof=proof, expected_resource_id=expected_leaf_id,
            restricted_coherence=True,
        )
        if preview.status != "candidate_valid" or chain is None:
            return ActivationResult("blocked", preview.reason)
        previous = (_pin_for(store.state, saved["vault_folder"])
                    or RootPin(saved["vault_folder"], None, None))
        return _commit(store, fence, RootPin(requested, chain, account.instance_id),
                       previous, requested, expected_revision, "activated", clock)


def rollback_vault_root(
    *, enabled: bool, store: SetupStore, fence: JobFence,
    clock: Callable[[], float] = time.time,
) -> ActivationResult:
    """Restore the previous ``vault_folder`` only. No provider I/O, no Vault
    changes. Refuses unless the current config still matches the pin."""
    if enabled is not True:
        return ActivationResult("blocked", "activation_disabled")
    data = _read_binding(store.state)
    current = _pin(data.get("current")) if data else None
    previous = _pin(data.get("previous")) if data else None
    if current is None or previous is None:
        return ActivationResult("blocked", "nothing_to_roll_back")
    with fence.quiesced() as busy:
        if busy:
            return ActivationResult("blocked", "jobs_in_flight")
        saved = store.load_nextcloud()
        revision = store.nextcloud_revision()
        if saved is None or revision is None or saved["vault_folder"] != current.path:
            return ActivationResult("blocked", "binding_diverged")
        return _commit(store, fence, previous, None, previous.path, revision,
                       "rolled_back", clock)


def check_root_pin(store: SetupStore, read_leaf_id: Callable[[str], object]) -> str:
    """Pre-run guard for Jobs (restricted contract, part 3).

    ``legacy_unpinned``: no activation ever started (today's behaviour), or
    a completed rollback to a root that was never pinned. ``ok``: the saved
    root still resolves to the pinned ``oc:id``. Anything else, including a
    ``pending`` pin or a lost binding after activation, must stop the Job.
    """
    saved = store.load_nextcloud()
    if saved is None:
        return "unavailable"
    path = store.state / BINDING_FILE
    marker = store.state / PINNING_MARKER
    if not path.exists() and not path.is_symlink():
        return "mismatch" if marker.exists() or marker.is_symlink() else "legacy_unpinned"
    data = _read_binding(store.state)
    pin = _pin(data.get("current")) if data else None
    if (pin is None or pin.path != saved["vault_folder"]
            or data.get("outcome") not in ("activated", "rolled_back")):
        return "mismatch"
    if pin.chain is None:
        return "legacy_unpinned"
    try:
        observed = read_leaf_id(pin.path)
    except Exception:
        return "unavailable"
    if type(observed) is not str:
        return "unavailable"
    return "ok" if observed == pin.chain[-1] else "mismatch"
