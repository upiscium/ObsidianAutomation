# #300 Phase I — durable, single-LXC operator intake and receipt protocol

**Status: source-only, NOT installed, NOT activated.** This child contains
an effect-capable *test bridge*, but the public WebUI Execute button remains
disabled, the production default queue is disabled, and no root daemon exposes
submit, receipt or a worker execution loop. No ObsidianAutomation production
systemd unit or remote Vault data is touched by this change.

The supported topology remains exactly one active service-execution LXC per
managed Vault. Simultaneous active instances acting on the same Vault are
unsupported; behavior and data integrity are not guaranteed. This does not
remove local inter-process update/worker locks, role UID separation, CAS/ETag
or independent approval requirements.

## Why the previous preview cannot simply execute

The previous Phase H Unix Socket validates OS peer UID/GID and returns
preview_only / can_execute=false. That preview neither reserves a schedule
slot nor provides permission to change systemd state. It can also become
obsolete before a human acts. Actual changes need a separate authenticated
OS-level admission and *durable intent* before any possible effect, plus a
readable, immutable receipt that survives a lost client reply.

Moreover, a one-shot service can complete before downstream OnSuccess
workers finish. Source-level systemd state checks cannot by themselves prove
the entire role's end-to-end result. Therefore the Phase I intake explicitly
supports only **pause** and **resume** of recurrence, not **run_once**.
Running a one-shot remains blocked until chain-completion evidence, atomic
local launcher exclusion and its own recovery contract are implemented and
reviewed. Pause disables a timer's future recurrence; it does **not** cancel
an already executing job or imply that current work was drained.

## Root-owned durable queue (not a production service)

The DurableOperationQueue class requires an existing, pinned root-owned
0700 directory by default. Neither browser requests nor Unix peer data can
choose a filesystem path, service unit name, command line or credential.
Its constructor defaults to enabled=False, require_root=True.

An explicitly created queue in a **disposable test context** can accept
exactly one fixed request into a durable record. Requests contain version=1,
UUIDv4 request_id, one of five immutable dashboard service IDs, and exactly
one of pause or resume. A global active.json marker is written and fsynced
BEFORE the immutable per-request file. The global admission.lock file is
0600, single-link, same-UID and exclusively flocked without blocking. No
second request ID may be admitted until the first is fully completed.

Duplicate IDs with identical contents return the existing durable state
rather than creating a second operation. Reused IDs with changed actions or
services are rejected. The queue's externally observable statuses are:

- accepted — intent stored and fsynced; no effect yet.
- processing — immutable claim stored and fsynced BEFORE possible effect.
- completed — a separately trusted worker reported a verified completed
  broker effect; a result receipt is durable and active marker cleared.
- completed_pending_cleanup — completion persisted, but the active marker
  removal was interrupted. Manual reconciliation required.
- indeterminate — effect may have occurred and the original active marker
  remains. Automatic retry is prohibited; manual reconciliation required.

The queue's receive/receipt socket does **not** execute any action. Its
separate work_once hook cannot run automatically, and when explicitly
called from a trusted worker it persists processing evidence before
invoking the worker's callback. Unknown, failed, interrupted or throwing
callbacks are conservatively indeterminate and retain the active marker.

**Redundant orphan protection:** before admitting a fresh request when the
active marker is absent, the queue scans a bounded, root-owned immutable
history. Every older request must have a matching durable processing claim
and verified completed result. Missing/corrupt/uncertain records block new
work even if active.json was lost. The journal never silently repairs or
deletes older records. Journal capacity is bounded to 2048 entries; archival
and manual repair are a separately reviewed operator action.

## Separate OS-local intake socket

OperationQueueSocket is a separately named AF_UNIX/SOCK_STREAM endpoint.
It reuses the existing read-only socket's pinned directory, 0600 listener
lease, 0660 socket inode and Linux SO_PEERCRED UID/GID checks. The fixed
queue input understands only:

- submit: fixed request ID, fixed service, pause/resume, literal
  operator_confirmed_v1 marker (an API format marker, NOT proof of a human
  approval) and exact v1 JSON fields.
- receipt: read an existing immutable receipt by exact UUIDv4.
- capabilities: returns only operation flags false (observation-only).

There is no execute, systemctl, run_once, arbitrary file, config or secret
command. There is no public Dashboard submit/receipt route. Both the socket
and intake queue are disabled/uninstalled on actual production hosts until
a separate security acceptance. A spoofed server's can_execute=true cannot
be treated as authorization by the strict client projection.

## Synthetic full path and real Linux identity test

The test-only bridge, run_accepted_control_once, demonstrates all of:

1. An actual AF_UNIX request is accepted and durably audited (no effect).
2. A separately explicitly invoked worker claims one operation.
3. A trusted server-owned systemd snapshot is acquired only by that worker,
   never from the original browser request.
4. A disabled-by-default OperationBroker must be separately effect-enabled
   and configured with a qualified same-LXC maintenance gate, installed
   unit provenance and fixed adapter. Test fixtures use a **fake adapter and
   a no-op test gate**, which are NOT acceptable production authorities.
5. Only when the broker reports local_effect_observed / completed for the
   exact request ID/service/action does the queue mark completion durable.
   A failed, stale or uncertain result leaves the queue and broker journals
   blocking further automatic effects.
6. After a lost response, a duplicate ID or receipt lookup cannot drive a
   second action.

An additional *explicitly executed* manual Linux test created an
unpredictably named root-owned 0710 directory under /run (removed on exit),
a root-owned 0700 queue directory, and a root-owned 0660 Unix socket accessible
to the test group. It used a real root process and a **separate nobody UID**
process to prove an allowed peer can submit an inert pause intent, while a
root peer not matching the configured allowed Web UID is disconnected. No
systemd unit was operated. Running test source as root from a developer
checkout does NOT qualify production code immutability, packaging, installer
privileges or future service access rules.

## Remaining gates before live Pause/Resume and Run once

- **Not installed:** no root-owned intake socket service unit, no root
  background worker, no enabling of queue admission, and no registered
  operator route to submit or settle effects.
- **Atomic local authority:** the current host/updater lock pair excludes
  cooperating updaters, but not all legacy/manual root launchers or worker
  OnSuccess admissions. A future effectful worker must own a qualified,
  cross-role gate for the *entire* operation and revalidate installed unit
  files and code digest under that gate.
- **Confirmation:** an HTTP user-facing confirmation must be explicit and
  bound to the preview request identity. The literal intake marker alone
  cannot authorize any root operation. The WebUI's disabled Execute control
  remains disabled until this security boundary is reviewed.
- **Results and repair:** a separate bounded receipt read API, audited manual
  recovery/cleanup of orphaned pending markers, and clear instructions for
  service errors, lost responses and host crashes are required.
- **Actual systemd:** disposable systemd units, cross-UID Web/Broker
  isolation under an installed package revision, race/crash/fault tests,
  and independent Security/Correctness reviews are still required.
- **Run once:** still explicitly denied in the queue. Terminal oneshot
  success does not prove all downstream OnSuccess jobs finished or that
  follow-up jobs cannot be scheduled after the observation.

Do not merge a Draft #300 child to main or activate production operations
without the approved #311 Human Final Merge and separate Cutover gates.
