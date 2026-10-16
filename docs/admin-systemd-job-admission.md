# #300 Phase J — Minimum systemd job admission for Run once

**Source/test only, NOT installed or effect-authorized.** The operator's
accepted scope is safe local systemd admission, request-ID deduplication,
fixed systemd status observation and minimal durable receipts. GitHub Sync,
Daily, Core Promotion, AI and other services retain responsibility for
their own backend writes, CAS/ETag, retries and business-level recovery.

## Three distinct outcomes

1. Request accepted: fsynced UUIDv4 request and processing records exist;
   the Manager has not necessarily received a Job request.
2. systemd_job_accepted: the root-owned worker received a typed systemd
   Manager Job path from StartUnit. This is ONLY acknowledgement of a queued
   systemd unit Job, not evidence of execution, OnSuccess completion or
   Gitea/GitHub/Nextcloud synchronization success.
3. Service or business completion: read current fixed systemd unit state
   and, separately, the service's own immutable result/recovery artifacts.
   The #300 Broker neither invents nor retries those business transactions.

Free-text journalctl tail remains useful for bounded recent history but is
NOT an atomic source of truth for admissions/deduplication: logs may be late,
rotated, rate limited or have no correlation with the operator request UUID.

## Fixed systemd Manager D-Bus call

BusctlStartUnitManager calls systemd Manager StartUnit with signature
ss (fixed service name, job_mode = fail), which returns an object path o.
The fail mode refuses replacement of competing queued jobs. The systemd
Manager also provides JobRemoved (uoss) for future terminal job observation,
but this source slice **does not subscribe to JobRemoved**. Adam's systemd
261 confirmed those signatures by read-only bus introspection.

Only the five fixed service names from the reviewed registry are eligible.
The implementation uses /usr/bin/busctl, no shell, no environment inheritance,
a <=5 second monotonic deadline, <=512 ASCII bytes of output, and accepts
only /org/freedesktop/systemd1/job/<nonzero-decimal> paths. Missing, malformed,
timed-out or nonzero D-Bus responses remain unconfirmed, never success.

RunOnceJobDispatcher is OFF by default and needs a separately supplied local
maintenance exclusion context. Under that gate it re-observes the fixed
timer and target service, verifies canonical unit path and no pending
Job/MainPID/ControlPID, checks the server-owned fresh observation, requires
the target timer to be paused, and verifies the boot ID before/after StartUnit.
It does not require unrelated sibling timers or all OnSuccess workers idle:
services own their downstream effects and systemd manages its own Job state.

## Durable request and receipt

DurableOperationQueue adds allow_run_once=False, independent of its existing
enabled=False default. A separately enabled root worker must have an enabled
RunOnceJobDispatcher before claiming any queue configured to allow Run once.
The existing root-owned singleton queue fsyncs intent and processing records
before calling the systemd Manager, and validates Job path, boot ID, action,
service and request ID before persisting the immutable outcome.

The new terminal receipt is systemd_job_accepted with authority
systemd_admission_only and can_execute=false, including the Job object path
and boot ID for correlation. It is intentionally not named completed. A
duplicate UUID returns that same receipt rather than scheduling again; an
uncertain/failed admission leaves the pending marker requiring explicit
manual reconciliation and will not be automatically retried.

The Unix Socket only accepts fixed queue submissions and reads receipts.
It does not execute work. The browser Execute button remains disabled and
no installed root daemon, systemd unit, sudo/polkit rule or public mutating
HTTP route has been added.

## Phase K isolated runtime evidence

The [isolated real-systemd test](admin-systemd-isolated-acceptance.md)
exercised the production Job parser through a test-only user-bus shim,
confirmed the Job receipt is NOT service success, and found that a real idle
systemd Job is an empty string rather than always 0. Both documented idle
encodings are now accepted with unchanged PID and unit-identity checks.
A separate disposable kernel-flock test confirmed cooperating host/semantic
update lock exclusion, inhibitor markers and safe release. Neither test
authorizes installed root Broker actions or uncooperative launchers.

## Legacy path and remaining acceptance

Earlier Phase-B OperationBroker.apply(run_once) retained synchronous
oneshot/OnSuccess completion requirements. This is historical source-only
compatibility; the new queue Worker uses RunOnceJobDispatcher instead.
Production wiring must never select the legacy path for Run once.
Pause/Resume remain under the existing Broker's fixed timer-state checks.

Source tests use fake Manager Job replies and fake systemd status observations,
with strict UUID, path, root gate, boot and failure behavior. The real D-Bus
method signatures were read-only inspected, but actual systemd Job admission
and JobRemoved signal correlation with disposable units still need E2E review.
A future installer must pin code/unit hashes, role ACLs and a verified
same-LXC update/launcher maintenance gate; a no-op test gate is not authority.

The supported topology remains a single active execution LXC per managed
Vault, not active-active. Final integration/main merge and any production
Cutover remain independent Human approval gates. #300 stays OPEN.
