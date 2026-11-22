"""#303 V1: wire the Vault root picker, enrollment and activation into the app.

Built only when the app starts with ``--vault-root-ui``. Without that flag the
Dashboard routes stay absent (404/405) exactly as before. With it, an
operator on the private ingress can:

1. confirm the shared Nextcloud account (enrollment, gate A);
2. list candidate folders and preview one (read-only);
3. explicitly switch ``vault_folder`` to a previewed folder (restricted
   contract: identity pin, Jobs quiesced, schedules paused afterwards);
4. roll back one step.

No Vault content is read, written, moved or deleted by any of these actions.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .snapshot_setup import SetupStore
from .webdav_connection import rclone_vendor
from .vault_root_activation import (
    ActivationResult, _pin, _read_binding, activate_vault_root,
    https_transport_factory, rollback_vault_root,
)
from .vault_root_dav_verifier import (
    CURRENT_PROVIDER_PROOF, DavResponse, DavTransportError, ProviderProof,
)
from .vault_root_enrollment import context_provider, enroll_account, load_enrollment
from .vault_root_selection import TrustedRootContext, VaultRootSelectionService

_BUSY = "SELECT 1 FROM runs WHERE status IN ('queued','running') LIMIT 1"


class StoreTransport:
    """Transport that always uses the *currently* saved shared connection."""

    def __init__(self, store: SetupStore, factory=https_transport_factory):
        self._store = store
        self._factory = factory

    def __repr__(self) -> str:
        return "StoreTransport(<redacted>)"

    def _current(self):
        saved = self._store.load_nextcloud()
        if saved is None:
            raise DavTransportError("connect")
        return self._factory(saved)

    def propfind(self, origin, href, depth, body, *, timeout, max_bytes) -> DavResponse:
        return self._current().propfind(origin, href, depth, body,
                                        timeout=timeout, max_bytes=max_bytes)


@dataclass
class VaultRootActions:
    """Effectful operations, each refused unless the register admits them."""
    store: SetupStore
    fence_factory: Callable[[], object]
    transport_factory: Callable[[dict], object] = https_transport_factory
    proof: ProviderProof = CURRENT_PROVIDER_PROOF

    def enrolled(self) -> bool:
        return load_enrollment(self.store) is not None

    def active_label(self) -> str | None:
        saved = self.store.load_nextcloud()
        return saved["vault_folder"] if saved else None

    def can_rollback(self) -> bool:
        # Mirrors rollback_vault_root: offered only when it can succeed.
        data = _read_binding(self.store.state)
        saved = self.store.load_nextcloud()
        if saved is None or rclone_vendor(saved["webdav_url"]) != "nextcloud":
            return False
        current = data.get("current") if data else None
        return bool(data and _pin(data.get("previous")) is not None
                    and data.get("outcome") in ("activated", "pending_activated")
                    and type(current) is dict
                    and current.get("path") == saved["vault_folder"])

    def enroll(self) -> str:
        return enroll_account(self.store, self.transport_factory, proof=self.proof)[0]

    def activate(self, path: str, leaf_id: str, revision: str,
                 ctx: TrustedRootContext) -> ActivationResult:
        saved = self.store.load_nextcloud()
        if saved is None:
            return ActivationResult("blocked", "binding_unavailable")
        if rclone_vendor(saved["webdav_url"]) != "nextcloud":
            return ActivationResult("blocked", "provider_unsupported")
        return activate_vault_root(
            enabled=True, store=self.store, fence=self.fence_factory(),
            account=ctx.account, transport=self.transport_factory(saved),
            requested=path, expected_leaf_id=leaf_id, expected_revision=revision,
            container_resource_id=ctx.container_resource_id,
            active_resource_id=ctx.active_resource_id, proof=self.proof,
        )

    def rollback(self) -> ActivationResult:
        saved = self.store.load_nextcloud()
        if saved is None:
            return ActivationResult("blocked", "binding_unavailable")
        if rclone_vendor(saved["webdav_url"]) != "nextcloud":
            return ActivationResult("blocked", "provider_unsupported")
        return rollback_vault_root(enabled=True, store=self.store,
                                   fence=self.fence_factory())


def _jobs(server) -> list:
    return [job for job in (getattr(server, "snapshot_job", None),
                            getattr(server, "daily_job", None),
                            getattr(server, "core_promotion_job", None)) if job is not None]


def jobs_busy(server) -> bool:
    """Read-only check; any error counts as busy (fail-closed)."""
    try:
        return any(job.db.execute(_BUSY).fetchone() for job in _jobs(server))
    except Exception:
        return True


class ThreeServiceBindingsAdapter:
    """Do not expose a System remap that the fixed Core Promotion cannot write.

    The legacy editor safely supported all three roles, but the current
    Promotion Runtime explicitly requires physical System == 98-System. A
    writable System field would strand Promotion or silently shift authority.
    """
    def __init__(self, inner):
        self.inner = inner

    def status(self):
        value = self.inner.status()
        value["editable_roles"] = ["daily", "project"]
        # Do not claim an old nondefault System mapping is currently fixed.
        roles = value.get("roles")
        value["system_fixed_under_promotion"] = (
            type(roles) is dict and roles.get("system") == "98-System"
        )
        return value

    def preview(self, payload):
        # The old editor's restricted-role proposal would otherwise reset a
        # legacy nondefault System folder silently when changing Daily.
        from .vault_bindings import load_bindings, BindingsError
        try:
            current = load_bindings(self.inner._store.state)
        except (BindingsError, OSError, ValueError):
            return 200, {"version": 1, "status": "blocked",
                         "reason": "unreadable_record", "writes_vault": False}
        if current.roles.get("system") != "98-System":
            return 200, {"version": 1, "status": "blocked",
                         "reason": "legacy_system_mapping_requires_reset",
                         "writes_vault": False, "activation_allowed": False}
        roles = payload.get("roles") if type(payload) is dict else None
        if (type(roles) is dict and "system" in roles
            and roles["system"] != "98-System"):
            return 200, {"version": 1, "status": "blocked",
                         "reason": "system_mapping_not_supported",
                         "writes_vault": False, "activation_allowed": False}
        return self.inner.preview(payload)

    def save(self, payload):
        # A previously previewed handle must not silently normalize a legacy
        # nonstandard System mapping, even if that mapping changed afterwards.
        from .vault_bindings import load_bindings, BindingsError
        try:
            current = load_bindings(self.inner._store.state)
        except (BindingsError, OSError, ValueError):
            return 200, {"version": 1, "status": "blocked",
                         "reason": "unreadable_record", "writes_vault": False}
        if current.roles.get("system") != "98-System":
            return 200, {"version": 1, "status": "blocked",
                         "reason": "legacy_system_mapping_requires_reset",
                         "writes_vault": False}
        return self.inner.save(payload)

    def reset(self, payload):
        return self.inner.reset(payload)

    def rollback(self, payload):
        # A legacy previous record may map System elsewhere. Do not restore
        # that unsupported target through the new three-service UI.
        from .vault_bindings_editor import _read_previous
        from .vault_bindings import BindingsError
        try:
            present, previous = _read_previous(self.inner._store.state)
            if (present and previous is not None
                and previous.roles.get("system") != "98-System"):
                return 200, {"version": 1, "status": "blocked",
                             "reason": "system_mapping_not_supported",
                             "writes_vault": False}
        except (BindingsError, OSError, ValueError):
            return 200, {"version": 1, "status": "blocked",
                         "reason": "unreadable_previous_mapping",
                         "writes_vault": False}
        return self.inner.rollback(payload)


def build_vault_bindings_ui(store: SetupStore, server, *,
                            proof: ProviderProof = CURRENT_PROVIDER_PROOF,
                            transport_factory=https_transport_factory):
    """#584 P2-4: the service behind ``/api/v1/vault-bindings*``."""
    from .vault_bindings_editor import VaultBindingsService

    from .three_services_vault_fence import MinimalVaultFence

    def fence():
        return MinimalVaultFence(store, server)
    return ThreeServiceBindingsAdapter(VaultBindingsService(
        store,
        context=context_provider(store, transport_factory,
                                 jobs_busy=lambda: jobs_busy(server), proof=proof),
        transport=StoreTransport(store, transport_factory), fence_factory=fence,
        proof=proof,
    ))


def build_vault_root_ui(store: SetupStore, server, *,
                        proof: ProviderProof = CURRENT_PROVIDER_PROOF,
                        transport_factory=https_transport_factory,
                        ) -> VaultRootSelectionService:
    """The service the Dashboard serves under ``/api/v1/vault-root*``."""
    from .three_services_vault_fence import MinimalVaultFence

    def fence():
        return MinimalVaultFence(store, server)
    actions = VaultRootActions(store, fence, transport_factory, proof)
    return VaultRootSelectionService(
        context=context_provider(store, transport_factory,
                                 jobs_busy=lambda: jobs_busy(server), proof=proof),
        transport=StoreTransport(store, transport_factory), proof=proof,
        restricted_coherence=True, actions=actions, revision=store.nextcloud_revision,
    )
