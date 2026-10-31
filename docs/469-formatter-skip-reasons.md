# Formatter skip-reason observability (OA #469 / Phase 1)

**Status:** source-only candidate; not deployed, not accepted for live writer. Based on
`upiscium/ObsidianAutomation` main commit
`f9f75be45e2d4a7677eecd42d4ade4df070b570d`.

## Reason for the change

On 2026-10-31, the first real, **read-only** Nextcloud Formatter Run Once on
the existing `obsidian-control` LXC returned `no_changes`:
`would_change=0`, `skipped=27`, `errors=0`. Run ID:
`859aad9a67b347e881f3c2cbc2273484`.

The v1 formatter plan internally assigns a per-path skip reason, but the
Formatter Job's persistent record stored only total `skipped`. Its temporary
read-only mirror and plan are deleted on completion. Therefore the 27 historical
reason categories **cannot be recovered from the old receipt**. "No changes"
does not mean all 27 files are conformant.

## Additive, privacy-bounded v1 API

Each newly completed run now stores an optional `skip_reasons_json` SQLite
column containing only a stable, sorted map of **fixed policy reason codes**
to positive integer counts. The existing `GET /api/v1/formatter` and
`GET /api/v1/formatter/runs/<run_id>` response records gain one key:

```json
{
  "skipped": 3,
  "skip_reasons": {
    "unsupported-html-block": 2,
    "unterminated-protected-block": 1
  }
}
```

- The sum of reason counts equals the `skipped` count. On a genuinely
  completed run with no skips, the map is `{}`.
- A row predating the schema upgrade, or a run without a valid completed plan,
  has `skip_reasons: null`. Do **not** infer zero reasons from a legacy row.
- An unknown or malformed reason from the input plan is **not echoed**. It is
  accumulated under `unclassified`. Persisted invalid JSON or invalid
  counts also yields null rather than untrusted text.
- The map never contains filenames, file paths, file bytes, diff hunks,
  provider URLs, credentials, or source plan entries. The last run's plan
  SHA-256 is already available separately.
- Each reason code is from a fixed finite allowlist (or `unclassified`).
  The WebUI displays the sorted count map via `textContent`, not HTML
  injection. Old results show an explicit unavailable message.
- The `runs` table is migrated additively with a nullable column. No
  historical values are backfilled or rewritten. Existing Run Once idempotency
  and manual-always-Dry-run contract remain unchanged.

## Operational boundary

The candidate does not change formatter policy, admissible files, Vault
mutation behavior, the Nextcloud/rclone read path, Gitea Snapshot or Core
Promotion, the shared Scheduler, or a Writer permission. The normal
`snapshot_app` still injects no Formatter Writer.

To obtain a breakdown for the historical 27 skips, independently authorize a
**new** single provider-backed read-only Formatter Run Once **after** reviewed
source publication and a separately gated, backed-up deployment to
`obsidian-control`. No automatic retries or replay of historical Run Once.
Keep mode `Off`; a read-only manual Run Once is sufficient.

Review the resulting reason distribution before deciding whether to expand the
formatter's set of safe transformations. A high skip count is a coverage issue,
**not** a reason to disable fail-closed Markdown safety checks.

## Verification scope

Synthetic fixture tests cover multiple reason buckets, zero-skipped behavior,
legacy schema with 27 unknown reasons, spoofed private reason text, persistent
restart and HTTP JSON privacy. Existing Formatter, Snapshot, WebUI, Daily,
Core Promotion and #318 rclone/WebDAV regression tests are included in the
associated source-only validation. No actual Nextcloud or live Vault was read
during this implementation.
