# #553 F1/C — B3 GET-only Observer failure recovery

**Source-only hardening of the already merged B3 read-only Job (#556).**
No canonical Writer, Core Promotion, deployment, Gitea action, or Schedule
activation is introduced.

## Correction

1. **Missing source is not source rotation.** A transient failure of B4
   `DailySetup.configured()` or `source_fingerprint()` now suppresses the
   current B3 tick without changing the desired enabled flag or stored
   binding. A recovered source may trigger one overdue read-only scan
   immediately; no backfill or canonical write occurs. A *valid, different*
   generation continues to revoke B3's schedule persistently, requiring
   deliberate re-enable.
2. **Worker termination is reconciled.** Both start-stage and final
   SQLite/clock errors fail closed. Terminal writes have bounded retries.
   If the worker dies without a confirmed terminal record, the existing
   scheduler's next tick (or a separately authorized manual submit) marks
   the interrupted attempt `unknown`. Only dead worker threads may be
   reconciled. An old request ID is replayed without another GitHub call.
   Status GET does not mutate the journal.
3. **Time regressions are presentation-safe.** Last attempt is selected by
   durable SQLite insertion order, not a wall-clock timestamp; the
   terminal timestamp cannot precede admission even after NTP rollback.
4. **Bounded diagnostics count bytes.** The private watcher output cap is
   measured in UTF-8 bytes rather than Unicode characters. Invalid encoding
   remains an `unknown` observation, not a partial success.

## Tests / boundaries

Disposable tests inject a failed running-state transition, invalid final clock,
transient final SQLite lock, persistent lock followed by recovery,
`KeyboardInterrupt`, temporary source outage/recovery, real generation
rotation, clock regression, and multibyte diagnostic output. They verify
idempotency and `vault_write_authorized=false`.

The local watcher still commits per-Project observations before the bridge
validates every result. On any partial/error output, the bridge returns
`unknown` and does not assert an entire verified scan. The next
`github_project_watcher.run_once` re-scans all eligible Project notes;
per-Project UI must not present partially committed rows as verified
without a matching successful run receipt.

SQLite files remain confined to a service-owned 0700 state directory.
Same-UID replacement/symlink races and persistent storage failure remain
low-level operational boundaries. A lasting DB outage cannot be made to
look like success; the next healthy tick or app restart reconciles it.

A source-only Merge is **not** approval to Deploy/Activate. Runtime
cutover must still preserve B2/B4 and Formatter, default B3 OFF, and use
independent production acceptance with rollback.
