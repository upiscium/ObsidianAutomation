# #483 R3B2B2B — Registration Reader and Legacy Writer Retirement Evidence

**State:** Source-only local candidate. NOT committed, installed, attached,
deployed, enabled, or production-authorized.

The objective is to add an independently rooted, server-owned configuration
reader without confusing source file access with permission to run a canonical
B3 Writer. Legacy-host revocation is an **independent** security condition and
cannot be inferred from a local file or a momentary systemd status.

## A. Fixed server-owned registration and secret source (read-only)

`github_b3_verified_registration.py` defines
`load_verified_b3_registered_scope()`, a **zero-argument** loader with no
HTTP-origin URL/identity/secret overrides, no runtime registration, no requests
to Nextcloud and no B3 Job or Journal effects.

**Proposed future installation; no source is provisioned by this change:**

- Fixed root-owned directory: `/etc/obsidian-b3-registered`, exact mode
  `0750`, group `obsidian-github-writer`.
- Distinct non-root reader UID `obsidian-github-writer`; the source reader
  must execute as that UID. The root's entire path ancestry must be root-owned
  and non-group/world-writable; `openat` with `O_NOFOLLOW` disallows
  symlink substitution.
- Two root-owned, group `obsidian-github-writer` regular files, mode
  `0640`, exactly one hard link each:
  `registered-scope.json` and `webdav-password`.
- The JSON is canonical, sorted, UTF-8, one LF, no duplicate keys, strict
  version 1 and exact fields: `source_generation`, `binding_ref`,
  `revision` (each 32 lowercase hex); `project_path` (relative to the
  canonical `10-Project/` tree, Markdown note); `repository`;
  `registered_base_url` (HTTPS, fixed Nextcloud DAV account path);
  `username` (the account named in the URL); and finite
  `timeout` (1–120 seconds). Only the *root-owned installer*, not the
  WebUI request, may choose these.
- Bounded 4096-byte manifest and 1024-byte credential, `O_NONBLOCK`,
  `O_NOFOLLOW`, `fstat`/path identity before/after, two identical reads
  of each file to detect a mid-read rotation. Secret is a nonempty single
  UTF-8 line; it is never logged or included in an exception. The
  `B3JobConfig.password` field remains `repr=False`. This is **not**
  a guarantee of Python memory zeroization.
- Return value is `B3RegisteredScope`, **not a cutover authorization**.
  It can be connected to the existing request binder by a *future*
  reviewed trusted server owner, but **it is not connected in this patch**.
- If any root path/role/mode/identity/URL/schema/secret invariant is not
  satisfied, refuse with the fixed `registration_source_unavailable` code.

Local root ownership authenticates who can replace configuration in this LXC.
It does not authenticate GitHub or Nextcloud content, prove the source is
immutable over a Writer transaction, prove the new credential was issued
separately from the legacy credential, or grant the right to execute a write.
A future privileged installer must record and verify actual ownership and
access roles before connecting this code.

## B. Independent old-LXC retirement (pure diagnostic, never permission)

`github_b3_legacy_retirement.py` introduces
`B3LegacyRetirementClaims` and `assess_b3_legacy_retirement`.
It checks scoped request binding/revision, old/new host identities, bounded
timestamps, exact legacy principal and credential-ID inventory, completeness
of reported provider-side credential revocations, a reported old-credential
write denial, and old host exclusion checks **after a reboot**.

These values are **caller assertions only**. Even when internally consistent,
the assessment returns `state=candidate`,
`reason=source_authenticity_unverified`, and
**`cutover_authorized=false` unconditionally**. Missing/unsafe evidence
returns `unknown` or `blocked`, likewise `false`.
It does **not** call Nextcloud, inspect credentials, restart an LXC,
stop/toggle any service, touch Vault/Gitea, or manufacture a trusted
`B3CutoverObservation(operator_approved=True)`.

Neither an inventory hash nor a provider denial copied into JSON is evidence
of provider-issued revocation. An actual retirement provider must independently
obtain authenticated, scope-complete source records for **every** legacy
write-capable account/password/token, verify revocation/denial at Nextcloud
and rule out alternate old principals, establish that no legacy process can
regain canonical write authority after restarting, and make this condition
durable across time. If network isolation is the selected method, its
persistence and bypass paths also need proof.

## C. Required separate gates before production

1. Review/merge this **source-only** slice and obtain independent review
   (or explicitly record a review exception, never misreport timeout as PASS).
2. Separately review/provision the fixed registration files under a
   privileged operator and **prove** root/reader roles on the real new LXC.
   No installation is performed here.
3. Implement a trusted provider/host evidence collector that authenticates
   and pins credential retirement from the real authority; do not accept
   caller-supplied claims as approval. Prove restart-persistent old-Writer
   denial and record the administrative rollback decision separately.
4. Implement a real shared **new-host canonical Writer guard**, including
   B3 and all other Vault canonical Writers/Promotion roles. It must cover
   the existing Journal/CAS write and remote re-GET proof; a host-local lock
   does not fence another LXC.
5. Ensure registered Producer data and credential scope are frozen or
   re-validated under the shared guard and cannot rotate during an admitted
   effect. Uncertain effects require independent reconciliation, not retries.
6. Obtain separately approved narrow live E2E/canary and **explicit Human
   cutover authorization**. R3B2A continues to always return
   `cutover_authorized=false` and this change cannot alter that policy.

**Current default: inert and fail closed.** There is no new runtime port,
WebUI Sync Now route, service, source tree, credential read against the real
host, live Writer or deployment from these modules. #483 and #316 remain OPEN.
