# B3-lite: automatic Project status (design)

Tracking: #610 (restore of purged features). Owner decisions (2026-11-28): the old B3
shrinks to **changing the `status` of a Project note and nothing else**; the change is
written **automatically** (no approval step); the rule stays the old one.

## What it does

For each Vault note with `type: project`, `github_watch: true` and a valid `github_repo`,
read the repository's recent activity (GET only) and, when the rule says so, rewrite the
note's front matter `status` line. Nothing else in the Vault is written.

Rule (existing `github_project_watcher.decide_status`, unchanged):

* `planning` / `running`: a commit within `active_window_days` (7) means `running`,
  otherwise `planning`.
* `stable`: a new commit means `running`; a new open Issue or PR means `planning`;
  otherwise it stays.
* `stopped`, `done`, `cancelled` are human decisions and are never read as input to a
  change or written. The automation writes only `planning`, `running` and `stable`.

## Why automatic writes are acceptable here, and the brakes

A wrong status is one line in one note and Snapshot keeps every earlier version, but an
unattended writer must not be able to loop or race the owner:

1. **Edit exactly one line.** `apply_status_line` (step 1, this change) refuses unless the
   note has a closed front matter with exactly one top-level `status` whose plain value is the
   value the decision was made from; the rest of the note is byte for byte unchanged.
2. **Conditional write** (step 2): `PUT` with `If-Match` on the ETag of the bytes that were
   read, exact-byte readback, the same pinned-root guard as the knowledge proposals. If the
   owner edited the note meanwhile, the write is refused and the note is retried next run.
3. **Cooldown:** a note is changed at most once per 24 hours (stops a project flipping every
   day around the 7-day boundary). **Cap:** at most 10 changes per run.
4. **Journal:** every change is recorded (note, old, new, reason, time) in the service
   database; the panel shows counts, never note names or text.
5. **Off by default.** The schedule is opt-in in the panel. Failures raise the existing
   failure banner (Job health), skipped slots are neutral.

## Steps (one PR each; deployment and enabling are separate approvals)

1. Decision planning and the one-line editor, pure and tested (`project_status.py`). No I/O.
2. Reading Project notes and writing the status over WebDAV (conditional PUT + readback +
   journal), GitHub reads reuse Daily's existing token, CLI `--dry-run` first.
3. Service and panel (`--project-status-ui`), schedule, health.

## As built

* **Step 2 (`project_status_vault.py`, `project_status_run.py`).** `python -m
  obsidian_automation.project_status_run --state DIR` reports counts (dry run); `--apply`
  writes, `--max-changes N` caps one run. State lives in `DIR/project-status/`
  (`watcher.sqlite3` GitHub baselines, `changes.sqlite3` the change journal). GitHub is read
  with Daily's token (`DIR/github-daily/github.token`).
* **Step 3 (`project_status_service.py`, `three_services/project-status.js`).**
  `--project-status-ui` adds the panel and the API (`GET /api/v1/project-status`,
  `POST .../runs {"apply": bool}`, `POST .../settings {"enabled": bool}`), with
  `--project-status-interval-seconds` (default 21600, minimum 3600) and
  `--project-status-max-changes` (default 10). The schedule is off until switched on in the
  panel; a scheduled run applies. A panel run can be "check only" (writes nothing to the
  Vault). One run at a time; a run left running by a crash is marked failed, never replayed.
* **Health.** Three failed runs in a row show the failure banner. A run counts as failed when
  it raised, or a write failed for any reason other than the owner having edited the note
  meanwhile (`note_changed` is normal).
