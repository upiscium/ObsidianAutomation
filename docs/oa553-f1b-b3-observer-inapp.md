# #553 F1/B — B3 GET-only Observer Job in existing single-LXC application

**Source-only stacked successor to PR #555; no production installation,
activation or Vault Writer authority in this change.**

This step connects the #553 F1/A B4-pull-only / GitHub-GET B3 bridge to the
existing `snapshot_app.py` one-process scheduler and private API. It does
not attempt the canonical B3 Writer, Core Promotion or Gitea publication.
The existing B2 Snapshot / B4 Daily / Formatter continue to own their
separate persisted jobs and are unchanged by this new Job until an approved,
pinned release is deployed.

## Operator-facing read model

- `GET /api/v1/b3/observer` returns the local B3 observer settings and last
  durable attempt, including finite observation/proposal counts. This GET
  **never starts a remote pull, GitHub call, Worker or Vault write**. HEAD
  has the same no-effect semantics.
- `GET /api/v1/b3/observer/runs/{32-lowercase-hex}` reads one local
  receipt only. Bad paths or missing records return 404.
- `POST /api/v1/b3/observer/runs` accepts exactly one
  `{"request_id":"..."}` with same-origin + one-time CSRF. It queues one
  asynchronous GET-only Project observation, returning 202 with a run ID,
  or returns 409 when unavailable/busy. Repeating an exact request ID
  returns the same durable receipt, never a second network observation.
- `POST /api/v1/b3/observer/settings` accepts only
  `{"enabled":boolean,"interval_seconds":integer}` with
  3600–86400-second bounds and the same CSRF/Origin controls.
- Duplicate JSON keys, nonfinite constants, unknown keys, stale input
  types, path tricks and unsupported HTTP verbs are rejected. No job
  method accepts arbitrary command, file path, remote repository or URL.
- JSON replies contain no Nextcloud secret, GitHub token, raw Project text,
  GitHub raw error, or live Vault path. Every status includes an explicit
  `vault_write_authorized=false` / `cutover_authorized=false`.

The current #480 `/api/v1/workflow-control` v1 read-only schema remains
unchanged: its B3 slot is still **pending** and must not falsely claim
canonical Writer readiness merely because a GET-only Watcher is connected.
Project-by-Project list (#534/#536) is a separate pure read projection and
requires a later opt-in integration against complete disabled/enabled
Project headers. Do not present a count of 9 watched Projects as a complete
Vault Project inventory.

## Durability and sequencing

- The Job is owned by the existing nonroot `obsidian-snapshot` app.
  Its private `github-b3/observer-job.sqlite3` and independent owner
  `flock` serialize commands across local processes. The existing B3
  `watcher.sqlite3` retains per-Project observations; no new LXC is needed.
- Schedule is OFF by default; construction creates only local owner state
  and never fetches Vault/GitHub. Restart converts incomplete queued/running
  attempts into `unknown/interrupted` and never auto-replays the same ID.
- Scheduled admission atomically persists the new run and advances
  `next_due` before launching the worker. One running process prevents
  duplicate work, including browser double clicks.
- The B4 configured source generation is pinned when enabling the B3
  schedule. Nextcloud source changes fail closed and the next tick
  persists OFF, requiring explicit re-enable. Independent B4 one-run
  source generation checks also reject source rotation during a pull.
- Normal duration may be tens of seconds. A queued/run result is not a
  verified Vault write. Unknown or partial GitHub observations never
  become success in the local Job.
- For now the B3 observer uses the B2/B4's current relative
  `clock()+interval` cadence; **this is not the user-requested
  timezone/12:00 wall-clock anchored scheduler** (#533/#535), which still
  requires integration. Do not advertise anchor semantics in the WebUI.

## Production gates and fast-track continuation

1. #555 F1/A must first pass source review/merge; this stacked source
   change consumes the new `github_b3_watcher_bridge` module. Inspect
   current main, exact-head CI and review before moving either PR.
2. Deploy only one exact reviewed full application build to the *existing*
   `obsidian-control` LXC, with B2/B4 + the current #301/#304 GUI changes
   included; do not clobber newer service drop-ins from another session.
   Record full state/config backup and one guarded rollback, without
   revealing credentials.
3. Default OFF and verify the new B3 GET status surface before enabling a
   **single** read-only Run Once. Independently confirm the new durable
   run record and Gitea/B2/B4 continuity before scheduling periodic B3.
4. Separate B3 Writer cutover: prove one active canonical Writer, approved
   scoped Nextcloud CAS, independent readback, retry/unknown protection
   and distinct Core Promotion drift guard. **This PR does not authorize
   any such effect.**
5. Integrate all Projects including `github_watch:false` (#534),
   anchored scheduling (#533), then B5 manual ZIP intake and B1 AI
   workflow on the same application. Keep #553/#483/#298 open until
   actual B1–B5 live acceptance.

This step is fully source-only. No GitHub/Gitea private job execution,
real Nextcloud PUT/DELETE, token revocation, remote push, live LXC service
restart or deployment is caused by building this PR.

## Verification

- Disposable source-only tests cover local owner lock, default OFF,
  idempotent manual requests, busy job handling, transition on source
  rotation, restart with unknown/interrupt, fail-closed collector
  responses, complete GET/HEAD vs POST boundary, CSRF/Origin and strict
  bounded JSON including duplicate-key refusal.
- Broader selected B2/B4/Snapshot/WebUI/Watcher regressions should stay
  green; this is not a full system or production E2E proof.

Refs #553, #483, #298, #305, #304, #533/#534 and PR #555.
