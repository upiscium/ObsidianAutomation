# #303 V1 — choosing the Vault root from the WebUI

The feature is OFF by default. It is enabled only when the app starts with
`--vault-root-ui`. Without that flag, `/api/v1/vault-root*` answers 404/405
exactly as before, and the page script does nothing.

## Enabling

For a fresh or updated LXC install, add the flag to the reviewed plan:

```sh
python3 tools/install_snapshot_lxc.py --bind-address <private IPv4> --vault-root-ui
python3 tools/install_snapshot_lxc.py --bind-address <private IPv4> --vault-root-ui --apply
```

The unit then runs:

```
obsidian-snapshot-app --state /var/lib/obsidian-snapshot [--bind …] --vault-root-ui
```

The flag requires the fresh shared `--state` app.

**Before the first start, the state directory must contain neither
`vault_root_binding.json` nor `vault_root_pinning.enabled`.** A leftover
activated pin would make every Job send one read-only `PROPFIND` per run.

## Using it (Dashboard → "Vaultフォルダの選択")

1. **Nextcloudアカウントを確認.** This sends one `PROPFIND Depth:0` of the
   account root and one OCS `cloud/user` GET. It records the instance ID,
   the account root ID and the user. The OCS user must equal both the user
   in the saved WebDAV URL and the saved login.
2. **最上位の候補を表示 → 中を見る → Preview.** Read-only. It lists only
   plain folders: shares and mounts are hidden, and a preview of one is
   refused. Labels are shown as text. Each candidate handle is
   short-lived.
3. **このフォルダに切り替える.** This appears only after a `candidate_valid`
   Preview and needs a confirmation dialog. The server enforces the Preview
   too: a handle that has not passed Preview gets `preview_required`. The
   server then:
   - takes the connection save's Job locks in the same order, plus the B3
     lock last. It refuses while any Snapshot, B4, Formatter or B3 run is
     queued or running. The scheduler tick waits during the short re-walk;
   - re-walks the root and requires the previewed folder's `oc:id`;
   - writes a pending pin, then changes **only** `vault_folder` (local CAS);
   - pauses the schedules, then finalizes the pin.

   Credentials and `rclone.conf` are unchanged.
4. **Re-enable the schedules** you want in the Dashboard. Every Job checks
   the pinned root's `oc:id` before each run and refuses on any mismatch.
5. **直前の切り替えを元に戻す** (one step) restores the previous
   `vault_folder`. The button is shown only when the rollback can succeed,
   i.e. the saved folder is still the one last switched to.

No action reads, writes, moves or deletes any file in the Vault.

## Guarantee and its limits (restricted contract, scope V1-S2)

**Guaranteed:**
- a Job never runs against a root whose identity changed since activation;
- a different account, server or instance is never accepted silently.

**Not guaranteed:**
- edits made by other Nextcloud clients between the final check and the
  switch, or after it;
- files placed directly on the server's disk;
- external storage that has not been rescanned.

**Scope V1-S2** (V1-S1 plus received shares):
- one shared account, Nextcloud 35.x;
- the Vault may be a local folder, or **one received share / a folder
  inside it**, if the account can create files and folders there;
- no external storage, group folders or a share nested in a share;
- writes only through Nextcloud clients;
- served at the host root path, with a user ID equal to the login name.

If the owner of a shared Vault deletes and recreates the folder, its ID
changes and every Job refuses until you switch to it again from the picker.
If the owner later makes the share read-only, Jobs that only read (Snapshot)
keep running; writes fail on the server. The picker refuses to switch to a
read-only share. If the owner shares both a folder and a subfolder of it, the
subfolder appears twice with one ID; switching to it from inside the parent
share is refused as an alias (fail-closed).

## Recovery

| Message | What to do |
| --- | --- |
| `instance_changed` | After a real server migration: stop the service, remove `vault_root_account.json` from the state directory, start it again and re-confirm the account |
| `rollback_uncertain`, `config_write_uncertain`, `pin_unfinalized`, `job_pause_failed` | Jobs refuse to run (fail-closed), and rollback may not be offered. Check `vault_folder` in the Nextcloud form. A new switch to the intended folder re-pins it. Saving the Nextcloud form also clears the pin (unpinned root) |
| A folder typed into the Nextcloud form | The root becomes unpinned again (today's behaviour). Use the picker to pin it again |

## Not verified here

- Rendering in a real browser (PC/Android): there is no browser in the
  development environment. The script has syntax checks, static contract
  tests and an HTTP end-to-end test.
- Deployment on obsidian-control: owner-approved (2026-11-13); pending connection details and the read-only precheck.
