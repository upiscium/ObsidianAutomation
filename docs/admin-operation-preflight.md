# #300 Service operation preflight — Phase A (NO EFFECT)

This is an **unwired pure function** implementing typed intent validation for
pause, resume and run-once on the five fixed Dashboard service identifiers.\nRun-once is restricted to deliberately paused recurrence (not currently\nscheduled timer execution), with a non-failed idle service.

It is NOT an operator API, privileged broker, durable audit, authorization, or
deployed feature. All HTTP mutation verbs remain 405. A preflight candidate
explicitly identifies its authority as `requires_broker_live_recheck`.

- Requests have exact fixed schema: v1, canonical UUIDv4, allowlisted service
  identifier, one of three fixed actions, exact observed timestamp.
- A local observation must be at most 30 seconds old and have one unambiguous
  stable service identity. Cross-field combinations of scheduled/paused, latest
  execution and health must exactly match the read model's classification. Unknown/masked/running/stale states fail closed.
- `deployment_quiescent` defaults false; even a true hint is NOT sufficient
  for real operations.
- The pure function emits no shell/systemctl argument and cannot mutate
  Nextcloud, Vault, systemd, credentials, or the filesystem.

## Future broker authority gates (NOT implemented)

Before binding an HTTP POST route or enabling controls, create a separate
fixed-allowlist local OS-authorized broker. It must independently re-observe
systemd and maintenance/deployment intent state *under an inter-process lock*,
hold durable idempotent request journal with crash-safe audit and recovery,
bind browser CSRF/Origin and private ingress, and use fixed command arguments.
It must be non-root at the Web process and narrowly privileged at the
broker; never accept arbitrary unit names, shell, execute, stage or activate.
Ensure #294 stage/activate and #309 cleanup effects retain their existing
human/effect gates. Require disposable systemd fixtures, cross-UID and
concurrency tests and independent Security/Correctness review. Production
activation is an additional explicit human gate.

## Known boundaries of this preflight

A caller can forge the in-memory observation or pass `deployment_quiescent=True`.
This pure function CANNOT independently attest either one and intentionally
grants no authority. Only a future broker with its own independently obtained
systemd properties, fixed-unit identity checks and deployment lock can do so.
The status panel's displayed service unit (e.g., pre-review status) is not
necessarily the full workflow's correct *run-once* entrypoint. Service-specific
trigger mapping requires explicit review before any operational effect.
