# #601 — Generic WebDAV compatibility (source-only, production OFF)

## Provider-neutral HTTPS connection

The WebDAV URL is the parent directory above the separately selected Vault
folder (for example https://dav.example.invalid/webdav/ + Vault).
A canonical HTTPS URL is required; embedded credentials, query or fragment,
unsafe path aliases, redirects and ambient HTTP(S) proxies are rejected.
The connection probe requires a bounded 64-KiB PROPFIND Depth:0 with one
matching DAV collection and successful 200 propstat.

Persisted [nextcloud] remote name and historical credential field keys remain
compatible. Classic Nextcloud files endpoints use rclone vendor=nextcloud,
and generic URL paths use vendor=other. This path-based hint is NOT reliable
provider identity. Reference: https://rclone.org/webdav/

## Core Promotion and manual repair

After whole-plan preflight, CREATE verifies the configured Vault root and
every ancestor through exact PROPFIND, and uses ascending MKCOL only for
missing approved parents. HTTP 201 and racing 405 require a fresh matching
collection read; 409, files where directories belong, missing root, unknown
responses and transport loss fail closed. A partial operation is not
automatically retried or deleted. Final files retain If-None-Match:* on CREATE,
strong If-Match for UPDATE, exact-byte GET verification, and checkpoint
nonadvancement after incomplete effects. A Nextcloud auto-MKCOL header is
retained on PUT as an optional compatibility hint, never a precondition.
Manual Core Live Reconciliation CREATE also uses the same parent verification,
and persists an attempting/failed receipt before an incomplete effect.

## Generic Root authority: explicit and weaker than Nextcloud

Nextcloud has stable OC-ID and OCS user binding. Standard DAV does not expose
an equivalent persistent resource identity. For generic DAV, the existing
Root enrollment action must be invoked explicitly to register the
HTTPS URL, account and configured Vault path after an authenticated,
read-only exact-collection probe. A private 0600 proof records the path-based
authority and a durable source epoch. Every provider-connection or Vault
folder save rotates the source epoch, invalidating prior authorization even
after A -> B -> A changes. Missing, malformed or symlinked proof, an uncertain
old OC-ID pin, or an offline/unverified collection blocks Job admission.

Every canonical Job re-probes the same authenticated HTTPS collection just
before acting and refuses stale or missing authorization. The Nextcloud OC-ID
candidate picker, folder activation and rollback remain unsupported under
generic DAV, and the UI disables those controls. Root path binding cannot
prove that the server has not swapped the directory inode behind the same
hostname/path. This weaker guarantee requires separate operator acceptance;
merging source alone is NOT production authorization.

## Reproducible verification and remaining gates

Python synthetic tests exercise the validation and XML bounds, invalid
collection, 201/405/409, file collision, network loss, partial MKCOL,
conditional updates, repair failure receipt, root epoch and rotation.
An optional local integration script starts real Apache 2.4.71 mod_dav_fs
with localhost-only HTTPS and Basic authentication, creates a disposable
synthetic Git/Vault fixture, and verifies nested CREATE/UPDATE/DELETE,
strong conditional 412 rejection, exact content, checkpoint and receipts.
It also observes actual 409/201/405 MKCOL statuses and cleans the server,
TLS certificate and synthetic credentials afterwards.

Apache can briefly issue a weak W/ ETag immediately after a fixture file is
created. Core Promotion correctly refuses UPDATE in that state; the
integration fixture uses older modification times to obtain stable strong
ETags without weakening production CAS requirements.

Nextcloud-format synthetic regression tests pass; actual isolated Nextcloud
protocol E2E and independent security review are still pending. The
production provider, Vault, credentials and Scheduler remain unchanged.
#601 stays OPEN and #306 stays BLOCKED until final acceptance.

## Real Nextcloud synthetic E2E (2026-11-23)

tests/integration_nextcloud_webdav.py runs against a disposable Nextcloud
35.0.2 container (rootless Docker, localhost only, sqlite, synthetic admin).
Passed: strict PROPFIND href match, full promotion (update, nested CREATE
without existing parents, delete, receipt, checkpoint), If-Match/If-None-Match
protection, awkward names (parentheses, apostrophe, "@", "%" in the file
name, non-ASCII), and read-only refusal of a file used as an ancestor.

Finding: servers re-encode the same resource differently (Nextcloud returns
"(" and "@" raw, %27 and %21 encoded, lowercase hex). The href check now
compares per-segment canonical encodings; %2F, %5C, control characters,
invalid UTF-8 and dot segments are still refused.

Operational notes: any provider connection or Vault folder save rotates the
generic Root epoch (so a password change needs re-enrollment); WebUI-supplied
HTTPS hosts are not restricted to public addresses (administrator-only
surface).

## WebDAV root as the Vault (#607)

`vault_folder = "/"` means the generic WebDAV URL itself is the Vault root
(for example `https://vault.example.invalid/` served from a DocumentRoot that
is the Vault). It is accepted only for generic URLs: any Nextcloud-style path
(`remote.php`, `/dav/files/`, `/index.php`, any case/percent spelling or
sub-path) is refused, because there the account root must never become a
Vault. An empty value stays invalid. Snapshot syncs `nextcloud:/` (a named
folder is unchanged), Core Promotion and Daily use the URL as the Vault base
with no extra prefix. Verified against real Apache (nested CREATE/UPDATE/
DELETE and a real `rclone sync nextcloud:/`) and the Nextcloud regression E2E.
Scope note: with `"/"` everything under the URL is mirrored to the private Git
repository, so the server must expose only the Vault there.
