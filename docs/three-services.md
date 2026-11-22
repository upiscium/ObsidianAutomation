# Three-service cutdown — implementation and production gate

**Status:** source-only implementation on an isolated branch. **No deployment,
Vault mutation, credential rotation, GitHub Core push, or existing timer
disablement is authorized by this document.**

## What remains

- **Snapshot:** Existing `SnapshotJob`, `SnapshotAdapter`, persisted
  `snapshot.sqlite3`, nonroot Gitea credential and verified readback.
- **GitHub Daily:** Existing `DailyJob`, deterministic Preview, bounded
  source capture/verification, CAS/conditional section-only Daily writer.
  `--daily-publish` is still required for actual Daily publication.
- **Core Promotion:** New `CorePromotionJob`, source binding, OFF-by-default
  durable SQLite schedule, explicit private checkpoint bootstrap and existing
  managed `run_promotion_cycle`. The old write transport retains policy SHA,
  content-addressed plan/receipt, conditional ETags, and live-drift detection.
- **Management:** One HTTP server with precisely three workflow routes,
  one-use CSRF, strict Host/Origin rules, bounded requests, private bind only,
  fixed Nextcloud/Gitea/GitHub connection endpoints and no generic executor.

## Removed from the active profile

AI Knowledge and Review, ChatGPT Export/External Source, GitHub Project Writer
and B3 observer, Vault Formatter, old root Operator/systemd console, phase
scheduler, arbitrary-workflow support and their WebUI cards/routes. The Daily
GitHub **read-only collector** remains because Daily needs source evidence.
The existing **Gitea Public Publisher** remains an external Snapshot→Core
projection dependency; there is no independent publisher management panel.

Retained internal utilities are not automatically active jobs. Their presence
must not be mistaken for authority to run abandoned workflows.

## App control contract

| API | Behavior |
| --- | --- |
| `GET /api/v1/{snapshot,core-promotion,daily}` | Per-job persisted settings, last attempt and result |
| `POST /api/v1/{name}/settings` | Set enabled/interval; refuses invalid or unconfigured |
| `POST /api/v1/{name}/runs` | Durable request-ID admission; HTTP 202 is **not** effect success |
| `GET /api/v1/{name}/runs/<run_id>` | Query exact result of the attempt |
| `GET /api/v1/core-promotion/configuration` | Read-only readiness/policy SHA |
| `POST /api/v1/core-promotion/bootstrap` | Explicit previously reflected Core baseline; no Vault PUT |
| `GET /api/v1/session` | One-use CSRF token |
| `GET/POST /api/v1/*/connections` | Fixed credential setup with bounded validation |

Routes for Formatter, B3, AI, ChatGPT Import, old systemd operations, and
`/api/v1/workflows` are **not mounted**. All mutable routes require an Origin
matching the bound HTTP Host and a one-use CSRF token. No arbitrary host path,
unit name, command or script is accepted.

## Core Promotion safety

1. The internal publisher policy bytes must equal the bundled reviewed
   `public-export.toml`; invalid/missing policy is not silently recreated
   after a mismatch.
2. Core Promotion starts **OFF**; a missing checkpoint, mismatched policy,
   missing Nextcloud connection, mismatched private Vault-target seal or
   changed source fingerprint refuses enabling. The durable target seal
   includes the server, account, selected Vault folder and enrolled root pin.
3. Checkpoint bootstrap must use a complete 40-character ancestor commit
   whose subject matches the generated Gitea public projection, and which is
   independently confirmed reflected in the actual canonical Vault. The
   source code cannot prove this last fact automatically.
4. Vault-target changes performed through the connection UI create a
   persistent quarantine marker **before** the new connection is saved.
   Switching back to the prior target does not clear it. The old checkpoint
   must be independently reconciled and explicitly reauthorized; deleting
   the marker to bypass validation is prohibited. Credential-only changes
   for the same target do not invalidate the sealed Vault identity.
5. Every run reuses the existing exact policy/plan/conditional WebDAV transport.
   The `CorePromotionJob` is an admission/status wrapper, not a replacement
   for conflict checks or canonical readback.
6. On crashes, lost responses or an unclassified exception, result is
   **indeterminate**. No automatic replay or new request ID is allowed until
   the operator has reconciled the private checkpoint, receipt and Vault state.
7. Separate Snapshot and Daily jobs remain operational if Promotion is
   unavailable or fails. Do not treat a public Core Git SHA as proof that
   Live Vault has been updated.

## Production migration acceptance — not yet executed

The current `obsidian-control` unit is `obsidian-snapshot.service`,
running from a versioned release under `/opt/obsidian-snapshot/releases/`
as nonroot `obsidian-snapshot` with persisted
`/var/lib/obsidian-snapshot`. Its **currently installed command includes**
`--vault-root-ui --vault-bindings-ui`; those options must be removed from
the new ExecStart, or the new binary would reject the command at startup.

Before any cutover, in this order:

1. Record exact running binary/source SHA, current B2/B4 status/settings, the
   ongoing run IDs and their terminal receipts. Inspect root/mapping pins and
   the selected Vault root without exposing secrets.
2. Verify no legacy Core Promotion canonical writer is still enabled or able
   to restart. Stopping an LXC once is not proof of credential revocation.
3. Preserve existing state/SQLite credentials, policy, immutable receipts,
   publisher access and full old release path. **Never purge live state.**
4. Build and stage the new source in an isolated release; run new component
   and negative-route tests. Do not modify the live unit yet.
5. Qualify Core Promotion's exact policy and generated baseline against the
   actual Vault, using read-only checks. Only then bootstrap its **new private**
   checkpoint and explicitly enable the schedule if approved.
6. Update the service unit's ExecStart on an approved cutover, preserving
   `--state`, bind address, and `--daily-publish` only if the existing Daily
   Writer authorization is still valid. Omit the two retired Vault UI flags.
7. On restart, verify B2/B4 schedules, Run Once admission, eventual persisted
   result and actual Gitea/GitHub/Nextcloud readback, then Core Promotion
   initial no-op/changed-policy scenarios and conflict refusal. Test desktop
   and Android WebUI. A mock-only or `202` response does not pass E2E.
8. On failure, disable newly admitted jobs, reconcile any indeterminate
   remote effect first, and restore the previous source/ExecStart without
   deleting newer canonical Vault data. Do not operate both old and new
   canonical writers concurrently.

The old separate Gitea Publisher/Runner may be required for the
`ObsidianVault` → GitHub `ObsidianCore` projection. Keep it outside the
three-panel UI but **do not stop it as an incidental consequence** of the
code purge.

## Deferred

No cross-Vault mapping editor, generated Daily/Monthly/Recurring/Subscription
jobs, AI enhancement, full-Vault Formatter or new infrastructure. They may be
reintroduced only as independently scoped requirements.
