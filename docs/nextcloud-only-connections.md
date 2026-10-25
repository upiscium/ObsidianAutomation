# Nextcloud-only shared Connections (#298 / #444)

The fresh single-LXC WebUI now accepts a Nextcloud WebDAV account and Vault
folder **without requiring Gitea**. This is the minimal B4-only setup. No
additional Nextcloud service account or separate Linux identity is created.

## Operator flow

1. Open the shared Nextcloud section of the WebUI. Set its WebDAV URL, Vault
   folder, username and app password. The server verifies the target Vault
   via a read-only PROPFIND request and stores the configuration privately.
2. Open the GitHub Daily Preview section and add a GitHub token. Use B4
   Run now to collect yesterday's (JST) active watched Project Issue/PR
   progress. The Project mirror is fetched into a disposable local folder,
   and results are immutable, deterministic local Preview only.
3. **Optional:** to use B2 Vault Snapshot → Gitea, fill only the Gitea
   destination fields in the Snapshot setup form. The server takes the
   existing Nextcloud credentials from the same validated store and probes
   the private Gitea destination. The B2 schedule remains Off by default.

## One canonical service-account store

SetupStore's existing private connections.json and rclone.conf now accept
either a Nextcloud-only record (four Nextcloud fields + rclone integrity
marker) or the previously supported complete B2 record (eight fields +
marker). A partial connection cannot be promoted to a ready B2 job merely
by existing on disk: SetupStore.load and SetupStore.connections still
require complete, validated Gitea settings. For B4, the source-only reader
returns the Nextcloud reference and the same rclone configuration.

No token/password values are returned from connection status APIs.
Existing full B2 configuration and atomic file/byte verification continue
to work. A failed connection probe does not replace the connection store.
B4 configuration is bound to semantic Nextcloud URL/Vault/user/password
rather than Gitea or randomly obscured rclone-password bytes.

## Network and effect boundaries

- Nextcloud probe: read-only WebDAV PROPFIND.
- Project mirror: remote-to-local rclone copy of watched Project Markdown,
  disposable per B4 Preview job.
- GitHub: authenticated GET for read-only source collection.
- Gitea: not accessed at all for B4-only setup; required and probed only
  when enabling B2 Snapshot.
- Obsidian Vault / Daily Note: **not modified** by this source-only feature.
- No extra Scheduler, service user, LXC, human review per Daily, or LLM.

The newly added API is GET/POST /api/v1/nextcloud/connections with the same
Host/Origin/one-use-CSRF protections already used by the WebUI. Changes to
shared source settings while a B2 or B4 run is in progress are refused.
Existing running schedules must not be silently mapped onto a different
Vault. Exact nextcloud-only behavior and optional Gitea upgrade are covered
by tests/test_nextcloud_only_setup.py.

## Remaining acceptance

This source PR does **not** install/start a service on obsidian-control,
supply actual credentials, alter existing Vault files, enable scheduled B4
Preview, or provide #444's strong-ETag section-only Daily Writer. A fresh
LXC installer, actual Nextcloud/GitHub read-only E2E and the final unattended
Daily Writer/Timer remain separate acceptance steps.
