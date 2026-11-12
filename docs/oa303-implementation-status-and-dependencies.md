# #303 implementation status and dependencies (R0)

Base `main@1248ee85156f` (2026-11-12). #298, #301, #302, #303 and #304 are OPEN.
The newer #298 product contract (2026-10-24) applies: one fresh LXC, one
non-root service, one shared Nextcloud account. The 2026-10-11 #311
integration-branch rule at the top of #303 is historical and is not reused.

Legend: **merged** = on main · **PR #578** = Draft, unmerged ·
**wired** = reachable from the running Dashboard (`main()`).

## Components

| Area | File / function | State | Wired | Evidence |
| --- | --- | --- | --- | --- |
| V1/A preview | `vault_root_preview.preview_root` | merged #557 | no | synthetic tests |
| V1/B0 parser | `vault_root_dav_fixture` | merged #563 | no | synthetic only |
| V1/B0 assembler | `vault_root_dav_adapter` | merged #563 | no | synthetic only |
| V1/B1 oracle/plans | `vault_root_proof_oracle`, docs, catalogs | merged #566/#568/#570 | no | finite synthetic oracle |
| Disposable provider | Adam rootless Nextcloud 35.0.2 | running (owner host) | — | P1-B/F1/P2 receipts (digests pinned); P3-D1..D5 receipts (see ADR) |
| V1/B2 verifier | `vault_root_dav_verifier` | **PR #578** | no | mock-transport tests |
| V1/B2 transport | `vault_root_dav_transport.HttpsDavTransport` | **PR #578** | no | fake-connection tests, no sockets |
| Nextcloud-only revision | `SetupStore.nextcloud_revision` | **PR #578** | no | tests |
| V1/C service | `vault_root_selection.VaultRootSelectionService` | **PR #578** | **no** (default OFF) | service + HTTP tests |
| V1/C routes | `admin_dashboard` `/api/v1/vault-root*` | **PR #578** | only if a service is passed; `main()` passes none | OFF = 404/405 |
| V1/C UI | `webui_design_v1/vault_root.js` | **PR #578** | script loads, inert when OFF | static tests |
| V1/D CAS | `SetupStore.replace_vault_folder` | **PR #578** | no | tests |
| V1/D activation/rollback | `vault_root_activation` | **PR #578** | no route, disabled by default | tests |
| V1/D Job pre-run pin | `vault_root_activation.check_root_pin` | **PR #578** | **not called by any Job** | tests |
| Trusted context builder | SetupStore → `TrustedRootContext` (needs pinned instance ID + account-root `oc:id`) | **not implemented** | — | blocked on A |
| Phase 2 `vault_bindings` | — | not started | — | deferred |

## Who reads the Vault root

Everything reads `vault_folder` from `SetupStore` (`connections.json`).
`rclone.conf` holds only the DAV base, so changing the root does not touch
credentials.

| Consumer | Reads via | On change today |
| --- | --- | --- |
| Snapshot (B2) | `SetupStore.connections()` / `source_connections()` | `connections_saved()` disables the schedule and clears the Gitea write verification |
| GitHub Daily (B4) | `github_daily_deterministic_writer` | Dashboard sets `enabled=False` |
| Formatter | `vault_format_source` (source generation) | `connections_saved()` sets `mode=off`; a generation mismatch also revokes |
| B3 Observer | source generation | settings become `effective_enabled=false` on a generation mismatch |
| Canary binding | `vault_format_canary_binding` (fixed `ObsidianVault`) | refuses any other folder (fail-closed) |

**Lock order** (from `POST /api/v1/nextcloud/connections`): Formatter
`_write_gate` → Snapshot `_lock` → Daily `_lock` → Formatter `_lock`.
`DashboardJobFence` adds B3 `_lock` last.

## Regression suites for this change

`tests/test_303_*.py` plus every test that imports `admin_dashboard`,
`vault_root_*` or `webui_design_v1`. That covers Nextcloud-only setup,
Snapshot setup, #301 TOCTOU, #304 setup UI, global menu, workflow
contract, Formatter/B3/B4 HTTP, and packaging.
