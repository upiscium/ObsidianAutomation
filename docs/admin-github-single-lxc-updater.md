# #300 Phase F — legacy GitHub updater coordination on one LXC

**Status: isolated implementation with fake-runner tests. NOT a production
rollout or authorization to invoke this legacy updater on a serving host.**
The preferred target updater remains the exact-SHA host-managed
obsidian-automation-update.

The old obsidian-github-production-update was designed for a separate LXC
and did not hold the host runtime's deployment/semantic-refresh locks.
Accidentally running it in the same host could modify timers or packages
during a different deployment.

## Opt-in, fail-closed coordinated mode

The new --single-lxc-coordinated flag is OFF by default, preserving the
historical separate-LXC behavior. When opted in:

1. Require the pre-provisioned root-owned host deployment journal directory
   and acquire existing host-update.lock nonblocking. Reject an existing
   pending-runtime.json. Do not create a host lock.
2. Acquire semantic-refresh-inhibited.lock SECOND, using the existing
   RefreshUpdateInhibit logic. The fixed additional owner is github-updater.
   Any pre-existing pending inhibit, including the same SHA and owner, is
   rejected pending explicit manual reconciliation of prior timer intent.
3. Validate exact source SHA, clean checkout and timer state while holding
   BOTH locks. Publish/fsync semantic-refresh-inhibited.json with owner and
   target SHA before any timer disable. Semantic refresh OnSuccess callbacks
   cannot start while this marker exists.
4. Disable both Project Sync and Daily Progress timers. On any failure of
   the first timer stop, attempt to contain both timers and retain the marker.
5. Timer disable is NOT equivalent to draining active jobs. Require an
   independently observed fixed inventory of all nine GitHub Sync/Daily
   dependency services, and both timers paused. Reject any queued Job,
   active PID, inconsistent unit path, missing unit or unknown state before
   checkout, pip install, daemon reload or other package/unit mutation.
   The public updater entrypoint does not accept a caller-supplied
   positive workflow snapshot.
6. Complete the original exact-SHA update and safe smoke under both locks,
   restore timer state, then remove/fsync the owner-matched marker only after
   durable success. On failure or process interruption, retain the marker,
   block other updates and require reviewed manual reconciliation. Never
   automatically replay a same-target update after a partial effect.

## Shared lock compatibility

The host-runtime updater takes host-update.lock then the shared refresh lock
in that order. Pre-review takes the shared refresh lock. The Dashboard
cooperating-updater gate takes both in the same order. Thus a coordinated
legacy GitHub update conflicts with those cooperating paths. An incomplete
github-updater inhibit marker also blocks host-runtime and the Dashboard
Broker after the lock is released.

This does NOT make independently installed old legacy binaries safe.
Existing uncoordinated executables must be disabled during the approved
single-host cutover, and the new consolidated host-managed updater is the
recommended normal path. An opt-in path is not a runtime authority receipt.

## Remaining acceptance blockers

- Full local service-launch and worker admission is not yet covered by this
  updater lock. Core Promotion retains its role-specific promotion.lock.
  Even a positive idle snapshot is not a globally atomic lock.
- Failed transactions may leave intentionally disabled timers and durable
  pending records. Same-target replay is prohibited until an operator safely
  reconciles original intent and remote outcomes.
- The first-install target must prepare host locks, Unix service identities
  and systemd unit inventory before coordinated mode can be used.
- No root Broker daemon, HTTP write path, service-control activation,
  actual-systemd E2E or production Vault effect has occurred.
- Only one active ObsidianAutomation execution LXC per managed Vault is
  supported; cross-LXC distributed fencing is not required for unsupported
  active-active configurations.

The fake-runner tests exercise locking, marker owner/SHA, quiescence, stop
failure containment, smoke failure, replay rejection and legacy compatibility.
