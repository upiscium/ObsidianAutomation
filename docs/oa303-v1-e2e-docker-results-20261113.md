# #303 V1 E2E results — local Docker (2026-11-13)

**Where it ran:** local Docker instead of a new LXC, per owner direction
("テスト用のNextcloudはローカルのDockerを使ってほしい"). Production stays on
obsidian-control.

**Setup:**
- **App:** `python:3.13-slim` (Debian 13), built from main `f3cff10`, then
  from `f6dc1b5` (#586 fix) for E12 onward.
- **Nextcloud:** the disposable 35.0.2 behind an nginx TLS proxy with a
  run-specific CA, reached at `https://nc.oa303.test`.
- **Account:** the synthetic test account `oa303_test_viewer` only.
- **Receipts:** owner-private (`e2e-20261113/`).

## Results (plan: `docs/oa303-v1-fresh-lxc-acceptance-plan.md`)

| Case | Result | Notes |
| --- | --- | --- |
| E1 OFF by default | PASS | `GET` 404, `POST` 405 without `--vault-root-ui` |
| E2 connection saved, section enabled | PASS | The first attempt hit the obscure-charset bug (503); see Findings |
| E3 enrollment | PASS | `enrolled`; account record 0600 |
| E4 top-level candidates | PASS | Local folders and the user, group, read-only and expiring shares are listed; no external storage was configured |
| E5 previews | PASS | Local, group-share and inside-share folders are valid; the current root is `already_selected`. The first run failed; see Findings |
| E6 refusals | PASS | Read-only share → `unsafe_target`. The nested-share case was not determined: while the subfolder was shared separately, the duplicate-ID check refused every folder in the parent first (finding 2). After that share was removed, `Sub` previews as valid |
| E7 switch | PASS | Without confirmation: 400. With confirmation: `activated`, `oc:id` chain of 2 |
| E8 Formatter dry-run after the switch | PASS | `no_changes`; guard `ok` |
| E9 owner recreates the folder | PASS | Guard `mismatch`; Formatter run `failed` / `source_or_plan_failed`; no Vault effect |
| E10 switch while a Job runs | not run live | Covered by the HTTP/unit tests |
| E11 rollback | PASS | Previous folder restored; rollback no longer offered |
| E12 folder changed in the Nextcloud form | PASS (on the #586 build) | `connection_saved`; guard `legacy_unpinned` |
| E13 password rotation saved in the form | PASS | Enrollment kept; old handle → `stale_source` |
| E14 container and app restart | PASS | State kept; old handle → `handle_expired` |
| E15 PC/Android browser | done by the owner on production, 2026-11-13 | — |
| E16 CSRF / foreign Origin / no Origin | PASS | 403 / 403 / 403 |
| E17 Vault untouched by picker actions | PASS | The viewer's whole tree hash was identical before and after E3–E8 |

## Findings

**1. The connection save failed at random (existing bug, fixed in #586).**
- `rclone obscure` prints URL-safe base64, but `SetupStore` accepted only
  `[A-Za-z0-9+/=]`.
- 167 of 200 obscure outputs were rejected, so the save returned 503
  `nextcloud_connection_unavailable`.
- Production runs the old check until #586 is deployed.

**2. A separately shared subfolder blocks its whole parent share
(fail-closed).**
- With `11-Knowledge/Sub` also shared on its own, every folder inside
  `11-Knowledge` was refused as `identity_alias`: the same `oc:id` appears
  at two levels.
- `docs/oa303-v1-webui-operation.md` now says so; it previously stated that
  only the subfolder was affected.
- To continue, the separate share was removed.

**Run notes:**
- E2 and E12 were rerun after finding 1.
- E2's pass criterion was relaxed on the rerun ("enrolled" was already
  true).
- Diagnostic wrappers ran only in the E2E container.
- All new Docker resources are kept until a cleanup approval:
  - containers `oa303-e2e-app` and `oa303-e2e-tls`;
  - the `trusted_domains` entry;
  - the fixture folders.
