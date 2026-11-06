# #483 R3B2B2D/A — Detached dual-issuer evidence MAC (source-only)

**State: local, source-only, never production-authoritative.**
This phase tests a narrow property: a bounded, canonical evidence envelope
can be checked with distinct caller-supplied issuer keys while preserving
the existing B3 rule of **no cutover authorization whatsoever**.
It does **not** claim to prove that Nextcloud or the old LXC actually
generated either report, that all legacy credentials were revoked,
or that all Writer roles cooperate in the new-host lock.

## Existing proof gaps

- R3B2B2B `B3LegacyRetirementClaims` checks internally consistent
  assertions but can be forged by a caller.
- R3B2B2C `B3NextcloudProviderReceipt` and
  `B3OldHostFenceReceipt` cross-check identity, generation, dates and
  completeness but do not authenticate their **issuers** or their truth.
- R3B2B2C `B3CanonicalWriterGuard` protects only processes that take
  the same new-LXC kernel lock inode. It is not wired into the B3 service,
  all canonical Writers, Mirror, Daily, Core Promotion or the old LXC.

## Detached MAC envelope contract

`github_b3_authenticated_evidence.py` exposes only the pure
`assess_b3_authenticated_evidence` read/verify function; **no signing API**,
live authentication, keyring loader, provider networking, database update,
file creation or Writer call is added.

Exactly two canonical UTF-8, sorted-key, compact JSON records (one LF
terminator, no duplicate keys, bounded to 12 KiB each) must contain:

- `schema_version=github-b3-mac-envelope/v1`, `issuer`, `key_id`,
  `sequence`, `payload` and `mac_sha256` (lowercase hex).
- Issuers are distinct and domain-separated:
  `nextcloud-provider-probe` and `legacy-host-probe`.
- The MAC covers the schema, issuer, key ID, monotonic sequence and **entire
  payload**, with separate domain binding for each issuer, using HMAC-SHA256.
- The future privileged owner must inject a pair of **different**, validated
  32-byte secrets and different 32-hex key identifiers into
  `B3DetachedIssuerKeys`. The keys are never logged or included in repr.
  A synthetic test-side signer creates disposable test messages only.
- The owner must inject a **protected durable prior sequence floor**, one
  per issuer. This module enforces `new_sequence > previous_sequence`,
  but **does not persist an updated checkpoint**. A caller can forge a
  `B3DetachedSequenceFloor`; therefore no real replay protection exists
  without separately approved storage, serialized admission, and restart
  proofs.
- After both MAC checks, payloads become the existing typed Provider and
  old-host receipt structures, and are passed to the existing R3B2B2C
  cross-check for binding/revision/generation, freshness, revocation
  inventory, host restart ordering and claimed write denial.

Failure always yields a fixed, nonsecret `unknown` or `blocked` result.
A complete, perfectly MAC-valid synthetic bundle yields **`candidate`**
with `reason=detached_mac_valid_external_authority_unverified`.
**Every single path returns `cutover_authorized=false`**. This module
does not mint a trusted `B3CutoverObservation`.

A valid HMAC proves only possession of a shared symmetric key. The verifier
also holds a signing-capable secret; anyone with it can forge the report.
Two different HMAC keys do **not** by themselves establish independent
issuing systems, truthful observations or key compromise resistance. In
particular, these are NOT native Nextcloud-signed receipts and are not
cryptographic attestation from Nextcloud software.

## Nextcloud-native evidence path to investigate before production

The documented Nextcloud administrative `occ` command set includes
`user:auth-tokens:list` and `user:auth-tokens:delete`. Deletion is a
**future privileged action**, not part of this code; the exact installed
server version, account identities, token scope/format and approved
procedures must be verified first:
https://docs.nextcloud.com/server/stable/admin_manual/occ_users.html

Nextcloud's Login Flow documentation also documents an OCS
`DELETE /ocs/v2.php/core/apppassword` operation for the **currently used**
app password; that endpoint must not be mistaken for arbitrary,
administratively authenticated revocation of every old Writer credential:
https://docs.nextcloud.com/server/stable/developer_manual/client_apis/LoginFlow/index.html

A real collector will need a separately operated, authenticated
administrative origin for the provider's account/token inventory and
post-revocation denial result, plus a separately authenticated old-host
read-only inventory after reboot. A passive systemd snapshot alone cannot
prove old credentials no longer work. Do not attempt a WebDAV PUT for denial
testing without a dedicated approved disposable target and operator gate.
Avoid disclosing raw passwords, bearer tokens, secret-like hashes or
sensitive provider response bodies in logs or Vault evidence.

## Remaining gates (all NYI)

1. Review and separately approve source-only publication of this bounded
   verifier, with exact-head CI and independent review (or a recorded
   exception; a timeout is NOT independent PASS).
2. Define/verify the actual Nextcloud provider administrative interface
   and credential inventory contract in the deployed server version.
   Establish a separate read-only approved probe and administrative
   change protocol for token revocation, including all alternate principals.
3. Provision a **trusted out-of-band issuer identity and key lifecycle**:
   preferably distinct asymmetric verification keys with an external signer;
   do not trust WebUI-supplied keys or issuer text. Store monotonic issuer
   sequence floors in tamper-resistant durable storage with atomic updates,
   issuer rotation/replay protections and restart exercises.
4. Authenticate the old-host inventory and post-restart state through a
   different trusted channel. Establish durable denial of all old canonical
   Writer authority, not merely the reported absence of timers.
5. Inventory every new-host canonical Writer, Daily, Mirror and Promotion
   effect and make each use the same operator-provisioned stable lock inode
   through the full effect/readback, with negative regression tests.
6. Obtain separately explicit Human approval before live provider calls,
   privileged provisioning, credential reads or revocation, host restart,
   deployment, canary or Writer cutover. The existing preflight and
   retirement evaluators must continue to refuse if proof is incomplete.

**No effect:** This code has no runtime registrations, no live secrets,
no service/timer control, no Nextcloud/Vault/Gitea API requests, no
network, no credential action and no route activation. #483/#316 remain OPEN.
