# Read-only mirror status v0

This is the first observer-only slice of #64 / #67. It does not alter the accepted
mirror service, timer, global lock, or transport. It also does not observe the
separate GitHub Project watcher.

## Usage

On the AI Writer host, with Python 3.11+ and the package installed:

```bash
python -B -m obsidian_automation.mirror_status
python -B -m obsidian_automation.mirror_status --json
```

The module can also run directly as a standalone standard-library-only file:

```bash
python3 -B mirror_status.py --json
```

Use an ordinary account permitted to read the selected systemd properties; do not
grant it Vault, lifecycle, or credential permissions. Run in the same boot/time
namespace as the local system manager. It cannot query another host or time
namespace safely by feeding in that host's monotonic values.

Temporary threshold changes are process arguments, not timer modifications:

```bash
python3 -B mirror_status.py --json --warning-seconds 900 --critical-seconds 1800
```

These 15/30 minute values are initial diagnostic defaults, not an adopted SLA.
The CLI exits 0=OK, 1=WARNING, 2=CRITICAL, 3=UNKNOWN. Wrap diagnostic commands in
`if ...; then ...; else ...; fi` where a parent shell has `errexit`; a warning exit
must not accidentally close a login shell.

## What is observed

Two fixed `systemctl show` calls, each with a 10-second timeout, request only:

- timer: LoadState, UnitFileState, ActiveState;
- service: LoadState, Type, ActiveState, SubState, Result, ExecMainCode/Status,
  ExecMainStartTimestampMonotonic, ExecMainExitTimestampMonotonic.

The observer does not read ExecStart, Environment, journal text, filesystem
artifacts, credentials, endpoint settings, or current remote bytes. It never calls
start/stop/restart/enable, fetches a URL, starts an LLM, or creates a job.
Unknown property enum values are bucketed, not echoed. Collector errors do not
include raw stdout/stderr or private settings.

## Classification

An enabled/active timer alone is insufficient. A completed `oneshot` in
`inactive/dead`, Result=success, ExecMainCode=1, ExecMainStatus=0, with ordered
positive main-process timestamps establishes a successful latest attempt.
Its age is computed using a monotonic clock in the same namespace. This avoids
wall-clock/timezone parsing and ordinary wall-clock adjustments.

Age at or above the warning/critical threshold changes severity. Missing/disabled/
inactive scheduling, a failed unit, or a recorded failed attempt is surfaced
separately. Unexpected values, absent completion evidence, timestamp mismatch,
read errors and unsupported unit types never produce OK.

An activating oneshot is reported as `in_progress`, not as a failed service or a
successful refresh. When available, `running_seconds` measures elapsed runtime;
**it is not lock wait**. `lock_wait_seconds` remains null. The observer does not
know whether time was spent waiting for a lock, network, rclone or something else.

## Deliberate limits

`source=systemd_latest_attempt`, `durable_success_history=false` and
`atomic_snapshot=false` are part of the JSON report. systemd observations are not
a transaction across both units and can race with a timer activation.

When the latest attempt failed, no historical last-success timestamp is inferred.
After a reboot/unloaded unit or during a current run, that information may also be
unavailable. `last_success_age_seconds=null` means unknown, NOT zero, infinity, or
"never succeeded". In-progress runs currently yield UNKNOWN rather than claiming
freshness. CLI output and exit status describe only the completed observation.

The next increment must persist a bounded Sync-owned health projection with
success/failure timestamps and explicit attempt/lock events. A limited observer
may read that projection, but must not gain access to unrelated credentials or
artifacts. Job backlog monitoring, notifications, durable alert deduplication,
external heartbeat, and read-view consistency remain separate incomplete work.
The present CLI is useful for manual diagnostics, not sufficient for unattended
alerting or generation admission control.

No healthy result proves a consistent local snapshot, current remote/local byte
identity, a canonical mutation, Human approval, or a Receipt. Those boundaries
remain the responsibility of the existing pipeline.

## Source semantics and acceptance

Systemd documents service process timestamps and exit metadata here:
https://www.freedesktop.org/software/systemd/man/latest/org.freedesktop.systemd1.html

Oneshot inactive/dead behavior is documented here:
https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html

Synthetic classifier/collector tests do not replace the user's read-only production
run. Verify on the deployed host without modifying its service or timer. Later
failure-injection tests belong in disposable fixtures, not the live mirror.
