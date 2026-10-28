# Vault Formatter Phase 1: shared scheduler and management UI (#472)

Parent: [Vault Formatter Epic #469](https://github.com/upiscium/ObsidianAutomation/issues/469).
Stacked prerequisites: [#470 / read-only formatter](https://github.com/upiscium/ObsidianAutomation/issues/470)
and [#471 / guarded CAS writer](https://github.com/upiscium/ObsidianAutomation/issues/471).

## Operating model

In the fresh nonroot snapshot_app --state application, Formatter owns a
separate vault-formatter/ SQLite WAL database, lock and run history.
It reuses the existing single Snapshot scheduler tick. No new systemd
timer, LXC, root Worker, GitHub token or Nextcloud account is necessary.

The Dashboard provides a Vault Formatter / 98-System panel:
- Mode Off (default) or Dry-run. Auto apply is visibly disabled.
- Schedule interval 300–86400 seconds.
- Save settings and Run Once (always Dry-run). Manual Dry-run works even
  when the periodic schedule is Off if Nextcloud is configured.
- Refresh the exact persisted run receipt, status, safe counts and next due.

The HTTP routes are GET /api/v1/formatter, GET /api/v1/formatter/runs/{id},
POST /api/v1/formatter/settings and POST /api/v1/formatter/runs.
They reuse the existing Origin + one-time CSRF checks. All reported fields
are bounded/typed metadata; no Vault body, secret, remote URL or content
diff is sent to the browser.

## Read-only remote source capture

The Formatter reuses the shared Nextcloud rclone.conf and configured Vault
folder. It first obtains a bounded rclone lsjson inventory of eligible 98-System
Markdown files (file count, individual sizes, total bytes, path constraints).
It downloads only that exact allowlist into a disposable private mirror,
with max-size and max-transfer guards, and verifies matching bytes through a
read-only rclone check --download protected by its OWN max-size and
max-transfer guards, followed by a second bounded inventory. Only then
does it call #470 create_plan. An oversized, mutated or unobservable source
is rejected, never silently truncated or treated as a successful preview.
The temporary allowlist and mirror are removed on success or failure.
For compatibility with Debian rclone 1.60.1-DEV, the fixed file allowlist
uses newline-delimited --files-from-raw (not unsupported --files-from0).
The input manifest rejects CR/LF and all control characters in path
components before constructing this file, while the raw mode preserves
ordinary spaces, '#', brackets and non-ASCII UTF-8 filenames.
The installed rclone rejects --files-from-raw combined with --max-size
because the raw allowlist overrides other name/size filters. The copy and
download-check retain their independent HARD total-byte transfer caps.
After the copy (and before download-check), the disposable mirror must
match the pinned source inventory exactly in filename, file count, file
type, per-file byte size and total bytes. Any mismatch fails closed; the
mirror is deleted without changing Nextcloud.
Only read-only inventory and remote-to-local copy/check are permitted.

The source path invokes only remote-to-local copy and check; no remote
rclone sync, push, upload or WebDAV PUT/MOVE/DELETE.

## Persistent result contract

- queued/running: bounded one active Formatter worker; duplicate calls are
  not resubmitted.
- would_change: a verified Dry-run found safe candidate changes; no Vault
  write occurred.
- no_changes: empty safe diff; NOT proof that Gitea/GitHub Core converged.
- failed: source/plan unavailable, with a fixed reason code and no private
  diagnostic text.
- unconfirmed: queued/running interrupted across restart; never replayed.
- applied/conflict/indeterminate: only for a separately injected and
  authorized CAS Writer; no such writer is passed by the normal service.

Construction does not begin jobs. A successfully saved change to the
Nextcloud source identity resets only the Formatter mode/schedule to Off.
A failed save or a Gitea-only settings change does not disable Formatter.
Other app jobs retain their independent operating modes.

## Production requirements NOT YET MET

Source tests and this app integration do not enable live mutation. The
normal app never injects a Writer and cannot set Auto apply, even from a
crafted HTTP request. Before unlocking writes:

1. Demonstrate real Nextcloud stale If-Match protection without mutating
   a different server-side version. Synthetic tests alone are insufficient.
2. Obtain explicit owner approval for exact Vault, narrow 98-System canary,
   bounded permissions, receipt root, rollback and retained source versions.
3. Review an independent Writer adapter binding #471 to those approved
   credentials and the original CAS plan; never let the UI supply raw PUT
   bytes or arbitrary paths.
4. E2E verify readback, private Snapshot, guarded Gitea publication,
   GitHub public allowlist and no Core drift feedback loop.
5. Authorize unattended Auto apply separately from source Merge and canary.

The Snapshot remains read-only; the public export allowlist does not change.
The formatter never overwrites user-owned Vault changes with old Core bytes.

## Tests

Use disposable synthetic fixtures:

    python3 -m pytest -q tests/test_vault_format_job.py
    python3 -m pytest -q tests/test_vault_format_writer.py tests/test_vault_formatter.py

Also run Dashboard, Snapshot, Daily and CI regression. No test implies
permission to merge, deploy, write to a real Vault, or enable production.
