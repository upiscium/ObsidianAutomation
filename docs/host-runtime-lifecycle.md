# Exact-SHA automation host lifecycle (contract 4)

This is the completion of the source-side bootstrap transaction for #110.
It manages only `obsidian-automation`, not the separate Gitea Runner or Snapshot
LXC. The dormant compatibility runner account in older authority manifests is
not a serving runner and is not started or removed by this lifecycle.

## Boundary and sequence

The installed launcher fetches an explicit full SHA, requires it to be reachable
from fetched `origin/main`, and invokes that revision's bootstrap from a detached
worktree. The target source is verified before importing lifecycle code. The
public CLI/child arguments remain compatible with the previous contract-3
launcher, which does not need to understand the new systemd transaction.

The target implementation:

1. Locks a root-owned deployment journal directory against concurrent updates.
2. Rechecks the clean production `main` checkout and target ancestry.
3. Captures the independent enabled/active states of all four managed timers and
   fsyncs `pending-runtime.json` **before** changing any timer.
4. Disables/stops those timers and drains all thirteen managed role services.
   It does not kill a running writer to make an update succeed. A 300-second
   drain deadline fails the update before package/authority mutation.
5. Updates the production checkout to the exact SHA, installs the non-editable
   package using the local build wheelhouse, reapplies authority and installs
   the target-owned launcher.
6. Atomically installs each of the sixteen canonical systemd units, rewrites
   their legacy runtime paths to the shared runtime, binds the AI revision env
   to the target SHA and reloads systemd. The unit set is not a multi-file atomic
   filesystem transaction; it is changed only while recurrence is quiesced.
7. Runs import checks, pre-review safe smoke, GitHub safe smoke, orchestration
   file ACL bootstrap and private-config readability gates for configured roles.
   No LLM request, GitHub observation or Nextcloud write is part of safe smoke.
8. Restores only the captured timer states after all gates pass. Starting a timer
   can cause a normal production run according to its scheduling policy.
9. Writes completion evidence and removes the pending intent only after success.

Managed recurrence is exactly:

```text
obsidian-ai-vault-pull.timer
obsidian-pre-review.timer
obsidian-github-sync.timer
obsidian-core-promotion.timer
```

The recurring timers use `OnActiveSec=` for their initial monotonic arm rather
than `OnBootSec=`. The updater deliberately stops and restarts timer units long
after boot; an already-passed `OnBootSec=` deadline does not provide a new
monotonic schedule when the target service's activation/inactivation timestamps
were garbage-collected. `OnActiveSec=` gives every deliberate timer activation
a fresh initial deadline, while the existing `OnUnitActiveSec=` /
`OnUnitInactiveSec=` values retain the steady-state cadence after a service run.

Restore acceptance is stronger than `ActiveState=active`. A timer that was
previously active must return as either `waiting` with a finite
`NextElapseUSecMonotonic`, or `running` while its trigger service executes.
`active/elapsed` is a failed restore. The transaction then uses the ordinary
fail-closed containment path and leaves all managed recurrence disabled with the
original intent journal preserved for same-target recovery.

A first install with absent/disabled timers stays disabled. An enabled but stopped
timer remains enabled but stopped; a disabled but running timer remains disabled
but running. Masked, linked, runtime-enabled, transitional and unknown timer
states are refused before mutation. Query/bus failures are not interpreted as
absent units. No `systemctl unmask` or Gitea/Snapshot unit command is issued.

## Run

Before first bootstrap install the documented Debian prerequisites, including
`python3-venv`, `python3-setuptools-whl`, `python3-wheel-whl`, `acl`, Git and CA
certificates. The wheelhouse must contain the build backend and its requirements.
Private credentials are provisioned separately, never from this repository.

On an existing canonical host, run the installed launcher with the full reviewed
merge SHA. Strict shell options belong in a child shell, not the SSH login shell:

```sh
bash -s <<'SCRIPT'
set -euo pipefail
/usr/local/sbin/obsidian-automation-update \
  --target-sha '<FULL_REVIEWED_MERGE_SHA>' --profile automation
SCRIPT
printf 'child-shell rc=%s\n' "$?"
```

Do **not** manually disable the timers before an ordinary contract-4 update: that
would change the intent being captured. Old contract-3 launchers hand off to the
new target-owned implementation. Fresh bootstrap continues to use the exact
source copy of `tools/production_bootstrap.py`; there is no floating-main deploy.
The automation production profile requires its canonical app/venv paths.

## Failure and recovery

Catchable failure after quiesce attempts to leave all managed timers disabled.
Original intent remains private in:

```text
/var/lib/obsidian-automation/deployments/pending-runtime.json
```

Receipts contain fixed stages, SHA identity and timer flags, not credentials,
command output, environment values or provider responses. A same-target retry
reuses the saved intent rather than mistaking disabled recovery timers for the
operator's original preference. A different target is refused while intent is
pending. Do not delete that journal to bypass a failed gate; inspect the failed
stage, repair the prerequisite and retry the same reviewed SHA. Escalation to a
new target needs an explicit operator recovery decision and retained evidence.

No code/state rollback is attempted automatically. A package or unit install may
be partially applied; the quiesced state and same-target replay are the recovery
mechanism. A SIGKILL or power loss cannot run Python cleanup. Persistent disabling
and the intent journal contain pre-restore interruption; interruption during
restore may leave a subset of already-validated new timers restored. Inspect the
journal and finish the same-target replay before treating the update as complete.
If systemd itself cannot stop a timer, containment records
`manual_intervention_required`, not a false promise that it is stopped.

## Acceptance

Unit tests use a stateful systemd model and real temporary unit/revision files.
They cover the timer matrix, first install, pending-intent replay, package/smoke/
restore failure, interrupted execution, concurrent lock exclusion, unsupported
states, and full bootstrap ordering. This is not evidence of a new real-LXC
upgrade. After merge, accept one actual exact-SHA update on the automation host,
confirm all previously serving timers return to their intended states, and check
fresh service invocations and downstream results. `Result=success` alone may be
an old result or an idle worker; it does not establish new work completed.
