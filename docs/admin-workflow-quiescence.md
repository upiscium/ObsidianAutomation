# #300 Phase E — fixed dependency-chain admission for Run once

**Status: isolated source and synthetic tests only. No live service controls,
root daemon, write API, or production systemd effect is enabled.**

The existing #367 broker validated only the target timer and its final
oneshot. That is insufficient to establish the state of the full workflow:
systemd Requires/OnSuccess jobs may still be executing after the visible
last service has become inactive. #369 added a partial local updater lock, but
it did not observe the worker/dependency graph.

This slice adds a fixed (reviewed, source-controlled) inventory of 28 managed
oneshot services and all five timer targets, partitioned into the three
single-LXC domains already documented in host_runtime_lifecycle.py:

- **AI domain:** Vault Pull, Pre-review and 18 service units, including the
  Semantic Index OnSuccess prepare/embed/finalize chain.
- **GitHub domain:** Sync, Daily Progress and nine service units; both timers
  share a Vault mirror prerequisite and must be admitted as one domain.
- **Core domain:** Core Promotion timer and its one service unit.

Before a **Run once** intent may be emitted, the broker now requires a
*separately provided, trusted workflow observer*. None is configured by
default. If absent, malformed, or throwing, the broker rejects the operation
before creating its durable journal or invoking systemctl.

The original strict phase-E admission (require_run_once_quiescent) checks
ALL fixed units in the relevant domain, not merely the requested service.
These historic rules are NOT the current privileged Run once path: for the
live require_run_once_nonoverlap predicate see the dedicated #437 section
below.

1. Every related timer loaded from the expected canonical systemd unit path,
   enabled state **disabled**, runtime **inactive/dead**, no queued Job,
   and an exact fixed timer-to-oneshot Unit identity.
2. Every role service loaded as **oneshot**, inactive/dead, no queued Job,
   MainPID or ControlPID, with its expected canonical FragmentPath.
3. No unexpected/missing units, additional systemd property names, unbounded
   strings, masked/inconsistent/missing or transitional state.

The SystemctlAdapter reads only these allowlisted properties through its
previously reviewed bounded 32 KiB streaming observer. The check runs under
the separately injected maintenance gate and before any broker journal effect.
The ordinary pause/resume scheduling actions do not require unrelated domain
idle snapshots, since stopping a timer is not a request to cancel an already
running service. Run once remains restricted to **paused recurrence**, now
requiring the other timer in the same authority domain to be paused too.

A positive read **is only point-in-time evidence**. It is NOT a qualified
atomic admission lock against future timer jobs, a complete provenance/content
attestation for each service unit, or proof that uncooperative root/legacy
updater paths cannot launch work. The supplied maintenance gate must still
exclude local service launchers and all updaters before real service-control
capabilities can be enabled. A full root-owned, cross-UID broker, CSRF/Origin
WebUI write path, and disposable real-systemd race/crash tests remain pending.
Existing #294 Stage/Activate and #309 cleanup effect boundaries remain intact.

Verification: synthetic full-role ready/busy/unknown/malformed cases,
regression tests for the gated broker's run_once, fixed installed role
inventory and source fixture Requires/OnSuccess closure. No production
systemd execution, Vault or remote data access is performed.

Error handling is deliberately asymmetric: exceptions thrown by an injected
observer are always replaced with one fixed workflow_observation_failed code,
even if the observer raises an exception type used by the validator. Only the
internal fixed-schema validator may report a more specific fixed reason.

Only built-in dicts are accepted for the fixed systemd property maps; custom
mapping subclasses cannot run user-defined iteration logic or inject error
strings into the broker's fixed rejection channel.

Key types are also checked before set comparisons, so even a built-in dict
holding a crafted str subclass cannot execute custom hash/equality logic.
More generally, only fixed enumerated workflow rejection codes are emitted;
unexpected injected exceptions are mapped to a single generic code.


## #437: Run once sibling timer admission in the live worker

The original phase-E require_run_once_quiescent remains strict: it requires
all same-domain timers disabled and inactive. The later privileged
require_run_once_nonoverlap uses a narrower live-conflict predicate under
the existing root-owned updater gate: the selected timer must be
explicitly paused (disabled/inactive/dead). A sibling may be
enabled/active/waiting, disabled/inactive/dead, or
enabled/inactive/dead (enabled-runtime is also accepted) only if
its fixed Unit, loaded canonical FragmentPath and empty Job are verified.
The final state is a configured-on-boot timer that is not currently running,
not an active recurrence. It is not evidence of future cross-domain exclusion.

This distinction matters on the deployed single-LXC configuration:
obsidian-github-daily-progress.timer is enabled/inactive/dead, while
the user deliberately paused obsidian-github-sync.timer for a one-shot.
The former was unnecessarily refused by the previous live-conflict
predicate, making the Worker fail closed with an indeterminate receipt.
No relaxing of selected-timer proof, job checks, source integrity,
all nine relevant GitHub service idle/identity checks, or the updater gate
is justified by this change.

The prior indeterminate production request must not be retried or
its Queue evidence removed automatically. Separate, explicitly reviewed
manual forensic reconciliation is required before another production
Run once acceptance attempt, even after this source fix is merged.
