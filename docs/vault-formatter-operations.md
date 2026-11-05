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
