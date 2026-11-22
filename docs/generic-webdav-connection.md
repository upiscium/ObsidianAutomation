# #601 — Provider-neutral WebDAV connection foundation (URL/API V1)

This change is source-only until parent creation and independent review / isolated Apache + Nextcloud acceptance are complete. No authorization for live credential changes, target migration, Vault writes, unattended timers or #306 E2E.

## Scope delivered

- Accept a canonical HTTPS WebDAV directory URL ending with /, e.g. https://dav.example.invalid/webdav/, instead of requiring a Nextcloud-specific /remote.php/dav/files/account/ path.
- Preserve the persisted credential keys and legacy [nextcloud] rclone remote name, without migrating stored credentials.
- Generate rclone WebDAV vendor = nextcloud for the traditional Nextcloud path, and vendor = other for generic paths. This is an adapter mode, not an identity/security proof. Official reference: https://rclone.org/webdav/
- On connection save, issue one read-only PROPFIND Depth:0 with a bounded 64 KiB response and require the exact Vault collection href, HTTP 207, and one successful resourcetype collection propstat. Redirects, malformed XML, wrong path, files, ambiguity and oversize fail closed.
- Apply the same URL validation to the per-field editor and full-provider save. The Nextcloud-named internal API route and fields stay for backward compatibility.
- Label the current common connection section WebDAV rather than Nextcloud. The OC-ID/OCS-specific Vault Root picker remains Nextcloud-only for now.

## Mandatory remaining #601 work

1. Core Promotion nested CREATE still depends on X-NC-WebDAV-Auto-Mkcol. Implement safe parent PROPFIND/MKCOL, preserve strong ETag CAS, checkpoints, unknown-outcome behavior and restart safety.
2. Define explicit capability gates for OC-ID and OCS-based root enrollment when the saved URL points to generic Apache. Do not assume a generic DAV server provides those Nextcloud identities.
3. Run isolated real Apache mod_dav_fs and Nextcloud protocol tests covering file-vs-collection, redirects, nested CREATE, ETag conflicts, interruption, 201/405/409 outcomes, and partial success. Do not use the production Vault for fixtures.
4. WebDAV URL denotes the directory above the separately configured vault_folder. No auto-discovery or server path rewrite is implied.
5. #306 remains BLOCKED until full #601 is implemented, independently reviewed, tested and accepted.
