"""#472 R2/B2c-D1/A0 — source-only effect-admission contract proof.

NO production permit, credential, Writer, service or network mutation.
Existing real lock/SSHSIG/Ledger fixture tests DO create disposable local
temporary claim and SQLite files, never a production Vault or credential.
_AbstractEffectProtocol and _ResourceQuarantine are TEST-ONLY: booleans stand
for unimplemented trusted Owner/broker checks, not real security authority.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from test_472_owner_coordination import owner_fixture
from test_472_signed_run_drain import (
    RUN, PLAN, POLICY, OPERATION,
)
from obsidian_automation.vault_format_owner_coordination import (
    OperatorRunCoordinator, OwnerCoordinationDenied,
)
from obsidian_automation.vault_format_run_ledger import (
    DurableRunLedger, RunIntent,
)
from obsidian_automation.vault_format_worker_ipc_client import (
    RunStatusObservation,
)


class _ContractDenied(ValueError):
    """This model has no authority outside its disposable test interpreter."""


@dataclass(frozen=True, slots=True)
class _EffectRecord:
    """Synthetic exact Run binding; no secrets, tokens or Writer handles."""

    run_id: str
    plan_sha256: str
    policy_sha256: str
    operation_id: str
    source_generation: str
    account_binding_sha256: str
    provider_proof_sha256: str
    receipt_root: str
    target_path: str
    precondition_etag: str
    before_sha256: str
    after_sha256: str

    @classmethod
    def from_intent(cls, intent: RunIntent) -> "_EffectRecord":
        return cls(
            run_id=intent.run_id,
            plan_sha256=intent.plan_sha256,
            policy_sha256=intent.policy_sha256,
            operation_id=intent.operation_id,
            source_generation=intent.source_generation,
            account_binding_sha256=intent.account_binding_sha256,
            provider_proof_sha256=intent.provider_proof_sha256,
            receipt_root=intent.receipt_root,
            # Synthetic immutable approved plan details. In real D1, these
            # MUST be recovered from the exact signed plan hash, not from
            # caller-controlled IPC fields.
            target_path="98-System/notes.md",
            precondition_etag='"synthetic-etag-1"',
            before_sha256="1" * 64,
            after_sha256="2" * 64,
        )


@dataclass(frozen=True, slots=True)
class _OwnerJournalSnapshot:
    """One indivisible test-model view of Owner-wide attempt/resource state.

    Real D1/B0 MUST use a durable transaction, not this in-memory assignment.
    """

    fenced_operations: frozenset[tuple[str, str]] = frozenset()
    # Immutable Owner-private attempt binding; a Run ID alone cannot prove
    # which approved target, plan, policy and provider the attempt used.
    attempt_records: frozenset[_EffectRecord] = frozenset()
    active_operations: frozenset[tuple[str, str]] = frozenset()
    unresolved_operations: frozenset[tuple[str, str]] = frozenset()
    targets: frozenset[tuple[str, str]] = frozenset()
    accounts: frozenset[str] = frozenset()
    # Durable Owner journal must distinguish actual remote effect from
    # independently verified never-dispatched/no-effect finality.
    terminal_outcomes: frozenset[tuple[str, str, str]] = frozenset()


class _ResourceQuarantine:
    """Read-only view into the shared Owner-wide journal snapshot."""

    def __init__(self, gate: _AtomicAdmissionGate) -> None:
        self._gate = gate

    @property
    def targets(self) -> frozenset[tuple[str, str]]:
        return self._gate._journal.targets

    @property
    def accounts(self) -> frozenset[str]:
        return self._gate._journal.accounts

    def require_clear(self, record: _EffectRecord) -> None:
        key = (record.account_binding_sha256, record.target_path)
        if record.account_binding_sha256 in self.accounts or key in self.targets:
            raise _ContractDenied("resource_unconfirmed")

    def quarantine_unknown_account(self, account_binding: str) -> None:
        self._gate._journal = replace(
            self._gate._journal,
            accounts=self.accounts | {account_binding},
        )


class _AtomicAdmissionGate:
    """Test-only SINGLE Owner-wide journal transaction/lifecycle domain.

    This is NOT a real SQLite transaction, flock, service or durable journal.
    Each Begin/settlement transition publishes ONE immutable snapshot, so no
    model observer can see quarantine lifted while the attempt remains
    active/unconfirmed. Real process-crash durability is NOT demonstrated.
    """

    def __init__(self) -> None:
        self.open = True
        self.closed_reason: str | None = None
        self.current_credential_generation = "0" * 64
        self.known_generations = {self.current_credential_generation}
        self.generation_epoch = 0
        self.last_invalidating_rotation_epoch = -1
        # All pre-restart Claim snapshots are invalid across every Run.
        # Owner-wide boot reconciliation, not a per-Run bool, reopens gate.
        self.boot_epoch = 0
        self.boot_reconciled = True
        self._journal = _OwnerJournalSnapshot()
        self.resources = _ResourceQuarantine(self)
        self.flock_holders: set[tuple[str, str]] = set()

    @property
    def fenced_operations(self) -> frozenset[tuple[str, str]]:
        return self._journal.fenced_operations

    @property
    def active_operations(self) -> frozenset[tuple[str, str]]:
        return self._journal.active_operations

    @property
    def unresolved_operations(self) -> frozenset[tuple[str, str]]:
        return self._journal.unresolved_operations

    @staticmethod
    def _key(record: _EffectRecord) -> tuple[str, str]:
        return record.run_id, record.operation_id

    def commit_begin(self, record: _EffectRecord) -> None:
        if not self.open:
            raise _ContractDenied("new_effects_suspended")
        key=self._key(record)
        old=self._journal
        if key in old.fenced_operations:
            raise _ContractDenied("attempt_already_recorded")
        self.resources.require_clear(record)
        target=(record.account_binding_sha256, record.target_path)
        # In production the following is ONE durable Owner-wide transaction.
        self._journal=replace(
            old,
            fenced_operations=old.fenced_operations | {key},
            attempt_records=old.attempt_records | {record},
            active_operations=old.active_operations | {key},
            targets=old.targets | {target},
        )

    def mark_uncertain(self, record: _EffectRecord) -> None:
        """Never revive a terminal attempt or accept an unbound effect key."""
        key=self._key(record)
        old=self._journal
        if any((run_id, operation_id) == key for run_id, operation_id, _ in old.terminal_outcomes):
            raise _ContractDenied("attempt_terminal")
        if (record not in old.attempt_records
                or key not in old.fenced_operations
                or (key not in old.active_operations
                    and key not in old.unresolved_operations)):
            raise _ContractDenied("attempt_not_pending")
        self._journal=replace(
            old,
            active_operations=old.active_operations - {key},
            unresolved_operations=old.unresolved_operations | {key},
        )

    def commit_verified_settlement(
        self, record: _EffectRecord, *, disposition: str,
    ) -> None:
        """Publish terminal attempt and resource release in ONE model update.

        Only call after independently verified operation-specific finality.
        No separately callable resource-release operation exists.
        """
        if disposition not in ("verified_remote_effect", "verified_no_effect"):
            raise _ContractDenied("settlement_disposition_invalid")
        old=self._journal
        key=self._key(record)
        if (record not in old.attempt_records
                or key not in old.fenced_operations
                or (key not in old.active_operations
                    and key not in old.unresolved_operations)):
            raise _ContractDenied("settlement_unverified")
        target=(record.account_binding_sha256, record.target_path)
        if target not in old.targets:
            raise _ContractDenied("resource_finality_unverified")
        self._journal=replace(
            old,
            active_operations=old.active_operations - {key},
            unresolved_operations=old.unresolved_operations - {key},
            targets=old.targets - {target},
            terminal_outcomes=old.terminal_outcomes | {
                (record.run_id, record.operation_id, disposition),
            },
        )

    def owner_restart(self) -> None:
        """Invalidate every pre-restart Claim AND in-flight Owner dispatch."""
        self.close_admissions("restart")
        self.boot_epoch += 1
        self.boot_reconciled = False
        old=self._journal
        # A restarted Owner loses every old session lease. Remote effects
        # may nevertheless be live; preserve every target in quarantine.
        self._journal=replace(
            old,
            active_operations=frozenset(),
            unresolved_operations=old.unresolved_operations | old.active_operations,
        )
        self.flock_holders.clear()

    def owner_reconcile_boot(self, *, trusted_owner_scan: bool) -> bool:
        """Explicit synthetic Owner-wide readback; never implicit per Run."""
        if trusted_owner_scan is not True:
            raise _ContractDenied("owner_boot_unverified")
        if (self.active_operations or self.unresolved_operations
                or self.resources.targets or self.resources.accounts
                or self.flock_holders):
            return False
        self.boot_reconciled = True
        return self.reopen_after_verified_boot()

    def owner_reconcile_post_restart(
        self, record: _EffectRecord, *,
        trusted_owner_peer: bool,
        trusted_private_attempt: bool,
        trusted_broker_finality: bool,
        broker_disposition: str,
        durable_broker_receipt: bool,
        settlement_journal_durable: bool,
    ) -> None:
        """Fresh OWNER-side post-restart reconciliation while gate is CLOSED.

        Test-only Owner-private function: its true/false inputs model
        independently authenticated Owner identity, private Ledger + immutable
        attempt binding, operation-specific Broker finality, and durable
        result/journal proof. NEVER expose these flags or this function through
        a Worker endpoint; they are not authorization credentials.

        This is distinct from _AbstractEffectProtocol.reconcile(), whose
        stale pre-reboot session MUST continue to fail closed.
        """
        if (self.boot_epoch < 1 or self.open
                or self.boot_reconciled is not False
                or self.flock_holders or self.active_operations):
            raise _ContractDenied("owner_boot_unverified")
        if (trusted_owner_peer is not True
                or trusted_private_attempt is not True
                or trusted_broker_finality is not True
                or durable_broker_receipt is not True
                or settlement_journal_durable is not True):
            raise _ContractDenied("owner_reconciliation_unverified")
        if (type(broker_disposition) is not str
                or broker_disposition not in (
                    "verified_remote_effect", "verified_no_effect",
                )):
            raise _ContractDenied("owner_disposition_invalid")
        old = self._journal
        if (type(record) is not _EffectRecord
                or record not in old.attempt_records
                or self._key(record) not in old.unresolved_operations
                or (record.account_binding_sha256, record.target_path)
                not in old.targets):
            raise _ContractDenied("owner_attempt_binding_unverified")
        # The only terminal transition atomically records exact disposition
        # AND lifts this one resource-conflict key. Account-wide quarantine
        # (unknown wider remote effect) remains independently in force.
        self.commit_verified_settlement(
            record, disposition=broker_disposition,
        )

    def owner_reconcile_unknown_account(
        self, account_binding: str, *,
        trusted_owner_peer: bool,
        trusted_account_scope_finality: bool,
        durable_broker_receipt: bool,
        settlement_journal_durable: bool,
    ) -> None:
        """Only independently verified ACCOUNT-WIDE finality can lift it.

        All known attempts/targets for this account must be terminal first.
        In a real Broker, delayed/out-of-band effects may make this proof
        impossible, requiring continued pending quarantine/Human resolution.
        """
        if (self.boot_epoch < 1 or self.open
                or self.boot_reconciled is not False
                or self.flock_holders or self.active_operations):
            raise _ContractDenied("owner_boot_unverified")
        if (trusted_owner_peer is not True
                or trusted_account_scope_finality is not True
                or durable_broker_receipt is not True
                or settlement_journal_durable is not True):
            raise _ContractDenied("account_finality_unverified")
        old = self._journal
        if (type(account_binding) is not str
                or account_binding not in old.accounts):
            raise _ContractDenied("account_binding_unverified")
        if (any(account == account_binding for account, _path in old.targets)
                or any(
                    (record.run_id, record.operation_id)
                    in old.unresolved_operations | old.active_operations
                    for record in old.attempt_records
                    if record.account_binding_sha256 == account_binding
                )):
            raise _ContractDenied("account_unresolved")
        # Owner-only one-snapshot update. Existing unrelated-account quarantine
        # is retained. Real SQL durability/OS identity are NOT modeled.
        self._journal = replace(
            old, accounts=old.accounts - {account_binding},
        )

    def close_admissions(self, reason: str) -> None:
        self.open=False
        if self.closed_reason is None or self.closed_reason == "restart":
            self.closed_reason=reason

    def reopen_after_verified_boot(self) -> bool:
        if (self.closed_reason != "restart"
                or self.boot_reconciled is not True
                or self.active_operations or self.unresolved_operations
                or self.resources.targets or self.resources.accounts
                or self.flock_holders):
            return False
        self.open=True
        self.closed_reason=None
        return True


class _AbstractEffectProtocol:
    """Deterministic contract reference, never a credential or IPC service.

    The boolean Owner/broker checks, event journal and lock are modeled in
    memory. They neither issue a capability nor prove durable fsync or an
    authentic real-world Owner. That is the purpose of later D1 phases.
    """

    def __init__(
        self, expected: _EffectRecord, *,
        admission_gate: _AtomicAdmissionGate,
    ) -> None:
        if type(admission_gate) is not _AtomicAdmissionGate:
            raise _ContractDenied("shared_owner_gate_required")
        if admission_gate.boot_reconciled is not True:
            raise _ContractDenied("owner_boot_unverified")
        self.expected = expected
        # Explicit shared Owner context is REQUIRED, never created silently.
        self.admission_gate = admission_gate
        self.resources = admission_gate.resources
        # A real Owner must persist these fields in its PRIVATE Ledger,
        # together with the signed Claim, under the same rotation authority.
        # Immutable claim-time snapshot, strictly from shared Owner state;
        # signed Vault source generation remains a separate Run identity.
        self.claim_reserved_epoch = admission_gate.generation_epoch
        self.claim_boot_epoch = admission_gate.boot_epoch
        self.claim_credential_generation = admission_gate.current_credential_generation
        self.ledger_state = "claimed"
        self.boot_reconciled = True
        self.signed_valid = True
        self.broker_only_egress = True  # Abstract trusted OS rule, NOT enforced.
        self.lock_held = False
        self.attempt_persisted = False
        self.start_ack_sent = False
        self.dispatch_recorded = False
        self.unresolved = False
        self.settled = False
        self.revoked = False
        self.off_requested = False
        self.pending_rotation: str | None = None
        self.events: list[str] = []

    @staticmethod
    def _deny(reason: str) -> None:
        raise _ContractDenied(reason)

    @property
    def current_credential_generation(self) -> str:
        return self.admission_gate.current_credential_generation

    @current_credential_generation.setter
    def current_credential_generation(self, value: str) -> None:
        # Used by tests to simulate a trusted external Broker change.
        self.admission_gate.current_credential_generation = value

    @property
    def known_generations(self) -> set[str]:
        return self.admission_gate.known_generations

    @property
    def generation_epoch(self) -> int:
        return self.admission_gate.generation_epoch

    @property
    def last_invalidating_rotation_epoch(self) -> int:
        return self.admission_gate.last_invalidating_rotation_epoch

    @property
    def rotation_fenced_prior_claim(self) -> bool:
        return self.claim_reserved_epoch <= self.last_invalidating_rotation_epoch

    def begin(
        self, record: _EffectRecord, *,
        trusted_ledger_check: bool = True,
        trusted_generation_check: bool = True,
        journal_outcome: str = "durable",
        before_fence=None,  # test-only deterministic interleaving injection
    ) -> None:
        # Local terminal checks are observational fast failures only. The
        # Owner-wide gate and all trusted inputs are still rechecked in the
        # atomic attempt-fence transaction before an ACK or any dispatch.
        if self.off_requested or self.pending_rotation is not None:
            self._deny("new_effects_suspended")
        if (self.rotation_fenced_prior_claim
                or self.claim_reserved_epoch <= self.last_invalidating_rotation_epoch):
            self._deny("claim_stale_after_rotation")
        if self.attempt_persisted or self.unresolved or self.settled:
            self._deny("attempt_already_recorded")
        if self.revoked:
            self._deny("revoked")
        if not self.admission_gate.open:
            self._deny("new_effects_suspended")
        if (self.claim_boot_epoch != self.admission_gate.boot_epoch
                or self.admission_gate.boot_reconciled is not True):
            self._deny("owner_ledger_unverified")
        if self.lock_held or self.admission_gate.flock_holders:
            self._deny("lease_busy")
        self.lock_held = True
        self.admission_gate.flock_holders.add(
            self.admission_gate._key(self.expected),
        )
        self.events.append("owner_lock_acquired")
        try:
            if (self.boot_reconciled is not True
                    or self.admission_gate.boot_reconciled is not True
                    or self.claim_boot_epoch != self.admission_gate.boot_epoch
                    or self.ledger_state != "claimed"
                    or trusted_ledger_check is not True):
                self._deny("owner_ledger_unverified")
            self.events.append("owner_ledger_checked")
            if self.signed_valid is not True or record != self.expected:
                self._deny("signed_binding_unverified")
            if (trusted_generation_check is not True
                    or self.claim_credential_generation != self.current_credential_generation):
                self._deny("credential_generation_changed")
            if self.broker_only_egress is not True:
                self._deny("broker_boundary_untrusted")
            self.events.append("generation_verified")
            if journal_outcome not in ("durable", "ambiguous"):
                self._deny("journal_unverified")
            # Inject a competing Off/rotation AFTER initial checks but BEFORE
            # the atomic journal fence transaction (test only). This is the
            # interleaving that a non-atomic check-then-write would miss.
            if before_fence is not None:
                before_fence()
            # In production commit_begin MUST be an atomic persistent
            # transaction with Off/rotation gate close and resource index.
            self.admission_gate.commit_begin(record)
            self.attempt_persisted = True
            self.ledger_state = "unconfirmed"
            self.events.extend(("attempt_fence_written", "resource_fenced"))
            if journal_outcome != "durable":
                self.unresolved = True
                self.admission_gate.mark_uncertain(record)
                self.events.append("journal_unconfirmed")
                self._deny("journal_unverified")
            self.events.append("start_ack")
            self.start_ack_sent = True
        except BaseException:
            self.lock_held = False
            self.admission_gate.flock_holders.discard(
                self.admission_gate._key(self.expected),
            )
            raise

    def broker_dispatch(
        self, record: _EffectRecord, *, owner_peer_authenticated: bool = True,
        trusted_broker_generation: bool = True,
    ) -> None:
        """Abstract enforceable broker boundary: only Owner initiates dispatch."""
        if (not self.lock_held or not self.attempt_persisted
                or not self.start_ack_sent or self.unresolved):
            self._deny("broker_admission_missing")
        key=self.admission_gate._key(self.expected)
        if (self.claim_boot_epoch != self.admission_gate.boot_epoch
                or self.admission_gate.boot_reconciled is not True
                or key not in self.admission_gate.flock_holders):
            # Owner restart already fenced active attempts as unresolved.
            # Never let a stale session change a terminal Owner journal.
            self.unresolved=True
            self._deny("owner_boot_unverified")
        if self.expected.account_binding_sha256 in self.admission_gate.resources.accounts:
            self.unresolved=True
            self.admission_gate.mark_uncertain(self.expected)
            self._deny("resource_unconfirmed")
        if self.dispatch_recorded:
            self._deny("dispatch_already_recorded")
        if owner_peer_authenticated is not True or record != self.expected:
            self._deny("broker_peer_or_identity_invalid")
        if (trusted_broker_generation is not True
                or self.current_credential_generation != self.claim_credential_generation
                or self.broker_only_egress is not True):
            self.unresolved = True
            self._deny("broker_generation_unverified")
        # In production broker journals dispatch before actual HTTP. The
        # model makes no request and holds no provider credentials.
        self.dispatch_recorded = True
        self.events.append("broker_dispatch_possible")

    def finish(
        self, *, trusted_readback: bool, durable_receipt: bool,
        trusted_operation_finality: bool = False,
        settlement_journal_durable: bool = True,
    ) -> None:
        """Readback+receipt alone NEVER proves operation-specific finality.

        Booleans are test-only placeholders for trusted Broker/Owner proofs.
        A normal success requires broker-specific finality, validated
        readback, durable result and durable Owner-wide journal commit.
        """
        if not self.lock_held or not self.attempt_persisted:
            self._deny("no_active_effect")
        if (self.claim_boot_epoch != self.admission_gate.boot_epoch
                or self.admission_gate.boot_reconciled is not True
                or self.admission_gate._key(self.expected)
                    not in self.admission_gate.flock_holders):
            # Stale Owner connection was revoked by reboot; do not call the
            # journal's mark_uncertain or settlement from this old session.
            self.unresolved = True
            self.lock_held = False
            self._deny("owner_boot_unverified")
        if (self.dispatch_recorded and trusted_readback is True
                and durable_receipt is True
                and trusted_operation_finality is True
                and settlement_journal_durable is True
                and self.claim_boot_epoch == self.admission_gate.boot_epoch
                and self.admission_gate.boot_reconciled is True
                and self.admission_gate._key(self.expected) in self.admission_gate.flock_holders
                and self.expected.account_binding_sha256 not in self.admission_gate.resources.accounts):
            self.admission_gate.commit_verified_settlement(
                self.expected, disposition="verified_remote_effect",
            )
            self.settled = True
            self.events.append("effect_settled")
        else:
            self.unresolved = True
            self.admission_gate.mark_uncertain(self.expected)
            self.events.append("effect_unconfirmed")
        self.lock_held = False
        self.admission_gate.flock_holders.discard(
            self.admission_gate._key(self.expected),
        )
        self.events.append("owner_lock_released")

    def disconnected(self) -> None:
        if not self.lock_held:
            self._deny("no_active_effect")
        if (self.claim_boot_epoch != self.admission_gate.boot_epoch
                or self.admission_gate.boot_reconciled is not True
                or self.admission_gate._key(self.expected)
                    not in self.admission_gate.flock_holders):
            self.unresolved = True
            self.lock_held = False
            self._deny("owner_boot_unverified")
        self.unresolved = True
        self.admission_gate.mark_uncertain(self.expected)
        self.lock_held = False
        self.admission_gate.flock_holders.discard(
            self.admission_gate._key(self.expected),
        )
        self.events.extend(("effect_unconfirmed", "owner_lock_released"))

    def restart(self) -> None:
        # Lifecycle control is modeled as Owner-initiated. An obsolete
        # session may not trigger another global reboot/epoch mutation.
        if self.claim_boot_epoch != self.admission_gate.boot_epoch:
            self._deny("owner_boot_unverified")
        self.admission_gate.owner_restart()
        if self.lock_held:
            self.unresolved = True
            self.admission_gate.mark_uncertain(self.expected)
            self.lock_held = False
            self.admission_gate.flock_holders.discard(
                self.admission_gate._key(self.expected),
            )
        self.boot_reconciled = False
        if self.ledger_state == "claimed":
            self.ledger_state = "unconfirmed"
        self.events.append("restart_unconfirmed")

    def recover_boot(self) -> None:
        # A per-Run observation MUST NOT reopen the Owner-wide gate. An
        # independent Owner-wide scan must confirm recovery and then only
        # post-restart authorized Claims can be admitted.
        self.boot_reconciled = True

    def reconcile(
        self, *, trusted_provider_outcome: bool, durable_receipt: bool,
        trusted_never_dispatched: bool = False,
        settlement_journal_durable: bool = True,
    ) -> None:
        # A broker must prove FINALITY for a dispatched operation; an
        # attempt fenced but NEVER dispatched may instead have a separately
        # authenticated durable no-effect terminal result. A matching
        # provider file hash alone is NOT proof of either outcome.
        # A stale pre-reboot session cannot adjudicate its own old attempt.
        # A fresh Owner-controlled reconciler must be established separately.
        if (self.claim_boot_epoch != self.admission_gate.boot_epoch
                or self.admission_gate.boot_reconciled is not True):
            self._deny("owner_boot_unverified")
        if not self.unresolved or self.lock_held or durable_receipt is not True:
            self._deny("reconciliation_unverified")
        if self.dispatch_recorded:
            if trusted_provider_outcome is not True:
                self._deny("reconciliation_unverified")
            disposition="verified_remote_effect"
            self.events.append(disposition)
        else:
            if trusted_never_dispatched is not True:
                self._deny("reconciliation_unverified")
            disposition="verified_no_effect"
            self.events.append("verified_no_dispatch")
        if settlement_journal_durable is not True:
            self._deny("settlement_unconfirmed")
        self.admission_gate.commit_verified_settlement(
            self.expected, disposition=disposition,
        )
        self.settled = True
        self.unresolved = False
        self.events.append("independent_reconciliation")

    def request_off(self) -> str:
        """Durably close admission gate BEFORE any blocking flock wait."""
        self.admission_gate.close_admissions("off")
        self.events.append("admission_gate_closed")
        self.off_requested = True
        self.events.append("off_requested")
        if (self.admission_gate.flock_holders
                or self.admission_gate.active_operations):
            return "pending_inflight"
        if (self.admission_gate.unresolved_operations
                or self.admission_gate.resources.targets
                or self.admission_gate.resources.accounts):
            return "pending_unconfirmed"
        self.stop()
        return "complete"

    def stop(self) -> None:
        # Even a direct trusted Owner stop may not ACK before the SINGLE
        # Owner-wide admission gate closes, including for other Run IDs.
        self.admission_gate.close_admissions("off")
        if (self.admission_gate.flock_holders
                or self.admission_gate.active_operations):
            self._deny("effect_inflight")
        if (self.admission_gate.unresolved_operations
                or self.admission_gate.resources.targets
                or self.admission_gate.resources.accounts):
            self._deny("effect_unconfirmed")
        self.revoked = True
        self.ledger_state = "revoked"
        self.events.append("stop_ack")

    def request_rotation(self, new_generation: str) -> str:
        if not isinstance(new_generation, str) or not new_generation:
            self._deny("invalid_generation")
        self.admission_gate.close_admissions("rotation")
        self.events.append("admission_gate_closed")
        self.pending_rotation = new_generation
        self.events.append("rotation_requested")
        if (self.admission_gate.flock_holders
                or self.admission_gate.active_operations):
            return "pending_inflight"
        if (self.admission_gate.unresolved_operations
                or self.admission_gate.resources.targets
                or self.admission_gate.resources.accounts):
            return "pending_unconfirmed"
        self.rotate(new_generation)
        self.pending_rotation = None
        return "complete"

    def rotate(self, new_generation: str) -> None:
        if (not isinstance(new_generation, str) or not new_generation
                or new_generation in self.known_generations):
            self._deny("generation_reused")
        # Direct trusted Owner rotation must close the shared admission gate
        # BEFORE it can acknowledge a new generation or reject an in-flight
        # effect. Unknown remote impact may never be silently overlooked.
        self.admission_gate.close_admissions("rotation")
        if (self.admission_gate.flock_holders
                or self.admission_gate.active_operations):
            self._deny("effect_inflight")
        if (self.admission_gate.unresolved_operations
                or self.admission_gate.resources.targets
                or self.admission_gate.resources.accounts):
            self._deny("effect_unconfirmed")
        self.current_credential_generation = new_generation
        self.known_generations.add(new_generation)
        self.admission_gate.last_invalidating_rotation_epoch = self.generation_epoch
        self.admission_gate.generation_epoch += 1
        # All pre-rotation claims held by other Runs now fail from the same
        # Owner-wide watermark, even if a credential digest is restored.
        self.events.append("rotation_ack")


@pytest.fixture
def model(owner_fixture):
    _, intent, *_ = owner_fixture
    return _AbstractEffectProtocol(
        _EffectRecord.from_intent(intent),
        admission_gate=_AtomicAdmissionGate(),
    )


def test_claim_ack_and_status_observation_are_not_effect_permits(model):
    assert RunStatusObservation(True).claimed_now is True
    assert not hasattr(RunStatusObservation(True), "writer")
    assert not hasattr(RunStatusObservation(True), "effect_lease")
    assert model.events == []
    assert model.lock_held is False
    assert model.attempt_persisted is False


@pytest.mark.parametrize("field,value", [
    ("run_id", "f" * 32),
    ("plan_sha256", "f" * 64),
    ("policy_sha256", "f" * 64),
    ("operation_id", "f" * 32),
    ("source_generation", "f" * 64),
    ("account_binding_sha256", "f" * 64),
    ("provider_proof_sha256", "8" * 64),
    ("receipt_root", "/different-private-receipts"),
    ("target_path", "98-System/unauthorized.md"),
    ("precondition_etag", '"changed-etag"'),
    ("before_sha256", "3" * 64),
    ("after_sha256", "4" * 64),
])
def test_exact_binding_mismatch_does_not_ack_or_persist(model, field, value):
    changed = replace(model.expected, **{field: value})
    with pytest.raises(_ContractDenied):
        model.begin(changed)
    assert not model.attempt_persisted
    assert "start_ack" not in model.events
    assert not model.lock_held


@pytest.mark.parametrize("boot,ledger,trusted", [
    (False, "claimed", True),
    (True, "unconfirmed", True),
    (True, "revoked", True),
    (True, "approved", True),
    (True, "claimed", False),
    (True, "claimed", 1),
])
def test_owner_ledger_boot_or_trust_failure_never_starts(model, boot, ledger, trusted):
    model.boot_reconciled = boot
    model.ledger_state = ledger
    with pytest.raises(_ContractDenied, match="^owner_ledger_unverified$"):
        model.begin(model.expected, trusted_ledger_check=trusted)
    assert not model.attempt_persisted
    assert "start_ack" not in model.events


@pytest.mark.parametrize("signature,generation_valid", [
    (False, True), (True, False), (True, 1),
])
def test_signature_and_generation_checks_are_required(model, signature, generation_valid):
    model.signed_valid = signature
    with pytest.raises(_ContractDenied):
        model.begin(model.expected, trusted_generation_check=generation_valid)
    assert "start_ack" not in model.events


def test_source_generation_is_distinct_from_owner_broker_credential_generation(model):
    # Even if the current trusted Broker digest equals the signed source
    # digest, it does not match the generation pinned by Owner on Claim.
    assert model.expected.source_generation != model.claim_credential_generation
    assert model.current_credential_generation == model.claim_credential_generation
    model.current_credential_generation = model.expected.source_generation
    with pytest.raises(_ContractDenied, match="^credential_generation_changed$"):
        model.begin(model.expected)
    assert not model.attempt_persisted
    assert "start_ack" not in model.events


def test_broker_rechecks_credential_generation_independent_of_source(model):
    model.begin(model.expected)
    model.current_credential_generation = "f" * 64  # unapproved out-of-band change
    with pytest.raises(_ContractDenied, match="^broker_generation_unverified$"):
        model.broker_dispatch(model.expected)
    assert model.unresolved and not model.dispatch_recorded
    model.disconnected()
    assert model.request_off() == "pending_unconfirmed"


def test_generation_rotation_after_claim_blocks_old_intent(model):
    model.rotate("a" * 64)
    with pytest.raises(_ContractDenied, match="^claim_stale_after_rotation$"):
        model.begin(model.expected)
    assert "start_ack" not in model.events


def test_begin_persists_attempt_before_ack_and_can_never_reissue(model):
    model.begin(model.expected)
    assert model.events.index("owner_lock_acquired") < model.events.index(
        "owner_ledger_checked"
    ) < model.events.index("generation_verified") < model.events.index(
        "attempt_fence_written"
    ) < model.events.index("start_ack")
    assert model.attempt_persisted and model.ledger_state == "unconfirmed"
    # The durable one-use attempt fence takes precedence over a transient
    # busy lease: a second Begin is replay, not a new waitable effect.
    with pytest.raises(_ContractDenied, match="^attempt_already_recorded$"):
        model.begin(model.expected)
    assert model.events.count("start_ack") == 1
    model.broker_dispatch(model.expected)
    assert model.events.index("attempt_fence_written") < model.events.index(
        "broker_dispatch_possible"
    )
    model.finish(trusted_readback=True, durable_receipt=True, trusted_operation_finality=True)
    assert model.settled
    with pytest.raises(_ContractDenied, match="^attempt_already_recorded$"):
        model.begin(model.expected)


def test_stop_and_rotation_cannot_ack_during_effect(model):
    model.begin(model.expected)
    with pytest.raises(_ContractDenied, match="^effect_inflight$"):
        model.stop()
    with pytest.raises(_ContractDenied, match="^effect_inflight$"):
        model.rotate("a" * 64)
    assert "stop_ack" not in model.events
    assert "rotation_ack" not in model.events
    model.broker_dispatch(model.expected)
    model.finish(trusted_readback=True, durable_receipt=True, trusted_operation_finality=True)
    model.stop()
    model.rotate("a" * 64)
    assert model.events.index("effect_settled") < model.events.index("stop_ack")
    assert model.events.index("owner_lock_released") < model.events.index("rotation_ack")


@pytest.mark.parametrize("readback,receipt", [
    (False, True), (True, False), (None, True), (True, 1),
])
def test_missing_independent_settlement_blocks_off_and_rotation(
    model, readback, receipt,
):
    model.begin(model.expected)
    model.broker_dispatch(model.expected)
    model.finish(trusted_readback=readback, durable_receipt=receipt, trusted_operation_finality=True)
    assert not model.lock_held
    assert model.unresolved
    with pytest.raises(_ContractDenied, match="^effect_unconfirmed$"):
        model.stop()
    with pytest.raises(_ContractDenied, match="^effect_unconfirmed$"):
        model.rotate("a" * 64)
    with pytest.raises(_ContractDenied, match="^attempt_already_recorded$"):
        model.begin(model.expected)


def test_matching_readback_and_receipt_without_operation_finality_remains_unconfirmed(model):
    model.begin(model.expected)
    model.broker_dispatch(model.expected)
    # Hash/ETag/readback plus a receipt cannot prove WHICH operation wrote
    # the bytes or that an older delayed PUT is no longer possible.
    model.finish(trusted_readback=True, durable_receipt=True)
    assert model.unresolved and not model.settled
    key=(model.expected.account_binding_sha256, model.expected.target_path)
    assert key in model.resources.targets
    assert (model.expected.run_id, model.expected.operation_id) in model.admission_gate.unresolved_operations
    assert model.request_off() == "pending_unconfirmed"


def test_uncertain_settlement_journal_never_releases_other_run_target(model):
    gate=_AtomicAdmissionGate()
    first=_AbstractEffectProtocol(model.expected, admission_gate=gate)
    first.begin(first.expected)
    first.broker_dispatch(first.expected)
    before=gate._journal
    first.finish(
        trusted_readback=True, durable_receipt=True,
        trusted_operation_finality=True, settlement_journal_durable=False,
    )
    assert first.unresolved and not first.settled
    key=(first.expected.account_binding_sha256, first.expected.target_path)
    assert key in gate.resources.targets
    assert gate._journal.fenced_operations == before.fenced_operations
    assert (first.expected.run_id, first.expected.operation_id) in gate.unresolved_operations
    second_record=replace(first.expected, run_id="6"*32, operation_id="7"*32)
    second=_AbstractEffectProtocol(second_record, admission_gate=gate)
    with pytest.raises(_ContractDenied, match="^resource_unconfirmed$"):
        second.begin(second_record)
    assert "start_ack" not in second.events


def test_settlement_journal_publishes_terminal_and_release_together(model):
    gate=_AtomicAdmissionGate()
    first=_AbstractEffectProtocol(model.expected, admission_gate=gate)
    first.begin(first.expected)
    first.broker_dispatch(first.expected)
    pre=gate._journal
    assert pre.active_operations and pre.targets
    first.finish(
        trusted_readback=True, durable_receipt=True,
        trusted_operation_finality=True,
    )
    post=gate._journal
    assert post is not pre
    assert (first.expected.run_id, first.expected.operation_id) in post.fenced_operations
    assert not post.active_operations and not post.unresolved_operations
    assert not post.targets
    # Pre-commit snapshot is still quarantined, never half-committed.
    assert pre.active_operations and pre.targets
    second_record=replace(first.expected, run_id="5"*32, operation_id="4"*32)
    second=_AbstractEffectProtocol(second_record, admission_gate=gate)
    second.begin(second_record)
    assert second.attempt_persisted


def test_reconciliation_journal_uncertainty_preserves_conflict_index(model):
    model.begin(model.expected)
    model.broker_dispatch(model.expected)
    model.disconnected()
    with pytest.raises(_ContractDenied, match="^settlement_unconfirmed$"):
        model.reconcile(
            trusted_provider_outcome=True, durable_receipt=True,
            settlement_journal_durable=False,
        )
    assert model.unresolved and not model.settled
    key=(model.expected.account_binding_sha256, model.expected.target_path)
    assert key in model.resources.targets
    assert model.request_off() == "pending_unconfirmed"


def test_disconnection_frees_kernel_lock_but_not_durable_uncertainty(model):
    model.begin(model.expected)
    model.disconnected()
    assert not model.lock_held and model.unresolved
    with pytest.raises(_ContractDenied, match="^effect_unconfirmed$"):
        model.stop()
    with pytest.raises(_ContractDenied, match="^effect_unconfirmed$"):
        model.rotate("a" * 64)
    with pytest.raises(_ContractDenied):
        model.begin(model.expected)


def test_restart_never_automatically_rearms_old_claim(model):
    model.begin(model.expected)
    model.restart()
    assert model.unresolved and not model.boot_reconciled
    assert model.ledger_state == "unconfirmed"
    model.recover_boot()
    with pytest.raises(_ContractDenied, match="^attempt_already_recorded$"):
        model.begin(model.expected)


def test_only_explicit_independent_reconciliation_may_settle(model):
    model.begin(model.expected)
    model.broker_dispatch(model.expected)
    model.disconnected()
    for provider, receipt in ((False, True), (True, False), (1, True)):
        with pytest.raises(_ContractDenied, match="^reconciliation_unverified$"):
            model.reconcile(
                trusted_provider_outcome=provider, durable_receipt=receipt,
            )
    assert model.unresolved
    model.reconcile(trusted_provider_outcome=True, durable_receipt=True)
    model.stop()
    model.rotate("a" * 64)
    with pytest.raises(_ContractDenied):
        model.begin(model.expected)


def test_existing_real_owner_lock_blocks_stop_rotation_and_recovery(owner_fixture):
    """Existing cooperative kernel-flock evidence only, not D1 permit."""
    owner, intent, *_ = owner_fixture
    owner.recover_after_quiescence()
    owner.register_signed(intent)
    owner.claim_signed(intent)
    change_calls = []
    with owner.drain.effect(RUN, PLAN, POLICY, OPERATION):
        with pytest.raises(OwnerCoordinationDenied, match="owner_drain_timeout"):
            owner.change_under_quiescence(
                lambda: change_calls.append("rotation") or True,
                timeout=0.02,
            )
        with pytest.raises(OwnerCoordinationDenied, match="owner_drain_timeout"):
            owner.recover_after_quiescence(timeout=0.02)
        with pytest.raises(OwnerCoordinationDenied, match="drain_timeout"):
            owner.stop_and_revoke(RUN, timeout=0.02)
    assert change_calls == []
    assert owner.ledger.verify_claimed(RUN, PLAN, POLICY, OPERATION)
    assert owner.stop_and_revoke(RUN) == "stopped_after_drain"
    assert owner.ledger.is_revoked(RUN)


def test_real_reboot_can_make_ledger_unconfirmed_while_signature_still_valid(
    owner_fixture,
):
    """Safely proves the existing signed-only gate ignores private Ledger."""
    owner, intent, state, *_ = owner_fixture
    owner.recover_after_quiescence()
    owner.register_signed(intent)
    owner.claim_signed(intent)
    assert owner.ledger.verify_claimed(RUN, PLAN, POLICY, OPERATION)
    new_ledger = DurableRunLedger(state)
    assert not new_ledger.verify_claimed(RUN, PLAN, POLICY, OPERATION)
    restarted = OperatorRunCoordinator(owner.drain, new_ledger)
    assert restarted.recover_after_quiescence() == 1
    assert not new_ledger.verify_claimed(RUN, PLAN, POLICY, OPERATION)
    assert owner.drain.verifier(RUN, PLAN, POLICY, OPERATION) is True
    # A synthetic signed-only effect() STILL enters despite the Owner
    # Ledger now being unconfirmed. Only a disposable claim marker is
    # created here; this test does NOT inject Writer or perform any HTTP.
    with owner.drain.effect(RUN, PLAN, POLICY, OPERATION):
        assert restarted.ledger.verify_claimed(
            RUN, PLAN, POLICY, OPERATION,
        ) is False


def test_same_or_restored_generation_cannot_rearm_pre_rotation_claim(model):
    old = model.current_credential_generation
    with pytest.raises(_ContractDenied, match="^generation_reused$"):
        model.rotate(old)
    assert model.generation_epoch == 0
    model.rotate("a" * 64)
    assert model.generation_epoch == 1
    assert model.rotation_fenced_prior_claim
    with pytest.raises(_ContractDenied, match="^generation_reused$"):
        model.rotate(old)
    assert model.current_credential_generation == "a" * 64
    with pytest.raises(_ContractDenied, match="^claim_stale_after_rotation$"):
        model.begin(model.expected)
    assert "start_ack" not in model.events


def test_broker_dispatch_denies_worker_bypass_and_wrong_identity(model):
    model.begin(model.expected)
    with pytest.raises(_ContractDenied, match="^broker_peer_or_identity_invalid$"):
        model.broker_dispatch(model.expected, owner_peer_authenticated=False)
    with pytest.raises(_ContractDenied, match="^broker_peer_or_identity_invalid$"):
        model.broker_dispatch(
            replace(model.expected, target_path="98-System/unapproved.md"),
        )
    assert not model.dispatch_recorded
    # A direct Worker call cannot use a previously seen Begin ACK. Only
    # the model's explicitly authenticated Owner dispatch can proceed.
    model.broker_dispatch(model.expected, owner_peer_authenticated=True)
    assert model.dispatch_recorded
    with pytest.raises(_ContractDenied, match="^dispatch_already_recorded$"):
        model.broker_dispatch(model.expected)
    model.finish(trusted_readback=True, durable_receipt=True, trusted_operation_finality=True)
    assert model.settled


def test_missing_enforceable_provider_broker_blocks_owner_admission(model):
    # A Worker that can PUT with raw credentials could bypass the gate.
    # This bit is a reference-model assumption, NOT an OS/network control.
    model.broker_only_egress = False
    with pytest.raises(_ContractDenied, match="^broker_boundary_untrusted$"):
        model.begin(model.expected)
    assert not model.attempt_persisted and "start_ack" not in model.events


def test_broker_generation_check_failure_never_dispatches(model):
    model.begin(model.expected)
    with pytest.raises(_ContractDenied, match="^broker_generation_unverified$"):
        model.broker_dispatch(
            model.expected, trusted_broker_generation=False,
        )
    assert model.unresolved and not model.dispatch_recorded
    model.disconnected()
    assert model.request_off() == "pending_unconfirmed"
    assert "stop_ack" not in model.events


def test_unknown_journal_fsync_is_unconfirmed_and_never_acknowledged(model):
    with pytest.raises(_ContractDenied, match="^journal_unverified$"):
        model.begin(model.expected, journal_outcome="ambiguous")
    assert model.attempt_persisted
    assert model.unresolved
    assert not model.lock_held
    assert "attempt_fence_written" in model.events
    assert "start_ack" not in model.events
    assert "broker_dispatch_possible" not in model.events
    assert model.request_off() == "pending_unconfirmed"
    with pytest.raises(_ContractDenied, match="^new_effects_suspended$"):
        model.begin(model.expected)


def test_fenced_but_never_dispatched_cannot_produce_success(model):
    model.begin(model.expected)
    # Incomplete broker protocol may not return a fabricated readback True.
    model.finish(trusted_readback=True, durable_receipt=True, trusted_operation_finality=True)
    assert not model.settled and model.unresolved
    assert model.request_off() == "pending_unconfirmed"
    with pytest.raises(_ContractDenied):
        model.begin(model.expected)


def test_pending_off_is_durable_non_success_until_independent_finality(model):
    model.begin(model.expected)
    model.broker_dispatch(model.expected)
    assert model.request_off() == "pending_inflight"
    assert model.off_requested and "stop_ack" not in model.events
    model.disconnected()
    assert model.request_off() == "pending_unconfirmed"
    assert "stop_ack" not in model.events
    with pytest.raises(_ContractDenied, match="^new_effects_suspended$"):
        model.begin(model.expected)
    model.reconcile(trusted_provider_outcome=True, durable_receipt=True)
    assert model.request_off() == "complete"
    assert model.revoked
    assert model.events.index("independent_reconciliation") < model.events.index(
        "stop_ack"
    )


def test_pending_rotation_does_not_change_generation_while_unknown(model):
    old = model.current_credential_generation
    model.begin(model.expected)
    model.broker_dispatch(model.expected)
    assert model.request_rotation("a" * 64) == "pending_inflight"
    assert model.current_credential_generation == old
    model.disconnected()
    assert model.request_rotation("a" * 64) == "pending_unconfirmed"
    assert model.current_credential_generation == old
    assert "rotation_ack" not in model.events
    model.reconcile(trusted_provider_outcome=True, durable_receipt=True)
    assert model.request_rotation("a" * 64) == "complete"
    assert model.current_credential_generation == "a" * 64
    assert model.rotation_fenced_prior_claim
    assert model.events.index("independent_reconciliation") < model.events.index(
        "rotation_ack"
    )


def test_reboot_after_fencing_before_dispatch_does_not_rearm(model):
    model.begin(model.expected)
    assert not model.dispatch_recorded
    model.restart()
    model.recover_boot()
    assert model.attempt_persisted
    assert model.unresolved
    assert model.ledger_state == "unconfirmed"
    assert model.request_off() == "pending_unconfirmed"
    with pytest.raises(_ContractDenied, match="^new_effects_suspended$"):
        model.begin(model.expected)


def test_owner_wide_quarantine_cannot_be_bypassed_by_default_resources(model):
    """Owner sessions must never quietly allocate independent target indexes."""
    gate = _AtomicAdmissionGate()
    first = _AbstractEffectProtocol(model.expected, admission_gate=gate)
    first.begin(first.expected)
    first.broker_dispatch(first.expected)
    first.disconnected()

    second_run = replace(
        first.expected, run_id="3" * 32, operation_id="4" * 32,
    )
    second = _AbstractEffectProtocol(second_run, admission_gate=gate)
    with pytest.raises(_ContractDenied, match="^resource_unconfirmed$"):
        second.begin(second_run)
    assert not second.attempt_persisted
    assert "start_ack" not in second.events
    assert not second.lock_held


def test_unconfirmed_target_blocks_another_run_identity_until_finality(model):
    """Different Run ID cannot bypass an unresolved delayed PUT."""
    gate = _AtomicAdmissionGate()
    shared = gate.resources
    original = _AbstractEffectProtocol(
        model.expected, admission_gate=gate,
    )
    original.begin(original.expected)
    original.broker_dispatch(original.expected)
    original.disconnected()
    next_run = replace(
        original.expected, run_id="e" * 32, operation_id="f" * 32,
    )
    independent = _AbstractEffectProtocol(
        next_run, admission_gate=gate,
    )
    with pytest.raises(_ContractDenied, match="^resource_unconfirmed$"):
        independent.begin(next_run)
    assert "start_ack" not in independent.events
    assert not independent.attempt_persisted

    # A distinct non-conflicting target may be admitted ONLY when the
    # complete effect-resource footprint is independently established.
    other_file = replace(
        next_run, run_id="9" * 32, operation_id="8" * 32,
        target_path="98-System/separate.md",
    )
    other = _AbstractEffectProtocol(
        other_file, admission_gate=gate,
    )
    other.begin(other_file)
    other.broker_dispatch(other_file)
    other.finish(trusted_readback=True, durable_receipt=True, trusted_operation_finality=True)

    # Exact operation-specific finality clears ONLY the affected quarantine.
    original.reconcile(trusted_provider_outcome=True, durable_receipt=True)
    independent.begin(next_run)
    assert independent.attempt_persisted


def test_unknown_remote_impact_quarantines_entire_provider_account(model):
    gate = _AtomicAdmissionGate()
    shared = gate.resources
    shared.quarantine_unknown_account(model.expected.account_binding_sha256)
    for idx in range(3):
        record = replace(
            model.expected, run_id=str(idx) * 32,
            operation_id=str(idx + 3) * 32,
            target_path=f"98-System/different-{idx}.md",
        )
        other = _AbstractEffectProtocol(
            record, admission_gate=gate,
        )
        with pytest.raises(_ContractDenied, match="^resource_unconfirmed$"):
            other.begin(record)
        assert "start_ack" not in other.events


def test_pre_rotation_claim_epoch_remains_invalid_even_with_restored_digest(model):
    old_digest = model.current_credential_generation
    assert model.claim_reserved_epoch == 0
    assert model.last_invalidating_rotation_epoch == -1
    model.rotate("a" * 64)
    assert model.generation_epoch == 1
    assert model.last_invalidating_rotation_epoch == 0
    assert model.claim_reserved_epoch <= model.last_invalidating_rotation_epoch
    # Simulate external attempt to roll back digest without resetting the
    # Owner-private, monotonic rotation epoch. No Claim revival.
    model.current_credential_generation = old_digest
    with pytest.raises(_ContractDenied, match="^claim_stale_after_rotation$"):
        model.begin(model.expected)


def test_unconfirmed_fenced_without_dispatch_requires_verified_no_effect(model):
    model.begin(model.expected)
    model.disconnected()
    with pytest.raises(_ContractDenied, match="^reconciliation_unverified$"):
        model.reconcile(
            trusted_provider_outcome=True, durable_receipt=True,
        )
    assert model.unresolved
    assert not model.settled
    assert model.request_off() == "pending_unconfirmed"
    # A separately trusted broker audit proves this exact operation was
    # NEVER dispatched; only this synthetic durable finality is accepted.
    model.reconcile(
        trusted_provider_outcome=False, durable_receipt=True,
        trusted_never_dispatched=True,
    )
    assert model.settled
    assert "verified_no_dispatch" in model.events
    assert "verified_remote_effect" not in model.events
    assert model.request_off() == "complete"
    assert model.revoked


def test_off_commits_closed_gate_between_begin_checks_and_fence(model):
    """Off wins transaction ordering even after Begin's initial checks."""
    observed = []

    def off_in_gap():
        observed.append(model.request_off())

    with pytest.raises(_ContractDenied, match="^new_effects_suspended$"):
        model.begin(model.expected, before_fence=off_in_gap)
    assert observed == ["pending_inflight"]
    assert not model.lock_held and not model.attempt_persisted
    assert not model.admission_gate.open
    assert "start_ack" not in model.events
    assert "broker_dispatch_possible" not in model.events
    assert model.events.index("admission_gate_closed") < model.events.index(
        "off_requested"
    )
    assert model.request_off() == "complete"
    assert model.revoked


def test_rotation_closes_gate_before_begin_can_persist_effect_fence(model):
    seen = []

    def rotate_in_gap():
        seen.append(model.request_rotation("a" * 64))

    with pytest.raises(_ContractDenied, match="^new_effects_suspended$"):
        model.begin(model.expected, before_fence=rotate_in_gap)
    assert seen == ["pending_inflight"]
    assert not model.attempt_persisted
    assert "start_ack" not in model.events
    assert not model.admission_gate.open
    assert model.request_rotation("a" * 64) == "complete"
    assert model.generation_epoch == 1
    with pytest.raises(_ContractDenied, match="^claim_stale_after_rotation$"):
        model.begin(model.expected)


def test_begin_fence_committed_first_makes_later_off_pending(model):
    model.begin(model.expected)
    assert model.attempt_persisted
    assert (model.expected.run_id, model.expected.operation_id) in (
        model.admission_gate.fenced_operations
    )
    assert model.request_off() == "pending_inflight"
    assert not model.admission_gate.open
    model.broker_dispatch(model.expected)
    model.finish(trusted_readback=True, durable_receipt=True, trusted_operation_finality=True)
    assert model.request_off() == "complete"
    assert model.events.index("attempt_fence_written") < model.events.index(
        "admission_gate_closed"
    ) < model.events.index("stop_ack")


def test_reboot_keeps_gate_closed_for_old_unconfirmed_claim(model):
    model.restart()
    assert not model.admission_gate.open
    assert model.ledger_state == "unconfirmed"
    model.recover_boot()
    # Boot reconciliation is NOT a new signed Claim. A fresh, independently
    # approved Run would require an entirely new Owner-controlled identity.
    assert not model.admission_gate.open
    with pytest.raises(_ContractDenied, match="^new_effects_suspended$"):
        model.begin(model.expected)
    assert "start_ack" not in model.events


def test_owner_wide_off_gate_denies_unrelated_run_id_and_target(model):
    """Off commits one Owner-wide admission fence, NOT a per-run flag."""
    gate = _AtomicAdmissionGate()
    resources = gate.resources
    first = _AbstractEffectProtocol(
        model.expected, admission_gate=gate,
    )
    second_record = replace(
        model.expected, run_id="b" * 32, operation_id="b" * 32,
        target_path="98-System/unrelated.md",
    )
    second = _AbstractEffectProtocol(
        second_record, admission_gate=gate,
    )
    assert first.request_off() == "complete"
    assert first.revoked
    assert not gate.open
    with pytest.raises(_ContractDenied, match="^new_effects_suspended$"):
        second.begin(second_record)
    assert not second.attempt_persisted
    assert "start_ack" not in second.events
    assert "broker_dispatch_possible" not in second.events
    assert not resources.targets


def test_owner_wide_rotation_gate_blocks_second_run_after_fence(model):
    gate = _AtomicAdmissionGate()
    resources = gate.resources
    first = _AbstractEffectProtocol(
        model.expected, admission_gate=gate,
    )
    second_record = replace(
        model.expected, run_id="b" * 32, operation_id="c" * 32,
        target_path="98-System/independent.md",
    )
    second = _AbstractEffectProtocol(
        second_record, admission_gate=gate,
    )
    first.begin(first.expected)
    first.broker_dispatch(first.expected)
    assert first.request_rotation("f" * 64) == "pending_inflight"
    assert not gate.open
    with pytest.raises(_ContractDenied, match="^new_effects_suspended$"):
        second.begin(second_record)
    assert not second.attempt_persisted
    assert "start_ack" not in second.events
    first.finish(trusted_readback=True, durable_receipt=True, trusted_operation_finality=True)
    assert first.request_rotation("f" * 64) == "complete"
    assert first.rotation_fenced_prior_claim
    # The shared Owner-wide rotation watermark (not only the closed gate)
    # permanently invalidates every older second-Run Claim.
    assert second.rotation_fenced_prior_claim
    with pytest.raises(_ContractDenied, match="^claim_stale_after_rotation$"):
        second.begin(second_record)


def test_owner_wide_rotation_watermark_survives_source_digest_restore(model):
    gate = _AtomicAdmissionGate()
    resources = gate.resources
    first = _AbstractEffectProtocol(
        model.expected, admission_gate=gate,
    )
    second_record = replace(
        model.expected, run_id="7" * 32, operation_id="6" * 32,
        target_path="98-System/unrelated.md",
    )
    second = _AbstractEffectProtocol(
        second_record, admission_gate=gate,
    )
    old_credential = gate.current_credential_generation
    first.rotate("a" * 64)
    assert gate.last_invalidating_rotation_epoch == 0
    assert gate.generation_epoch == 1
    assert second.claim_reserved_epoch == 0
    # Simulate an illicit rollback of the broker-visible digest: global
    # monotonic epoch still invalidates the separate Run's old Claim.
    gate.current_credential_generation = old_credential
    with pytest.raises(_ContractDenied, match="^claim_stale_after_rotation$"):
        second.begin(second_record)
    assert not second.attempt_persisted
    assert "start_ack" not in second.events


def test_direct_stop_ack_closes_owner_wide_gate_before_ack(model):
    gate=_AtomicAdmissionGate()
    first=_AbstractEffectProtocol(model.expected, admission_gate=gate)
    second_record=replace(
        model.expected, run_id="3"*32, operation_id="4"*32,
        target_path="98-System/unrelated.md",
    )
    second=_AbstractEffectProtocol(second_record, admission_gate=gate)
    first.stop()
    assert first.revoked and "stop_ack" in first.events
    assert not gate.open
    assert gate.closed_reason == "off"
    with pytest.raises(_ContractDenied, match="^new_effects_suspended$"):
        second.begin(second_record)
    assert "start_ack" not in second.events


def test_unknown_account_quarantine_blocks_off_and_direct_stop(model):
    gate=_AtomicAdmissionGate()
    gate.resources.quarantine_unknown_account(model.expected.account_binding_sha256)
    first=_AbstractEffectProtocol(model.expected, admission_gate=gate)
    assert first.request_off() == "pending_unconfirmed"
    assert not gate.open and first.off_requested
    assert "stop_ack" not in first.events
    with pytest.raises(_ContractDenied, match="^effect_unconfirmed$"):
        first.stop()
    assert not first.revoked
    assert gate.resources.accounts


def test_unknown_account_quarantine_blocks_rotation_and_direct_rotate(model):
    gate=_AtomicAdmissionGate()
    gate.resources.quarantine_unknown_account(model.expected.account_binding_sha256)
    first=_AbstractEffectProtocol(model.expected, admission_gate=gate)
    old_generation=gate.current_credential_generation
    assert first.request_rotation("a"*64) == "pending_unconfirmed"
    assert "rotation_ack" not in first.events
    assert gate.current_credential_generation == old_generation
    with pytest.raises(_ContractDenied, match="^effect_unconfirmed$"):
        first.rotate("a"*64)
    assert not gate.open and gate.resources.accounts


def test_restart_cannot_reopen_owner_gate_with_unknown_account_quarantine(model):
    gate=_AtomicAdmissionGate()
    gate.resources.quarantine_unknown_account(model.expected.account_binding_sha256)
    first=_AbstractEffectProtocol(model.expected, admission_gate=gate)
    first.restart()
    first.recover_boot()
    assert not gate.open
    assert gate.resources.accounts
    with pytest.raises(_ContractDenied, match="^new_effects_suspended$"):
        first.begin(first.expected)


def test_restart_invalidates_other_preexisting_claim_before_any_effect(model):
    gate=_AtomicAdmissionGate()
    first=_AbstractEffectProtocol(model.expected, admission_gate=gate)
    second_record=replace(
        model.expected, run_id="a"*32, operation_id="b"*32,
        target_path="98-System/preexisting.md",
    )
    second=_AbstractEffectProtocol(second_record, admission_gate=gate)
    first.restart()
    second.recover_boot()
    # No individual Run may reopen admissions after Owner-wide restart.
    assert not gate.open
    assert "start_ack" not in second.events


def test_owner_terminal_journal_distinguishes_remote_effect_from_no_effect(model):
    gate=_AtomicAdmissionGate()
    no_effect=_AbstractEffectProtocol(model.expected, admission_gate=gate)
    no_effect.begin(no_effect.expected)
    no_effect.disconnected()
    no_effect.reconcile(
        trusted_provider_outcome=False, durable_receipt=True,
        trusted_never_dispatched=True,
    )
    key=(no_effect.expected.run_id,no_effect.expected.operation_id)
    assert (key[0], key[1], "verified_no_effect") in gate._journal.terminal_outcomes
    assert (key[0], key[1], "verified_remote_effect") not in gate._journal.terminal_outcomes
    assert not gate.resources.targets


def test_only_owner_wide_verified_boot_scan_can_admit_fresh_claim(model):
    gate=_AtomicAdmissionGate()
    first=_AbstractEffectProtocol(model.expected, admission_gate=gate)
    old_record=replace(
        model.expected, run_id="e"*32, operation_id="b"*32,
        target_path="98-System/old.md",
    )
    old=_AbstractEffectProtocol(old_record, admission_gate=gate)
    first.restart()
    with pytest.raises(_ContractDenied, match="^owner_boot_unverified$"):
        _AbstractEffectProtocol(
            replace(old_record, run_id="c"*32, operation_id="d"*32),
            admission_gate=gate,
        )
    old.recover_boot()
    with pytest.raises(_ContractDenied, match="^owner_boot_unverified$"):
        gate.owner_reconcile_boot(trusted_owner_scan=False)
    assert not gate.open
    assert gate.owner_reconcile_boot(trusted_owner_scan=True) is True
    assert gate.open
    with pytest.raises(_ContractDenied, match="^owner_ledger_unverified$"):
        old.begin(old_record)
    fresh_record=replace(
        old_record, run_id="a"*32, operation_id="f"*32,
        target_path="98-System/fresh.md",
    )
    fresh=_AbstractEffectProtocol(fresh_record, admission_gate=gate)
    fresh.begin(fresh_record)
    assert fresh.attempt_persisted


def test_verified_remote_effect_disposition_is_in_owner_journal(model):
    gate=_AtomicAdmissionGate()
    first=_AbstractEffectProtocol(model.expected, admission_gate=gate)
    first.begin(first.expected)
    first.broker_dispatch(first.expected)
    first.finish(
        trusted_readback=True, durable_receipt=True,
        trusted_operation_finality=True,
    )
    key=(first.expected.run_id,first.expected.operation_id)
    assert (key[0],key[1],"verified_remote_effect") in gate._journal.terminal_outcomes
    assert (key[0],key[1],"verified_no_effect") not in gate._journal.terminal_outcomes
    assert not gate.resources.targets


def test_owner_restart_fences_another_previously_active_session_dispatch(model):
    gate=_AtomicAdmissionGate()
    a=_AbstractEffectProtocol(model.expected, admission_gate=gate)
    b_record=replace(
        model.expected, run_id="a"*32, operation_id="b"*32,
        target_path="98-System/other-session.md",
    )
    b=_AbstractEffectProtocol(b_record, admission_gate=gate)
    b.begin(b_record)
    assert b.lock_held and not b.dispatch_recorded
    a.restart()
    with pytest.raises(_ContractDenied, match="^owner_boot_unverified$"):
        b.broker_dispatch(b_record)
    assert not b.dispatch_recorded
    assert (b_record.run_id,b_record.operation_id) in gate.unresolved_operations
    assert (b_record.account_binding_sha256,b_record.target_path) in gate.resources.targets


def test_widened_account_quarantine_blocks_already_admitted_dispatch(model):
    gate=_AtomicAdmissionGate()
    a=_AbstractEffectProtocol(model.expected, admission_gate=gate)
    a.begin(a.expected)
    a.broker_dispatch(a.expected)
    a.disconnected()
    b_record=replace(
        model.expected, run_id="e"*32, operation_id="f"*32,
        target_path="98-System/independent-file.md",
    )
    b=_AbstractEffectProtocol(b_record, admission_gate=gate)
    b.begin(b_record)
    gate.resources.quarantine_unknown_account(b_record.account_binding_sha256)
    with pytest.raises(_ContractDenied, match="^resource_unconfirmed$"):
        b.broker_dispatch(b_record)
    assert not b.dispatch_recorded
    assert (b_record.account_binding_sha256,b_record.target_path) in gate.resources.targets


def test_old_owner_session_cannot_terminally_settle_after_restart(model):
    gate=_AtomicAdmissionGate()
    other=_AbstractEffectProtocol(model.expected, admission_gate=gate)
    other.begin(other.expected)
    other.broker_dispatch(other.expected)
    a_record=replace(
        model.expected, run_id="4"*32, operation_id="5"*32,
        target_path="98-System/unrelated-owner.md",
    )
    a=_AbstractEffectProtocol(a_record, admission_gate=gate)
    a.restart()
    with pytest.raises(_ContractDenied, match="^owner_boot_unverified$"):
        other.finish(
            trusted_readback=True, durable_receipt=True,
            trusted_operation_finality=True,
        )
    assert other.unresolved and not other.settled
    assert (other.expected.account_binding_sha256,other.expected.target_path) in gate.resources.targets
    assert (other.expected.run_id,other.expected.operation_id) in gate.unresolved_operations
    assert not gate._journal.terminal_outcomes


def test_unknown_account_quarantine_after_dispatch_blocks_terminal_success(model):
    gate=_AtomicAdmissionGate()
    first=_AbstractEffectProtocol(model.expected, admission_gate=gate)
    first.begin(first.expected)
    first.broker_dispatch(first.expected)
    gate.resources.quarantine_unknown_account(first.expected.account_binding_sha256)
    first.finish(
        trusted_readback=True, durable_receipt=True,
        trusted_operation_finality=True,
    )
    assert first.unresolved and not first.settled
    assert gate.resources.accounts
    assert (first.expected.account_binding_sha256,first.expected.target_path) in gate.resources.targets
    assert first.request_off() == "pending_unconfirmed"


# #472 R2/B2c-D1/A0 PR #554 R1: post-restart reconciliation is Owner-only.
# These are disposable design-contract tests, not a real credential or broker.


def _r1_restarted_attempt(model, *, dispatched: bool):
    gate = _AtomicAdmissionGate()
    old_owner_session = _AbstractEffectProtocol(
        model.expected, admission_gate=gate,
    )
    old_owner_session.begin(old_owner_session.expected)
    if dispatched:
        old_owner_session.broker_dispatch(old_owner_session.expected)
    old_owner_session.restart()
    assert not gate.open and not gate.boot_reconciled
    assert gate.unresolved_operations and gate.resources.targets
    return gate, old_owner_session


def _r1_valid_finality(disposition):
    return dict(
        trusted_owner_peer=True,
        trusted_private_attempt=True,
        trusted_broker_finality=True,
        broker_disposition=disposition,
        durable_broker_receipt=True,
        settlement_journal_durable=True,
    )


def test_r1_fresh_owner_reconciles_post_restart_remote_effect(model):
    gate, stale = _r1_restarted_attempt(model, dispatched=True)
    assert gate.owner_reconcile_boot(trusted_owner_scan=True) is False
    with pytest.raises(_ContractDenied, match="^owner_boot_unverified$"):
        stale.reconcile(trusted_provider_outcome=True, durable_receipt=True)
    gate.owner_reconcile_post_restart(
        stale.expected, **_r1_valid_finality("verified_remote_effect"),
    )
    key = (stale.expected.run_id, stale.expected.operation_id)
    target = (stale.expected.account_binding_sha256, stale.expected.target_path)
    assert (key[0], key[1], "verified_remote_effect") in gate._journal.terminal_outcomes
    assert key in gate.fenced_operations
    assert key not in gate.unresolved_operations
    assert target not in gate.resources.targets
    assert not gate.open and not gate.boot_reconciled
    assert not stale.settled  # Old per-Run session never gains Owner authority.
    assert gate.owner_reconcile_boot(trusted_owner_scan=True) is True
    with pytest.raises(_ContractDenied):
        stale.begin(stale.expected)
    new_record = replace(
        stale.expected, run_id="9" * 32, operation_id="8" * 32,
    )
    fresh = _AbstractEffectProtocol(new_record, admission_gate=gate)
    fresh.begin(new_record)
    assert fresh.attempt_persisted


def test_r1_fresh_owner_reconciles_verified_never_dispatched(model):
    gate, stale = _r1_restarted_attempt(model, dispatched=False)
    gate.owner_reconcile_post_restart(
        stale.expected, **_r1_valid_finality("verified_no_effect"),
    )
    key = (stale.expected.run_id, stale.expected.operation_id)
    assert (key[0], key[1], "verified_no_effect") in gate._journal.terminal_outcomes
    assert (key[0], key[1], "verified_remote_effect") not in gate._journal.terminal_outcomes
    assert not gate.unresolved_operations and not gate.resources.targets
    assert gate.owner_reconcile_boot(trusted_owner_scan=True) is True
    with pytest.raises(_ContractDenied):
        stale.begin(stale.expected)


@pytest.mark.parametrize("untrusted", [
    "trusted_owner_peer", "trusted_private_attempt",
    "trusted_broker_finality", "durable_broker_receipt",
    "settlement_journal_durable",
])
def test_r1_post_restart_owner_fail_closed_for_missing_trust(model, untrusted):
    gate, stale = _r1_restarted_attempt(model, dispatched=True)
    trusted = _r1_valid_finality("verified_remote_effect")
    trusted[untrusted] = False
    before = gate._journal
    with pytest.raises(_ContractDenied):
        gate.owner_reconcile_post_restart(stale.expected, **trusted)
    assert gate._journal is before
    assert not gate.open
    assert gate.owner_reconcile_boot(trusted_owner_scan=True) is False


@pytest.mark.parametrize("disposition", [
    "verified", "unknown", "verified_no_dispatch", "", None, True,
])
def test_r1_post_restart_owner_denies_invalid_disposition(model, disposition):
    gate, stale = _r1_restarted_attempt(model, dispatched=True)
    before = gate._journal
    with pytest.raises(_ContractDenied):
        gate.owner_reconcile_post_restart(
            stale.expected, **_r1_valid_finality(disposition),
        )
    assert gate._journal is before and gate.resources.targets


def test_r1_post_restart_owner_denies_foreign_attempt_identity(model):
    gate, stale = _r1_restarted_attempt(model, dispatched=True)
    foreign = replace(stale.expected, run_id="4" * 32)
    swapped_target = replace(stale.expected, target_path="98-System/evil.md")
    before = gate._journal
    for record in (foreign, swapped_target):
        with pytest.raises(_ContractDenied):
            gate.owner_reconcile_post_restart(
                record, **_r1_valid_finality("verified_remote_effect"),
            )
    assert gate._journal is before
    assert gate.unresolved_operations and gate.resources.targets


def test_r1_post_restart_unresolved_account_requires_independent_proof(model):
    gate, stale = _r1_restarted_attempt(model, dispatched=True)
    account = stale.expected.account_binding_sha256
    gate.resources.quarantine_unknown_account(account)
    gate.owner_reconcile_post_restart(
        stale.expected, **_r1_valid_finality("verified_remote_effect"),
    )
    assert gate.resources.accounts == {account}
    assert gate.owner_reconcile_boot(trusted_owner_scan=True) is False
    with pytest.raises(_ContractDenied):
        gate.owner_reconcile_unknown_account(
            account, trusted_owner_peer=True,
            trusted_account_scope_finality=False,
            durable_broker_receipt=True, settlement_journal_durable=True,
        )
    assert gate.resources.accounts == {account}
    gate.owner_reconcile_unknown_account(
        account, trusted_owner_peer=True,
        trusted_account_scope_finality=True,
        durable_broker_receipt=True, settlement_journal_durable=True,
    )
    assert not gate.resources.accounts
    assert gate.owner_reconcile_boot(trusted_owner_scan=True) is True


def test_r1_pending_off_cannot_be_reopened_by_post_restart_reconciler(model):
    gate = _AtomicAdmissionGate()
    stale = _AbstractEffectProtocol(model.expected, admission_gate=gate)
    stale.begin(stale.expected)
    stale.broker_dispatch(stale.expected)
    assert stale.request_off() == "pending_inflight"
    stale.restart()
    assert gate.closed_reason == "off" and not gate.open
    gate.owner_reconcile_post_restart(
        stale.expected, **_r1_valid_finality("verified_remote_effect"),
    )
    assert gate.owner_reconcile_boot(trusted_owner_scan=True) is False
    assert not gate.open and gate.closed_reason == "off"
    assert stale.request_off() == "complete"
    assert stale.revoked and not gate.open



def test_r1_multiple_outstanding_effects_remain_closed_until_all_reconciled(model):
    gate = _AtomicAdmissionGate()
    first = _AbstractEffectProtocol(model.expected, admission_gate=gate)
    a_record = first.expected
    first.begin(a_record)
    first.broker_dispatch(a_record)
    first.disconnected()
    b_record = replace(
        a_record, run_id="4" * 32, operation_id="9" * 32,
        target_path="98-System/other-target.md",
    )
    second = _AbstractEffectProtocol(b_record, admission_gate=gate)
    second.begin(b_record)
    second.broker_dispatch(b_record)
    first.restart()
    assert len(gate.unresolved_operations) == 2
    assert len(gate.resources.targets) == 2
    gate.owner_reconcile_post_restart(
        a_record, **_r1_valid_finality("verified_remote_effect"),
    )
    assert gate.owner_reconcile_boot(trusted_owner_scan=True) is False
    assert not gate.open and len(gate.resources.targets) == 1
    gate.owner_reconcile_post_restart(
        b_record, **_r1_valid_finality("verified_no_effect"),
    )
    assert len(gate._journal.terminal_outcomes) == 2
    assert not gate.resources.targets and not gate.unresolved_operations
    assert gate.owner_reconcile_boot(trusted_owner_scan=True) is True
    with pytest.raises(_ContractDenied):
        second.begin(b_record)


def test_r1_recovery_rejects_same_key_wrong_policy_or_operation_plan(model):
    gate, stale = _r1_restarted_attempt(model, dispatched=True)
    before = gate._journal
    for field, value in (
        ("plan_sha256", "9" * 64),
        ("policy_sha256", "9" * 64),
        ("source_generation", "9" * 64),
        ("account_binding_sha256", "9" * 64),
        ("receipt_root", "/wrong-root"),
        ("precondition_etag", '"changed-etag"'),
        ("after_sha256", "9" * 64),
    ):
        alternate = replace(stale.expected, **{field: value})
        assert alternate != stale.expected
        with pytest.raises(_ContractDenied, match="^owner_attempt_binding_unverified$"):
            gate.owner_reconcile_post_restart(
                alternate, **_r1_valid_finality("verified_remote_effect"),
            )
    assert gate._journal is before


def test_r1_recovery_not_accessible_from_normal_pre_reboot_session(model):
    gate = _AtomicAdmissionGate()
    stale = _AbstractEffectProtocol(model.expected, admission_gate=gate)
    stale.begin(stale.expected)
    stale.disconnected()
    with pytest.raises(_ContractDenied, match="^owner_boot_unverified$"):
        gate.owner_reconcile_post_restart(
            stale.expected, **_r1_valid_finality("verified_no_effect"),
        )
    assert gate.unresolved_operations and gate.resources.targets
    assert gate.open  # No implicit global Off/rotation from local disconnect.


def test_r1_account_unknown_quarantine_cannot_clear_before_known_attempt(model):
    gate, stale = _r1_restarted_attempt(model, dispatched=True)
    account = stale.expected.account_binding_sha256
    gate.resources.quarantine_unknown_account(account)
    with pytest.raises(_ContractDenied, match="^account_unresolved$"):
        gate.owner_reconcile_unknown_account(
            account,
            trusted_owner_peer=True,
            trusted_account_scope_finality=True,
            durable_broker_receipt=True,
            settlement_journal_durable=True,
        )
    assert gate.resources.accounts and gate.resources.targets
    assert gate.owner_reconcile_boot(trusted_owner_scan=True) is False


@pytest.mark.parametrize("untrusted", [
    "trusted_owner_peer", "trusted_account_scope_finality",
    "durable_broker_receipt", "settlement_journal_durable",
])
def test_r1_account_quarantine_denies_missing_account_scope_proof(model, untrusted):
    gate, stale = _r1_restarted_attempt(model, dispatched=False)
    account = stale.expected.account_binding_sha256
    gate.resources.quarantine_unknown_account(account)
    gate.owner_reconcile_post_restart(
        stale.expected, **_r1_valid_finality("verified_no_effect"),
    )
    values = dict(
        trusted_owner_peer=True,
        trusted_account_scope_finality=True,
        durable_broker_receipt=True,
        settlement_journal_durable=True,
    )
    values[untrusted] = False
    before = gate._journal
    with pytest.raises(_ContractDenied):
        gate.owner_reconcile_unknown_account(account, **values)
    assert gate._journal is before
    assert gate.owner_reconcile_boot(trusted_owner_scan=True) is False


def test_r1_post_restart_reconciliation_does_not_reissue_old_effect(model):
    gate, stale = _r1_restarted_attempt(model, dispatched=False)
    old = stale.expected
    key = old.run_id, old.operation_id
    gate.owner_reconcile_post_restart(
        old, **_r1_valid_finality("verified_no_effect"),
    )
    before = gate._journal
    with pytest.raises(_ContractDenied, match="^owner_attempt_binding_unverified$"):
        gate.owner_reconcile_post_restart(
            old, **_r1_valid_finality("verified_remote_effect"),
        )
    assert gate._journal is before
    assert len(gate._journal.terminal_outcomes) == 1
    assert key in gate.fenced_operations
    assert gate.owner_reconcile_boot(trusted_owner_scan=True) is True
    with pytest.raises(_ContractDenied):
        stale.begin(old)



def _r1_terminal_from_other_session(model, *, dispatched: bool):
    gate = _AtomicAdmissionGate()
    b = _AbstractEffectProtocol(model.expected, admission_gate=gate)
    b.begin(b.expected)
    if dispatched:
        b.broker_dispatch(b.expected)
    a_record = replace(
        model.expected, run_id="3" * 32, operation_id="4" * 32,
        target_path="98-System/independent-owner.md",
    )
    a = _AbstractEffectProtocol(a_record, admission_gate=gate)
    a.restart()
    assert not gate.open and gate.unresolved_operations
    gate.owner_reconcile_post_restart(
        b.expected,
        **_r1_valid_finality(
            "verified_remote_effect" if dispatched else "verified_no_effect",
        ),
    )
    assert not gate.unresolved_operations and not gate.resources.targets
    return gate, b


def test_r1_late_stale_finish_cannot_resurrect_terminal_journal(model):
    gate, stale = _r1_terminal_from_other_session(model, dispatched=True)
    after = gate._journal
    with pytest.raises(_ContractDenied, match="^owner_boot_unverified$"):
        stale.finish(
            trusted_readback=True, durable_receipt=True,
            trusted_operation_finality=True,
        )
    assert gate._journal is after
    assert not gate.unresolved_operations and not gate.resources.targets
    assert gate.owner_reconcile_boot(trusted_owner_scan=True) is True


def test_r1_late_stale_broker_dispatch_cannot_resurrect_terminal_journal(model):
    gate, stale = _r1_terminal_from_other_session(model, dispatched=False)
    after = gate._journal
    with pytest.raises(_ContractDenied, match="^owner_boot_unverified$"):
        stale.broker_dispatch(stale.expected)
    assert gate._journal is after
    assert not gate.unresolved_operations and not gate.resources.targets
    assert gate.owner_reconcile_boot(trusted_owner_scan=True) is True


def test_r1_late_stale_disconnect_cannot_resurrect_terminal_journal(model):
    gate, stale = _r1_terminal_from_other_session(model, dispatched=False)
    after = gate._journal
    with pytest.raises(_ContractDenied, match="^owner_boot_unverified$"):
        stale.disconnected()
    assert gate._journal is after
    assert not gate.unresolved_operations and not gate.resources.targets
    assert gate.owner_reconcile_boot(trusted_owner_scan=True) is True


def test_r1_owner_journal_refuses_mark_uncertain_after_terminal(model):
    gate, stale = _r1_restarted_attempt(model, dispatched=True)
    gate.owner_reconcile_post_restart(
        stale.expected, **_r1_valid_finality("verified_remote_effect"),
    )
    after = gate._journal
    with pytest.raises(_ContractDenied, match="^attempt_terminal$"):
        gate.mark_uncertain(stale.expected)
    assert gate._journal is after
    assert not gate.unresolved_operations
    assert not gate.resources.targets



def test_r1_stale_owner_restart_does_not_advance_boot_epoch(model):
    gate, stale = _r1_restarted_attempt(model, dispatched=False)
    epoch = gate.boot_epoch
    before = gate._journal
    with pytest.raises(_ContractDenied, match="^owner_boot_unverified$"):
        stale.restart()
    assert gate.boot_epoch == epoch
    assert gate._journal is before
    assert not gate.open
    assert gate.unresolved_operations
