# #300 Phase D — single-LXC cooperating updater exclusion (unwired)

This is a **partial** maintenance exclusion primitive, not an operational
service controller or production authorization. No privileged daemon,
HTTP mutation, systemd unit, installer, or effect activation is introduced.

The cooperating_updater_gate function takes two already-existing lock inodes
in the same order as the current host-runtime updater:

1. /var/lib/obsidian-automation/deployments/host-update.lock
2. /run/obsidian-automation/semantic-refresh-inhibited.lock

It rejects missing/symlinked/replaced/hardlinked/insecure lock files, unsafe
directory permissions, busy locks, pending-runtime.json and
semantic-refresh-inhibited.json. Pending markers are rechecked under both locks.
Lock files are never created by the gate, and locks remain held through the
caller's context. Synthetic tests use disposable paths and current UID.

**Important boundary:** the existing host updater and pre-review updater use
these locks, but not every local launch/update path has been qualified.
Specifically, the GitHub updater, Core Promotion, independent timer/OnSuccess
jobs and worker dependency chain may not honor this pair. Therefore successful
acquisition does **not** authorize OperationBroker.apply, and this gate is
not wired to effects_enabled. It only excludes *cooperating* updaters.

To enable service controls in the supported **single active LXC per Vault**:
first inventory and bring each relevant local effect launcher into the same
reviewed exclusion contract (or prove an equivalent effect-specific boundary),
verify exact unit identity and in-flight dependency quiescence, and then test
cross-UID broker + disposable actual systemd interactions and crash recovery.
Remote CAS/ETag, #294 stage/activate and #309 Sync cleanup remain independent
authorities. No distributed multi-LXC lock is required for the unsupported
case of multiple active instances against the same Vault.
