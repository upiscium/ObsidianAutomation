# #472 D1/B1/B1 — Broker-private durable operation-state MVI

**Status:** local-only, source-only, deliberately UNWIRED. Base: D1/B1/A0 PR #561 merged at main 12c949ddd217cc1aa4225c3f5d1b2fb5e7ec6a15. No Commit/Push/PR/Ready/Merge, deployment, Broker service, OS UID, credentials, provider HTTP/PUT, or live Vault mutation.

## Narrow purpose

`vault_format_broker_journal.py` implements a Broker-local persistence **state machine**, not a trusted Broker service. It is not imported by any Owner, Worker, Formatter or runtime code. It reuses the D1/B0 internal SQLite path/inode validation helpers but writes to a **separate Broker-private** directory and database.

The separately provisioned Broker-owned 0700 directory is mandatory. Explicit `bootstrap=True` creates Broker-owned 0600 DB/identity files; ordinary open cannot silently recreate missing files. SQLite rollback mode `DELETE`, `synchronous=FULL`, pinned DB opened FD/inode and short `BEGIN IMMEDIATE` transactions are used. Missing/unsafe/replaced state or ambiguous persistence fails closed. This is NOT power-loss / adversarial-same-UID rollback certification.

### Credential generation: durable metadata, not real credential authority

- `start_boot()` closes Broker-wide admission, advances Broker boot epoch and turns prior `dispatch_possible` attempts into durable `unconfirmed` without releasing resource or account quarantine. Handle construction does not trigger boot.
- `reconcile_boot()` may reopen only when no known possible/unconfirmed dispatch and no account-wide quarantine, never overriding Off. **An authenticated full Broker/Provider recovery scan is not implemented**, and this method cannot be wired as production admission.
- `provision_generation_metadata(account,generation)` stores only a non-secret digest, initial epoch 0 and revision digest bound to the Broker DB identity. It does NOT fetch/install/verify a Provider credential, implement actual rotation or create a Broker service.
- `observe_credential` yields an A0-compatible read-only observation of that metadata. It is not Owner Claim approval, a credential-currentness proof or Writer authorization.
- `freeze_generation(account)` atomically prevents new operations under that account and reports `pending_inflight`, `pending_unconfirmed` or `ready_for_separate_rotation`. This is NOT actual credential rotation, epoch advance or verified stop ACK; those remain future gates.

### Operations: one-shot identity, uncertainty, limited never-dispatched terminal evidence

- `fence_pre_dispatch(EffectAttempt)` persists the exact canonical attempt digest and one-shot Run/operation key plus unique account/target conflict. Account metadata and generation epoch must match, and the Broker-wide gate must be open. This method does NOT authenticate a real Owner/SSHSIG/Plan and cannot issue an effect permit.
- `mark_dispatch_possible` **rechecks existing account quarantine inside the same `BEGIN IMMEDIATE` transaction** before authorizing a previously pre-fenced operation, so an earlier possible dispatch on a different target blocks it with `broker_account_unconfirmed`. If clear, that transaction atomically records possible dispatch and a **new account-wide quarantine** *before* any future Provider transport. `INSERT OR IGNORE` is forbidden at this boundary: already-quarantined accounts must never silently admit another dispatch. Previously fenced but never-dispatched operations remain individually cancellable without clearing the other operation's account quarantine. Different accounts remain independent. No Provider transport or network effect is implemented in B1/B1.
- `mark_uncertain` and post-crash boot retain all spent attempt IDs, target locks and unknown account quarantine; other Runs/targets under the account remain blocked. No readback, deadline or content hash can manufacture a terminal result.
- `cancel_never_dispatched` is permitted ONLY when the exact operation is still `pre_dispatch` and has never crossed the dispatch-possible boundary. The Broker-private SQL stores `verified_no_effect`, a positive monotonic receipt sequence and canonical SHA-256 transcript digest in the SAME transaction that clears that target lock. The spent attempt remains immutable. This represents a **local state-machine fact**, conditional on a later Broker-exclusive and fenced Provider send path. It is not a real provider no-effect/cancellation proof if credentials or egress can bypass the Broker.
- `observe_finality` maps a known matching attempt to A0 `BrokerFinalityObservation`: `pending` (without receipt) or the locally verified never-dispatched terminal state. Unknown or mismatched attempt identity is rejected. **There is deliberately no `verified_remote_effect` issuer.**
- `request_off` durably closes the global Broker gate before returning pending/complete status. It does NOT stop an OS process or certify a remote effect has settled.

### Major remaining production gates

1. Distinct trusted Broker/Owner/Worker UIDs, Broker-only credential ACL and egress, a trusted Broker daemon/socket, connected Owner `SO_PEERCRED` authentication, exact Owner Claim/SSHSIG/immutable Plan proof.
2. Real Broker Provider CAS/PUT with durable pre-send idempotency and operation-specific remote finality, protection against delayed requests, and explicit account-wide cancellation proofs; matching file hash/ETag alone is insufficient.
3. Monotonic protected credential rotation authority/watermark, authenticated Provider-currentness proof, Owner/Broker-wide recovery and unknown-effect reconciliation. No automatic unlock of uncertain operations.
4. Owner-held stable effect `flock` across Begin → Broker send → durable proof → Owner terminal settlement; do not re-acquire Owner's lock through Broker callbacks.
5. Distinct-UID OS/process/fault-injection and power-loss acceptance on approved filesystem, bounded live single-file `98-System/**` canary, and deployment/Auto apply, all separately approved.

**Source correctness ≠ Provider-effect correctness.** A locally constructed `BrokerFinalityObservation`, successful SQLite commit or synthetic receipt is not a transferable capability and must NEVER directly unlock Owner-private D1/B0 Journal or authorize a Writer.

## Verification boundary

`tests/test_472_broker_journal_d1b1b1.py` uses disposable local files and tests duplicate attempts, cross-Run conflicts, exact immutable identity, generation mismatch, never-dispatched settlement, account quarantine, restart/Off/rotation state, rollback and unsafe DB. Tests do not exercise actual Broker credentials/network, different UIDs or live Vault. A0 and D1/B0 remain independent/unwired.
