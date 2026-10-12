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
   `ensure_artifact_layout` retries parent-directory fsync even when a
   managed directory already exists: a failed first-use fsync can leave an
   entry visible without establishing crash durability. New directory creation
   **always** requires a successful parent fsync. For an already-existing
   administrator-provisioned directory whose parent refuses read access, a
   caller with **no write permission to that parent** may rely on the trusted
   provisioning boundary; a caller who can write the parent must fail closed.
   This narrow existing-layout exception preserves intentional service-user
   isolation and is not proof against privileged actors changing directory
   permissions or namespace durability between attempts.
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

The zero-byte mode probe and owner-only data staging require a stable
**trusted parent-directory ACL policy throughout publication**. The concrete
production Summary layout has root-controlled ancestors, but the final
`20-Summary/` parent and `github-daily-summary/` namespace are owned by
`obsidian-github-summarizer`, **not root**. The service UID and privileged
operators are trusted to preserve that namespace/ACL policy; no other
untrusted identity may change its default ACL or rename its ancestors. A
compromised peer with the same service UID or a privileged administrator
could change the policy between the mode probe and publication, and remains
outside this helper's adversarial concurrency model.

On 2026-10-11, a **read-only** inspection of production
`/var/lib/obsidian-github-pipeline/daily-progress/20-Summary` observed
service-owned `0770` parent, service-owned `0750` final Summary directory,
a named Renderer reader ACL, and the Summary service `UMask=0027`. The 326
sampled JSON CAS files under that Summary subtree were all mode `0640`,
with no sampled group-writable, world-readable, or multi-link files. This
is bounded **current-state evidence** for the relevant service, not proof
of every CAS root, immutable future ACLs, or absence of an authorized
racing same-UID/root process.

Do not claim protection against concurrent trusted-principal ACL-policy
mutation, and do not authorize an ACL migration while CAS producers are
running. If simultaneous ACL changes become a supported use case, introduce
a separate, reviewed ACL-aware publication protocol rather than treating
the mode snapshot as a live ACL attestation.

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
- GitHub Actions regression on Python 3.11/3.13 and the authority fixture,
  plus an independently recorded isolated Python 3.12 full-suite run at the
  exact reviewed head. The current workflow does not run Python 3.12 in CI.
- A separate security/correctness/compatibility review of the *shared*
  CAS helper and the #291 consumer, including any directory race assumptions.
- Independent authorized publication, rollout/rollback plan, and protected
  live-environment smoke. **Passing CI alone is not deployment approval.**

Production connectivity is via Adam's direct RDC shell, using existing
`SSH_AUTH_SOCK=/run/user/1000/ssh-agent` and strict host-key checking. All
production changes use the existing reviewed, exact-SHA managed updater,
not direct `git pull`, ad hoc live rsync, Nextcloud writer access, or timer
restarts.

## Read-only existing Daily CAS metadata admission (#292)

A byte-identical CAS hit in the generic `_store_immutable` helper does not
guarantee approved mode or effective named-user ACL. The Draft therefore
includes a separate, **non-mutating** `github_daily_cas_admission` diagnostic
for the specifically reviewed Daily Summary CAS namespace. It can be invoked
*after* the exact package version has been independently authorized and
installed, on a quiescent system, for example:

```sh
/opt/obsidian-automation/venv/bin/python -I -B -m \
  obsidian_automation.github_daily_cas_admission \
  --root /var/lib/obsidian-github-pipeline/daily-progress/20-Summary/github-daily-summary \
  --owner-name obsidian-github-summarizer \
  --reader-name obsidian-github-renderer
```

The command accepts **read-only** absolute-path/role parameters, discovers
numeric UIDs through system name service, and checks at most 65,536 files.
It restricts nested directories and canonical SHA-based file suffixes to the
reviewed Daily Context/Output/Provenance/Final/Resume namespaces. Each regular,
single-link JSON inode must belong to the dedicated Summarizer UID/GID, have
exact mode `0640`, a valid Linux POSIX access ACL with **exactly** the two
approved named users, and effective read-only access for both. The CAS root
and each stage directory must have exact `0750` owner/mode plus an approved
**access ACL (mask r-x) and inherited default ACL (mask rwx)**. Directory
ACLs likewise must contain only the named Summarizer and Renderer users, no
unreviewed named-group or other principals, and exactly the currently
approved access and inheritance rights. It refuses unexpected ACL identities
on both directories and files, world/group write permissions, malformed
ACLs, symlinks, special files, ambiguous file classes, orphan `.tmp` entries,
and an empty namespace. The scanner opens root, stage directories and
published inodes with Linux `O_NOFOLLOW` and traverses **directory file
descriptors**, not path strings that could be retargeted between one scan and
the next. It uses `fstat` / file-descriptor `getxattr`,
**never reads source, prompts, model responses, or JSON bodies**, and prints
only bounded counts/status.

This specific policy follows the 2026-10-11 read-only production observation:
Summary service `UMask=0027`, a service-owned Summary namespace, matching
access/default named-user ACLs on the root and each existing stage directory,
a named Renderer reader ACL, and 325 canonical JSON files under
`20-Summary/github-daily-summary` all with the identical expected 0640
mode/ACL signature. Another related Summary state scope was counted
separately; do not conflate the file counts.

The diagnostic is **not automatically executed** by the current Stage CLI,
does not authorize Stage/Activate, does not attest ACL stability after the
scan, does not validate signed provenance, and intentionally does not change
the global helper's behavior for other service identities. The operator must
separately verify trusted parent-namespace control and quiescence, record the
exact audit receipt at an approved time, and explicitly approve any remediation
of over-permissive existing objects or orphan files. Never silently chmod,
delete, or rewrite a published CAS item.
