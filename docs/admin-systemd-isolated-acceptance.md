# #300 Phase K — isolated real-systemd admission and local exclusion

**2026-10-17 JST. Source acceptance evidence only; NOT a production
deployment, immutable installed unit review or root-worker enablement.**

## Isolation policy

The tests below ran on Adam using a live systemd user Manager and temporary
test-specific lock files. No ObsidianAutomation system units, timers, packages,
Vault, Nextcloud, Gitea or credentials were modified. The system-level D-Bus
Manager was never used for a StartUnit request. The test creates random
oa300-isolated-*.service and .timer transient user units, schedules each
timer for one hour later, runs only test-owned oneshots, stops/reset-failed
all created units and removes each temporary script/marker directory.

The production BusctlStartUnitManager's bounded source parser and reader
are exercised through a *test-only* Popen shim replacing the system bus
transport with the user bus and NixOS binary. The test registers only its
random transient ServiceSpec in the test process, restoring the normal
allowlist immediately after the call. No product unit is affected.

## Real systemd user-manager results

Systemd version 261 (Adam, Linux user Manager):

- First StartUnit returned a valid systemd Job object path.
- Overlapping StartUnit returned a Job object WITHOUT a second independent
  oneshot execution.
- The first service reached ActiveState=inactive, Job= (empty string),
  Result=success after exactly one run.
- A distinct new StartUnit after completion generated another Job, and
  two executions total rather than three.
- A separate failing service also returned a Job object but ended with
  Result=exit-code and ExecMainStatus=23. A Job acknowledgement is therefore
  demonstrably NOT service success or Gitea/GitHub business completion.
- The Manager exposed StartUnit(ss)->o and JobRemoved(uoss) on introspection;
  no terminal JobRemoved signal subscriber was installed.

## Kernel lock and marker tests

In a second disposable filesystem tree, the two shared host lock files
(host-update.lock and semantic-refresh-inhibited.lock) were created with the
reviewed private owner/inode/mode. A competing separate process could not
acquire either while the operator gate held both. The host updater holding
the first lock and the pre-review updater holding the second independently
blocked the operator gate. The host pending-runtime.json journal and refresh
semantic-refresh-inhibited.json marker failed closed, as did a symlinked
lock file. All locks were released cleanly and test markers removed.

The existing Core Promotion role uses its own nonblocking promotion.lock.
The root Broker must not hold that role mutex across systemctl start (the
service itself acquires it). Shared updater exclusion and systemd Job
admission remain the Broker's local boundaries.

## Concrete implementation defect found and fixed

Earlier source tests represented an idle Job property as Job=0. Actual
systemd 261 systemctl show -p Job renders an idle unit as Job= (empty).
The old _verified_live and fixed workflow quiescence guards would have
incorrectly rejected that state. Both now admit exactly empty string or 0
as idle, with all other identity, PID, ActiveState and SubState checks intact.
Every nonempty, nonzero Job remains rejected. Regression tests cover the
real idle form for Pause, Resume, Run once and workflow dependencies.

## Reproduction

On a Linux host with an active user systemd Manager and an installed
project Nix development shell, from an immutable source checkout:

    nix develop -c python tests/manual_admin_systemd_job_isolated.py
    nix develop -c python tests/manual_admin_local_exclusion_isolated.py

Both are manual acceptance scripts. They deliberately do not run during
normal pytest/CI and never install or operate a product service.

## Remaining runtime limits

This verifies the real user-manager Job admission behavior, difference
between ack and service failure, cooperative updater kernel locks, and
pending/unsafe-marker refusal. It does NOT qualify arbitrary uncooperative
root launchers, pre-Phase-F legacy GitHub updater binaries lacking shared
locks, installed root-owned unit/revision identity, production root/nonroot
WebUI IPC, durable actual-daemon crash/restart, or external backend effects.

It supports source integration to feature/298-management-dashboard but does
not authorize #311 -> main Merge or production LXC Cutover. Exactly one active
service execution LXC per Vault remains the only supported topology.
