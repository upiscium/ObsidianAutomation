# B4 Daily Preview in the #298 single-LXC app

This source-only integration adds the B4 Job and browser controls to the
existing obsidian-snapshot-app --state application. It does not deploy,
configure credentials, or write any Obsidian Daily Note.

## One scheduler

The B2 SnapshotJob keeps the application's single scheduler thread. Its
optional tick_hooks parameter calls DailyJob.tick as part of that same loop.
Both workflows have distinct SQLite states and run receipts and execute as
one nonroot Unix service user. No second systemd daemon or scheduler runs.

Fresh --state mode attaches disabled B4 at STATE/github-daily. Existing
--config mode does not attach B4. B4 is Off and unconfigured by default.
When the GitHub token and read-only Project mirror have been provisioned,
manual Run now is available even while the interval scheduler is Off.

## Web interface and HTTP contract

All endpoints are on the existing Dashboard loopback port with its
Host, Origin and single-use CSRF checks.

- GET /api/v1/daily shows configured, schedule On/Off, next due, last result.
- POST /api/v1/daily/settings accepts enabled and interval_seconds.
- POST /api/v1/daily/runs accepts a unique 32-digit hex request_id.
- GET /api/v1/daily/runs/ID reads a durable run status and output digest IDs.

Interval is 3600-86400 seconds (default 86400); merely saving settings
does not execute an effect. Same request IDs replay the same receipt.
An overlapping new request is rejected while work is running. After an
interrupted run, persisted queued/running records become unconfirmed and
are never automatically replayed.

A successful result is preview_ready, not published. The standard Collector
only calls authenticated GitHub read APIs, preserves original evidence in
private CAS and produces no-LLM Markdown/JSON Preview artifacts. The user
Vault and Nextcloud are untouched.

## Configuration and unfinished acceptance

The private state directory must already contain the validated watcher.toml,
github.token, local pull-only Project mirror and precreated CAS/Preview
directories per docs/github-daily-b4-runonce.md. This PR does not expose
a token-writing WebUI route and cannot configure GitHub itself.

Pending #298: GitHub Connections, pull-only mirror provisioning, full fresh
LXC installer and combined WebUI end-to-end acceptance. Pending #444:
strong-ETag and If-Match section-only Daily Writer, scheduler calendar policy,
restart and production acceptance. No live LXC services or tokens are touched
by this source-only PR.
