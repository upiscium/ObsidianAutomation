# ADR #303 — gate A (account binding) and the VERIFIED criterion

- Status: criterion **accepted by the owner on 2026-11-13** ("例示のもので進めて").
- **I, X, L and R accepted as VERIFIED for V1-S1 by the owner on 2026-11-13**
  ("I/X/L/RをVERIFIEDとして承認する"). `CURRENT_PROVIDER_PROOF` reflects this.
- **A accepted as VERIFIED for V1-S1 by the owner on 2026-11-13** after P3-A
  ("両方とも承認する"). C stays UNKNOWN, so only the restricted contract
  admits provider reads.
- Production still performs no provider I/O. Enrollment, the picker and
  activation are not wired into `main()`, and the Job guard reads only once
  a root is pinned, which needs an activation.
- **Deploy precondition.** Before deploying this, confirm that the production
  state directory contains neither `vault_root_binding.json` nor
  `vault_root_pinning.enabled`. A leftover *activated* pin with a chain would
  now make every Job send one Depth:0 `PROPFIND` with the saved credentials.
  Before this change, such a pin made every Job refuse. A `connection_saved`
  binding, or a pin without a chain, still causes no I/O.
- Base: `main@def40466` (after #578).

## 1. VERIFIED criterion

Under the old rule, finite evidence was never promoted, so activation could
never become possible. The owner replaced it with this rule.

A gate is **VERIFIED for a declared scope** only when all three hold:
1. Disposable-instance evidence covers that scope.
2. An independent review has checked the receipts against the claims.
3. The owner explicitly accepts **that gate by name**.

Promotion is a reviewed code change to `CURRENT_PROVIDER_PROOF` plus its test.
Outside the declared scope the gate stays unverified.

### Declared scope V1-S1

- One shared Nextcloud account, Nextcloud **35.x**, local storage backend.
- Excluded from the Vault tree: group folders, external storage and
  server-side symlinks.
- The Vault is written only through Nextcloud clients (L option A).
- The Nextcloud is served at the host root, not under a sub-path.
- The user ID matches `[A-Za-z0-9_.-]+` and equals the login name.
- HTTPS with a certificate that the system trust store accepts.

### Declared scope V1-S2 (owner-accepted 2026-11-13, "両方とも承認する")

V1-S2 is everything in V1-S1, plus one addition: the Vault root may be **a
received share** or a folder inside it.

**Why:** on obsidian-control the saved Vault `ObsidianVault` is a received
share (`mount-type=shared`, `is-mount-root=true`, `SGDNVCK`), which V1-S1
refused.

**Rules** (`SHARE_ROOTS_ACCEPTED = True` in `vault_root_dav_verifier`):
- A selected path may enter one share, at its mount root
  (`mount-type=shared`, `is-mount-root=true`). Every level below must be
  inside that share (`is-mount-root=false`). A second share root nested
  inside it is refused.
- When the target lies inside a share, its `oc:permissions` must contain
  `C` and `K` (create files and folders).
- Any other mount type (external storage, group folder, …) is refused as
  before.
- **Federated or unknown owners are refused** (owner decision 2026-11-13,
  option A). A share's `oc:owner-id` must be a plain local user ID
  (`[A-Za-z0-9_.-]{1,64}`); `user@host` and anything else is an alias.
  Local user IDs containing `@`, spaces or quotes (allowed by Nextcloud,
  e.g. e-mail style or LDAP) are therefore refused as well (fail-closed).
- A row missing `nc:is-mount-root` or `oc:permissions` is refused
  (`alias_unverified` / `identity_unverified`). `oc:owner-id` is required
  only on shared rows: a share without it is an alias, while local rows
  are unaffected.
- The identity pin, the Job guard, and the collision and duplicate-ID checks
  are unchanged. The pinned ID of a share root is the owner's folder ID.

**Evidence (P3-SR, disposable Nextcloud 35.0.2, 2026-11-13).** The share
root's `oc:id` equalled the owner's folder ID and stayed the same after
each of:
- an owner rename;
- a recipient rename of its mount point;
- an unshare and reshare;
- a restart.

The share root reports `is-mount-root=true` and items inside it `false`.

**P3-OW (read-only, 2026-11-13).**
- On the disposable instance, every local row reported the account itself
  as owner, and the 2 received shares reported the sharing user in plain
  local-ID form.
- On obsidian-control, `ObsidianVault` reported an owner that is present,
  in local-ID form, and not the account.
- No values were recorded.

**Accepted but not tested:** group shares. They look identical to a user
share and live on the same server.

**Refused by the owner-ID rule:** federated shares. Their real owner format
was not observed, so the rule fails closed on anything that is not a plain
local ID.

**Not tested:** expiring shares, and a permission downgrade while in use.

**Behaviour on change.** While a share is removed, Jobs refuse because the
root is gone. If the owner recreates the folder, the ID changes and Jobs
refuse (`mismatch`).

### State per gate

Steps: (1) disposable evidence, (2) independent receipt review, (3) owner
acceptance.

| Gate | Evidence in scope | Status |
| --- | --- | --- |
| A | P3-A (2026-11-13), see §3; receipt reviewed | **VERIFIED** (V1-S1: OCS user id binding), owner 2026-11-13 |
| I | P3-D1: `oc:id` stable across move, rename and restart; format matches `instanceid` | **VERIFIED** (V1-S1), owner 2026-11-13 |
| X | P3-D2: received share = `mount-type shared` + owner's `oc:id`; local items empty | **VERIFIED** (V1-S1: user shares only), owner 2026-11-13 |
| L | P3-D3: index-complete; out-of-band files absent until scanned (option A) | **VERIFIED** as index completeness (V1-S1), owner 2026-11-13 |
| C | — | Not applicable; stays UNKNOWN under the restricted contract |
| R | P3-D5: old password 401 immediately; synthetic tests for the local revision | **VERIFIED** (V1-S1), owner 2026-11-13 |

## 2. Gate A — what must hold

| Element | Requirement | Implementation (`vault_root_enrollment.py`) |
| --- | --- | --- |
| Account | The authenticated user equals the user segment of `/remote.php/dav/files/<user>/` | OCS v1 `cloud/user` `id` must equal the URL user segment **and** the saved login, else `account_mismatch`. `current-user-principal` is not used (empty on the own files root, P3-A) |
| Instance | The same Nextcloud instance as at enrollment | Instance ID = `oc:id` minus the zero-padded `oc:fileid`; stored, then required on every walk through `oc:id` validation |
| Account root | A trusted container for every walk | Account root `oc:id` stored and used as `container_resource_id` |
| Record validity | Bound to what it was made under | Stores a digest of WebDAV URL + user, never the password. Valid only while those are unchanged; a credential-only change keeps it. Mode 0600 |

`enroll_account()` makes one Depth:0 `PROPFIND` of the account root and one
GET of `/ocs/v1.php/cloud/user?format=json`. It refuses with no I/O while
the register does not admit provider reads. It uses a transport built from
the saved credentials. The OCS user ID must equal both the URL user and the
saved login (V1-S1). Re-enrolling the same URL + user is
refused with `instance_changed` when the instance or account root differs
from the existing record. It fails closed when:
- the response is not a collection, or carries a mount type;
- the `oc:id` format or the instance ID is invalid;
- the OCS answer is not status 100 with a plain `id`, or names another user;
- the root has no `oc:fileid`/`oc:id` (the P3-A cross-account stub);
- the response is a redirect or a non-207;
- a transport error occurs.

`context_provider()` builds the server-side `TrustedRootContext` for the
V1/C picker and V1/D from:
- the saved connection;
- the enrollment record;
- the active root's identity: the pin leaf only if the pin is finalized and
  belongs to the same instance and source, otherwise one Depth:0 read;
- the local revision, read before and after any I/O.

It returns `None` (`binding_unavailable`) when any of these is missing or
the saved connection changed while the context was being built.

**Operator notes.**
- **Recovery after a real migration (`instance_changed`).** Remove the
  private `vault_root_account.json` from the service state directory while
  the service is stopped, then enroll again. This deliberately needs host
  access: a different instance behind the same URL is never accepted from
  the UI.
- **Login name.** Logins by email or alias, or with different letter case,
  fail with `account_mismatch`. That is the V1-S1 limit.
- **Concurrent saves.** Two shared saves that restore identical bytes within
  one context build (A→B→A) are not detected. The window is one build; the
  CAS at activation and the Job pin check still apply.

**Not wired.** No route triggers enrollment, and `main()` still constructs
neither the picker nor activation. Both remain default OFF.

## 3. P3-A experiment (owner approved 2026-11-13; run once)

Read-only, on the existing disposable stack. Client SHA-256 `223a0341…720c4c`.
Receipt `p3-20261112/a.jsonl` (owner-private). Planned:

| # | Credential | Request | Expected |
| --- | --- | --- | --- |
| 1 | admin | `PROPFIND Depth:0` admin files root (enrollment body) | 207; principal = admin |
| 2 | viewer | `PROPFIND Depth:0` viewer files root | 207; principal = viewer |
| 3 | admin | `PROPFIND Depth:0` **viewer's** files root | 403/404; no listing |
| 4 | viewer | `PROPFIND Depth:0` **admin's** files root | 403/404; no listing |
| 5 | admin | `GET /ocs/v1.php/cloud/user?format=json` | `id` = admin |

The run makes no writes. A user whose login name differs from the user ID
needs a configuration change, so it is outside V1-S1 and is not tested.

### Results (Nextcloud 35.0.2)

| # | Observed |
| --- | --- |
| 1 | 207, 1 row, the own files root. `oc:id` = padded `oc:fileid` + `instanceid`; collection; no mount. **`current-user-principal` empty** |
| 2 | Same for the viewer. Principal empty |
| 3 | **207 (not 403/404)**: a stub collection at the viewer's files href, **without `oc:fileid`/`oc:id`**. Its principal is the *requester* (admin) |
| 4 | Same in the other direction: stub, no file identity, principal = viewer |
| 5 | HTTP 200, OCS status 100, `id` = the admin account's user ID (equal to the URL user of step 1) |

**Consequences.**
- The planned principal check would have refused every legitimate account
  (fail-closed, never successful). Enrollment now uses the OCS user ID, which
  was observed correct.
- Cross-account access exposed no file identity of the other account. A
  cross-account URL cannot enroll: it has no `oc:fileid`/`oc:id`, and the OCS
  ID ≠ URL user.
- The "403/404" expectation was wrong: Nextcloud answers 207 with a stub.
  The guarantee rests on the missing identity, not on the status code.

**Limits.** One instance and version. OCS was observed for admin only.
Login ≠ user ID and app-password scope were not tested; both are outside
V1-S1. The owner accepted A for V1-S1 with these limits.
