# #303 V1/B — synthetic-only Nextcloud DAV evidence assembly (nonclosing)

Scope: Source-only, unconnected prototype (Draft PR publication approved).
Existing V1/A root preview is the only policy decision engine; V1/B translates verified internal
page records into DirectoryEvidence. No HTTP/WebDAV request is made.
No Dashboard import, new UI, endpoint, credential loader, provider transport,
database change, production service, activation, or Vault write.

## Bounded synthetic-only implementation

The function preview_from_verified_dav_pages() accepts only server-internal
RootSelection and tuple[VerifiedDavPage,...]. It returns only the
fixed/redacted RootPreview schema and never exposes inventory or resource IDs
to an HTTP consumer.

- Rejects anything other than exact HTTP 207; all redirects, non-boolean
  redirect flags and missing provider-verifier attestations fail closed.
- Rejects mismatched namespace, parent ID/path, source revision or snapshot
  token across pages/levels. A matching string does not prove actual
  provider snapshot coherence.
- Allows bounded forward pagination: max four pages per selected level,
  exact cursor linkage and terminal completeness, no extra pages or loops.
  Max 2048 entries per directory, reported 512 KiB per page, 2 MiB across
  request and max 16 selected levels.
- Each entry requires claimed verifier-proven canonical identity and
  explicit alias classification. V1/A ultimately rejects files/symlinks,
  aliases, globally repeated IDs, Unicode canonical/compatibility/legacy
  aliases, wrong types, incomplete lists, stale paths and active jobs.
- The no-op and job/source guards run before evidence assembly via V1/A.
  Every result retains writes_vault=false, activation_allowed=false and
  requires_explicit_activation=true.

These booleans, snapshot tokens, and identity/revision strings are merely
claims on internal objects. The tests fabricate them. Arbitrary Python
code could construct these records, and the module has no runtime wiring.
It MUST NOT accept browser-authored records, be treated as proof of DAV
provenance, or be used to authorize a live root change.

## Mandatory future trusted-provider feasibility — NOT IMPLEMENTED

1. Establish the service-private owner of the one configured Nextcloud
   account and exact DAV base using existing authority. Never serialize
   credentials, raw paths, URL or secrets. Deny off-origin redirects.
2. Implement bounded authenticated read-only DAV PROPFIND and hardened XML
   Multi-Status verification with byte cap enforced DURING read, strict
   timeout, and bounded allocation BEFORE XML parsing. Reject 401/403/404,
   429, 5xx, redirects, out-of-base href, encoded traversal/slashes,
   duplicate paths, partial results, symlink/mount/alias uncertainty and
   unknown source identity.
3. Verify on a disposable Nextcloud instance whether provider-supplied
   account-scoped, globally unique stable canonical resource IDs and explicit
   alias/mount classification exist for EVERY child, including unselected
   siblings. ETags, names, paths, hashes and guessed inodes do not prove this.
   Provider-specific oc:fileid attributes require separate scope/stability
   evidence; their availability is not assumed.
4. Distinguish verified directory listing epoch/coherence from connection
   source generation. Equality of synthetic snapshot tokens does not prove
   the remote view was frozen. SetupStore.generation() does not support
   Nextcloud-only records; future code requires a separate verified binding.
5. If identity or coherent listing requirements cannot be demonstrated,
   keep V1/B fail-closed and NYI, rather than manufacturing IDs or silently
   weakening the V1/A collision/identity rules. Owner must separately
   authorize any actual provider read-only proof and credential use.

## Next independent gates

V1/A: merged PR #557, offline-only contract.
V1/B here: synthetic-only assembly, no transport or real DAV identity proof.
V1/B later: separate trusted transport and identity verification approval.
V1/C: private read-only UI; short-lived opaque server-side candidate IDs.
V1/D: explicit owner-approved activation with source + directory recheck,
all Job and Writer producers quiesced and rollback; distinct Merge/Deploy/Run
Once approvals. Logical role-to-folder mapping is deferred Phase 2.

Current obsidian-control release and its Jobs remain untouched.
#303/#301/#304/parent #298 remain Open. Draft PR publication of this
synthetic-only code is separately owner-authorized; no Ready/Merge, new LXC,
account, provider calls, Deploy or activation.
