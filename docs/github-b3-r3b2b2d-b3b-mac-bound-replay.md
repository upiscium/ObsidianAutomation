# #483 R3B2B2D/B3B — MAC-bound local replay journal handoff

**Scope: source-only, unattached, not a real issuer authority or cutover gate.**

R3B2B2D/A introduced an in-process, dual issuer-key HMAC verifier for
canonical, bounded Provider and legacy-host evidence envelopes. B1 fixed a
root-owned local snapshot input path. B2 added strict offline OCC/legacy-host
observation interpreters. B3A introduced a standalone root-owned,
serialized, hash-chained local replay journal. **Before B3B, nothing
proved that a recorded sequence or bundle digest was derived from the
same signed envelope pair that passed the evidence cross-check.**

B3B introduces a bounded, **unattached** source-only handoff:
`github_b3_mac_replay_binding.py` and
`assess_and_record_b3_mac_bound_replay`.

## Precisely what is now bound

1. Caller supplies the two exact immutable byte buffers of canonical
   provider and old-host HMAC envelopes, a proposed distinct 32-byte
   issuer-key pair, external read-only sequence floor, and fixed 32-hex
   epoch/binding/revision/source-generation and clock inputs. **The key
   origin and floor origin are not established here.**
2. The module first calls the existing
   `assess_b3_authenticated_evidence` on these exact bytes. This
   verifies each issuer's MAC, schema, matching issuer/key ID, sequence
   greater than the supplied floor, strict receipt typing,
   cross-provider/old-host revision/fencing/generation and the claimed
   token inventory/revocation, restart and denied-write consistency.
   Any unknown/blocked result stops **before all journal I/O**.
3. **Only** the existing
   `candidate/detached_mac_valid_external_authority_unverified`
   result can proceed. The module re-parses the *same* exact canonical
   buffers and extracts issuer, key IDs and both sequence integers.
   The caller cannot independently choose a journal sequence or raw
   bundle digest in this entrypoint.
4. A domain-separated, fixed-order, length-delimited SHA-256 digest is
   computed over both exact byte buffers:
   `SHA256("obsidian-b3-mac-to-replay-journal/v1\0" ||
   u32be(len(provider)) || provider ||
   u32be(len(old_host)) || old_host)`.
   Exposed only as a hash inside the local diagnostic journal.
   A different valid MAC pair, swapped order, altered payload or
   changed sequence has a different digest (barring a SHA-256
   collision). A caller-supplied digest is never accepted.
5. The matching `B3ReplayDiagnostic` is passed to the merged B3A
   `append_b3_replay_diagnostic` **as one call**, which obtains an
   exclusive, nonblocking Linux `flock` on the same preprovisioned
   inode. Under that lock it compares both sequence counters to the
   *latest recorded* pair (not merely the caller's proposed floor),
   checks exact epoch, binding/revision/source generation/key IDs and
   repeated bundle digest, verifies the full canonical hash chain,
   and appends + `fsync`s one record if still admissible.
6. A successful local `recorded` maps only to
   `candidate/mac_bound_local_checkpoint_external_authority_unverified`.
   A blocked journal result maps to `blocked/local_replay_journal_refused`,
   unknown/corrupt/unavailable result maps to a redacted `unknown`.
   **All paths return `cutover_authorized=false`.** There is no
   `B3CutoverObservation(operator_approved=True)`.

The new module is **not imported, routed, registered or invoked by any
production service**. The B3A journal is also unattached. Its future
root-only directory/genesis files were not installed or read during
development. The tests alone provision disposable temporary directories
and unmistakably fake, synthetic repeated-byte issuer keys and receipts.

## Verified in the isolated synthetic test fixture

Tests cover a correctly MAC-validated and coherent pair recorded exactly
once, two sequential pairs advancing both issuer counters, replay of the
same bytes, valid re-signed payload/changed digest with stale sequence,
one counter equal to the current journal despite a lower caller floor,
swapped provider/host order, altered MAC, blocked claimed revocation,
mismatched binding/generation/key IDs, invalid epoch/clock, malformed or
oversized envelopes and missing/corrupted ledger. Invalid MACs and
cross-receipt failures must **not even invoke** the journal function.

Other fixtures cover a crash-like `fsync` acknowledgement failure
after a durable append (unknown to caller, second call blocked), a
fabricated `cutover_authorized=true` journal return (rejected), and
cooperating concurrent tasks attempting to record the same bundle
(only one local `candidate`, one journal record).

## Critical remaining trust gaps / non-authority

- **This is a MAC proof only under caller-injected symmetric keys.**
  The verifier also holds keys capable of forging each HMAC. Nothing
  yet proves that Nextcloud itself or the old LXC issued the two
  records, that the reported token revocation really occurred, or
  that the old Writer has no other credentials. The B2 OCC and
  systemd parsers are **not tied to actual provider/host collectors**.
- **The floor is still externally supplied and not transactionally
  coupled to the B1 `replay-floor.json`.** This B3B function does
  compare against the latest locked B3A journal state at append time,
  but does not publish/hand off that durable result to the B1
  non-Writer verifier, authenticate the journal's origin, or bind
  independently trusted issuer identities to the locked record.
- **Root or storage rollback is not prevented.** The local B3A
  SHA-256 hash chain and `flock` detect corruption/cooperating
  concurrent replay, not a privileged actor who can restore an old
  fully valid journal or forge both records. A separately controlled
  monotonic witness or external anti-rollback anchor remains NYI.
- **MAC validation and append are not a single externally trusted
  transaction.** The data buffers are immutable in-process and the
  append performs latest-journal comparison under the lock; however
  the caller owns the injected keys, epoch and floor and can present
  fabricated evidence. A future privileged service must independently
  authenticate origins, use out-of-band keys/signatures and ensure
  the trusted identity/credential snapshot does not rotate between
  verification, persistence and any remote effect.
- A journal append does not authorize a remote effect and there is
  **no atomicity with actual Nextcloud token revocation, a WebDAV
  write denial or canonical Writer execution**. Failure after
  successful `fsync` may be acknowledged as unknown, and retry is
  conservatively rejected.
- The B3 Canonical Writer guard is **unattached**. No proof that
  every new-host Writer, Daily, Mirror, Core Promotion or legacy-LXC
  process cooperates on a single stable lock, nor that old-host
  reboot-persistent egress is fenced.
- No actual production files, root role, credentials, Nextcloud,
  old LXC, Gitea, Vault, systemd, Writer, deployment or cutover
  were accessed. No publication until separately approved.

## Next gates

1. Source-only code review (including security review), regression
   tests and exact-head CI; timeout is NOT an independent review PASS.
2. Root-of-trust design: genuine independently authenticated, version
   pinned Nextcloud administrative collector and separate old-LXC
   post-restart observer, with safe token-scope and redaction.
3. Protected monotonic anti-rollback witness and trusted, atomic
   read/commit handoff from B3A latest journal to B1 non-Writer
   sequence-floor checking. Explicit replay-epoch rotation and
   recovery protocol.
4. Full producer/Writer/Core Promotion effect inventory and one
   shared lock, plus actual old principal/token revocation and
   post-restart denial observation.
5. Human authorization and completed independent security review
   before privileged root provisioning, live credential access,
   host actions, service deployment, Writer enablement or cutover.

Issue #483/#316 remain OPEN and superseded PR #528 remains Draft.
