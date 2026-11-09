# #553 F3 — B3 phase scheduling and cached Project list (source-only)

Scope: **B3 GET-only Observer** integrated into the existing single nonroot
`obsidian-snapshot-app`. Historical source worktree base:
`main@527bfd7f58b8270cf7c61b2ea6de1e402a551350`.
An early isolated qualification worktree used
`main@09def9b580fcb394b28a90835832e4fb5f1055a3`.

**Publication checkpoint (2026-11-09, before the R6 docs correction):**
R0–R5 source/test/CI changes were **committed and pushed** to
[Draft PR #564](https://github.com/upiscium/ObsidianAutomation/pull/564),
published HEAD `1f188c3925e0fe982a7ba5ed40e4aafaba1e30f0`.
At this checkpoint the PR was **Draft / Open / unmerged**; the
**PR #564 source revisions were not deployed or activated**. This
does not negate any separately approved, earlier default-OFF B3
deployment. Publishing or merging these source-only changes does not
authorize production phase scheduling,
canonical Vault Writer, Core Promotion or Provider I/O. The phase
schedule defaults **OFF**; operational effects require separate approval.

## Phase-locked observer schedule

- `B3PhaseRuntime` attaches to the **same SQLite DB and RLock** as the
  existing B3 Observer, using new `phase_settings` and `phase_slots`
  tables. It starts **OFF**, revision zero, Asia/Tokyo 12:00 / 3600 seconds
  as a dormant default. No service or environment setting implicitly enables
  it. The existing relative interval setting remains intact.
- Future trusted configuration is revision-CAS, bound to a valid B4 source
  generation, and denied if legacy B3 relative interval scheduling is ON.
  The legacy settings update is also denied while phase mode is enabled.
  No public phase-control POST exists yet; there is no automatic migration
  of an existing B3 interval schedule.
- One due UTC slot is processed per scheduler tick. Transactional slot-fence
  insertion, next-due advance and B3 run admission share a **single SQLite
  transaction**; request ID is derived from revision, source binding and slot
  epoch. Already admitted, interrupted or skipped slots never replay.
  Overdue by more than 120 seconds or busy slots are skipped without catch-up
  bursts. One-day DST skip/fold policy is inherited from `phase_schedule.py`.
- Temporary source outage only pauses admissions. Valid different generation
  revokes the schedule. Configuring OFF cancels future admissions, not an
  already running observation. Manual Run Once remains independent.
- A phase slot additionally binds an **expected source generation** through a
  dedicated `phase_collector(expected_generation)` callback. The B4
  `DailySetup.project_source(expected_generation=...)` compares the exact
  captured source **before any Nextcloud mirror pull**. For phase-only
  captures it freezes the rclone credentials into a private 0600 disposable
  configuration file, captured together with the semantic source generation
  under the trusted SetupStore rotation lock, then releases that lock before
  network I/O. A queued old-A slot therefore cannot silently use B
  credentials after rotation; even if the shared rclone file changes during
  the pull, the collector continues using the frozen A config.
  The worker rechecks the durable slot binding before and after collection.
  **The final positive receipt's generation check and SQLite commit share
  the same source-update RLock** as SetupStore credential saves, so an
  update cannot interleave that short critical section. Detected mid-flight
  rotation yields `unknown`, never a verified new-source observation
  under an old slot. Existing manual/relative collectors retain their
  zero-argument B4-compatible invocation.
- Shared scheduler adds the phase tick as a separate **default-OFF hook**.
  Neither B2 nor B4 scheduling, credentials nor Writer permissions change.

## Read-only all-Project UI data

- The existing authorized B3 collector may build a **bounded local-only**
  Project index from the disposable already-pulled `10-Project` mirror and
  read-only watcher SQLite. It includes disabled, terminal and misconfigured
  Project notes, not only the eligible GitHub watcher set. At most 512 Markdown
  candidates and 256 Project/observation rows, 64 KiB per note, 256 KiB cache.
- Notes are read via O_NOFOLLOW opened inodes, never exposed as raw Markdown.
  The `github_project_watch_view.py` pure DTO sanitizes display names,
  repositories and status and never claims a verified canonical Vault write.
- A cache is published to a private 0600 atomic JSON file **only after**
  the source context's post-yield generation verification succeeds, and only
  if a fresh generation fingerprint still matches. Source rotation refuses
  old cache on any later GET. Cache publication failures are optional and
  never transform a separately verified watcher receipt into Writer success.
- `GET /api/v1/b3/projects` reads only this cache and local source
  generation, checking the generation **before and after** the bounded cache
  read. It does NOT open the remote Vault, call GitHub, trigger watcher or
  mutate SQLite. No cache / stale more than 24 hours / different
  generation / invalid file → 503, fixed nonsecret error.
- The local JSON cache is untrusted at the HTTP boundary. Publication and
  readback enforce an exact fixed schema, six permitted projection keys,
  eight permitted per-Project keys, and state precedence valid only while
  global control is unverified; `recently_observed`,
  `globalControlVerified=true`, future/old projection dates and fabricated
  `lastVerifiedVaultWrite` are always rejected. This is value validation,
  **not** cryptographic authenticity against compromise of the service UID.
- The existing dark Status Manager B3 detail shows cached Project names,
  repositories, watch statuses, B3 observer status and count **read-only**,
  using textContent (never innerHTML). It does not display buttons that
  activate B3 Writer or Core Promotion.
- Global watch authority in the cached DTO is explicitly **unverified**:
  an Offline old LXC is not proof that every possible old Writer has been
  excluded. `projectedAt` means UI projection time, not GitHub observation
  freshness or a verified Vault modification.

## Known incomplete work and risks

1. **Phase settings UI is intentionally NYI**. A separately reviewed
   versioned/CSRF/revision-gated control route and human activation are
   required before any recurring production schedule. The old B3 relative
   settings route remains unchanged unless phase ON is explicitly configured.
2. **Source with zero watched Projects:** `DailySetup.project_source()`
   currently refuses `no_watched_github_projects` before yielding. The
   optional cache will be unavailable in this case, even though the product
   UI should eventually list disabled Projects. Address this through a
   separately reviewed read-only source-discovery contract, not by weakening
   B4 or making GET initiate a Nextcloud pull.
3. **Cache and live data are separate:** GET is a snapshot of the *last*
   bounded collector; it is not live synchronized with Nextcloud and cannot
   prove source freshness without a new authorized collection.
4. **Independent review is scope-bounded.** At the R5 published HEAD
   `1f188c3925e0fe982a7ba5ed40e4aafaba1e30f0`, independent read-only
   OpenCode reviews covered (a) warning/error classification, (b) current
   Project-only SQLite evidence and (c) F3 cross-boundary phase/GET/UI/CI
   integration. None reported a new P0/P1/P2 in its reviewed scope.
   GitHub Actions Run #42528911051 completed successfully across five
   jobs, including Node browser regressions in both Python matrix jobs.
   An isolated source integration with `main@e415dec978a77af4531773fafbb405b3c6f344c1`
   passed 813 pytest cases (2 skipped, 36 subtests passed) and four Node
   suites. These are SHA-specific source-only checkpoints, not a standing
   approval for future main/HEAD changes, full production E2E certification,
   Ready/Merge, Deploy or activation.
5. Runtime rollout and enabling the hourly B3 observer, and especially
   canonical CAS Writer/Core Promotion, remain *separate effect gates*.
   The `/api/v1/workflow-control` B3 canonical Writer slot stays pending.

## Synthetic evidence

- B3 phase tests inject fake clock, restart, transient generation outage,
  rotation **after slot admission, during collection, and immediately before
  terminal Receipt commit**, stale revision, legacy-relative mutual exclusion,
  active worker Off, duplicate tick, late missed slot and safe no-op default.
  B4 source tests verify that an old expected generation fails before the
  Nextcloud pull callback and that an in-flight shared configuration change
  cannot change the private pinned-rclone file used by the collector.
- Project cache tests cover disabled/misconfigured/terminal notes, stale
  cache/generation binding, opened-inode symlink rejection, cache permissions,
  GET-only no-provider behavior and source rotation after watcher return.
- Existing B3/HTTP, B2/B4, Formatter, Dashboard and browser History tests
  remain required before any release.
