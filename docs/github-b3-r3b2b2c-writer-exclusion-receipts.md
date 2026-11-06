# #483 R3B2B2C — New-host cooperative Writer lock and legacy receipt cross-check

**Status: local source-only, unattached, unprovisioned and unapproved for live
operation.** R3B2B2C isolates two engineering problems: making new-host
canonical Writer processes cooperate on ONE pinned kernel lock, and checking
that future independent provider/old-host observations refer to the same
revocation/fencing generation. It does not solve either external trust
boundary by inference.

## A. Canonical Writer guard, new host only

`github_b3_canonical_writer_guard.py` exports
`B3CanonicalWriterGuard()`, a callable context manager compatible with the
existing B3 `WriterGuard` interface. **No WebUI/server/Timer/Writer is wired to
this new module**, and importing it opens nothing.

A future privileged installer (NOT this PR) must provision:

- Exactly one stable root-owned directory:
  `/var/lib/obsidian-b3-canonical-guard`, group
  `obsidian-canonical-writers`, mode `0750`. Every ancestor is owned by
  root, has no group/world write permissions, and contains no symlinks.
- Exactly one **pre-existing** empty, regular file
  `canonical-writer.lock` in that directory, root-owned, group
  `obsidian-canonical-writers`, mode `0640`, hardlink count 1.
- A non-root, explicitly provisioned supplemental group membership in
  `obsidian-canonical-writers` for **every** process that can change the
  canonical Vault: Project Status, Overview, Daily Writer, local/remote
  Mirror/Sync and Core Promotion as applicable. This group is NOT created
  or populated by this work; a missing role/membership must fail closed.
- An operational invariant: the privileged owner MUST never rotate the
  directory, lock inode or group during effects. The guard rechecks
  directory/file identity before and after the critical section, but cannot
  atomically prevent a privileged installer from replacing the tree between
  checks. A changed path makes the effect **unknown**, never a successful
  serialized effect.

The guard never creates a lock file or directory and never invokes a
provider, Writer or Journal. It uses `openat(O_NOFOLLOW|O_NONBLOCK)`,
bounded exact inode/group/mode checks, same-process nonblocking serialization,
and a nonblocking **Linux `flock(LOCK_EX)`** on the shared pre-existing
inode. It retains the FD through the caller's entire context and releases
on error. Same-process contention and separate-process contention are
covered by disposable synthetic tests. It is **advisory only**: any Writer
that does not adopt the same inode can still write, and a lock on this LXC
cannot fence a Writer on the legacy LXC.

The existing `B3TrustedJobPort` acquires an injected guard before
revalidating cutover and Producer/config sources, before `journal.start()`;
the existing B3 Job keeps it for both CAS effects and remote GET readback.
This patch **does not inject the new guard** or claim that other canonical
Writers already participate. The operator must separately review and modify
all roles before granting real shared-Writer authority.

## B. Independent receipt correlation, diagnostic only

`github_b3_retirement_receipts.py` accepts two typed objects: a *claimed*
Nextcloud Provider revocation/denial receipt and a *claimed* old-host
post-restart fencing receipt. It verifies:

- Exact registration binding, revision and generation on both records.
- The same fencing-generation identifier, matching sorted nonduplicate
  legacy principals, well-formed nonsecret Provider audit token IDs, and
  a fixed provider/host identity.
- Fresh, strictly ordered timestamps:
  `fence < old-host restart <= host observation <= post-restart Provider
  write-denial probe <= Provider observation <= now`.
- Complete reported principal/token inventory; exact match between reported
  revoked and known IDs, absence of alternate old write authority, and an
  affirmative reported post-restart denial.
- Reported old-host Writer units inert and egress denied after the restart.

Even a perfectly consistent bundle returns
`candidate / independent_source_authentication_missing` with
**`cutover_authorized=false`**. Missing/mismatched/unsafe/stale data returns
`unknown` or `blocked`, also **false**. This module merely correlates
records; no origin signature, secure transport or trusted source key is
verified here. A caller can forge both records and the positive claims.
Administrative token IDs MUST NOT be raw passwords or unsalted bearer-token
hashes. No real host, credential, Nextcloud or WebDAV request is made.

A *future* privileged source adapter must independently authenticate both
the Nextcloud administration account/provider API and the old LXC host,
establish that the complete credential inventory includes all relevant
principals, verify provider-side revocation, observe denial with the old
writer authority **without accidentally writing**, and prove durable
restart-proof denial. Monotonicity of provider audit cursors and old-host
boot identities must be anchored outside an attacker-writable process; a
one-time JSON report alone is not enough.

## C. Explicit remaining gates (not authorized by this work)

1. Independent security review and source-only PR admission of this code;
   an independent-review timeout is **not PASS**.
2. Separately approved privileged provisioning of stable root/group/lock
   permissions and installation of the new shared Writer guard for ALL
   canonical writer roles, not B3 alone.
3. Separately approved **read-only real provider and old-host source proof**,
   including authentication, all-principal/token enumeration, actual
   revocation, durable denial and host reboot verification. This may need
   more than one administrator or a provider-defined revocation mechanism.
4. A trusted, independently verified cutover authority that cannot be
   fabricated by `B3LegacyRetirementClaims` or
   `B3NextcloudProviderReceipt`; maintain TOCTOU/revocation checks
   throughout the Writer lifetime.
5. Explicit Human permission for bounded live canary, authenticated
   credential operations, old/new Writer cutover, systemd/LXC changes,
   Nextcloud/Vault/Gitea modifications and production deployment.

**Safety invariants:** Existing `github_b3_cutover_preflight.py` and
`github_b3_legacy_retirement.py` still never authorize cutover; the
new cross-check never authorizes cutover; no real Writer effect, source-tree
creation, privilege change, key/secret usage, provider request or service
mutation can result from this source-only code.
