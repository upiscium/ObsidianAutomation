# #304 R1/A — guided first-run connection setup (source-only)

Status: local source-only prototype; not committed, pushed or deployed.

## Why

The approved Obsidian Status Manager has a read-only readiness checklist
under hamburger > Global System Settings. It already contains original
Nextcloud/Gitea/GitHub connection forms and guarded save handlers.
However, the steps for initial configuration are not explained in order.

The owner accepted the black theme, phone Back operation and simplified
Global-only Settings menu. This slice keeps those UX decisions unchanged.

## Guided flow

Inside the existing collapsed "初回セットアップの確認" panel, show
three locally defined steps:

1. Nextcloud: configure the shared Vault Nextcloud account.
2. Gitea Snapshot: **optional**, only if Vault Snapshot is in use.
   It must not block an unrelated GitHub Daily workflow.
3. GitHub Daily: optional if Daily Progress is in use.

Previous/Next changes the explanatory step **without** touching the
browser's page History API, automatically fetching status or submitting
forms. "設定フォームを開く" scrolls/focuses the original connection
section. The existing explicit "接続設定の状態を確認" button fetches
only the three existing redacted GET status DTOs and refreshes the
selected step's status label. This indicates a saved configuration or
"verified when saved", not current endpoint health, Vault mutation,
or proof of B1-B5 operational readiness.

- Each step button is type=button, not form submit.
- No credential values are read from the original forms, copied into
  a new form, persisted in browser storage or sent via new transport.
- The existing Nextcloud-only and Gitea Snapshot forms currently
  contain some duplicated Nextcloud fields: **this slice does not
  hide or synthesize those values** because the backend save contract
  still expects the full B2 payload. A later #301/#304 integration
  must redesign that server-side contract safely before removal.
- An unavailable status never makes a step "complete".
- The guide does not activate jobs, register a new Scheduler or
  create a Vault mapping. Those actions require independent admission.

## Code and verification

Modified:
- admin_dashboard.py: static R1/A guide markup only in Global Settings.
- webui_design_v1/setup_readiness.js: status-only step state and
  original-form focus navigation, using existing R0 GET transport.
- webui_design_v1/status_manager.css: small dark responsive step panel.
- tests/setup_readiness.test.cjs: controlled guide interaction tests.
- tests/test_setup_readiness_ui.py: contract and no-new-POST tests.

The next gate is source-only review and a separate owner-approved
Commit/Push/Draft PR. Do not merge/deploy/restart production or change
credentials, Vault/Nextcloud/Gitea/GitHub contents, systemd timers
or Gitea Runner without separate explicit authorization.

Full Setup Wizard AC-02 additionally needs:
- revised write-only credential-save UX to avoid repeated Nextcloud
  credentials without storing them in the browser;
- AI-provider connection support under #301;
- Vault root/bindings under #303;
- fresh single-LXC E2E acceptance under #298.
