# #304 R0 — Global System Settings setup-readiness checklist

Status: SOURCE-ONLY, NOT COMMITTED OR DEPLOYED. This is a small follow-up
to #481's production wireframe-based Obsidian Status Manager. It is not
owner PC/phone acceptance for #481 or full Setup Wizard completion.

## User flow

- Open existing hamburger menu -> Global System Settings.
- A collapsed optional panel, "初回セットアップの確認（読み取り専用）",
  sits above existing Nextcloud, Gitea and GitHub forms.
- Click "接続設定の状態を確認" to request exactly three existing
  status-only, same-origin HTTP GET endpoints:
  /api/v1/nextcloud/connections,
  /api/v1/snapshot/connections,
  /api/v1/daily/connections.
- The UI presents fixed local labels rather than server-supplied strings,
  URLs, tokens or errors. A Nextcloud or GitHub configured flag means
  settings saved; it is NOT live connectivity evidence. Gitea
  read_verified_at_save means verification happened at save time, not now.
  Invalid/missing DTO, rejected responses and timeouts become unavailable.
- Each row links locally to the existing original settings form. No Save,
  Run once, Queue admission, credential replacement or scheduler activity
  can be triggered by reading the checklist.
- Nothing is fetched on page load. A 9.5 second deadline covers JSON
  decoding; older responses cannot overwrite newer observations.

## Source-only scope

1. admin_dashboard.py: add the optional collapsed panel, one external
   JS script and one exact static GET/HEAD route. Keep all backend
   handlers and legacy operational IDs unchanged.
2. webui_design_v1/setup_readiness.js: pure status parsing, fixed
   copy-only labels and a user-triggered, GET-only controller with
   no credentials or persistent browser storage.
3. webui_design_v1/status_manager.css: minor responsive panel styling
   within the wireframe's gray/light palette.
4. tests/test_setup_readiness_ui.py and tests/setup_readiness.test.cjs:
   fixed route/Host/Origin/CSP tests, fake-browser transport, malformed
   DTO rejection and stalled JSON deadline tests.

The current package-data wildcard includes all JS in the UI assets folder.
No new build tooling, Unix account, LXC, login flow, API mutation or
production token is added.

## Non-goals and following slices

- R0 cannot declare B1-B5 integrated or AI Provider/Vault path readiness.
  It lists only 3 redacted connection statuses. Gitea Snapshot and
  GitHub Daily still require their own backend validation and controls.
- Full first-run Wizard / credential save confirmation, avoidance of
  repeated Nextcloud fields, AI Provider setup and Vault folder mapping
  require later scoped design/implementation under #304/#301/#303.
- This source-only candidate is NOT an authorization to Commit, Push,
  create a PR, Ready/Merge, deploy, mutate the Vault, restart services,
  or retire the existing Gitea Runner.
- Parent Epic #298 and #304 remain Open. Issue #481 must remain Open
  until the owner reports actual PC + phone usability acceptance.
