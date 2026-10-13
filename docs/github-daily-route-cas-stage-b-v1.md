# Daily v2: Collector-owned source CAS — Stage B prototype (#336)

**Status: nonwired, isolated, not production admitted**. No CLI, timer, GitHub
network fetch, Nextcloud, Vault, model or Writer integration is added here.
The branch includes the #335/339 source-route code and explicitly integrates
the crash-consistent atomic CAS code already merged to `main` via #293.

## Local trust contract

This design is **not a cryptographic GitHub provenance signature**.
`index.parse_capture_index` proves that the two source byte blobs and the
manifest agree. A malicious writer able to manufacture all three blobs could
forge internally consistent SHA values. The additional storage gate is an
operating-system role boundary: the privileged administrator provisions
nonworld-writable ancestors, a dedicated unprivileged
`obsidian-github-collector` UID/GID and distinct reader
`obsidian-github-summarizer` UID. Only that Collector may create CAS objects,
and the reader only opens, validates and reads objects. The model and Writer
have no write authority to this namespace.

The exact, code-pinned production layout is:

```text
/var/lib/obsidian-github-pipeline/daily-progress/15-VerifiedSource/
  evidence/<sha256>.github-daily-evidence.json
  receipt/<sha256>.github-daily-source-route.json
  capture/<sha256>.github-daily-capture-index.json
```

The layout **must already exist**; no service creates or repairs the
directory/ACL structure. Each of the four CAS directories is owned by the
Collector UID/GID and has mode `0750`, no other named ACL principals, a
specific named summarizer reader `r-x`, and a default ACL restricting inherited
access. All ancestors must be trusted root-owned or Collector-owned and
not group/other-writable; the trusted Collector UID, root operator and stable
filesystem/ACL policy remain explicit assumptions. The reader checks
effective ACLs on each file and refuses anything other than one regular,
single-link, Collector-owned `0640` inode with its named-reader read right.
Absent/altered named ACLs, missing OS service identities, a wrong eUID,
symlink, hardlink, FIFO, unexpected file mode, wrong SHA or invalid payload
all fail closed.

A real production host audit must inspect each ancestor, namespace owner/GID,
access/default POSIX ACL and the effective rights of all deployed roles
before enabling the code. The tests patch only the fixed root and trusted
anchor in an isolated fixture, use synthetic role identity, and exercise real
Linux POSIX named ACL xattrs. They do **not** prove live role isolation.

## Crash-consistent write protocol

1. Require a completed `FreshRouteCollection` from the authenticated
   source verification path, and reconstruct its versioned `CaptureIndex`
   from exact source bytes. Revalidate the full route closure and recorded
   capture time, mode, SHA, date, count and byte budgets before I/O.
   This is an operational assertion about a trusted Collector process, not
   cryptographic proof that Python objects came from one invocation.
2. Check the actual Collector eUID/egid and open all fixed CAS ancestor and
   stage directory descriptors with `O_DIRECTORY|O_NOFOLLOW`, checking owner,
   mode and effective ACL. A compromised same-UID writer or privileged root
   remains outside this local attestation model.
3. Call the already merged `artifact_lifecycle._store_immutable` for the
   Evidence bytes and Route Receipt bytes, each under its own SHA-derived
   fixed filename. That helper writes an owner-private staging inode, fsyncs,
   and publishes via Linux `renameat2(RENAME_NOREPLACE)`. No direct-final
   `O_EXCL` or check/rename fallback may be used; the prototype checks the
   atomic helper is present and fails closed otherwise.
4. Publish the canonical, SHA-derived Capture Index **last**, using that same
   atomic primitive. Only a completed index is an admission candidate.
   `fsync` failure is not reported as publication success. A crash before
   commit may leave complete but **uncommitted** Evidence/Receipt objects;
   they cannot be selected by the reader without a committed index.
5. Read back every published object via its pinned directory descriptor and
   check type, ACL, content SHA and byte identity. A duplicate exact-byte
   retry is idempotent. Hash/name collisions with differing bytes reject.

**First-publication freshness:** Before writing any blob, the Collector checks
whether the exact Capture Index SHA already exists. If it does, it reopens
and verifies the entire index/receipt/Evidence triplet; only an exact-byte
match may be retried at any later time. On that verified-replay path,
the Collector **fsyncs the pinned Capture Index parent directory again before
returning success**: a prior rename may have succeeded while its parent
directory fsync failed, leaving a visible but not yet durable manifest.
A failed replay fsync is also a hard error; correct bytes alone do not prove
durability. If the index does not exist, the captured UTC time recorded by the
source receipt must be no more than **one hour** old and
at most **60 seconds in the future**, relative to the host's current UTC
clock. A scheduled recent capture must still target the host's current or
previous JST date at *publication* time. An older first publication, including
a partial Evidence/Receipt pair left by an earlier crash, is blocked rather
than silently promoted. The same age and JST-date checks are repeated **after**
the two source blob writes, immediately before invoking the final atomic CAS
Capture Index writer; a slow source-object write cannot silently turn a
once-fresh capture into a stale first commit. Explicit historical reconfirmation
may target an older day, but its own re-verification must still be fresh.
This bounded wall-clock gate cannot authenticate the source process or prevent
a compromised same-UID Collector from forging a timestamp. There remains a
small but unbounded OS scheduling/fsync interval *inside* the existing #293
final-index writer after this last check and before its atomic rename;
therefore do not claim an indivisible clock-and-filesystem transaction.
Any downstream publication decision must independently check its own date
and eligibility. A legitimately longer capture must be recollected rather
than silently extending these limits based on a stale receipt.

A crash *during* index publication may make a complete index visible but
not durable until a successful directory `fsync`. On host recovery a full
manifest may be present or absent; if present and its referenced objects pass
the exact reader check, it is complete. No claim of atomic multi-file POSIX
rename is made. Reader admission is an all-or-nothing **logical** transaction,
not one filesystem primitive that atomically renames three files.

## Read contract

`read_verified_capture(capture_sha256)` accepts only one lowercase SHA-256
digest. It constructs fixed paths from validated SHA values; it does not
resolve a model-provided path, GitHub URL or arbitrary JSON reference.
It reads the capture index first from its code-pinned CAS location, checks
regular inode, ownership, ACL, size <=4KiB and its own SHA, then uses the two
validated digest fields to retrieve Evidence <=32MiB and Route Receipt <=4MiB
with their own inode/ACL/SHA checks. It reruns
`parse_capture_index(index,evidence,receipt)` and the entire
`parse_verified_routes` verification.

The typed result has `collector_storage_attested=true` **only** when the
on-host identity/ACL/inode checks pass. It still has
`daily_publication_authorized=false`: neither verified local storage nor
the SHA of a payload alone authenticates GitHub against a compromised
Collector UID, calibrates progress semantics or permits the Daily Writer.
This ordinary Python dataclass and its Boolean flag are **not** an unforgeable
capability: later consumers must perform their own trusted CAS lookup and
readback, not accept an object or attestation flag supplied by a model or
untrusted caller.

There is no “scan latest file” operation; an already trusted scheduler must
supply the exact index digest it previously accepted. No v1 historical
Evidence is rewritten/reclassified, and `explicit_historical_reconfirmation`
remains distinct from a scheduled recent capture. Missing, invalid, incomplete,
ambiguous or unavailable source remains BLOCKED/PENDING — never an empty Day.

## Nonproduction coverage and remaining release gates

Synthetic offline test coverage includes complete transaction, same-key retry,
early failure between objects, manifest-last ordering, forged index/source,
legacy unsafe CAS helper rejection, wrong process identity, invalid digest,
symlink/hardlink/FIFO, content mutation, missing receipt, unexpected ACL/mode
and trusted layout failure. Tests exercise actual file xattrs where the
filesystem supports POSIX ACLs. They never access GitHub, production Vault,
Nextcloud or a live service account.

Required before accepting #336 as production-capable:
- Independently review this exact Stage B diff and merged #293 integration;
  run GitHub CI and isolated complete suite, including cross-UID/ACL tests
  on an administrator-provisioned scratch host (not production state).
- Verify actual filesystem owner/GID/ACL/ancestor stability, service process
  credentials and no unintended group capabilities. Preserve the existing
  known root/same-UID adversary limitations.
- Explicitly connect trusted v2 route receipts to #325/#327 source-entity
  Context readers. No direct model/writer authority transfer. Pass through
  unresolved source state without discarding evidence.
- Separately resolve #337 bounded 409 empty-repository compatibility, and
  conduct real-date private quality/materiality review #290/#283 with
  versioned v2 Summary/Projection/CAS and explicit human Writer/Timer
  rollout consent.

**Do not merge to main, deploy, change systemd/Writer/Timer, mutate original
v1 immutable refs or trust this test-only Stage B path as a production attestation.**
