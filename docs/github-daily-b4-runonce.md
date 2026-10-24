# B4 deterministic Daily: manual source-verified Preview (single-user Standard)

This development-stage adapter is an explicit **read-only GitHub GET to local
private Preview**. It uses the merged v3 source-route + temporal receipt +
content-addressed Standard CAS and the deterministic no-LLM digest.

**It is not an unattended Daily writer, a live Nextcloud operation, or a
complete #298 installer, Connections UI, scheduler, or service.** Those are
separate integration/acceptance gates. In particular, it never edits an
Obsidian Vault, starts systemd units, or submits a GitHub mutation.

## One-service-user offline preconditions

Prepare a nonroot service-owned state directory, with no symlinks in its path:

- `/path/to/state/`: owner-only mode 0700.
- `watcher.toml`: regular, one-link, service-owned 0600 TOML; its
  `[watcher]` section must point `vault_root` at
  `/path/to/state/mirror` and `state_db` at
  `/path/to/state/watcher.sqlite3`. `github_api_base` must be
  `https://api.github.com`, `project_folder` must be `10-Project`.
- `github.token`: regular, one-link, service-owned 0600, containing a GitHub
  access token (optional terminal LF). Do not put this token on the CLI
  command line, in logs, or in the Vault. Future #298 Connections should
  populate it via an operator-only secret store.
- `mirror/`: preexisting, service-owned 0700, **pull-only local copy** of
  Project notes with `github_repo` and `github_watch: true` under
  `10-Project`. The adapter does not sync Nextcloud itself.
- `cas/`, `cas/evidence/`, `cas/receipt/`, `cas/capture/` and
  `preview/`: precreated directories, service-owned 0700.

For isolated acceptance use a disposable private mirror with test Project
notes. Do not copy user credentials into a test record.

The reviewed commit can be installed by a *separately approved* fresh-LXC
installer. This source PR does not run an installer.

## Manual Run Once

From an already approved, installed and configured service runtime:

```sh
obsidian-github-daily-preview-once --state /path/to/state
```

It always selects **the previous JST date** at the time of execution. Authenticated
GitHub API calls are pinned to `https://api.github.com` and read-only. The
result is a compact JSON receipt with source Capture SHA, digest SHA, event/
entity counts and two local artifact *names*. It reports
`status=preview_ready`, `model_used=false`,
`publication_authorized=false`, `vault_modified=false`; it does NOT imply
a Note was published.

Failed source verification, missing Project mapping or credential, invalid
private paths, conflicting output bytes, API rejection, or other execution
failures exit nonzero without claiming a complete preview. Partial CAS output
is retained for forensic review; retries can reuse byte-identical files but
may create a new Capture SHA as GitHub's observed source changes.

## Next owner handoff

- #298 Connections: supply and verify token securely; point B4 at the shared,
  read-only Nextcloud Vault Project mirror (one account, one service user).
- #298 Scheduler + Job/Run API: submit this synchronous callable as the
  B4 Preview step; persist typed `queued/running/preview_ready/failed/
  unconfirmed` outcomes with idempotent request IDs, schedule default Off.
- #444 Writer: separately verify immutable CAS inputs and **strong ETag
  / If-Match, section-only** Daily Note updates, then Timer/restart E2E.
- Require independent authorization before changing the live Vault or
  automatically running scheduled GitHub/Nextcloud work.

This adapter deliberately does **not** expose an HTTP token API or invent a
second Scheduler in parallel with #298 B2.
