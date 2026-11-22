"""Serialize Vault configuration changes against *all* three live Job owners.

The old DashboardJobFence omitted Core Promotion. Reusing it would allow a
Vault root/mapping switch to escape the target-identity quarantine.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager

from .vault_bindings import physical_folder


_RUNNING = "SELECT 1 FROM runs WHERE status IN ('queued','running') LIMIT 1"
_IDENTITY = ("webdav_url", "nextcloud_user", "vault_folder")


class MinimalVaultFence:
    def __init__(self, store, server):
        self.store, self.server = store, server
        self._before = None
        self._before_system = None

    @contextmanager
    def quiesced(self):
        promotion = self.server.core_promotion_job
        gate = promotion._effect_gate if promotion is not None else None
        if gate is not None and not gate.acquire(blocking=False):
            yield True
            return
        try:
            with ExitStack() as stack:
                jobs = tuple(j for j in (
                    self.server.snapshot_job, self.server.daily_job, promotion,
                ) if j is not None)
                for job in jobs:
                    stack.enter_context(job._lock)
                try:
                    busy = any(job.db.execute(_RUNNING).fetchone() is not None
                               for job in jobs)
                    if promotion is not None and promotion._unresolved():
                        busy = True
                    old = self.store.load_nextcloud()
                    self._before = (tuple(old.get(k) for k in _IDENTITY)
                                    if old is not None else None)
                    self._before_system = physical_folder(self.store.state, "system")
                except Exception:
                    # If source identity or a Job cannot be observed, do not
                    # allow any change to the canonical Vault destination.
                    busy = True
                yield busy
        finally:
            self._before = None
            self._before_system = None
            if gate is not None:
                gate.release()

    def after_change(self):
        """Invoked with Job locks held while a root/binding marker is pending."""
        source = self.store.load_nextcloud()
        if source is None or self._before is None or self._before_system is None:
            raise RuntimeError("vault_configuration_source_unavailable")
        current = tuple(source.get(k) for k in _IDENTITY)
        system = physical_folder(self.store.state, "system")
        if current != self._before or system != self._before_system:
            runtime = self.server.core_promotion_runtime
            if runtime is not None:
                # This barrier must be durable before the root/bindings change
                # is finalized; A->B->A must never resurrect an old checkpoint.
                runtime.quarantine_for_vault_structure_change()
        snapshot = self.server.snapshot_job
        if snapshot is not None:
            snapshot.connections_saved(self.store.generation())
        for job in (self.server.daily_job, self.server.core_promotion_job):
            if job is not None:
                job.source_invalidated()
