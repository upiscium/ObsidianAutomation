# Vault Formatter Phase 1: shared scheduler and management UI (#472)

Parent: [Vault Formatter Epic #469](https://github.com/upiscium/ObsidianAutomation/issues/469).
Stacked prerequisites: [#470 / read-only formatter](https://github.com/upiscium/ObsidianAutomation/issues/470)
and [#471 / guarded CAS writer](https://github.com/upiscium/ObsidianAutomation/issues/471).

## Operating model

In the fresh nonroot snapshot_app --state application, Formatter owns a
separate vault-formatter/ SQLite WAL database, lock and run history.
It reuses the existing single Snapshot scheduler tick. No new systemd
timer, LXC, root Worker, GitHub token or Nextcloud account is necessary.

The Dashboard provides a Vault Formatter / 98-System panel:
- Mode Off (default) or Dry-run. Auto apply is visibly disabled.
- Schedule interval 300–86400 seconds.
- Save settings and Run Once (always Dry-run). Manual Dry-run works even
  when the periodic schedule is Off if Nextcloud is configured.
- Refresh the exact persisted run receipt, status, safe counts and next due.

The HTTP routes are GET /api/v1/formatter, GET /api/v1/formatter/runs/{id},
POST /api/v1/formatter/settings and POST /api/v1/formatter/runs.
They reuse the existing Origin + one-time CSRF checks. All reported fields
are bounded/typed metadata; no Vault body, secret, remote URL or content
diff is sent to the browser.

## Read-only remote source capture

The Formatter reuses the shared Nextcloud rclone.conf and configured Vault
folder. It first obtains a bounded rclone lsjson inventory of eligible 98-System
Markdown files (file count, individual sizes, total bytes, path constraints).
It downloads only that exact allowlist into a disposable private mirror.
Each selected batch has at most the 2-MiB per-file byte budget in expected
total source bytes. The copy uses HARD max-transfer=expected_bytes+1, while
a read-only rclone check --download uses HARD max-transfer=2*expected_bytes+1
(both remote and local streams count on the target rclone version). Local
files are checked after each batch against the pinned inventory. A final
bounded remote inventory follows. Only then does it call #470 create_plan.
An oversized, mutated or unobservable source is rejected, never silently
truncated or treated as a successful preview.
The temporary allowlist and mirror are removed on success or failure.
For compatibility with Debian rclone 1.60.1-DEV, the fixed file allowlist
uses newline-delimited --files-from-raw (not unsupported --files-from0).
The input manifest rejects CR/LF and all control characters in path
components before constructing this file, while the raw mode preserves
ordinary spaces, '#', brackets and non-ASCII UTF-8 filenames.
The installed rclone rejects --files-from-raw combined with --max-size
because the raw allowlist overrides other filters. Partitioning keeps a
single changed remote file from consuming the entire 64-MiB job budget
before rejection. Before its verification pass, each completed batch must
match the pinned source inventory exactly in filename, file count, file
type, per-file byte size and total bytes. Any mismatch fails closed; the
mirror is deleted without changing Nextcloud.
Only read-only inventory and remote-to-local copy/check are permitted.

The source path invokes only remote-to-local copy and check; no remote
rclone sync, push, upload or WebDAV PUT/MOVE/DELETE.

## Persistent result contract

- queued/running: bounded one active Formatter worker; duplicate calls are
  not resubmitted.
- would_change: a verified Dry-run found safe candidate changes; no Vault
  write occurred.
- no_changes: empty safe diff; NOT proof that Gitea/GitHub Core converged.
- failed: source/plan unavailable, with a fixed reason code and no private
  diagnostic text.
- unconfirmed: queued/running interrupted across restart; never replayed.
- applied/conflict/indeterminate: only for a separately injected and
  authorized CAS Writer; no such writer is passed by the normal service.

Construction does not begin jobs. A successfully saved change to the
Nextcloud source identity resets only the Formatter mode/schedule to Off.
A failed save or a Gitea-only settings change does not disable Formatter.
Other app jobs retain their independent operating modes.

## Source-only scheduled Writer admission semantics (#472 R1)

The ordinary application still creates `FormatterJob(writer=None)`; this
section documents behavior only when a separately approved Writer is injected
in isolated tests or a future authorized runtime. It is **not** permission to
activate live Auto apply.

- Each queued scheduled `auto-apply` run pins the current settings epoch and
  canonical Nextcloud source generation. A change to Off, Dry-run, schedule
  interval, or the source identity while the Plan is being scanned revokes that
  run. Even an Off→On transition never revives the old queued approval.
- The final admission check is performed **after** the Plan scan and immediately
  before the Writer invocation. A revoked run records `failed /
  writer_admission_revoked` with **no Writer call** and no automatic replay.
- Off settings requests serialize with the one Writer invocation: if a Writer
  is already in flight, Off is *acknowledged only after it returns*. A button
  click or pending HTTP response cannot cancel a PUT already transmitted.
  The mode update may wait for the full bounded Writer call and may outlast
  the operator's HTTP client timeout; a timed-out click is **not** proof that
  Off has taken effect. The credential-save and post-save
  `connections_saved()` paths also acquire the **reentrant Writer gate
  before** Snapshot/Daily/Formatter locks; never call the revocation hook
  under Formatter's lock without first owning that gate. The lock order is
  Writer gate → Snapshot/Daily locks (only on credential routes) →
  Formatter lock → shared connection store. The shared Scheduler, Snapshot
  and Daily workers do not themselves acquire the new Writer gate.
- Known CAS preflight conflicts are reported as `conflict`, known preflight
  availability/policy failures as `failed`, and unobservable writes or
  malformed Writer receipts as `indeterminate`. Before displaying `applied`,
  the Job validates an exact Plan SHA, operation ID, receipt SHA, target paths,
  before/after digests and consistency of `vault_modified` with the Writer
  outcome. During R1, a `complete` receipt must cover **every**
  `would_change` entry in its Plan. A future partial-scope Writer needs a
  separate explicit selection-admission contract in R2: updating only a
  subset must not look like whole-plan success. Partial CAS conflict is
  distinct from partial HTTP authorization/refusal (`failed /
  writer_partial_write_rejected`), and `writer_outcome=partial` is retained.
  These are **structural checks**, not independent verification of
  receipt durability or remote state: the authorized Writer remains responsible
  for private immutable evidence and provider readback. No raw Vault body,
  path, ETag, credential or exception string is included in public results.
- A process crash still marks queued/running rows `unconfirmed` on restart.
  The new in-memory admission token is never restored from SQLite and
  cannot turn an interrupted run into an automatic write retry.

R1 does not establish a recurring Writer credential broker, cross-process
rotation lock, actual production approval, or Snapshot→Gitea→Core end-to-end
convergence. Those remain later #472 gates.

## R2/A0 — recurring admission proposal (source-only, no effects)

`vault_format_recurring_admission.py` defines a pure, separately injectable
proposal builder for a future scoped Writer. It is **not** used by the normal
app, Scheduler, HTTP API, or the one-shot #471 Canary adapter:

- A frozen `RecurringPolicy` pins `98-System/**`, the versioned Formatter
  policy, exact Nextcloud source-generation hash, account-binding digest,
  external provider-proof reference, and explicit file-count / byte budgets
  (never exceeding the existing CAS Writer ceilings).
- The external `verify_operator_policy` callback must confirm the *exact*
  canonical policy digest against an independently retained Human approval.
  Merely constructing a policy object or providing a verifier which always
  returns True does **not** grant real write authority. No normal runtime
  supplies such a verifier.
- `prepare_recurring_admission` snapshots the complete operator-policy
  scalar values *before* any callback. Even `object.__setattr__` mutations to
  a caller-owned frozen dataclass cannot enlarge the verified limits or change
  the reported binding after approval. Only those private pinned values are
  used for checks and the admission proposal.
- It creates a **bounded** immutable JSON snapshot of authoritative Plan
  metadata, omitting untrusted display-only `diffs`; overlarge serialized
  metadata is rejected before JSON decoding. It checks source generation
  before and after proof verification, Plan SHA, and every selected target's
  before/after SHA, source/desired size and format-line metadata.
  It admits **the full `would_change` selection or none**. If a Plan exceeds
  file-count or source/output byte budgets, it **rejects** rather than
  silently selecting the first N entries. Zero-change Plans return no
  operation to apply.
- Every run supplies an already-persisted 32-hex `run_id`. The deterministic
  `operation_id` is derived from that run_id only, *not* from mutable Plan,
  credential or policy contents. Replaying a run with a different Plan must
  still collide with the same durable Writer operation marker. The marker
  is created before HTTP effects; it does not prove a previous effect finished.
- A resulting `RecurringAdmission` is **descriptive metadata**, not a
  capability token. A later effect-time adapter must revalidate all pins
  under the existing Writer gate / connection-store lock, require an
  independently approved CAS-provider proof, reserve the receipt/operation
  marker durably, and recheck source identity before every GET/PUT. Its
  conditional PUTs and readback remain owned by the #471 Writer, never by
  Snapshot, Dashboard or the Plan builder.
- A0 uses no credentials, performs no WebDAV request, does not write any
  operator approval, and is not registered in `snapshot_app`. Independent
  review, R2/B effect-time binding, and user authorization are later gates.

## R2/B1 — opt-in effect-time Writer adapter (source-only)

`vault_format_recurring_binding.py` adds an **unwired** `RecurringEffectWriter`
for future approved recurring runs. The ordinary `snapshot_app` continues
to construct `FormatterJob(writer=None)`; no HTTP route, timer or CLI
can create this adapter. This is not evidence of a production permission.

- Each instance is pinned to **one run ID**, one validated `RecurringPolicy`,
  and one private receipt root. The complete Plan is copied into bounded,
  non-aliasing metadata before any injected verifier runs.
- Three **separately trusted injected callbacks** are mandatory: exact
  operator policy digest approval, an already-persisted current run's
  run/Plan/policy/operation authority, and actual provider CAS capability
  proof. A permissive synthetic callback is not a real approval. B1 does
  **not** implement the durable authority issuer or automatically trust
  a scheduler row.
- External operator-policy, run and provider verifiers **and injected
  source-generation callbacks** are called without holding the SetupStore
  lock. The approved policy itself is also rechecked at every request
  boundary, so revocations observed between GET and PUT fail closed. This avoids
  blocking another worker that needs SetupStore to complete validation.
  The observed generation is then checked against the canonical credential
  hash and account binding inside the lock. The lock is released **before**
  each actual HTTP transport callback to prevent waiting on a worker that
  itself needs SetupStore. Run/provider authority checks and canonical
  credential checks are performed before each GET, conditional PUT and
  readback; they are **not atomic with transmission**. The underlying
  `apply_plan` owns remote
  bytes, immutable receipt records, conditional If-Match and the operation
  replay marker; no secondary PUT implementation is introduced. A second
  concurrent call fails immediately with `recurring_effect_busy`.
- These checks guarantee rejection **only if revocation is observed before
  the individual request**. They cannot atomically bind a remote PUT to an
  authority decision: a revocation after the last check can race a request
  already transmitted. Production admission still requires a separately
  governed authority lease / cancellation barrier and cross-process
  credential-rotation protocol. Verifier callbacks must be read-only,
  idempotent and externally trusted, not user-configurable lambdas.
- If a credential or approval changes *after the initial GET*, the Writer
  can conservatively classify the attempted effect `indeterminate`, even if
  the synthetic transport proves no PUT was transmitted. This incident is
  never automatically replayed.
- B1's synthetic fake-WebDAV cases include full-plan conditional writes,
  readback, same-run replay with a changed Plan, revocation between GET and
  PUT, and missing/denied verifier handling. None uses real credentials.

**Outstanding R2/B gates:** a concrete independently governed, durable run
issuer/verifier; origin and lifecycle of per-run grants, cross-process
credential-rotation coordination and bounded stop/drain; exact trusted receipt
root configuration; real CAS capability acceptance; separate owner consent
for an isolated canary; and then a further explicit approval for recurring
production Auto apply. Merely merging this source is not one of those grants.

## R2/B2a-S — detached signed-run verification and bounded drain (source-only)

The **unwired** `vault_format_signed_run.py` verifier and
`vault_format_run_drain.py` effect/stop gate implement a narrow future
authority boundary. They are **not** instantiated by `snapshot_app`,
Scheduler or WebUI. Normal operation remains `FormatterJob(writer=None)`.
These two modules do not issue approvals, create signing keys or enable
production writes.

- An external operator signs a **canonical JSON run grant** using OpenSSH
  SSHSIG (namespace `obsidian-vault-formatter-run`). The approved signer key
  and detached grant/signature must live in a directory owned by a separately
  trusted OS identity; the ordinary Formatter worker must not control its
  signer private key or `allowed_signers` trust anchor. A testing directory
  owned by the same UID demonstrates behavior but **not** production trust.
- The grant binds exact `run_id`, Plan and policy SHA-256, deterministic
  operation ID, Nextcloud source generation, account-binding digest, provider
  proof digest, scope/policy version, **one receipt-root path digest**, and a
  bounded issue/expiry interval. Files are opened via no-follow descriptors,
  checked for owner/perms/hardlinks and passed as pinned descriptors to the
  system OpenSSH signature verifier. A merely present `approved=true`
  field, unsigned scheduler row or `lambda True` is never sufficient.
  No raw Vault document contents or credentials appear in this grant.
- A pre-provisioned `effect-drain.lock` inode coordinates all **cooperating**
  effect writers and the trusted stop actor using a kernel exclusive lock on
  a **single local filesystem**. The gate refuses a receipt root whose path
  digest differs from the one pinned by the signed verifier. Under the lock,
  it checks the signature and fsyncs a local claim in the writer-owned receipt
  directory **before** yielding to an already authorized Writer. A process
  crash releases the kernel lock but leaves the claim **while that directory
  remains intact**. The worker can also delete its own claim and CAS Writer
  marker, so these are **not tamper-proof one-use evidence**. The source-only
  tests explicitly demonstrate this boundary. Actual unattended one-use
  enforcement still requires a separate, operator-controlled, non-worker-
  writable durable claim ledger or an equivalent trusted issuer.
- `stop_and_drain(run_id)` attempts the **same lock** for at most a fixed
  timeout. If an effect still holds it, stop returns `drain_timeout` and
  must **not** be displayed as acknowledged Off. After the writer releases
  the lock, the trusted operator records and fsyncs a revoked marker.
  A previously existing marker is revalidated and **both its file and
  directory are fsynced again** before returning an acknowledged
  `already_stopped`. A short marker write or either fsync failure never
  acknowledges Off; tests cover both retry cases.
  Subsequent verification refuses that run even if its signed grant is
  otherwise valid. No other formatter runs, Snapshot, AI or Daily timers
  are stopped by this source-only gate.
- This provides a clear *acknowledgement* boundary **only when all writers,
  credential rotations and stop actors participate in the same lease**.
  It does not forcibly cancel an already sent PUT, protect against
  uncoordinated external credential changes, guarantee flock on a network
  filesystem, or provide rollback. No worker/stop integration is enabled yet.

**Remaining production gates:** independently operated Human approval,
OS ownership and root-pinned trusted signers, a **non-worker-writable durable
claim ledger**, provisioning of the same stable lock inode for all effect
processes, a bounded operator stop workflow,
credential rotation under the same lease, explicit runtime injection after
separate authorization, real Nextcloud CAS proof, one-time canary, and
Snapshot→Gitea→ObsidianCore convergence. A signature for each run is
suited to a controlled canary but **not** scalable unattended operation:
a later trusted delegated issuer under a long-lived approved policy is
required for the intended recurring use, without re-asking the Human
for every run. No such issuer exists in B2a.

## R2/B2a-L — durable run-intent registry (source-only)

`vault_format_run_ledger.py` introduces an independent SQLite run-intent
registry for a future trusted operator workflow. It is **not** a Human approval
issuer, not an HTTP/API endpoint, and not connected to the Scheduler or Writer.

- Each sealed `RunIntent` binds one run ID to exact Plan/policy hashes,
  operation ID, connection generation, account binding, provider-proof hash,
  and receipt root. An externally controlled operator-verification callback
  must explicitly approve registration; merely passing a permissive callback
  is **not** real operator authorization and must never be exposed to clients.
  The persisted intent is copied before the callback; the callback receives
  a second detached copy. Neither a mutable caller object nor callback-side
  mutation can replace the Plan or run ID accepted for persistence.
- The owner-private state directory requires mode 0700; the database is 0600
  and contains no credentials or Vault bodies. SQLite `BEGIN IMMEDIATE`
  serializes cross-connection claim and revocation updates, with synchronous
  full commits. A run may be claimed **once**. Duplicate run/operation IDs and
  mutations to a registered Plan are rejected rather than silently replayed.
- The read-only `verify_claimed` has the exact four-argument shape required
  by the R2/B1 run callback, but no production callback injection exists.
  **Every fresh registry instance starts with verification disabled** until
  explicit owner-recovery reconciliation. Recovery changes all older
  `claimed` entries to `unconfirmed` before enabling any positive check;
  they are never automatically returned to approved/claimable. Revocation
  also fails closed. A future owner process must coordinate recovery with
  all other active processes; this source-only ledger is not a lease.
- This registry provides durable state, **not** a cross-process authority lease
  that spans a remote PUT. A registry row also does not prove that the
  external callback was a legitimate Human approver. That independent
  authority signature/issuer, trusted receipt-root ownership, coordination
  of revoke with in-flight effects, stop/drain and real-provider acceptance
  are separate R2/B2b+ requirements.

## R2/B2b-R0 — operator-side quiescence and combined stop proof (source-only)

This slice adds **only** the opt-in local `OperatorRunCoordinator` interface
(`vault_format_owner_coordination.py`) and synthetic tests. Its normal
`snapshot_app`, Formatter Scheduler, Admin API and WebUI **are not connected**
to it; normal operation remains `FormatterJob(writer=None)`. The
owner-side test signer and Ledger run under the *same temporary test UID* and
therefore **do not demonstrate a separately trusted production OS principal**.

- The operator process must hold the same **pre-provisioned, stable local
  `effect-drain.lock` inode** as every cooperating Writer. Owner recovery
  waits for an exclusive lock before changing previously `claimed` rows to
  irreversible `unconfirmed`. Timeout cannot be reported as recovery
  completed. The new test executes a **fresh Python interpreter**, not merely
  `fork`, to confirm the old claim is not reaccepted before/after recovery.
- The authoritative SQLite Ledger directory has the fixed operator-owned
  directory `<signed authority_root>/run-ledger/` (0700) with fixed
  `formatter-run-intents.sqlite3` (0600), rather than a caller-selected
  state database. `OperatorRunCoordinator` rejects another
  database even when its schema and Run IDs look valid. The signing/authority
  root is operator-provisioned and must not be writable by the Worker.
- Owner `register_signed` only registers an exact `RunIntent` matching the
  independently verified SSHSIG grant and pinned source-generation/account/
  provider/root digests. `claim_signed` reserves the already approved run
  **exactly once** under the same lock. Both require a completed owner
  recovery barrier. These methods do **not** sign grants or automatically
  authorize future runs. They do not return a remote-effect token.
- `change_under_quiescence` holds the same lock while a separately supplied
  operator callback makes and **independently verifies** a change, returning
  literal `True` only on success. The interface does **not** read or rotate
  real Nextcloud credentials, implement a canonical credential-generation
  store or prove durable updates by itself. Failed/late callbacks never return
  a successful owner-change acknowledgement.
- `stop_and_revoke` performs bounded drain, **durably writes the signed
  revocation marker**, and then runs the Ledger revoke/verification callback
  **before releasing the same lock or acknowledging stop**. The new optional
  `DurableRunDrain.stop_and_drain(on_drained=...)` hook only acknowledges after
  the callback returns literal `True`. If the Ledger step fails, the signed
  revocation remains in force, the call fails closed, and a retry verifies the
  existing marker and completes the Ledger revoke. An **uncoordinated** raw
  `stop_and_drain()` still means *only* the signed-run marker was stopped;
  do not expose that result as a combined Ledger/Writer Off acknowledgement.
- The source-only fixture uses a fake WebDAV transport to verify **one** CAS
  conditional PUT and **post-PUT GET/readback** guarded by signed grant,
  owner Ledger Claim, shared drain and `RecurringEffectWriter`. A separate
  synthetic OS process holding the shared effect gate also verifies that the
  Owner cannot ACK stop, recovery or credential change until it releases it. It does not access a real Vault or
  Nextcloud account. Worker-owned receipt markers remain tamperable by their
  own UID; the owner Ledger is only independent if deployed under a genuinely
  different OS principal that the worker cannot modify.

**Still not production ready:** no separately operated approved signer/private
key or controlled delegated issuer, no authenticated IPC from a distinct
Worker identity to the Owner, no deployed Owner/Writer credential-generation
lease, no resilient crash/restart of the actual multi-process service, no
network filesystem `flock` guarantee, no real CAS provider proof, no canary,
no Snapshot→Gitea→Core publication E2E and **no Auto apply activation**.
Every participant must use the same stable local lock; direct edits outside
that lease remain unprotected. This B2b source checkpoint must never be
interpreted as permission to inject a Writer or to run against live Vault data.

## Production requirements NOT YET MET

Source tests and this app integration do not enable live mutation. The
normal app never injects a Writer and cannot set Auto apply, even from a
crafted HTTP request. Before unlocking writes:

1. Demonstrate real Nextcloud stale If-Match protection without mutating
   a different server-side version. Synthetic tests alone are insufficient.
2. Obtain explicit owner approval for exact Vault, narrow 98-System canary,
   bounded permissions, receipt root, rollback and retained source versions.
3. Review an independent Writer adapter binding #471 to those approved
   credentials and the original CAS plan; never let the UI supply raw PUT
   bytes or arbitrary paths.
4. E2E verify readback, private Snapshot, guarded Gitea publication,
   GitHub public allowlist and no Core drift feedback loop.
5. Authorize unattended Auto apply separately from source Merge and canary.

The Snapshot remains read-only; the public export allowlist does not change.
The formatter never overwrites user-owned Vault changes with old Core bytes.

## Tests

Use disposable synthetic fixtures:

    python3 -m pytest -q tests/test_vault_format_job.py
    python3 -m pytest -q tests/test_vault_format_writer.py tests/test_vault_formatter.py

Also run Dashboard, Snapshot, Daily and CI regression. No test implies
permission to merge, deploy, write to a real Vault, or enable production.
