# #483 R3B2B2D/B3A — Root-owned append/fsync sequence journal

**Status: local source-only proof, not production-configured or cutover-authoritative.**

The merged R3B2B2D/A+B1 use a read-only, caller/provisioner-provided
`B3DetachedSequenceFloor`. That can *compare* signed sequence numbers,
but does not advance a durable floor. Replaying a valid report twice still
produces a diagnostic candidate. R3B2B2D/B3A adds an entirely **unattached**
root-only journal writer that advances a **local** pair of sequence counters,
without granting any B3 Writer authority.

This is deliberately a *separate diagnostic-admission effect* from the
actual provider/host issuer verification, credential revocation, Writer
execution, and the root-owned B1 verifier directory. It does not authorize
a real B3CutoverObservation and is not imported by any active process.

## Provisioning contract (NONE performed by this work)

A separately authorized privileged installer must create:

- Fixed directory `/var/lib/obsidian-b3-replay-ledger`, root:root
  `0700`. All path components must be root-owned, non-group/world-writable,
  and not symlinks. No HTTP/CLI request can select another path.
- One pre-existing **regular single-link** `retirement-sequences.jsonl`,
  root:root `0600`, size bounded to 1 MiB. No symbolic links, hardlinks,
  FIFO/device files or auto-creation.
- A **valid, complete genesis** containing a privately chosen 32-hex
  `epoch`, exact `binding_ref`, `revision`, `source_generation`,
  two distinct issuer key IDs and independent starting provider/host
  sequence floors. Genesis is **not created by the module**; installation
  and rollback policy remain separately gated. The genesis includes a
  zero previous-hash and a zero bundle-hash sentinel.
- All calls must run under the dedicated privileged, preapproved journal
  owner. The module accepts only exact `B3ReplayDiagnostic` typed input;
  it loads no issuer keys, never runs external processes, and cannot
  accept request-selected arbitrary filesystem paths.

In production, `_owner_uid()/_owner_gid()` require root:root. Synthetic
tests override those fixed identity and path hooks to a **disposable
pytest temporary directory owned by the current test user**. No real
`/var/lib/obsidian-b3-replay-ledger` directory or file is created/read
during development.

## Algorithm, fail-closed cases

`append_b3_replay_diagnostic` rejects incorrect IDs, key ID duplication,
invalid counter ranges or malformed SHA-256 digest before filesystem I/O.
It opens the fixed directory by descriptor traversal (`O_NOFOLLOW`),
validates ownership/mode, opens the pre-existing file with
`O_RDWR|O_APPEND|O_NOFOLLOW|O_NONBLOCK`, verifies inode/size/owner/mode
against the fixed pathname, then acquires a **nonblocking exclusive
Linux `flock`** on that same inode. Lock contention fails closed.

Under the lock it:

1. Rechecks stable fixed directory and file identity, reads the
   entire bounded existing journal, and requires a complete,
   canonical LF-terminated JSON line format with exactly defined fields,
   no duplicate keys, correct embedded SHA-256 checksums, and a strictly
   chained `previous_sha256` link. The first line must be genesis.
2. Requires unchanged epoch/binding/revision/generation/issuer key IDs,
   **both** provider and host sequence numbers strictly greater than the
   latest record, and a previously unseen nonzero opaque bundle SHA-256.
3. Checks the 512-record and 1-MiB capacity bounds. When saturated,
   processing **stops**; no implicit rotation or unsafe truncation occurs.
4. With the same exclusive file lock, appends exactly one complete new
   canonical record, calls `fsync` on the file, and rechecks the inode,
   final size and directory path before reporting diagnostic
   `state=recorded`. All results (including this one) have
   **`cutover_authorized=false`**.
5. If an interrupted/partial write leaves an incomplete final line, or
   corruption/format mismatch/replayed digest/changed path is seen,
   processing refuses rather than repairing, truncating or accepting a
   previously valid prefix. The operator needs a separate, reviewed
   recovery protocol to preserve the highest known counter values.

A local `recorded` result means the append and `fsync` completed under
local OS filesystem and lock assumptions. It **does not** mean that a
legitimate provider or old host supplied the record, or that credential
revocation/old Writer fencing has been proven.

## High-priority limitations / external trust still NYI

- **Root rollback remains possible.** A sufficiently privileged host
  operator can restore an earlier valid journal (with internally
  consistent hashes) or the entire disk snapshot. A SHA-256 hash chain
  provides corruption detection but **NOT malicious rollback protection**
  against a writer who controls the root file. A separately anchored
  monotonic counter / protected append-only remote witness, backed-up
  receipt chain or equivalent external proof is still required for
  anti-rollback claims.
- **No real authority binding.** `B3ReplayDiagnostic` contains
  caller-supplied key IDs, sequence numbers and an opaque bundle digest.
  This module **does not verify the detached HMAC envelopes**, that their
  sequence values match, or that Nextcloud and the old-LXC actually issued
  truthful reports. Future privileged code must independently authenticate
  and *atomically bind* the verified bundle to this journal effect.
  Otherwise a privileged invoker could record a forged or irrelevant
  bundle. The append does NOT mint or authorize an operator-approved
  `B3CutoverObservation`.
- **No handoff to the B1 verifier.** The earlier fixed
  `replay-floor.json` remains read-only and uncoordinated with this new
  journal. A separate trusted, monotonic handoff must provide the
  journal's last accepted pair to the non-Writer verifier; a WebUI caller
  must never pick a floor or a journal epoch.
- **No crash-recovery authorization.** A crash after the append is on
  durable storage but before the caller receives success can return
  unknown; repeating the same receipt will be rejected as already
  recorded. This is conservative at-most-once local consumption, not
  atomicity with the eventual remote Writer effect.
- **No complete threat-model closure.** `flock` serializes cooperating
  instances on the same local inode; a privileged process ignoring
  locks or a non-local filesystem without reliable flock/fsync
  semantics can violate the assumptions. Filesystem checks prevent
  ordinary unprivileged path changes, not arbitrary root tampering.
  External key lifecycle, independent issuer signatures, old-host
  reboot/credential denial and all-Writer shared canonical guard
  adoption remain independent, NYI production gates.
- Real Nextcloud/Vault/Gitea endpoints, old LXC/systemd, credential
  reads/revocations, production root paths, deployment/release,
  B3 Writer, WebUI Sync Now and live cutover are not accessed.
  #483/#316 stay OPEN; old PR #528 stays Draft.

## Separate approval gates

1. Source-only independent review of the exact new module, disposable tests,
   and journal contract, with CI. If OpenCode times out, **record as
   incomplete**, never claim an independent PASS.
2. Independently reviewed production privileged installer and root
   ownership/role specification, immutable epoch enrollment and
   externally anchored rollback-resistance design.
3. Authenticated Nextcloud admin/old-LXC collectors and a trusted
   validation-to-journal handoff, including crash/rotation/restart tests.
4. A separate operator privilege and environment gate for actual live
   ledger provisioning or reading credentials. No live commands here.
5. All canonical Writers/Core Promotion must hold the same new-host
   stable lock, old Writer authority must be disproven, and a separate
   operator approval is required before B3 effects or cutover.
