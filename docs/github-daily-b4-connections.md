# B4 GitHub Connections and shared Nextcloud Project mirror

This source-only integration supports the fresh, single-LXC ObsidianAutomation
application. It does not install or start any production services.

## WebUI sequence

1. In the **common Nextcloud Connections** form, save the WebDAV URL,
   Vault folder, username and app password. **Gitea is not required for B4.**
2. In the GitHub Daily Preview panel, enter the GitHub access token and choose
   the GitHub connection verification/save button. The server checks a
   read-only authenticated GitHub.com API endpoint and stores the token
   in an owner-private file. The token is never redisplayed.
3. When a valid watched Project Note exists under the Vault 10-Project folder,
   B4 Run Now collects the previous JST day's GitHub changes and creates
   deterministic, private Markdown and JSON Preview artifacts.
4. B4 scheduling remains Off after credential save. Enabling it schedules
   local Preview production only, not publication into the Daily Note.

The B4 Job and B1–B5 workflow catalog share the one B2 application scheduler
and one WebUI. No second LXC, Nextcloud account, systemd service, root Worker,
or model inference is required.

## Read-only Project mirror contract

B4 reuses the common Nextcloud/WebDAV account, including one already
configured for B2 Snapshot when present. On each Run Once, rclone copies the source Vault's 10-Project folder's Markdown
notes into fresh private local staging. The fixed copy command never uploads,
moves, sync-deletes or edits the remote Vault.

The Project scanner selects valid frontmatter notes with type project,
github_watch true, a valid github_repo and an active status. If no watched
Project is found, the run fails rather than claiming an empty successful
capture. Staging is discarded after a successful or failed run.

B4 source configuration is bound to the Nextcloud WebDAV remote and Vault
folder. If either changes, B4 is unconfigured and the schedule returns Off.
Rotating the unrelated Gitea token does not invalidate B4 settings.

## WebUI API

- GET /api/v1/nextcloud/connections shows only the shared WebDAV configuration
  status. POST saves only four Nextcloud fields after a read-only WebDAV probe.
  It has the same Host/Origin/single-use CSRF boundary. Replacing the source
  pauses an enabled B4 Preview schedule before credentials change.
- GET /api/v1/daily/connections exposes only configuration status, available
  shared Nextcloud source and mirror mode.
- POST /api/v1/daily/connections accepts only a GitHub token, with existing
  Host/Origin/single-use CSRF validation. Source changes are refused while a
  B4 Run is actively queued or running.
- Existing Daily Job status, settings and Run Once API routes remain separate.
  No arbitrary commands, Vault paths, token readback or provider mutation
  endpoints are added.

A returned status of preview_ready means a verified local Preview exists.
It does NOT mean the Daily Note has been updated.

## Remaining integration

B4-only setup now needs just one common Nextcloud connection and a GitHub
token. When B2 is later configured, B2 becomes the authoritative source and
the redundant standalone Nextcloud record is discarded. The existing B2
Snapshot form still requires the full WebDAV + private Gitea connection; B2
and B4 never use separate active Nextcloud accounts. While B2 is configured,
Nextcloud changes are made via the B2 form, not a second account in the
common form. The common Connections/Wizard can unify the remaining B2 form
fields in a later usability pass.

The approved production installation on obsidian-control, real GitHub and
Nextcloud credentials, actual Vault Project mirror, and reboot acceptance
are not part of this source-only change. #444 additionally requires the
strong-ETag/If-Match section-only Daily Writer and unattended publication.
