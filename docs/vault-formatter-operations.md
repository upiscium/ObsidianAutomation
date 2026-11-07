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

## R2/B2c-A0 — peer-authenticated Unix IPC contract (source-only)

`vault_format_owner_ipc.py` adds an **explicitly opt-in, Linux-only,
one-request IPC handler** for a previously accepted `AF_UNIX/SOCK_STREAM`
connection. It is **not a listening server** and does **not create** a
filesystem socket, directory, daemon, users/groups, private signing key,
credentials, or a runtime Writer. The regular formatter remains read-only.

- The handler obtains the connecting peer's UID from kernel
  `SO_PEERCRED` **before reading any request bytes**. An unknown UID is
  denied immediately without attempting to read a request frame. The trusted
  Owner UID must be the local process EUID and differ from the provisioned Worker
  UID; a same-UID Worker cannot claim authority merely by selecting a role
  field in JSON. This is Linux-specific and is not an HTTP authorization
  mechanism. An `AF_UNIX` forwarding proxy would authenticate the proxy,
  **not** the original client.
- Requests are versioned JSON, exactly one newline-delimited frame per
  connection with a strict **2048-byte** limit (including its newline),
  duplicate-key rejection and exact operation schemas. The client **must
  write-half-close** with `shutdown(SHUT_WR)` after sending the request. The
  handler reads through EOF before dispatch under a monotonic **total
  frame-read deadline**, rejecting second frames even in later `recv()` chunks.
  There is no request pipelining, and failures use static error codes. No Vault body, raw WebDAV credential,
  arbitrary URL/path, free-form shell command, or generic function callback
  is accepted.
- Only the configured Worker UID can request `run.claim` (which calls the
  existing **signed-grant + one-shot Ledger** owner gate) or `run.status`
  (a strictly **read-only snapshot** of current Ledger claim *and* signed
  grant validity). A successful status query may return
  `{"ok":true,"claimed_now":false}` when the signed grant was revoked
  but the Ledger still records a Claim. `ok` describes protocol success,
  **not effect permission**. The status result `claimed_now` is **NOT**
  a new effect-time lease and must never authorize a PUT without the existing
  shared drain lock, CAS plan/policy/provider checks and Writer's own
  pre-request validation.
- Only the separately trusted Owner UID can request `run.stop` (same
  bounded Drain + durable Ledger revoke acknowledgement) or
  `owner.recover` (coordinated fail-closed invalidation of old claims).
  There is no `sign`, `approve-any`, `write`, `rotate-password`,
  `raw-put`, or arbitrary path command. A lost response **must not** cause
  a mutating request to be replayed automatically: repeated Claims remain
  denied by the durable Ledger.
- Synthetic tests use real Linux `SO_PEERCRED` to verify that the local
  **same UID is denied Worker privileges**, and exercise simulated role
  policy with a test-only monkeypatch. Such synthetic role simulation is
  **not** evidence that actual distinct UIDs successfully communicate.
  Isolated tests use socketpairs and a temporary local filesystem listener
  with a freshly executed Python client. That real accepted connection also
  reports the same EUID and correctly denies Worker-only actions. It does
  **not** prove a truly distinct-UID Worker. Peer credentials also do not
  authenticate an original requester behind a proxy or a deliberately
  transferred already-connected socket descriptor.

**Still required before production:** a separately operated Owner UID and
Worker UID (and distinct trust/claim ownership), secure pre-provisioned
socket parent and listener (including socket inode owner/mode, group access,
path lifecycle, connection quota and restart cleanup), cross-UID integration
tests with real OS peer credentials, an authenticated Owner startup and
delegated policy/run issuer, stable lock inode across all processes, bounded
failure/reconnect handling, credential generation rotation integration,
independent provider proof, live single-run Nextcloud CAS canary, full
Snapshot→Gitea→ObsidianCore convergence and **separate Human approval**.

## R2/B2c-B1 — pre-provisioned Owner Unix listener lifecycle (source-only)

The opt-in `vault_format_owner_listener.py` supplies a **one-accept-at-a-time**
Linux Unix socket listener for the already source-only `OperatorUnixIPC`.
The listener is **not imported by `snapshot_app`**, installed as a service,
started by the scheduler or exposed via WebUI. The normal formatter remains
`FormatterJob(writer=None)`.

- The trusted Owner OS principal must **separately pre-provision** an absolute
  socket directory owned by the Owner UID and the designated IPC group with
  exact mode `0710`, plus an existing `owner-listener.lock` file with
  exact mode `0600` owned by that UID. No runtime code creates the directory,
  lease file, users, groups or a service. The Worker should have a **different
  UID**, be authorized to traverse the group-executable directory and connect
  to the `0660` group socket. Socket group changes require the Owner already
  to be entitled to use that group. The IPC handler continues to make the
  final **kernel `SO_PEERCRED` UID** authorization decision.
- Startup opens path components with **`O_NOFOLLOW`**, rejecting untrusted
  replacement ancestors, incorrect final owner/group/mode and a pre-existing
  socket path (including a stale Socket inode). It obtains `flock` on the
  pre-provisioned stable lock inode before binding. It verifies the newly
  bound inode's owner/group/exact `0660` mode **before listening** and
  limits the backlog. If the path is occupied, it **never deletes it
  automatically**; separate trusted operator remediation is required.
- Each `serve_once` verifies pinned parent, lock and socket identities,
  accepts at most one connection with a bounded timeout, and delegates to
  `OperatorUnixIPC`, which authenticates the connected peer and requires
  a single JSON frame followed by client `shutdown(SHUT_WR)`/EOF. There is
  no background loop, request-forwarding proxy or arbitrary call surface.
  A `run.status` observation is never an effect-time Writer lease.
- On `close`, a verified Owner unlinks **only the originally bound inode**
  under the pinned directory and exclusive lease, then synchronizes the
  directory. A switched socket inode, inaccessible parent or in-flight
  handler never receives a successful close acknowledgement or foreign
  unlink. A failure while closing the newly bound socket during startup
  also leaves its name intact for operator inspection. After `accept`, the
  Listener independently closes the accepted FD and converts unexpected
  handler exceptions to a fixed `listener_request_unverified` reason. If
  accepted-FD cleanup fails, that Listener instance permanently refuses
  new requests and any automatic pathname unlink. The operator must handle
  any **`listener_cleanup_unverified`** condition. A crash may leave a stale
  socket name; restart fails closed.
- Local disposable tests use a short-lived private `/tmp` fixture, real
  Linux sockets and real same-UID `SO_PEERCRED` checks without touching any
  Vault or host service. An additional **root-only temporary fixture** is
  defined to launch a `nobody`-UID Worker with a pre-provisioned group and
  test real `run.claim` across distinct OS principals; it is **SKIPPED**
  on ordinary non-root developer machines. This is not evidence of
  cross-UID success until it has executed on a reviewed isolated root-capable
  test runner. The source-only `root-stage-fixtures` GitHub Actions job
  now includes a **separate three-test acceptance step** covering genuine
  cross-UID Claim/Owner-only denial, wrong parent group and wrong parent UID,
  with a JUnit guard that rejects SKIP. This step ran successfully for B1 in
  PR #544's exact-head Actions run **#42201086197**: **3 PASS, 0 SKIP** in
  the isolated root job. That is disposable CI evidence, not a production
  Owner/Worker identity or credential-rotation acceptance.
  The disposable Worker client signals connected readiness explicitly
  before the Owner accepts its single request and reads response JSON through
  a newline rather than assuming one Unix-stream `recv` returns the frame.
  The real Worker fixture also checks that the different UID is denied
  `run.stop` and `owner.recover` after a successful `run.claim`, before
  the trusted Owner performs the stop. Neither fixture provisions persistent
  OS users.

**Still missing before production:** verified independent Owner and Worker
UID/group provisioning in the target deployment, target-host cross-UID
acceptance, systemd ownership and restart/cleanup policy, protection
against FD forwarding/proxies, bounded lifecycle recovery and credential
rotation under the same durable effect lease, a delegated approved issuer,
live Nextcloud CAS and actual Vault canary, and Snapshot→Gitea→Core E2E.
A source Merge does **not** authorize production installation or Auto apply.

## R2/B2c-C0 — bounded Owner process session and boot reconciliation (source-only)

The new opt-in `vault_format_owner_session.py` provides **`BoundedOwnerSession`**
for an **already separately trusted Owner process** with an explicitly
constructed B1 `OwnerUnixListener` and B2b coordinator. It is **not** a
process spawner, daemon, systemd unit, scheduler, credential store, grant
issuer, Worker client or Writer adapter. No app import or startup hook was
added. Normal `snapshot_app` continues to construct `FormatterJob` with no
Writer and cannot enable Auto apply.

- The session pins the constructing PID, EUID, exact Listener, IPC handler,
  coordinator and Ledger binding. A session object inherited across fork,
  rebound to another coordinator or run a second time is rejected. This is
  **process identity fencing**, not independent OS authority provisioning.
- Startup first obtains the B1 **exclusive lifetime lock** by opening its
  trusted pre-provisioned Listener. **While holding that lock**, and **before
  accepting any client request**, it invokes deterministic
  `recover_after_quiescence()` under the shared effect-drain lock. This
  converts old `claimed` Ledger entries to `unconfirmed`, never recreates
  grants and refuses a nonfresh Ledger. If another Owner already holds the
  Listener lock, the contender is rejected **without** invalidating that
  Owner's active Claim. The socket may be bound/connectable during recovery,
  but no request can be dispatched before recovery completes.
- The handler accepts **at most 1–16 connections** and allows **at most
  1–24 cumulative idle polls** (no activity-based reset, general loop or
  background task). Each accept and IPC message retains B1/B2c time limits.
  `OwnerSessionResult` provides counts/exit reason only after the Listener
  closes successfully; it is not a capability, Writer permit, or stop ACK.
- On boot recovery, dispatch, listener-close, or ledger identity failure,
  the session returns **no success result**, redacts OS/SQLite error text,
  and cannot be retried on the same instance. Uncertain stale socket and
  Owner cleanup still require separately reviewed operator intervention.
- Disposable tests verify that a freshly constructed Owner session in a
  **different real PID** invalidates a previously claimed synthetic Run ID
  before serving, refuses an Owner-UID call to the Worker-only Claim
  endpoint, and exits after a fixed number of requests with the Socket
  removed. The test-only Owner
  per-request readiness gates let the parent queue and release each request
  separately, avoiding both an idle-poll race and reliance on a multi-entry
  Unix socket pending-connection queue. Other tests enforce
  inherited-process rejection, bounded idle exit, no double boot, fail-closed
  recovery under active effect lock, no response before recovery and no false
  success on unverified close. These fork-based tests use the **same UID**;
  the earlier B1 root-capable CI fixture independently proved an actual
  different-UID Worker Claim but is **not a combined C0 production test**.

**At the time of C0 (before the D0 source-only proposal below):**
no production Owner launcher/supervisor or target-host UID/GID/service policy,
Worker client, delegated signed-Run issuer, credential-generation change
under the stable effect lease, effect-time Writer binding, real Nextcloud
CAS/Vault canary or publication convergence existed. A C0 source Merge
confers **no** permission to run a production Listener or enable Auto apply.

## R2/B2c-D0 — Worker-side Unix IPC Claim/Status (source-only)

The opt-in `vault_format_worker_ipc_client.py` adds `WorkerUnixClient`
for a separately provisioned, restricted Worker OS principal, and
`RunStatusObservation` for read-only inspection. It is **not imported by the
normal app**, does not provision users/services/sockets/credentials, and does
not construct or inject a Formatter Writer. It sends **only**
`run.claim` and `run.status` to the existing B2c Owner IPC; it has no
stop, recovery, sign, generic RPC, PUT, or credential-rotation method.

- The configured Owner UID must differ from the actual **non-root** Worker
  EUID; a root Worker is rejected rather than treated as restricted. Before
  sending anything, the Worker traverses the trusted absolute socket
  ancestry using Linux `O_PATH | O_NOFOLLOW` directory FDs and validates
  the preprovisioned Owner-owned `0710` parent with designated IPC group
  and `0660` Owner-owned socket inode. The Worker verifies **connected
  `SO_PEERCRED` Owner UID**, and checks that the named socket inode has
  not been replaced. Unix-domain FD forwarding or a proxy authenticates the
  **connected peer**, not the original human/requester. No HTTP credentials
  or Vault body is accepted.
- Every command creates one connection with a **3-second monotonic
  budget for socket I/O** (connect/send/receive), including one strict
  request frame, `shutdown(SHUT_WR)` and a response read through EOF.
  This is **not** a complete wall-clock deadline: trusted-path
  `os.open` / `os.stat` metadata calls cannot be interrupted by the
  socket timeout. Production provisioning must separately qualify the
  socket directory as a trusted **local filesystem** and exclude
  blocking/network-backed metadata before activation. Duplicate JSON
  fields, trailing bytes, second frames, oversized responses, invalid
  positive acknowledgements, unexpected response keys and unrecognized
  reason codes fail closed with fixed error codes. No request is auto-sent
  again on a timeout, disconnection or lost acknowledgment.
- A `claim_once(RunIntent)` attempt irreversibly consumes that **client
  instance's** Claim slot **before** validation or I/O. An unconfirmed
  transport outcome is **not** proof that the Owner did nothing: the durable
  Owner one-use Ledger still supplies the cross-instance replay fence, not
  the client object's local boolean. Do not start another run merely
  because the Claim reply was lost; operator reconciliation is separate.
- `status(RunIntent)` sends only the four exact Run identity fields and
  yields `RunStatusObservation(claimed_now=...)`. The result is **a
  read-only observation**, never a Writer permit, effect lease, credential
  generation confirmation or authorization to retry `run.claim`.
- The new local tests use real Unix streams with **explicitly synthetic
  peer UID and filesystem trust shims** to exercise frames, non-retry,
  malformed/extra replies, wrong peer, socket inode switch and rejection
  of Worker-owned paths. They **do not** prove distinct OS principals.
  A separate **root-only disposable CI fixture** uses an actual restricted
  `nobody` Worker with a preprovisioned `0710` Owner directory and
  `0660` group socket, exercising the real kernel `SO_PEERCRED` and
  one-shot Claim plus read-only Status, a consumed same-instance retry,
  and an explicitly tested cross-instance second Claim rejected by the
  Owner's durable Ledger. The existing root-stage CI job
  adds an **exact one-test JUnit gate rejecting every SKIP**. This new
  D0 fixture has **not yet run on a published D0 commit**.

### D1+ effect-time Writer/credential-generation boundary (design only)

A valid Claim ACK or Status observation is **necessary but insufficient**
to write to a canonical Vault. The future **Owner-mediated effect path** must:
1. Have the **Owner (not the Worker)** hold the same pinned effect-drain
   lock inode used by the Owner's stop/revoke and credential rotation, across
   broker dispatch, server-side CAS and trusted readback. The restricted
   Worker does not hold that lock or perform provider writes directly.
2. While the **Owner** holds the lock, revalidate the exact signed Run grant,
   policy, plan hash, account binding, provider proof and claim/receipt
   identity. The **Owner-private Ledger must not be read or mutated by the
   restricted Worker**. D1 must first design and independently test an
   Owner-verified, replay-safe effect admission / durable attestation that
   cannot be bypassed by a stale grant or boot-`unconfirmed` Claim.
   In particular, do not make the Worker hold the shared exclusive lock and
   then synchronously call an Owner IPC command that attempts to acquire the
   same lock: that would deadlock. Re-read the current credential-generation
   binding and reject any mismatch **before** transmitting a PUT.
3. Enforce the already reviewed `98-System/**` allowlist, change budgets,
   stale ETag / `If-Match` conditions, and a fresh provider readback.
   An ACK or cached `claimed_now=true` must **never** bypass these checks.
4. Coordinate Owner Off/revoke/rotation through the **same lock**; no Off
   or rotation-success acknowledgment until cooperating in-flight effects
   have drained and the durable postconditions are verified. Lost effects
   are `unconfirmed`, not retried blindly.
5. Keep the existing Snapshot and Gitea promotion pipeline read-only.
   Require separate target-host OS identities/service, signer/issuer,
   provider CAS canary, real Vault/E2E and Human Auto apply approval.

**No D1 adapter or credential-generation integration exists in D0.**
Merging this source-only client would not enable real Vault writes or
production Owner/Worker connectivity.

## R2/B2c-D1/A0 — effect-time admission design and disposable contract (source-only)

See [the D1/A0 protocol and risk analysis](vault-formatter-effect-admission-d1.md)
and `tests/test_472_effect_admission_d1_contract.py`. These describe an
Owner-held effect lease, required durable one-shot attempt/settlement record,
credential-generation authority and denial of Off/rotation after an
unconfirmed remote effect. The test-only model **does not issue permissions**,
change Ledger schema, install services or activate any Writer. A signed-only
`DurableRunDrain.effect()` is not a replacement for Owner-private Ledger
effect-time admission. No production Vault write is authorized by D1/A0.

**D1/A0 PR #554 R1 (source-only, local correction):** A post-restart
`unconfirmed` operation cannot require already-complete boot reconciliation
before its independently verified finality is durably recorded. A future
authenticated **Owner-only** recovery path must read the Owner-private exact
attempt, verify Broker operation-specific finality (or verified never-dispatched
status), and atomically record that disposition and release the target conflict
key **while the global admission gate remains closed**. Unknown provider
account-wide effects remain quarantined until independently resolved. Only
then may Owner-wide boot reconciliation reopen a restart gate; pending
Off/rotation remains closed, and old Claim boot epochs remain invalid.
The R1 `trusted_*` booleans are disposable test abstractions, never Worker/IPC
authorization fields. No SQL, Owner/Broker/Writer runtime wiring, credential
mutation or Vault request is implemented by this note.


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
