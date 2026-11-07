# #303 V1 — shared Nextcloud Vault-root selection (bounded source-only contract)

Status: **source-only Draft PR publication authorized, nonclosing**.
The owner separately approved the reviewed three-file V1/A Commit/Push/Draft
PR after the offline tests and independent correction review. **This does not
authorize Ready, Merge, Deploy, activation, WebDAV/Nextcloud I/O, credential
changes, production config changes, Vault mutation or scheduler/Job effects.**

## Decision and current code baseline

- The newer #298 UX-first goal (2026-10-24) is a **single LXC / service /
  shared Nextcloud account / WebUI**. #303 still contains an older 2026-10-11
  mega-branch/#311 gate. Do not automatically adopt that old gate or silently
  rewrite #303's historical text. Reconfirm the PR base at publication time.
- Current `SetupStore` owns one verified shared Nextcloud record and protects
  secret data in `connections.json` / `rclone.conf`. `vault_folder` is one
  user-entered relative folder (1–100 characters, restricted ASCII alphabet,
  no empty/dot parent components) shared by Snapshot/B4/Formatter. This is
  **already a path-setting mechanism**, not a safe interactive Vault picker.
- `GET /api/v1/nextcloud/connections` returns only version, configured,
  scope and writes_vault=false. It provides no folder identity, enumeration,
  generation or "connected" proof. The existing Nextcloud POST accepts **all
  four secret-bearing fields**, probes, then may pause/rebind dependent jobs.
  **Do not reuse this POST for read-only root preview.**
- `SetupStore.generation()` is unavailable for Nextcloud-only configuration
  because it requires a *full* Gitea connection. A future preview revision
  must be derived separately from canonical verified Nextcloud state with
  an opaque, short-lived server-side reference. Never return a raw password
  hash, private `connections.json` content, rclone config or bearer token.
- #301/#550 Gitea-only rotation correction and #552 Gitea form were source
  merged. The current active `obsidian-control` release has a specifically
  qualified **no-Formatter-Writer** compatibility lock arrangement, not the
  full upstream Writer gate. #303 preview must not change that deployed
  source, infer remote ACLs, enable a Writer, or start jobs.

## V1 authority and data flow

1. User selects a single **Vault-root candidate under the configured shared
   Nextcloud account**. No separate Nextcloud account and no arbitrary path or
   backend URI entry. The user is not selecting a logical destination for B1/
   B3/B4; existing `00-DailyNote`, `10-Project`, `11-Knowledge`,
   `04-AI` and `98-System` roles remain fixed in V1.
2. **Future provider adapter** (not implemented in this slice) reads the
   canonical existing shared account from a service-private source, verifies
   an exact bound account base and session revision, performs bounded,
   read-only DAV `PROPFIND`/directory enumeration under that base, forbids
   redirects / aliased collections, and returns **complete** immediate
   siblings plus stable canonical resource identities for each component
   in a trusted internal `DirectoryEvidence`. Fail closed if resource identity
   or alias/redirect status cannot be established; do not claim that file
   names alone prove identity. Never accept browser-supplied `DirectoryEvidence`.
3. **Pure offline validator** `vault_root_preview.preview_root()` consumes
   internal, **server-created** `RootSelection` and immutable
   `DirectoryEvidence` only; no file/network/process access. Exact parent
   chain, server root, source namespace/revision, authoritative canonical
   resource IDs and full sibling inventories are mandatory. **All observed
   child IDs are unique globally across all supplied parent listings**, not
   merely within each parent or among selected components. Reject absent
   folder, traversal, cross-account root, casefold/NFKC collisions,
   symlink/alias/identity loops, file/unknown target, missing evidence,
   stale revision and active jobs. A same-root request returns `no_change`
   **without** asserting remote availability.
4. Preview output is a **finite redacted status**:
   `{version, status, reason, writes_vault:false, activation_allowed:false,
   requires_explicit_activation:true}`. It contains **no** root path, account
   name, private token, resource ID or inventory. Its `candidate_valid`
   state is neither approval to change a root nor a proof that the provider
   has been contacted by this prototype.
5. **Future UI/API** must separate candidate discovery, read-only preview,
   and *separately authorized* activation. An eventual candidate-label
   view may show allowlisted folder names via safe text nodes only on the
   private ingress, with fixed pagination and no secret URL in logs. Use
   short-lived server-bound opaque selections rather than raw WebDAV URLs or
   client-supplied identity objects. Existing Host/Origin/CSRF protections
   and bounded timeouts apply. Listing failures are typed and nonleaking.
6. **Future activation**, NOT V1-source-only work: recheck current connection
   revision and actual server resource identity; quiesce Snapshot, Daily,
   Formatter and any B3/AI/Writer producers sharing the source; preserve
   existing CAS/review receipts and old mapping; preview impact/rollback;
   require explicit human save confirmation; only then perform versioned,
   atomic source update under the admitted job/Writer gate appropriate to
   the *actual* runtime. Race/timeout/uncertain commit must not be treated
   as successful activation. Restore jobs only under separately approved
   schedule authority. No silent PUT/DELETE/move of existing Vault files.

**V1 default behavior:** the configured `vault_folder` remains authoritative.
The offline prototype is **not imported by the running Dashboard**, and no
new HTTP route, Nextcloud query, credential cache or state file is created.

## Bounded synthetic implementation now

Files:
- `src/obsidian_automation/vault_root_preview.py` — typed, deterministic
  fail-closed offline preview and redacted result. All inputs, including
  root identity, source revision and job state, are **internal server-derived
  contract objects**, not HTTP payloads.
- `tests/test_303_v1_vault_root_preview.py` — synthetic allow/deny matrix,
  source-revision mismatch, full sibling inventory, NFKC case-fold collision,
  symlink/alias/identity loops, different namespace, active job, no-change,
  identifier limits, no raw fields in result, no filesystem/network/process I/O.
- This document — scope, future integration gates, policy conflict and limits.

The **new-candidate** validator is deliberately more conservative than
legacy `validate_nextcloud`: it rejects dot-prefixed directory components
and paths deeper than 16 segments. The **active** root is independently
validated under the legacy grammar (including dot-prefixed and >16-component
paths) so users can preview migration *from* an existing valid setup, and
same-root no-ops still work. No automatic migration is performed. Each
directory listing is capped at 2,048 children; each child label is capped
at 255 characters **before** Unicode NFKC/case folding. The future adapter
must also cap raw HTTP bytes and enforce deadlines before allocating or
parsing listing evidence.

## Implementation gates — future only

| Gate | Unit of work | Proof required | No-go / authorization |
| --- | --- | --- | --- |
| V1/A | This offline contract | Synthetic Python tests, no-I/O assertions, source review | No provider/UI/credential effects |
| V1/B | Authoritative read-only directory adapter | Disposable DAV fixture; bounded enumeration; 401/403/404/429/5xx, redirects, cursor/limit, case/symlink/alias/ETag tests; zero remote writes | Human source-only approval, no production credentials |
| V1/C | Dashboard read-only selection/preview UI | Correct saved-source revision, opaque candidate ID, no leakage, race/stale handling, mobile history, no save path; exact HTML/JS and CSRF regression | Separate Commit/Push/Draft PR/Ready/Merge gates |
| V1/D | Root binding save/activation plan | Quiescence, source-revision CAS, rollback, no effect on existing canonical notes, fixed ACLs, all jobs and Writer state inventory | Separate effect design + Human activation, never part of V1/A |
| Phase 2 | Logical role-to-folder `vault_bindings` | Versioned schema, CAS/ETag and immutable receipt/path migration inventory, job pinning, explicit atomic adoption | **Deferred**, not silently bundled with V1 |

## Acceptance and limitations

- **Proven now:** the pure preview logic is deterministic, fails closed on
  synthetic invalid evidence and stale source, never returns a raw path/secret
  and does not invoke external I/O.
- **Not proven now:** actual Nextcloud root enumeration, authenticated DAV
  `PROPFIND`, stable globally unique canonical resource identities, aliases/
  redirects and hardlinks/mounts across provider versions, real folder
  existence or authorization to select that root, provider inventory
  freshness/coherence (a **connection** revision is not necessarily a
  **directory** revision), UI/phone rendering, downstream path remapping,
  safe production credential update or any canonical Vault write. If
  Nextcloud cannot supply authoritative identity/alias evidence, V1/B must
  remain blocked rather than inventing IDs from paths or ETags.
- No token or password samples from the user's current environment are used
  in tests; all fixtures are fabricated `.invalid` domains.
- B1/B3/B4 and Core Promotion path semantics are currently not unified by
  this patch. #302 shared-account privilege model and #303's complete
  acceptance checklist therefore remain open.

**Authority:** #303, #301, #304 and parent #298 remain OPEN. Only the
separately owner-authorized V1/A three-file Commit/Push/Draft PR publication
is permitted. No Ready/Merge, activation, provider I/O, Job change or Deploy;
keep the currently running LXC unchanged.
