# Atomic CAS publication — Linux v1 (#292)

This document is an **implementation contract and operator safety boundary**, not a
permission to deploy or change the production Vault.

## Scope and trust

The helper `artifact_lifecycle._store_immutable` persists bytes supplied by
its caller. The caller must already validate schema, canonical serialization,
content identity, stage authority, and any filename SHA. The helper does not
make untrusted model or GitHub data authoritative and does not sign artifacts.

The directory hierarchy must be trusted: its parent/ancestors must not be
replaceable by an untrusted writer. The helper validates and opens the final
parent directory without following its final symlink component, then uses
descriptor-relative operations inside that directory. It does **not** supply
a general-purpose, descriptor-pinned traversal of all ancestor directories.
No external actor is authorized to rename/rebind these ancestors during
publication.

## Publication state machine

1. Require the existing final parent directory to be nonsymlinked.
   On first use, `ensure_artifact_layout` durably syncs the containing
   directory immediately after creating each managed artifact subdirectory.
   A parent fsync failure is not accepted as a successful first-use layout.
2. Open that directory with `O_DIRECTORY|O_NOFOLLOW` where available.
3. Create a random **zero-byte** `.obsidian-mode-probe-<token>.tmp`
   inode with requested mode `0644` to observe the service umask and
   inherited default-ACL effective mask. No sensitive content is ever written
   to this probe. Close and unlink it before allocating data staging.
4. Create the actual `.obsidian-cas-<token>.tmp` data inode with mode
   **`0600` at `O_CREAT|O_EXCL|O_NOFOLLOW` time**, then enforce owner-only
   permissions before writing. Do **not** create it read-accessible and
   narrow access afterward: chmod cannot revoke another process's already
   open read handle. Write and `fsync` the complete data, restore the mode
   observed from the zero-byte probe (and the inherited named-user ACL mask),
   then `fsync` again before publication.
5. On Linux, publish by `renameat2(..., RENAME_NOREPLACE)` in the *same
   directory*. A missing libc symbol or unsupported kernel/filesystem fails
   closed; never implement check-then-rename as a fallback.
6. If the canonical target already exists, verify only an `O_NOFOLLOW`,
   `O_NONBLOCK`, regular, single-link inode of the exact same length and
   bytes. Identical writers are idempotent. Conflicting or special-file
   targets fail closed and never overwrite. An already-verified identical
   target is a fast path that skips redundant temporary allocation and data
   writes, while still `fsync`ing the parent directory. Missing targets
   always use atomic no-replace publication rather than check-then-rename.
7. Remove any uncommitted staging inode for an ordinary failed attempt and
   `fsync` the parent directory before reporting success.

`fsync` failure is not success. A crash before the no-replace rename leaves
no partially published final name, though a private orphan temporary inode
may remain. A crash after the rename but before parent-directory durability
may leave an already-complete final file **or** no final file after recovery;
the next identical request must either read the complete published value or
make a fresh publication. No guarantee is made for hardware/filesystems that
violate Linux sync/rename durability semantics.

## Reader and compatibility expectations

- Consumers must not accept an incomplete, symlinked, nonregular or
  hash-mismatched **published** artifact. A genuinely corrupt final artifact
  remains an error, not a silent cache miss. PR #291 binds complete
  Context/Output/Provenance identities before reuse.
- Pre-existing hardlinked final artifacts are rejected even if their bytes
  match. The observed production Obsidian state sample (66,950 files) had
  link count 1; this is not an exhaustive guarantee for all hosts or future
  files. Escalate a link-count compatibility failure instead of relaxing it
  automatically.
- Linux `renameat2(RENAME_NOREPLACE)` is required. macOS, Windows, or
  unsupported network/shared filesystems are not supported by this path.
- The empty permission-probe inode requests `0644`, while the data
  inode requests `0600` from creation onward. The inherited ACL mask of
  the private data inode is expanded only after its bytes are completely
  synced. On production `UMask=0027`, both existing and newly published
  final CAS objects use `0640`; the default named-Renderer ACL is restored
  with effective read access only for the complete payload. Other services
  retain their own umask/group/default-ACL contract.
- A random temporary filename alone is **not** a confidentiality
  boundary. The temporary file's owner-only mode and inherited ACL mask
  prevent group/Renderer reads of partially written payloads. Once fully
  synced and permissions restored, authorized readers can still see the
  complete *unpublished* temporary bytes before rename. A same-UID actor
  may read any staged content; directory ownership, ACL, process isolation
  and umask therefore remain mandatory security prerequisites. Orphan
  temporary file contents must never be treated as safe to disclose.

## Existing-object permission admission and ACL policy

A matching CAS hit **attests bytes and inode identity only**. The existing
fast-path reader checks regular type, single-link count, size and exact bytes;
it does **not** measure the existing object's mode or effective named-user ACL.
It never retroactively `chmod`s an existing final inode. Therefore returning a
byte-identical hit must not be interpreted as proving that an earlier writer
created it under today's service umask or ACL. Before authorizing production
resume, the deployment gate must inventory the exact canonical state roots,
final-file modes and effective ACLs; unexpectedly permissive existing files are
an **operational BLOCK**, requiring separately approved reconciliation rather
than silent acceptance. The observed production CAS `0640` sample is evidence
for the currently inspected host, not a universal filesystem guarantee.

The zero-byte mode probe and owner-only data staging assume a stable,
**trusted root-owned parent default-ACL policy throughout publication**.
An administrator with authority to change that default ACL between probe
and data creation can also change the DAC/ACL of published objects, and is
outside the untrusted-writer threat model. Do not claim protection against
concurrent privileged ACL-policy mutation, and do not authorize such a
migration while CAS producers are running. If simultaneous policy changes
become a supported use case, introduce a separate, reviewed ACL-aware
publication protocol rather than treating the mode snapshot as a live ACL
attestation.

## Orphan staging maintenance

The `.obsidian-cas-*.tmp` pattern is an **uncommitted temporary namespace**,
not a canonical CAS artifact. SIGKILL, machine loss or power loss can leave
such files behind. There is **no automatic, age-only deletion**: another
writer might still be using a temp inode and age is not proof of ownership.

Operational procedure: collect a read-only inventory (path, owner, mode,
size, mtime and running producer state) under each approved state root.
Follow separately authorized maintenance/recovery after producers are
stopped and inode ownership is established. Never recursively delete hidden
files, clear the CAS directory, or treat orphan temp counts as permission
to publish/repair a malformed canonical object. Bound and monitor the
number/bytes of abandoned temp files; escalate if they grow repeatedly.

## Acceptance and deployment

- Adversarial crash points: short write, interrupted stage before/after
  file fsync, before and after rename, parent fsync failure; direct FIFOs,
  symlinks, hardlinks, same-key races and conflicting writes.
- Full Python 3.11, 3.12 and 3.13 regression and authority-fixture CI.
- A separate security/correctness/compatibility review of the *shared*
  CAS helper and the #291 consumer, including any directory race assumptions.
- Independent authorized publication, rollout/rollback plan, and protected
  live-environment smoke. **Passing CI alone is not deployment approval.**

Production connectivity is via Adam's direct RDC shell, using existing
`SSH_AUTH_SOCK=/run/user/1000/ssh-agent` and strict host-key checking. All
production changes use the existing reviewed, exact-SHA managed updater,
not direct `git pull`, ad hoc live rsync, Nextcloud writer access, or timer
restarts.
