# #472 R2/B2c-D1/B0 — Owner-private Admission Journal (source-only)

Status: **local source-only MVI**. The predecessor [D1/A0 contract](vault-formatter-effect-admission-d1.md) merged in PR #554. No Commit/Push/PR, runtime wiring, credential change, provider effect or deployment is authorized for this B0 work.

## Durable local state, not a production permit

`vault_format_effect_journal.py` is an isolated SQLite persistence component. The ordinary `snapshot_app`, Formatter, Writer, scheduler, Worker client and Owner listener **do not import it**.

The Owner must pre-provision a private 0700 directory. A disposable **explicit bootstrap** operation (`bootstrap=True`) then provisions one 0600 SQLite file plus a separately pinned 0600 journal-identity file; ordinary open (`bootstrap=False`) never creates or replaces either file. Missing files, file inode substitution, mismatched journal identity, symlinks, mode and ancestry mismatches fail closed. No automatic rebootstrap is allowed after a crash or deletion. Both identity and database files remain within the same trusted Owner UID boundary: same-UID malicious rollback of both files or restoration of an older matching-identity DB needs an independent externally protected monotonic witness in a later phase and is **not** covered by B0. B0 uses SQLite rollback journal mode, `PRAGMA synchronous=FULL`, and short `BEGIN IMMEDIATE` transactions. The single Owner-wide transaction domain orders a gate, one-shot exact (Run ID, operation ID, plan/policy/source/provider/receipt/target/ETag/before/after hashes, boot/credential epochs) attempt record, conflict key (account, canonical target), and unknown-effect account quarantine. No SQL transaction is held across a flock wait or Provider I/O.

New state begins CLOSED. An explicit Owner boot transition advances the persisted global boot epoch and turns any previously active attempt into permanently unconfirmed until independent finality is established; its resource key remains fenced. Merely opening another journal handle cannot start a new boot or rearm a Claim. Existing Off/rotation closure takes precedence over reboot. A rejected/doubled Begin cannot recycle its one-shot operation key.

Off first closes the Owner-wide gate and only then observes journal-level inflight/unconfirmed/quiescent status. Rotation keeps the gate closed, prevents earlier credential epochs from admission and tracks a pending new digest. This local status is **not** an OS stop ACK or actual Broker credential rotation. The Owner must separately hold the pre-provisioned stable effect flock and verify a trusted Broker generation.

For an independently proven exact operation disposition, the Owner may persist either `verified_remote_effect` or `verified_no_effect`; in one short transaction the journal marks the terminal outcome and releases only that matching target-conflict key. A spent attempt is never removed. Unknown account-wide impact remains quarantined until separately authenticated account-scope finality. Under any ambiguous evidence or SQLite error: no success is acknowledged and no blind PUT retry occurs.

After a crash, a *new* trusted Owner may reconcile old attempts with the gate closed. No old Run/Worker session can gain authority merely because the journal row is available. The boot gate must remain closed until all active, unresolved or unknown-account effects are reconciled; it may not override an earlier Off/rotation closure. A duplicate Boot request within one still-unreconciled epoch is denied rather than silently bumping the epoch again. Once all known effects are resolved, the journal can record boot reconciliation **without reopening** an earlier Off or pending Rotation closure; this prevents a permanently stuck Rotation after restart. Status uses one SQLite read transaction so gate and outstanding counts come from one coherent database snapshot.

## Not implemented and explicitly not authorized

1. Verification of signed grants and exact immutable approved plans, Owner-private Run Ledger Claim/boot binding, the actual Owner-held `flock`, or real per-UID authenticated IPC.
2. Broker-only credentials/egress, credential generation readback/rotation, canonical WebDAV CAS, provider operation-specific finality and independently proven account-scope cancellation.
3. An authenticated Owner-wide post-crash scan. `complete_boot_recovery` only checks journal data; its use as production admission without external proof is **unsafe**. `record_verified_outcome` and `release_account_after_verified_finality` perform persistence **without authenticating supplied finality**. No Worker-exposed endpoint may call them.
4. Production SQL directory-fsync/power-loss proof, cross-UID/OS-permission acceptance, network/Provider effects, service activation, Writer injection, auto-apply, release or deployment.

These are deliberately unimported persistence mechanics for later D1/B1 review, **not** a drop-in authorization service.

## Verification boundary

The isolated `tests/test_472_effect_journal_d1b0.py` suite covers initial Closed, durable Begin/Off ordering, replay and target conflicts across independent SQLite handles, reboot epoch/uncertain persistence, transactionally coupled terminal and resource release, account quarantine, rotation watermark, invalid paths, private filesystem modes, SQL-busy fail-closed and uncommitted-transaction process death. The pre-existing #472 and Formatter regression tests remain independent evidence.

Next D1/B1 needs explicit Owner–Broker finality/credential authority, stable-lock ownership/call graph and process/OS crash fault injection. All activation and live Vault/CAS gates require separate human authorization.
