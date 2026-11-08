# #303 V1/B1 — disposable Nextcloud identity/provenance feasibility plan

Status (2026-11-08 JST): **design + synthetic-only test matrix; NOT a provider
proof**. Source base `main@bc61c8803cfc92b3ad51fc2ad0b877a8e17b2cf4`
(PR #563 already merged). R0 was originally reviewed as local/uncommitted;
the owner subsequently authorized **only reviewed source-only
Commit/Push/Draft PR publication** for the combined R0/R1 slice.
No Ready/Merge/Deploy/activation, Nextcloud/provider I/O, credentials,
Vault data or Job/Writer effects are authorized by that approval.
Keep #303/#301/#304 and parent #298 OPEN.

## Decision under test

Can a **single configured shared Nextcloud account** supply sufficiently
authoritative server-side evidence to choose a V1 Vault root, without
inventing canonical resource identities, alias classification, complete
sibling inventories or cross-level snapshot coherence?

V1/B0 has two UNWIRED synthetic contracts:
- `vault_root_dav_fixture.py`: fabricated Depth:1 Multi-Status XML parser,
  explicit synthetic classifier claims, V1/A final preview.
- `vault_root_dav_adapter.py`: fabricated VerifiedDavPage assembler with
  reported byte/cursor/snapshot claims, V1/A final preview.
These records are constructible; `verified=True`, matching `snapshot_token`,
or `origin=synthetic_fixture` are **not** producer authentication. No
production importer can use them as proof. Even `candidate_valid` cannot
activate a root or authorize Vault writes.

## Public protocol evidence (not a real-tenant observation)

Nextcloud 35 developer documentation:
- https://docs.nextcloud.com/server/stable/developer_manual/client_apis/WebDAV/basic.html
- Authenticated file DAV is usually `/remote.php/dav/files/{user}/...`.
- `oc:fileid` is unique within a Nextcloud instance; `oc:id` is fileid
  namespaced by instance, documented as globally unique.
- `DAV:resourcetype` can identify a collection, but not independently
  establish absence of a share or mount.
- `nc:mount-type`: local(empty)/shared/group/external/external-session;
  `nc:is-mount-root` identifies mount roots. Missing or unknown properties
  must be treated explicitly, never defaulted to "not an alias".
- `oc:permissions` and `oc:share-types` are supplementary observations;
  read permission is not a stable path or canonical-identity proof.
- Unknown requested DAV properties may be reported as 404 in individual
  `propstat` elements, even when the HTTP response is 207.
- `DAV:getetag` may assist detecting changes but is not a guaranteed
  atomic multi-directory snapshot token.

RFC 4918 section 9.1: https://www.rfc-editor.org/rfc/rfc4918.html
- `PROPFIND Depth:1` reports the collection and member responses; response
  order is unspecified, and property access may be limited by ACL.
- The RFC does **not** furnish a versioned, atomic multi-level tree snapshot.

Observed external issue requiring defensive checks (not tested locally):
- https://github.com/nextcloud/server/issues/63415
  Reported 2026-08-19: some older legacy `/remote.php/webdav` endpoints
  could produce HTTP 207 for non-existent paths. Use modern authenticated
  `/remote.php/dav/files/{user}` only; require a valid parent self row,
  matching canonical href and ID, and a complete valid Multi-Status body
  rather than trusting HTTP 207 by itself. Report is provider-version and
  endpoint specific; do not generalize it to all versions.

## Owner approval gates (separate and strict)

B1-P0 (now): public specification review, offline fixture/matrix validation,
no service access. No instance creation or test account access required.

B1-P1 (FUTURE, requires explicit approval): provision and inspect a
**disposable** provider instance with pinning of Nextcloud server version,
app inventory, storage backend, TLS origin, authenticated DAV base, assigned
test-only user, and isolated test-only data. No real Vault/account credentials.
If the disposable provider is not already seeded, creating accounts/folders,
shares or mounts is an explicit **separate write/setup authorization**, not
part of a read-only inspection.

B1-P2 (FUTURE, requires explicit read-only provider-I/O approval): bounded
authenticated `PROPFIND Depth:0/1` against ONLY the approved disposable
origin and allowlisted fixture paths. No `GET` of file contents, `PUT`,
`DELETE`, `MOVE`, `MKCOL`, `PROPPATCH`, `REPORT`, browser navigation,
auto-redirect, or arbitrary remote URI entry. No production credentials.
Capture only redacted, private receipts (hashed fixture IDs, not raw tokens).

B1-P3 (FUTURE, separate approval): live concurrent mutation experiments
require a **distinct disposable-writer/fixture-seeding authority**; they
cannot be characterized as a read-only run. Until approved and observed,
cross-level coherence is `UNKNOWN`, never `VERIFIED`.

B2 source publication and C/D UI/activation are later unrelated gates;
B1 proof does not enable root switching, scheduler or canonical Vault writes.

## Proposed future read-only probe contract (NO transport implemented)

- Allowlisted owner-provisioned canonical HTTPS origin and exact account DAV
  base; no browser-supplied endpoint, no redirects, no DNS fallback/cross-host
  credential forwarding; credentials only in service-private headers.
- Only `PROPFIND`, explicit `Depth:0` for self-resource identity and
  `Depth:1` for complete immediate children; never `Depth:infinity`.
- Request properties: `d:resourcetype`, `d:getetag`, `oc:fileid`,
  `oc:id`, `oc:permissions`, `nc:mount-type`,
  `nc:is-mount-root`, `oc:share-types`. Additional child count properties
  may be recorded only as hints; they do not prove exhaustiveness.

Illustrative **offline request body only** (no endpoint, transport or credentials):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns"
            xmlns:nc="http://nextcloud.org/ns">
  <d:prop>
    <d:resourcetype/>
    <d:getetag/>
    <oc:id/>
    <oc:fileid/>
    <oc:permissions/>
    <oc:share-types/>
    <nc:mount-type/>
    <nc:is-mount-root/>
  </d:prop>
</d:propfind>
```

Missing values and individual `propstat` 404 responses are expected
possibilities. They must be recorded as unsupported/unknown rather than
silently converted into validated absence of aliases or mount boundaries.
- Transport limits **enforced during read, before parsing**: proposed
  per-response raw body <= 1 MiB, cumulative selected-tree <= 2 MiB,
  <= 2,048 child entries/directory, <= 16 levels, bounded response time
  and total deadline (precise limits require provider-version qualification).
  Disable response decompression unless decompressed byte bounds are
  independently enforced; reject truncated/chunking failures.
- Status handling: 207 alone is insufficient; validate each `propstat`.
  Any missing REQUIRED identity/mount/alias property, inaccessible child,
  incomplete body, redirect, out-of-root href, unknown canonical ID, or
  returned partial tree is fail-closed / `PROVIDER_PROOF_BLOCKED`.
- The recorded `oc:id` and `oc:fileid` must be checked separately for
  scope, stability under rename/move, same logical object under shares
  and external mounts, and cycles/aliases. `oc:id` uniqueness does not by
  itself prove two mounted paths reference the same underlying object.
- Independent account-source revision and provider-inventory revision
  required. A computed string / ETag copied into every record does not
  establish coherence. All ancestor and sibling observations must be
  reproducibly coherent during the preview-to-save interval.
- Do not reuse effectful `POST /api/v1/nextcloud/connections` for preview,
  and do not change the running Dashboard or existing job status.

## Proof decision register (actual capability remains UNKNOWN)

| Gate | Evidence | Must show | Current |
| --- | --- | --- | --- |
| A | Authenticated server scope | exact account, instance, DAV base | UNKNOWN |
| I | Global canonical identity | stable `oc:id` or justified equivalent, all children | UNKNOWN |
| X | Alias/mount discrimination | shared/group/external and loops cannot hide | UNKNOWN |
| L | Complete listing | independently evidenced exhaustive authorized-view siblings; no hidden/omitted uncertainty | UNKNOWN |
| C | Coherent tree | documented provider snapshot or transaction/fence that survives the preview-to-save boundary | UNKNOWN |
| R | Source revision | connection rotation invalidates preview | UNKNOWN |
| O | Output boundary | no exposed raw path/secret/ID or activation | synthetic-only PASS |

**Admission rule:** No V1/B1 provider compatibility claim unless ALL
A/I/X/L/C/R receive independently grounded real disposable evidence. A
single unproven item keeps provider integration `BLOCKED/NYI`, even when
all offline synthetic source tests pass.

### L — independently grounded listing completeness

The inventory universe is **all immediate children visible to the approved
service account under its authorized DAV namespace**, across every selected
ancestor. This is not the server's unrestricted physical filesystem.
The fixture provisioning authority must separately know the complete
intended account-visible child set, including file IDs, permission boundary
and hidden-child setup, before a future read-only probe. A fixture-specific
oracle can compare the complete response to that expected set and detect a
valid-looking 207 whose body silently omits one member.

**Crucial limitation:** a client cannot infer an omitted sibling solely from
the returned self row, valid IDs or `DAV:getetag`. The fixture oracle can
prove a *finite instance observation*, not a universal completeness
guarantee. A child that exists but is ACL-hidden may not be enumerated:
determine whether the selected-root safety policy depends on that hidden
child. If collision/alias safety cannot be established for the actual
scope, `L=UNKNOWN` and root selection stays blocked. Provider capability
must establish exhaustive authorized-view semantics, independently of
client-supplied `complete=True` assertions. Do not substitute a child-count
property as an authoritative oracle.

### C — cross-level coherence and preview-to-save temporal contract

The required protected interval begins **before** collecting all selected
ancestor and sibling evidence and ends only after the future root-binding
state transition is atomically resolved. The consistency invariant is:
the same authenticated source revision, canonical root identity, all
selected parent/child IDs, sibling-collision inventory and alias/mount
classification must remain valid throughout that interval. A late
request or a successful preview alone cannot authorize a save.

Acceptable *candidate mechanisms for study* (not proven here):
1. A provider-documented server-side snapshot/epoch that covers **every**
   selected directory plus all returned siblings, can be revalidated
   atomically at the future save boundary, and rejects intervening edits.
2. An explicitly approved transaction/lock with documented exclusive
   semantics across the selected provider tree that remains held through
   the future atomic source CAS. Local Job quiescence cannot fence other
   remote Nextcloud writers.

Repeated equal ETags, two identical reads, matching client-computed tokens,
or a final check before an unprotected save are **not** sufficient.
A-B-A mutation between polls, deletion/recreation with the same path,
cross-parent moves, and a mutation immediately after the last check
must be included in an eventual approved disposable race study.
Even passing all finite race tests does not prove a universal guarantee.
If the real provider has neither documented mechanism, `C=UNKNOWN` and
V1/D activation remains disallowed; an alternative weaker per-resource
recheck policy would require a separate explicit design decision and
new safety analysis, not an implicit V1/B1 fallback.

### X — symlinks and aliases

Provider `DAV:resourcetype=collection` and a local/empty
`nc:mount-type` are not proof of the absence of a backend symlink,
hardlink-like alias, mount, share path, or cycle. V1/B1 must explicitly
test symlink-to-in-root, symlink-to-out-of-root, loop, and absent
classification as independent *seeded* cases. If the storage backend
cannot expose trustworthy link classification for **all** siblings,
record `X=UNKNOWN` and deny selection rather than synthesizing
`is_alias=False`.

## Scenario matrix and execution policy

The companion `tests/fixtures/oa303_v1b1_proof_matrix.json` is an
**INERT, NON-EXECUTABLE test specification**; its phase/method fields are
not capabilities, consent, URLs or credentials. It cannot grant authority
to a runner. The separate human approval remains required for disposable
scope and service-private credentials, and any runner must independently
enforce allowlisted instance identity, modern DAV endpoint, paths, TLS and
no redirects. `tests/test_303_v1b1_proof_matrix.py` checks a closed
test-plan schema (including scalar types and nested description maps),
fixed critical coverage IDs **and exact per-case Depth:0/1 or OFFLINE_ONLY**,
explicit phase/prerequisite labels and no unexpected credential/URL/claim
fields. It also checks the offline PROPFIND XML template's exact property
multiplicity, empty values and attribute/text restrictions. These lexical
checks are **defense-in-depth hygiene**, not a credential detector, sandbox
or authenticated runtime enforcement. No socket is opened.

The matrix `expected` values are **future proof-admission expectations**,
not B0 `RootPreview.status` values or observed provider results:
`observe` means collect independently grounded facts (never auto-PASS);
`fail_closed` means the provider-proof gate must reject an unsafe/ambiguous
case (including when V1/A correctly returns `no_change` but connectivity
has NOT been established); `unknown` means no admissible guarantee.
All A/I/X/L/C/R gates remain `UNKNOWN` in this artifact.

Case phases:
- `read_only`: can be observed when immutable fixture is independently
  preseeded and B1-P2 has been separately authorized.
- `seed_required`: data/shares/mounts must already exist or require a
  separately approved disposable *setup* action; NOT B1-P2 activity.
- `mutation_required`: concurrent rename/create/delete or credential
  rotation needs additional approval; do not execute under read-only scope.
- `offline_only`: purely synthetic malformed/legacy response injection,
  source-revision logic and comparison of independently collected,
  separately approved data; no provider request.
- Cross-instance identity comparison is **offline-only** using separately
  authorized, redacted evidence or requires an independent second-instance
  observation approval. A single approved origin never authorizes discovery
  or probing of a second origin.
- Fault injection into real DAV responses is NOT a consequence of ordinary
  fixture seeding and requires separate test-proxy authority; synthetic
  malformed 207 bodies stay offline in this slice.

Minimum cases cover normal listing, missing/forbidden/auth failures,
response/propstat problems, stable identity under repeated read, shares,
external mounts, global ID reuse, Unicode/case aliases, cross-root
path containment, inventory overflow, and concurrent directory changes.
Unsupported provider behavior is a valid **negative feasibility** result,
not a reason to manufacture resource IDs or change V1/A policy.

## Exit criteria and retained records

R0 completion was local-only: one design + synthetic matrix/tests; no
network, credentials, service writes, provider snapshot or Issue update.
Subsequent owner approval permits source-only Commit/Push/Draft PR for R0/R1,
but does not expand provider, deployment or activation authority.

Future read-only proof receipts must pin server version and request/response
schema; list scenario IDs and typed failure codes; report observed vs expected
without raw account names, tokens, URLs, paths, or ID values. Keep the
mapping from redacted fixture labels to disposable test data on a separate
approved machine. If any field cannot be proven, record `UNKNOWN` or
`FAILED` explicitly; never transform it into a success boolean.

No new LXC is created in this slice. A disposable instance may eventually be
needed, but its provisioning and lifecycle are new owner decisions.
