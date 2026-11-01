# Single-file Formatter CAS Canary Binding — source-only R2+ (#471)

This module defines a future, explicitly approved, **single-file** canary
binding for the guarded CAS Writer. It creates no scheduler, HTTP route,
CLI, credential or entitlement. The normal `snapshot_app` continues to
construct `FormatterJob` with `writer=None`; production Writer remains
disabled and Formatter stays at 24-hour Dry-run.

## Existing source and scope

- Reuse `SetupStore.load_nextcloud` and
  `FormatterSource.generation`. The shared store already verifies its
  private connection registry and rclone-config hash. No second
  credential store or new user account is required.
- Admit only the canonical HTTPS account root
  `/remote.php/dav/files/<encoded-account>/` and the exact
  `ObsidianVault` folder. Reject `..`, `.`, encoded slash/backslash,
  percent-ambiguous user names, repeated slashes, path aliases,
  foreign account, non-HTTPS, noncanonical raw query/fragment delimiters
  (including trailing empty `?` / `#`) and unrelated Vault roots.
- Select exactly one preapproved `98-System/**.md` or `.markdown`
  target. Existing Writer path admission excludes hidden, traversal,
  symlink/unsafe and nontext paths. The grant pins exact plan SHA,
  original/desired SHA, source-generation SHA, account/endpoint binding
  SHA, separate provider-proof SHA, one operation ID and a private
  receipt directory.
- The **proof digest is not cryptographic authorization** by itself.
  A separate operator-controlled, independent proof verifier must
  accept the exact `(provider_proof_sha256, source_generation,
  account_binding_sha256)` tuple. No verifier is installed by default.
  Test callbacks that return True are synthetic fixtures, **not** an
  acceptable production implementation.

## Source-only one-operation boundary

1. Validate the grant's exact path, hash, operation ID and receipt root.
2. Before reading credentials, detach the incoming mutable Plan as a
   JSON-only snapshot; reject malformed snapshots. Verify that snapshot's
   exact v3 digest, approved `would_change` path, and original/desired SHA
   values. Pass only the frozen snapshot to the guarded Writer. Mutating
   the caller's Plan later cannot substitute different write contents.
3. **Acquire the same in-process SetupStore RLock** used for credential
   updates. Refuse an unavailable lock. Keep this lock across the
   entire Writer invocation, including the conditional PUT and GET
   readback, so ordinary in-process connection rotations serialize.
4. Compare `FormatterSource.generation()` and the freshly validated
   connection against the grant. Build the canonical Vault root and
   confirm the account/endpoint binding SHA.
5. Require explicit external proof verification. After it returns,
   **recheck connection generation and binding**, and check them
   again immediately before *each* WebDAV GET/PUT/readback request.
   A discovered generation drift denies subsequent requests.
6. Delegate the exact selected file to existing `apply_plan` with a
   fixed operation ID. The Writer owns its private no-follow directory,
   immutable prior receipts, content-hash and formatter recomputation,
   strong ETag, `If-Match` conditional PUT, GET readback, partial/conflict
   outcomes and replay marker.

No HTTP client, scheduler or app service creates a grant or automatically
installs this adapter. A callable created by trusted Python code is not
a human approval or an authorized production service path.

## Remaining external concurrency and authority limit

**The SetupStore RLock is in-process, not a cross-process OS lock.**
Another credential-writing process could change private settings during
an in-flight network request, after the last generation check. Checks
before the next request detect drift, but no client-side check can revoke
an already transmitted PUT. The independently approved, future
single-file canary must run under an operator-attested exclusive
credential-change window (quiesce the normal dashboard/other writers)
or gain a separately reviewed cross-process lock integrated into every
credential updater. Without that boundary, **do not invoke** the
Adapter on a real provider.

The human must independently validate the real Nextcloud proof and
account binding, and use a fixed credential generation and private
one-shot operation record. This code has **not** done those actions.

## Tests and next gates

Synthetic tests cover correct single-target application when a
controlled in-memory WebDAV server is supplied; two candidates with one
approved path; exact plan/hash binding; missing/rejected proof verifier;
rotation during verifier; changed generation during preflight; shared
in-process lock synchronization; canonical URL rejection; weak/stale
ETags; replay; private receipts and normal Writer-off defaults.
Tests do not use real Nextcloud or canonical Vault bytes.

The first live v3 scheduled Dry-run produced zero `would_change` files.
A real single-file canary therefore needs separate owner approval of a
**disposable, exact canonical `98-System/**` path**, pre-backup,
revision hashes, receipt root and recovery method. Do not invent a
candidate by modifying an existing note without that Gate. A successful
one-file canary does not authorize unattended auto-apply (#472);
#469/#471 remain open until their separate acceptance criteria are met.
