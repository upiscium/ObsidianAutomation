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
2. Open that directory with `O_DIRECTORY|O_NOFOLLOW` where available.
3. Create a random `.obsidian-cas-<token>.tmp` inode in that directory using
   `O_CREAT|O_EXCL|O_NOFOLLOW` and the normal caller umask. The final canonical
   pathname does not yet exist.
4. Write the entire content and `fsync` the staged file.
5. On Linux, publish by `renameat2(..., RENAME_NOREPLACE)` in the *same
   directory*. A missing libc symbol or unsupported kernel/filesystem fails
   closed; never implement check-then-rename as a fallback.
6. If the canonical target already exists, verify only an `O_NOFOLLOW`,
   `O_NONBLOCK`, regular, single-link inode of the exact same length and
   bytes. Identical writers are idempotent. Conflicting or special-file
   targets fail closed and never overwrite.
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
- The supplied 0o644 staging mode is filtered by the process umask and
  directory ACL. On the production summarizer's `UMask=0027`, existing and
  newly created file mode is 0640; regression coverage checks this parity.
  Other services retain their own umask/group/default-ACL contract.

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
