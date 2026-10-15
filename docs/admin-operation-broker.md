# #300 Phase B — audited local operation broker (UNWIRED, NOT ACTIVATED)

**Status:** isolated implementation, not accepted operator service, no production
deployment, no WebUI POST endpoint, and **no permission to execute live systemd**.
This effect-capable Python transaction engine is tested only using synthetic
adapters. Constructor defaults: effects_enabled=False and require_root=True.
No CLI, socket listener, installer or service constructs it for production.

## Implemented source behavior

The input is a trusted **server-owned** status snapshot (never a browser-provided
arbitrary copy), plus a strict v1 request (UUIDv4, one of five fixed service IDs,
pause/resume/run_once, observed_at). The snapshot preflight checks syntax and
freshness, not current execution authorization.

The transaction engine:

1. Takes an exclusive nonblocking advisory lock in an existing private 0700
   audit directory. Paths are traversed by pinned openat descriptors without
   symlinks. Records are owned 0600, created O_EXCL, never overwritten.
2. Acquires a **separately qualified** cross-updater maintenance exclusion
   context manager, held until the durable outcome is complete. The engine does
   not manufacture one or accept a browser boolean as gate evidence.
3. Observes the fixed systemd timer/service pair directly. Requires an exact
   timer Unit target, expected /etc/systemd/system FragmentPath, loaded oneshot,
   idle Job/MainPID/ControlPID, stable timer state and a compatible service
   result. A mismatch with the initial trusted snapshot is rejected.
4. Persists and fsyncs pending.json and immutable request-ID intent BEFORE
   executing one command from the fixed argument map (no shell):
   pause = systemctl disable --now <fixed timer>;
   resume = systemctl enable --now <fixed timer>;
   run_once = systemctl start <fixed timer Unit service>.
   Run-once is allowed only with recurrence ALREADY paused, and an idle,
   non-failed service, to avoid competing with scheduled activation.
5. **Keeps the maintenance gate held for the entire synchronous command**,
   including all oneshot dependencies. The systemctl adapter's run-once timeout
   is bounded to 7200 seconds (two hours), rather than the erroneous 15-second
   limit. The eventual broker must run this in a dedicated worker, not block
   an HTTP request. If it times out or fails, the operation is indeterminate:
   the durable pending marker remains for manual recovery, NEVER assumed
   success.
6. Observes the exact timer/service after the command under the same gate.
   Run-once is confirmed only if recurrence remains paused and the oneshot
   reports a NEW successful monotonic attempt in the same kernel boot. The
   completed result is fsynced before removing pending.json.
7. The read-only lookup_result(UUID) verifies the immutable intent and outcome
   schema under the broker lock and returns a durable result **without
   re-running systemctl**. A lost reply or a maintenance-gate exit exception
   after result persistence is therefore recoverable. If a completed result
   exists but pending cleanup was interrupted, it reports
   completed_pending_cleanup requiring manual reconciliation.

Request IDs are rejected on effect replay, never re-executed. If an effect
may have occurred but cannot be verified, the broker persists indeterminate
and retains pending.json, blocking all subsequent operations. A SIGKILL or
lost process can likewise leave pending. Manual recovery is a separate
reviewed process; do not delete the marker to bypass its guard.

The systemd observation adapter reads only fixed property names through
a streaming pipe capped to 32 KiB BEFORE accepting bytes into memory; over-bound
or invalid output is rejected and the systemctl reader is terminated. stderr
goes directly to /dev/null (never buffered), as do the outputs of effectful
systemctl commands. Shell execution and environment inheritance are disabled.

Audit contains fixed IDs, action, time, boot ID, previous monotonic start
and outcome only: never credentials, systemctl output, arbitrary paths,
command lines, Vault content, raw exceptions or secrets. Errors are fixed
codes. Snapshot freshness is checked again after acquiring the maintenance
gate; unexpected maintenance/adapter exceptions are sanitized.

**What this does not prove:** the injected maintenance gate must exclude ALL
other actors capable of starting a workflow for the full command duration.
A newer systemd success is not sufficient if another actor can intervene. No
existing updater set has been qualified to meet that precondition.

## Remaining mandatory live-use gates

- **Deployment exclusion NOT QUALIFIED:** updater paths do not all share one
  lock. host_runtime_lifecycle.py uses the host-update.lock in
  /var/lib/obsidian-automation/deployments and semantic-refresh-inhibited.lock
  under /run/obsidian-automation. Legacy GitHub updater and other paths may use
  different locks. A simple pending-marker check races. The maintenance gate
  MUST hold actual common locks across all deployed updaters or block effects.
  Passing nullcontext or a caller-supplied True is not such evidence.
- **No privilege boundary yet:** non-root WebUI must talk through a separately
  hardened root-owned local broker socket with SO_PEERCRED/OS filesystem ACLs.
  NEVER run this module in privileged WebUI process.
- **No exposed HTTP mutation:** browser CSRF, Origin, replay, bounded body,
  rate-limit, audit/confirmation and private ingress are not wired. The
  existing status Dashboard keeps returning 405 for all write HTTP verbs.
- **Unit identity is not content identity:** production must pin staged unit
  revision, ownership, FragmentPath and content, and prevent concurrent swaps.
- **Pipeline concurrency:** final oneshot idle cannot prove all dependency
  workers idle; the live gate must check the complete workflow/queue/locks.
  Resume can trigger an immediate timer, so this is an effect not a preview.
- **Production qualification:** cross-UID permissions and disposable actual
  systemd unit end-to-end, competing updaters, crash/fault injection including
  fsync loss and manual reconciliation must be reviewed. No current tests
  qualify production.

The synthetic tests exercise fixed-command mapping, state transitions,
new successful run identity, stale/forged observations, wrong unit/Job/PID,
symlink and directory permissions, duplicate requests, lock contention,
indeterminate effects, durable read-only result lookup, journal persistence
and redaction. They cannot prove exclusion against real updater processes.

Branch is stacked upon Draft #364. No code merge to the integration branch or
main is implied. Final main PR #311 and live deployment each require their own
explicit human approval.
