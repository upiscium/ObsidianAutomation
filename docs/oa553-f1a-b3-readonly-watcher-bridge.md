# #553 F1/A — reuse B4's pull-only Project source for B3 observations

**Source-only; no runtime registration, scheduler or canonical Vault Writer.**

The 2026-11-07 migration objective is to finish B1–B5 on existing
`obsidian-control`, with the old Writer LXC stopped and Proxmox boot disabled
per operator report. Do not block #298 on the former old-host two-issuer
attestation chain. Actual Writer exclusivity and conditional WebDAV CAS still
must be proven before a B3 write.

## Read-only existing-production canary

On `obsidian-control` with the nonroot `obsidian-snapshot` identity, a
one-time B4 `DailySetup.project_source()` pull copied Project Markdown into
a *disposable local directory*, then `scan_projects()` found **9 active watched
Projects in 9 repositories**, with zero validation warnings. The temporary
mirror was removed on context-manager exit.

A second operator-directed, bounded **B3 watcher GET-only** invocation reused
the exact same B4 token/source and the existing
`github_project_watcher.run_once()`. Results: **9/9 GitHub observations,
0 warnings, 0 errors, 0 pending status proposals**, about 47 seconds. It
persisted diagnostic state only in
`/var/lib/obsidian-snapshot/github-b3/watcher.sqlite3`, under an
`obsidian-snapshot`-owned 0700 directory; **no Nextcloud/Vault PUT/DELETE,
no Gitea or GitHub Push, and no service activation was performed by the B3
canary.** No B3 cron/scheduler or UI state was activated.

Current B4 per-run `mirror/` is empty *outside a scheduled run*. A B3
status read must not infer persistent Vault content from that directory
or pull Nextcloud in a GET handler.

## New bounded bridge (this PR)

`github_b3_watcher_bridge.collect_b3_read_only` takes only server-owned
`DailySetup` and preprovisioned private state directory; it obtains
the scoped disposable Project mirror, server-held GitHub token and
existing WatcherConfig from B4. It redirects potentially private events
to bounded in-memory sinks; the public result contains only a finite
observation count, proposal count and redacted fixed reason. A local
nonblocking flock serializes B3 collector executions; an invalid
owner/mode/symlink state directory, concurrent run, bad/partial GitHub
observation, excessive output, missing source or exception returns
`unknown`/`blocked`, never `observed`.

The existing `run_once` writes only local SQLite project observation
state; it does not apply status changes. Failed partial captures do not
become verified success merely because some rows were persisted. All
returns include `vault_write_authorized=false` and
`cutover_authorized=false`.

## Next runtime attachment gate

- After the #534 per-Project observer contract has been accepted,
  one designated integrator may schedule this bridge using the
  existing `snapshot_app` single-process timer/queue.
- A read-only B3 status route may *only read the persisted evidence*,
  independently check freshness and source-generation binding, and
  distinguish unobserved, stale and partial outcomes; GET must not
  trigger a pull, journal write or remote effect.
- Writer application is separate: generate bounded, revision-bound
  Project status/Overview proposals; serialize with B4/Formatter/Core
  Promotion; use #318-safe conditional WebDAV CAS + independent GET
  readback, with durable idempotency and explicit enablement.
- Production restart/deploy, automatic schedule and any Vault effect
  require a separate exact-SHA release gate and audited rollback.
  No amount of B3 GET-only success proves old credentials revoked.

## Test scope

Disposable tests exercise success and bounded counts, nonroot state
permissions, lock conflict, invalid symlink/owner, source failure,
partial and malformed output, failure redaction, bounded capture,
and the **real existing watcher code** with fake GitHub data and
temporary Markdown/SQLite. No external network/production secrets are used.

Related: #298, #483, #534, #305, #318.
