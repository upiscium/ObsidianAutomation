# #483 R3B2B2D/B — fixed privileged evidence source reader

**Status: source-only, unattached, no live issuer trust or Writer authority.**

R3B2B2D/A (merged PR #543) verifies two HMAC-SHA256 envelopes using
caller-injected keys and sequence floors. Such caller objects were untrusted.
This B slice changes the **local input boundary**, not the external Nextcloud
authentication story: one separately provisioned non-Writer verifier role
loads a fixed admin-owned snapshot, verifies that its file identities are
stable, and runs the prior fail-closed pure verifier. Nothing is provisioned,
committed to a ledger, authenticated against live providers, or used to
authorize a canonical Writer.

## Exact fixed source and DAC admission

`github_b3_pinned_retirement_source.py` exports only
`assess_pinned_b3_retirement_evidence(*, binding_ref, revision,
expected_generation, now)`. The caller **cannot choose a filename, path,
source key, signing role, provider URL, token, or checkpoint**.

A future separately approved privileged operator must provision (NONE exist
as a result of this work):

- Fixed `/etc/obsidian-b3-retirement-evidence`, root-owned, group
  `obsidian-b3-evidence-verifier`, mode `0750`. Every ancestor is
  root-owned and not group/world writable, with no symlink components.
- Dedicated non-root `obsidian-b3-evidence-verifier` identity/group,
  deliberately distinct from `obsidian-github-writer` and the legacy
  writer identities. Only this verifier role can read below.
- Five fixed root-owned, same-group `0640` regular **single-link** files.
  Each must pre-exist, be short, and have no symlink. The source reader
  uses `openat(O_NOFOLLOW|O_NONBLOCK)`, `fstat`, `stat`, exact
  size, inode, uid/gid, mode, mtime and ctime checks.
- `snapshot.json` (<=2048 bytes) contains only the exact canonical
  `schema_version=github-b3-pinned-retirement-source/v1`, one hex
  generation, and the SHA-256 digests of the four fixed files.
- `issuer-keys.json` (<=1024 bytes) contains a generation-bound pair of
  **synthetic in tests only**, distinctly identified 32-byte HMAC keys in
  hex form. No credentials/keys are created or loaded outside the test
  fixture during development. The private bytes are not returned to a
  WebUI caller or included in result/error strings.
- `replay-floor.json` (<=512 bytes) contains generation-bound prior
  provider and host sequence numbers. This is an **operator-owned
  read-only floor**, NOT a transactionally updated ledger.
- `provider-envelope.json` and `old-host-envelope.json` are each
  <=12 KiB, containing the two exact canonical signed/claimed receipt
  objects accepted by the earlier R3B2B2D/A verifier.

The reader holds the directory descriptor throughout the bounded operation.
It reads all file content and identities, double-reads all bytes, compares
path metadata again, verifies snapshot hashes and consistent generation,
and re-traverses the root path to detect directory replacement. A root
actor with the ability to rotate the files may still change them outside
the check window: a privileged source owner MUST separately guarantee
freeze/atomic install before using this in any effect-sensitive operation.

Missing group/user, absent pre-provisioned files, wrong size/mode/owner,
FIFO, directory, hardlink, symlink, noncanonical/duplicate-key JSON,
invalid digest, file or root replacement, wrong key/generation, replay
floor mismatch or an invalid MAC **fails closed**. All result dictionaries
contain `cutover_authorized=false`, including the only possible
positive `candidate` with an `external_authority_unverified` reason.

## Critical limits: no false Nextcloud authority

- **Local root-owned file admission is not remote attestation.** The
  server that writes the files could supply arbitrarily constructed reports.
  The verifier is given the same symmetric HMAC keys that can forge
  signatures. Asymmetric independent signing/provenance remains NYI.
- A local `replay-floor.json` is NOT durable anti-replay: this source
  code does not advance it. Replaying the same valid input still yields a
  candidate until an independently serialized, protected checkpoint owner
  updates the floor. Only that future owner can prevent concurrent
  duplicated consumption and rollback after a restart.
- The Nextcloud administrative documentation shows
  `user:auth-tokens:list <user> --output=json` and
  `user:auth-tokens:delete` for token management; the latter is
  destructive and entirely outside this phase. Scope may include app
  passwords and sessions. The provider's real output, credential
  owners, alternative principals, backend version, revocation
  guarantees, and proof of write denial must be assessed separately.
  No Nextcloud CLI, OCS, WebDAV, or host API is called here.
- Old LXC restart-persistent writer exclusion, writer token revocation,
  new-host stable group/lock provisioning, and all canonical Writer/Core
  Promotion adoption remain NYI. A local in-process `candidate` is
  not a remotely verified exclusion and cannot be used to mint
  `B3CutoverObservation(operator_approved=True)`.

Official Nextcloud references (verified publicly, not against the user's
installed server):
- https://docs.nextcloud.com/server/stable/admin_manual/occ_users.html
- https://docs.nextcloud.com/server/stable/developer_manual/client_apis/OCS/ocs-api-overview.html

## Follow-on gates

1. Source-only independent review + CI. If unavailable, record the gap
   explicitly; a timeout is never independent PASS.
2. A separate read-only, version-pinned administrative collection contract
   for actual Nextcloud token inventories and old-host post-restart
   state, with independent provenance, complete scope and safe redaction.
3. Privileged key/issuer identity enrollment and a durable, serialized
   protected replay ledger. Prefer public-key verification against
   externally operated signers; do not grant the Writer both signing keys.
4. Verified adoption of the same pre-provisioned new-host canonical Writer
   guard by every effect source including Daily, Mirror and Core Promotion.
5. Separate explicit Human approval for any credential access/revocation,
   Nextcloud queries, host actions, deployment, real canary or cutover.

This phase intentionally does not alter shared `admin_dashboard.py`,
`unified_workflows.py`, #316 B3 Job/Journal/CAS, any service, production
directory or real Nextcloud/Vault/Gitea data. #483/#316 remain OPEN, and
superseded PR #528 remains Draft.
