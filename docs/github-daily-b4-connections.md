# B4 GitHub Connections and shared Nextcloud Project mirror

This source-only integration supports the fresh, single-LXC ObsidianAutomation
application. It does not install or start any production services.

## WebUI sequence

1. In existing B2 Connections, configure the shared Nextcloud WebDAV account
   and its private Gitea Snapshot destination.
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

B4 reuses the already configured B2 Nextcloud/WebDAV account. On each
Run Once, rclone copies the source Vault's 10-Project folder's Markdown
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

This first slice reuses B2 SetupStore. Consequently initial B4-only setup
still requires the current B2 Connections form, including Gitea configuration,
despite B4 never contacting Gitea itself. Removing that unnecessary
prerequisite via the common #298 Connections/Wizard remains future work.
It must keep the single Nextcloud account workflow.

The approved production installation on obsidian-control, real GitHub and
Nextcloud credentials, actual Vault Project mirror, and reboot acceptance
are not part of this source-only change. #444 additionally requires the
strong-ETag/If-Match section-only Daily Writer and unattended publication.
