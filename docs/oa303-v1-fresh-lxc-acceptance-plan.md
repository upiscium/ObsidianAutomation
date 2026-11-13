# #303 V1 — fresh-LXC acceptance plan (E2E)

**Status:** plan only. Running it needs its own approval: create the LXC,
install, use the test account.

**Goal:** prove the V1 Vault-root flow on a **new empty Debian 13 LXC**
under the #298 fresh-install contract: one non-root service and one shared
Nextcloud account. It never touches the production Vault or obsidian-control.

## Preconditions

| Item | Requirement |
| --- | --- |
| Host | New Debian 13 LXC, private IPv4 only, no port-forwarding |
| Source | The reviewed `main` commit; the wheel is built from a clean `git archive` and its sha256 is recorded |
| Nextcloud | A **test** account on a disposable or test instance (35.x, HTTPS). Never the production account |
| Test data | In that account: one local folder tree, and these received shares with a few notes each: a **user** share and a **group** share (full permissions); a **read-only** share; a share whose owner also shares a subfolder of it separately, giving a **nested** share root. One external-storage mount, if available |
| Install | `tools/install_snapshot_lxc.py --bind-address <ip> --vault-root-ui`, plan first, then `--apply` |

## Cases

### OFF by default

| # | Case | Pass criterion |
| --- | --- | --- |
| E1 | Install **without** `--vault-root-ui`, then open the Dashboard | No "Vaultフォルダの選択" section. `GET /api/v1/vault-root` returns 404. POSTs return 405 |

### First use (account and candidates)

| # | Case | Pass criterion |
| --- | --- | --- |
| E2 | Reinstall with the flag; save the Nextcloud connection | The section appears and reads 「Nextcloudアカウントの確認が必要です」 |
| E3 | Press 「Nextcloudアカウントを確認」 | `enrolled`. A 0600 `vault_root_account.json` exists |
| E4 | List the top level | Local folders and both shares are listed. External storage is not listed |
| E5 | Preview a local folder, a share root, and a folder inside a share | `candidate_valid` for each |

### Switch and Jobs

| # | Case | Pass criterion |
| --- | --- | --- |
| E6 | Preview a read-only share; preview a nested share | Refused (`unsafe_target`) |
| E7 | Switch to a folder inside the user share, cancelling the dialog first | Cancel: nothing changes. Confirm: `activated`; schedules paused; the binding file holds the `oc:id` chain |
| E8 | Re-enable Snapshot and run it once | The Job runs. The guard reads `ok` |
| E9 | Owner deletes and recreates that folder (same name), then Run Once | The run fails with the root-pin refusal: Snapshot `failed/sync`, B4 `source_admission/source_unavailable`. Internally the guard reports `mismatch`. No Vault effect |
| E10 | Switch while a Job is queued or running | `jobs_in_flight`; nothing changes |

### Recovery and persistence

| # | Case | Pass criterion |
| --- | --- | --- |
| E11 | Roll back | The previous folder is restored and the rollback button disappears |
| E12 | Change the folder in the Nextcloud form, then Run Once | The pin is reset to unpinned (`connection_saved`). Run Once works as before. The save pauses schedules (existing behaviour), so re-enable them |
| E13 | Rotate the app password in the form | The enrollment stays valid (same URL and user). Old picker handles return `stale_source` |
| E14 | Restart the service | The state persists; outstanding handles expire; nothing is pinned automatically |

### Browser and safety

| # | Case | Pass criterion |
| --- | --- | --- |
| E15 | PC and Android browser | The section renders. Long labels wrap. Dialogs work. No horizontal scroll |
| E16 | POST without CSRF, or from a foreign Origin | 403 |
| E17 | Throughout | No Vault file is created, changed, moved or deleted by any picker action (compare the account's file listing before and after) |

## Records

- One receipt per case: status, reason codes and file listing hashes. No secrets and no paths beyond the test account.
- Unexpected results stop the run. No retry and no automatic cleanup.

## Exit

All cases pass. Any deviation is filed as an issue before production
changes. The LXC and the test account are removed only with a separate
cleanup approval.
