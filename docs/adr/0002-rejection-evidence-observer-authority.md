# ADR-0002 — Rejected-Validation evidence observer and effect authority

- **Status:** Proposed — no identity, ACL, service or production effect activated
- **Issue:** [#321](https://github.com/upiscium/ObsidianAutomation/issues/321)
- **Related:** #309 Phase A [Draft #320](https://github.com/upiscium/ObsidianAutomation/pull/320), #298/#300/#304, #319, [ADR-0001](0001-single-host-management-dashboard.md)
- **Scope:** one AI state root and its immutable rejection proof; Standard and Strict transport modes retain separate remote authority

## Decision to evaluate

Use a dedicated long-lived **`obsidian-ai-rejection-observer`** system UID for
one-shot, read-only evidence collection. It must not be Reader, Generator,
Validator, Sync, root, or the management web UI; it receives **no Nextcloud,
WebDAV, Gitea or model-provider credentials**, and no write access to the
canonical Vault. Its only potential persistent write is a separately owned,
versioned, private proof directory (proposed:
`/var/lib/obsidian-ai/state/18-Rejection-Preflight/observer`).

The observer **does not delete anything**. A subsequent Sync-owned,
separately versioned intent/result process would independently revalidate
the original proof, generation and remote bytes immediately at the effect
boundary, with strong ETag and conditional `If-Match` DELETE. Evidence and
execution are distinct authorities. Existing Human Review terminal cleanup
continues to use its different, six-stage Human decision/Receipt contract.
Ordinary deterministic rejection requires no Human Acknowledge.

This ADR is **not an approval for any DELETE, role provisioner edit, new
daemon, secret distribution, service activation or production cutover**.

## Why a separate identity is required

`validation_rejection_cleanup.prepare_rejection_cleanup` independently
reads a **current** `deterministic_reject` scheduler generation, final
Validation attempt, bound Context/Selection/Generation/Validation artifacts,
original publication requests, and Sync publication results. A previous
rejected case, a filename in `04-AI`, or a serialized proof alone does not
authorize a destructive operation.

The current `tools/provision_automation_authority.py` intentionally grants
different private evidence to Reader, Generator, Validator and Sync. Widening
any of those existing user identities to include all cross-role evidence would
extend its existing writer authority. The new read-only observer is a
separation-of-duty boundary, not an excuse to consolidate role accounts or
grant a broader Nextcloud account.

### Proposed Linux access matrix (not applied)

`--x` means traverse only; `r-x` means directory listing and existing and
inherited future-file read; `r--` applies to a **single existing regular file**.
Named ACLs, file inheritance and systemd namespace restrictions must all be
verified with the exact real UIDs, not just an owner-mode or root smoke test.

| Source in `/var/lib/obsidian-ai/state` | Observer access | Reason |
|---|---|---|
| `state` parent | `--x` | No unrestricted state listing |
| `00-Untrusted` | `r-x` | Original proposal and Generation artifact |
| `02-Orchestration` | `--x`, exact SQLite file `r--` | Latest generation, stage output and attempt evidence |
| `02-Orchestration/input-selections` | `r-x` | Legacy submitted Selection |
| `02-Orchestration/semantic-selections` | `r-x` | Semantic Deep submitted Selection |
| `05-Context` | `r-x` | Original Context and its source bindings |
| `10-Validation` | `r-x` | Original rejected Validation record |
| `16-Human-Projection` parent | `--x` | Traverse without listing |
| `16-Human-Projection/{reader,generator,validator,evaluator,reviewer,executor,sync}` | `r-x` each | Four required originals **and detection of forbidden later/duplicate requests** |
| `17-Human-Projection-Result` | `r-x` | Exact successful Sync publication receipts |
| `24-Locks` parent | `--x` | No lock-file creation or replacement |
| `24-Locks/canonical-io.lock` | `r--` existing inode | Cooperate with writers using exclusive advisory `flock` without `O_RDWR` |
| New `18-Rejection-Preflight/observer` | `rwx` owner only | Immutable, append-only logical proof records; no delete intent |
| New proof directory (Sync role) | `r-x` only | Sync verifies witness independently; no proof modification |
| `vault`, `/etc/obsidian-ai`, `20-Review`, `25-Execution`, `27-Transport`, `30-Receipts`, unrelated Index/Corpus and remote endpoints | **no read/write** | Deliberately excluded effects and private contents |

**Important:** The existing Phase A scanner enumerates **all seven**
`ROLE_NAMES` projection-request directories to detect a conflicting later
Review/Completed request; giving the observer only Reader/Generator/Validator
directories would make the negative-evidence check unsound. This necessarily
exposes other private projection request metadata at POSIX **directory**
granularity, even though the application opens only bounded named originals.
If that cross-role read blast radius is unacceptable, a future authoritative,
producer-sealed projection index/negative witness must replace the broad scan;
a client-side filename filter is not an ACL. Do not falsely claim exact
per-case OS enforcement.

**SQLite concurrency qualification:** Production `pre_review_job._connect_rw`
currently uses `PRAGMA journal_mode=DELETE`, while `_connect_ro` uses
`sqlite3.connect(file:...?mode=ro)`. The disposable Linux namespace fixture
now verifies **distinct mapped Scheduler/Observer UIDs**, observer file
read-only ACL, and fresh committed observations while Scheduler keeps the DB
open in both actual **DELETE** journal mode and optional **WAL** mode with
existing readable `-wal`/`-shm`. The Observer does not write either DB or
journal files. This is evidence for the scoped Linux fixture—not proof that
the eventual *actual production systemd UIDs*, directory ACL inheritance,
Linux mount namespaces or all upgrade/rollback states are qualified. Do
**not** grant observer directory write permissions to make SQLite work;
if production read-only SQLite fails, use a writer-sealed bounded snapshot
and independently reconcile it. Never set `immutable=1` on a live DB.

## Locking without granting production write authority

Existing `production_io.canonical_io_lock` opens
`24-Locks/canonical-io.lock` with `O_RDWR | O_CREAT`, so that helper is
**not** suitable for a read-only observer. This child proposes a new
`canonical_io_lock_observer` helper that:

1. Requires the trusted, **pre-existing** lock directory/file; never creates a missing lock.
2. Opens the same file `O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC`.
3. Checks an opened regular file with exactly one hardlink, then checks the
   `dev+ino` against the live path **before and after** exclusive `flock`.
4. Fails closed on missing, symlink, hardlink, FIFO or renamed/replaced lock.
5. Leaves the lock bytes and directory unchanged and releases the advisory
   lock even on observer exceptions.

A bootstrap provisioner would have to precreate the lock and grant a narrowly
scoped read ACL on the **file**, not directory write access. Linux
`flock(LOCK_EX)` on an `O_RDONLY` descriptor is tested with a disposable
regular file. This only coordinates **cooperating local processes**. It does
not prove remote ETag consistency, prevent a publisher racing *after*
lock release, or substitute for Phase B durable anti-resurrection guards.

### Proof output and immutable storage proposal

The observer writes only under its dedicated `18-Rejection-Preflight/observer`
directory, with owner-only initial `0700` directory / `0600` file modes,
`UMask=0077`, no default broad group ACL, and a separately applied narrow
Sync named-user read-only ACL. **Do not claim the numeric modes remain exactly
`0700`/`0600` after applying a named ACL:** POSIX ACL masks can change the
effective group mode bits, and running `chmod 0600` *after* granting Sync
read access may mask that ACL away. Test effective `getfacl` permissions with
the real Observer and Sync UIDs and prove all other roles remain denied.

The proof must be:

- separately versioned and keyed to `job_id/generation_id`, selected
  Planner Selection/Context/Generation, rejected Validation SHA and **exact
  four** `04-AI` request/result/content hashes;
- constructed from trusted records at the time of the check, never from a
  browser path, Human decision, previous proof or a later Selection guess;
- atomically published by write-to-exclusive temporary regular file,
  `fsync`, link-or-rename-no-replace, parent `fsync`, with bounded
  recovery of incomplete temp files and immutable exact-content replay;
- explicitly labeled `preflight` rather than `delete-approved`.

The existing Phase A optional helper stores in Validator-owned
`16-Human-Projection/validator`; **do not enable that legacy helper as the
new Observer service**. Relocating and changing the contract needs its own
tested, reviewable patch. The current Reader-submitted Selection binding is
Reader-owned application evidence; Reader's directory-write permission
means POSIX ACL alone does **not** protect against a compromised Reader UID.
The initial threat model trusts role-specific producer code, not a hostile
Reader UID; a stronger adversarial threat model requires an independent,
sealed assignment receipt at submission. No automatic backfill of historical
Legacy Selection identity.

### Effect side (deferred Phase B)

Only a Sync-role, narrowly scoped, auditable executable may attempt remote
conditional DELETE. It must revalidate the current generation, original
private request/result and exact remote bytes/strong ETag immediately before
each target; perform only the fixed four-path `If-Match` DELETE; fail closed
on changed bytes, unavailable/weak ETag, Human edit, newer generation and
uncertain remote status. Validation projection is deleted **last**. A
versioned durable result, idempotent missing-file handling, server-side
non-resurrection guard in every publishing route, loss-of-response recovery,
and management Dashboard incidents on cleanup conflict are mandatory before
any effect-enabled release. Sync must not blindly trust the observer's proof.

## Disposable test and deployment qualification

- Build a hermetic temporary state tree and verify same-file lock exclusion,
  noncreation, FIFO/symlink/hardlink/replacement fail-closed and no file edits.
  These are provided as **local functional** tests in the child branch.
- Completed in disposable Linux **user and PID namespaces**: **actual mapped
  distinct Observer (1001), Sync (1002), Scheduler/Producer (1003), and
  unauthorized (1004) UIDs** with effective named POSIX ACLs. The fixture
  requires that its Python child runs as PID 1 of the disposable PID namespace;
  `unshare --fork --kill-child` and the process-group timeout/exception cleanup
  ensure a terminated namespace init cannot leave a separate-UID SQLite
  writer behind. This test has no mount or network namespace, and is not a
  substitute for systemd sandbox or real host-UID tests. Confirms read-only
  original sources in all seven request-role directories, denial of writes to sources,
  Sync read-only proof sharing, unrelated Vault/secret denial, immutable
  existing lock participation, and live Scheduler SQLite committed updates
  observed in read-only DELETE and WAL modes. Uses disposable `/tmp` files,
  never production UID/ACL/DB/service or remote access; tests skip with a
  visible reason if the CI runner cannot create subordinate user/PID namespaces.
- A producer-created **new** file with a directory default ACL and creation
  mode `0644` is readable by the Observer. **Subsequent `chmod 0600` masks
  this named ACL and revokes Observer access** until the producer/owner
  explicitly reinstates the named read permission. Provisioning must verify
  each producer's actual immutable-file creation mode, final chmod, default
  ACL inheritance and effective masks. A one-time recursive ACL on old files
  does not qualify future publication; do not indiscriminately chmod existing
  immutable evidence or broaden its group permissions.
- Remaining production gates: tests with the intended real **host systemd
  UIDs** and their namespace hardening, precise Standard/Strict account ACLs,
  lock inode persistence through restart/upgrade, SQLite rollback/recovery,
  sealed proof atomic publish and hostile Writer/Reader simulation, no forged
  cross-role receipts or remote effects.
- Re-check the exact Phase A `#320` HEAD and its Planner selection binding.
  Current in-flight uncommitted #320 corrections and the older #287 worktree
  must be preserved; none of them are silently ported by this ADR.
- Separate Security and Correctness reviews of the final provisioner, proof
  publication and Sync executor, plus CI, approved ADR, exact Human acceptance
  and independent live-effect stage gate. No extension to existing role
  permissions or production installation may follow merely from a Green test.

## Current decision status

This branch establishes the proposed authority map and a *nonwired* read-only
local lock primitive, **not** a deployed or operational observer. The observer
UID/ACL, sealed proof writer, remote Sync executor, Dashboard cleanup alerts,
service units and live migration remain unimplemented. Phase A #320 and Phase B
must not infer authority from this Proposed document or local lock test.
