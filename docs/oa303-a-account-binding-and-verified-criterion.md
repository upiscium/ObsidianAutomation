# ADR #303 — gate A (account binding) and the VERIFIED criterion

- Status: criterion **accepted by the owner on 2026-11-13** ("例示のもので進めて").
- No gate has been promoted by this document.
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

### Readiness per gate (not promoted)

"Evidence ready" means step 1 only. Steps 2 (independent receipt review)
and 3 (owner acceptance) are still pending for every gate.

| Gate | Evidence in scope | Status |
| --- | --- | --- |
| A | P3-A not yet run | Evidence missing (P3-A) |
| I | P3-D1: `oc:id` stable across move, rename and restart; format matches `instanceid` | Evidence ready; review + acceptance pending |
| X | P3-D2: received share = `mount-type shared` + owner's `oc:id`; local items empty | Evidence ready within V1-S1; review + acceptance pending |
| L | P3-D3: index-complete; out-of-band files absent until scanned (option A) | Evidence ready (index completeness); review + acceptance pending |
| C | — | Not applicable; stays UNKNOWN under the restricted contract |
| R | P3-D5: old password 401 immediately; synthetic tests for the local revision | Evidence ready; review + acceptance pending |

## 2. Gate A — what must hold

| Element | Requirement | Implementation (`vault_root_enrollment.py`) |
| --- | --- | --- |
| Account | The authenticated user equals the user segment of `/remote.php/dav/files/<user>/` | `DAV:current-user-principal` must be `/remote.php/dav/principals/users/<same user>/`, else `account_mismatch` |
| Instance | The same Nextcloud instance as at enrollment | Instance ID = `oc:id` minus the zero-padded `oc:fileid`; stored, then required on every walk through `oc:id` validation |
| Account root | A trusted container for every walk | Account root `oc:id` stored and used as `container_resource_id` |
| Record validity | Bound to what it was made under | Stores a digest of WebDAV URL + user, never the password. Valid only while those are unchanged; a credential-only change keeps it. Mode 0600 |

`enroll_account()` makes one Depth:0 `PROPFIND` of the account root. It
refuses with no I/O while the register does not admit provider reads. It
uses a transport built from the saved credentials. The saved login must
equal the principal's user ID (V1-S1). Re-enrolling the same URL + user is
refused with `instance_changed` when the instance or account root differs
from the existing record. It fails closed when:
- the response is not a collection, or carries a mount type;
- the `oc:id` format or the instance ID is invalid;
- the principal is a different user;
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

## 3. P3-A experiment (planned; needs approval before it runs)

Read-only, on the existing disposable stack:

| # | Credential | Request | Expected |
| --- | --- | --- | --- |
| 1 | admin | `PROPFIND Depth:0` admin files root (enrollment body) | 207; principal = admin |
| 2 | viewer | `PROPFIND Depth:0` viewer files root | 207; principal = viewer |
| 3 | admin | `PROPFIND Depth:0` **viewer's** files root | 403/404; no listing |
| 4 | viewer | `PROPFIND Depth:0` **admin's** files root | 403/404; no listing |
| 5 | admin | `GET /ocs/v1.php/cloud/user?format=json` | `id` = admin |

The run makes no writes. A user whose login name differs from the user ID
needs a configuration change, so it is outside V1-S1 and is not tested.
