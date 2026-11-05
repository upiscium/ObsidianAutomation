# #483 R3B2B2A — B3 Producer Source Admission (Source Only)

**State:** Source-only gate; not provisioned, attached, deployed, or approved for production.

This slice introduces an **unattached read-only local Producer reader**
(`github_b3_verified_producer.py`). It validates a specific, already-created
Watcher/Producer record under a fixed, privileged-provisioned Unix identity.
It does not collect GitHub data, infer authority, prepare records, start a
Writer, touch Nextcloud, or attest a legacy Writer retirement.

## Fixed local source trust contract

- Host role: **new consolidated LXC only**; this directory is not provisioned
  by this PR and its presence is never inferred from an old host.
- Proposed directory:
  `/var/lib/obsidian-github-pipeline/29-B3VerifiedSource`.
- The producer is `obsidian-github-sync`, reader is
  `obsidian-github-writer`; these must be **distinct non-root UIDs**.
  The root path ancestry must be root-owned, non-group/world-writable;
  the final directory is producer-owned, group
  `obsidian-github-pipeline`, exact mode `0700` or `0750`.
  The Writer may read but must never be able to replace Producer records.
- Every object must be producer-owned, group
  `obsidian-github-pipeline`, exact mode `0640`, regular non-symlink and
  exactly one link. The reader uses `openat` with `O_NOFOLLOW`, bounded reads,
  inode identity checks and a second manifest read. Any discrepancy refuses.
- Files: `<requestId>.b3-source.json`, `<statusSHA>.b3-status.json` and
  `<overviewSHA>.b3-overview.json`. IDs are lower-case 32 hex and the SHA
  fields lower-case 64 hex, so no path supplied by the HTTP body can
  introduce path separators or alternate filenames.
- Manifest schema is strict, canonical sorted JSON with one trailing LF:
  `record_version=1`, `request_id`, `binding_ref`,
  `expected_revision`, `source_generation`, `source_record_id`,
  `prepared_at`, `status_sha256` and `overview_sha256`.
- This source reader returns only `B3PreparedProposalPair`, which is
  then checked for request binding, canonical proposal semantics and
  freshness by the already-merged
  `bind_b3_registered_inputs` before effect admission.
- Missing/unsafe ownership, UID, group, permissions, path, content, schema,
  or manifest consistency **fails closed**. The default deployment has
  no source provisioning as a result of this PR.

**Limits:** This is local Linux UID-based source provenance, not
cryptographic attestation from another machine, authenticated GitHub
observations, a guarantee that the producer UID is uncompromised, or an
immutable source freeze for the entire Writer critical section.
The pre-Writer re-read in R3B2B1 detects drift at that check, but a future
producer/Writer freeze design must address changes after it. A local
SHA-256 checksum alone does not authenticate a producer.

## Separate hard requirements before any real Writer

1. A source-only reviewed and privileged-provisioned **server-owned
   registration/credential loader**, with pinned WebDAV destination and
   canonical project scope. This PR never reads secrets.
2. A real **single-new-LXC cooperative canonical Writer guard**, shared with
   other canonical Writer/Promotion roles and held through both Writer CAS
   operations and GET proof. A host-local lock cannot fence old LXCs.
3. A separate, independently validated **legacy old-Writer retirement**:
   enumerate all old canonical credentials and Writer processes/timers,
   verify **durable** source of denial across an old-host restart
   (e.g., provider-confirmed revocation of every old canonical app password,
   not merely `systemctl stop`), verify old identity cannot write, prevent
   untracked duplicate credentials, and retain a rollback protocol. The
   credentials/account scope and cloud denial evidence must be checked by
   an approved authenticated administrative workflow; **no request or
   self-signed local JSON can substitute for these observations**.
4. A separately approved controlled live canary with bounded, isolated
   canonical effects and explicit post-write GET reconciliation. If
   a write has an unknown effect, do not automatically retry.
5. Human-reviewed source/PR/E2E evidence and **explicit cutover approval**.
   Do not construct an operator-approved `B3CutoverObservation` from
   this local source reader or R3B2A's `candidate` status.

**Present default:** fail closed; B3 WebUI Sync Now and new Writer are
unattached. No Nextcloud/Vault, old/new LXC, credentials or systemd changes
have been made by this source-only work.
