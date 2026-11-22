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

1. Core Promotion nested CREATE now preflights and PROPFIND-checks the configured root and every ancestor, creating only missing parents via standard MKCOL. The optional legacy Nextcloud auto-MKCOL header is no longer the only parent-creation mechanism. Synthetic tests cover 201, 405 with revalidation, 409 fail-closed, missing root, parent-file collision, lost response and checkpoint preservation. The independent security review and real-server acceptance are still pending.
2. Generic DAV does not provide trusted OC-ID/OCS. Explicit capability gates now block Nextcloud-specific root enrollment, activation and rollback, and canonical JobRootGuard admission under generic URLs. These guards are intentionally conservative: a separate **provider-neutral root authority contract** will be needed for an operator to use a generic DAV target with all three Jobs.
3. Local Apache httpd 2.4.71/mod_dav_fs over loopback HTTPS was verified to return 207 with a valid collection for an isolated fixture. An attempted complete promotion E2E failed at UPDATE preflight because the client did not observe a strong ETag on the immediately seeded fixture; a subsequent read-only GET did return strong Apache ETags. This is NOT an E2E PASS. Repeat this in a controlled reproducible fixture and then test Nextcloud, nested CREATE, ETag conflicts, interrupted operations and retries before Ready/Merge/Deploy. No production Vault or credentials are used.
4. WebDAV URL denotes the directory above the separately configured vault_folder. No auto-discovery or server path rewrite is implied.
5. #306 remains BLOCKED until full #601 is implemented, independently reviewed, tested and accepted.
