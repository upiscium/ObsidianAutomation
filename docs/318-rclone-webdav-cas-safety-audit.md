# #318 — Canonical rclone/WebDAV mutation boundary audit

Scope: source-only snapshot `e787c8ee3e27ffd169617c1a6a76d8ef494f9c2e` (branch `task/oa318-cas-safety-20261028`), 2026-10-28. See [Issue #318](https://github.com/upiscium/ObsidianAutomation/issues/318), [#308](https://github.com/upiscium/ObsidianAutomation/issues/308), [#302](https://github.com/upiscium/ObsidianAutomation/issues/302). A later main revision `dac183f0` has four changes unrelated to the scoped writer/rclone modules; this branch was not rebased.

## Observed hazard and boundary

A **disposable** Nextcloud 35.0.1 / rclone 1.75.1 fixture previously showed `copyto --header-upload 'If-None-Match: *'` issuing a **DELETE after HTTP 412**, removing an existing canonical file. That evidence is **not** a finding of production loss or present source usage.

Do not use rclone's generic upload, move, conditional-header upload or cleanup to mutate canonical Vault data. Its remote filter files are for selecting downloaded content, **not** a remote write-authorization boundary. For authoritative mutation, keep typed WebDAV requests with verified strong ETag and exact-byte readback; ambiguous or rejected PUT must never trigger a compensating DELETE.

## rclone command sources and production-command exposure

| Source / entrypoint | Inspected operation | Classification |
|---|---|---|
| `src/obsidian_automation/vault_mirror.py:refresh_pull_only_mirror` | `rclone sync <remote> <local mirror> --delete-after --delete-excluded` behind canonical/read-view locks | **Not affected in source**; deletion flags operate on **local** mirror, not canonical remote |
| `src/obsidian_automation/snapshot_adapter.py:SnapshotAdapter.__call__` | `sync, sync, check` from remote Vault to new isolated local `runs/<run>/mirror`; `--delete-after` local | **Not affected in source**; private Git snapshot push is a distinct backend |
| `src/obsidian_automation/github_daily_setup.py:pull_project_notes` | `rclone copy` from `10-Project` remote to disposable local capture | **Not affected in source** |
| `src/obsidian_automation/vault_format_source.py:pull_system_markdown` | `lsjson`, `copy`, `check` from `98-System` remote into bounded local source | **Not affected in source** |
| `src/obsidian_automation/snapshot_setup.py:_rclone_config` | `rclone obscure -` encodes input locally; no canonical I/O | **Not affected in source** |
| `tools/install_snapshot_lxc.py`, `tools/private_config_transfer.py`, `tools/host_runtime_lifecycle.py`, `examples/{ai,github-sync,promotion}/*.service` | Local config/service deployment and module entrypoints, not direct conditional remote-upload command construction in inspected source | **No unsafe path identified in source** |
| Installed production units, private rclone config and runtime processes on actual LXC hosts | No live production inspection permitted for this task | **Unverified**; this source audit does not certify installed settings |

No `copyto`, `moveto`, `deletefile`, `purge`, or `--header-upload` command token was found among tested source rclone invocation paths. `tests/test_318_rclone_pull_boundary.py` captures actual argv for the four remote-read paths and adds a fail-closed inventory for approved Python rclone command owners/callsite counts plus systemd and shell entrypoint scans. Negative fixtures deliberately inject Python uploads (PATH, absolute path, and shell-command string), an extra upload inside an approved function, direct or quoted systemd commands, a systemd drop-in override, shell scripts with or without extensions, and a GitHub workflow command. These cases must be rejected before a new callsite can be accepted. The test remains a bounded source tripwire, not proof against arbitrary dynamically assembled commands or unseen installed overrides. Static inspection cannot prove safety of arbitrary dynamically assembled commands or an installed runtime not represented in the checkout.

## Typed canonical mutation writer inventory

| Writer boundary | Conditional authorization | Classification |
|---|---|---|
| `webdav_create.py:conditional_create` | HTTP `PUT If-None-Match: *`; successful create followed by exact-byte GET | **Used as typed HTTP**. Rejects 412 without compensating DELETE |
| `core_promotion_transport.py` | Planned `create: PUT If-None-Match: *`, `update: PUT If-Match: <strong ETag>`, **explicit plan-authorized** `delete: DELETE If-Match: <strong ETag>` | **Used as typed HTTP**. Valid plan DELETE is not a failed-PUT rollback and must remain supported |
| `managed_promotion_transport.py` and `core_live_reconciliation.py` | WebDAV conditional HTTP preconditions / preflight | **No rclone mutation identified**; independent existing tests continue to apply |
| `vault_format_writer.py` | Conditional `PUT If-Match`, readback and source authority | **No rclone mutation identified** |
| `github_daily_progress.py`, `github_project_overview.py`, `github_project_status_mutation.py` | Conditional WebDAV PUT / ETag and readback | **No rclone mutation identified** |
| `human_projection_cleanup.py` | Dedicated guarded projection DELETE | **Separate authorized deletion feature**, not compensating deletion after PUT; no blanket DELETE prohibition |
| Other installation-specific writers not represented in this source snapshot | Would require read-only production command/config inventory under an appropriate separate grant | **Unverified** |

## Synthetic regression evidence

- `tests/test_318_rclone_pull_boundary.py`: actual argv captures for mirror/snapshot/Daily/formatter, remote-to-local direction, forbidden upload command tokens, approved Python command owners/callsite counts, service/shell invocation scans, adversarial upload-entrypoint fixtures, and legitimate local `--delete-after`.
- `tests/test_318_webdav_rejected_put.py`: disposable loopback fixture checks byte-for-byte and SHA-256 preservation on 412, 401, 403, 503, ambiguous lost reply and synthetic timeout. A partial two-file create confirms a preceding accepted file remains while conflicting canonical bytes stay unchanged. All rejected paths assert exactly one PUT and **no DELETE**.
- `tests/test_core_promotion_transport.py`: conditional `If-Match`/strong-ETag stale update and `If-None-Match` racing create checks preserve competing remote bytes, stop mutation after rejection and leave the checkpoint unchanged. Existing success test continues to prove explicit plan-authorized Core Promotion DELETE works.
- No live Nextcloud, Vault, credential, production service, Gitea, remote rclone upload, Gateway or release/deploy operation is required or used in these regressions.

## Result and remaining gates

The audit found **no current source-side rclone canonical-upload call** and the new fixtures did **not** reveal a production-code defect requiring a writer modification. Preserve current typed HTTP implementation; do not add a generic CAS adapter or disable authorized Core Promotion DELETE.

Before production acceptance, separately compare the deployed unit/config command lines with the checked-in sources, run CI on the eventual approved PR, and obtain independent correctness/security reviews. This audit/test diff is **uncommitted** by design and not integrated into main. No production permission or installed runtime authorization is implied.
