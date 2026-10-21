# #442 — Root-only manual reconciliation of an indeterminate Run once

## Scope and precedence

This procedure restores the *ability to accept new operations* after a
single, explicit human acknowledgement of one unknown Run once result. It
never asserts that the old Job was admitted or that business processing
succeeded. It never retries the old UUID. This is a **separate MVI gate**
from fixing the sibling-timer admission predicate in PR #440, and separate
from enabling the live Operator/authority for a new effectful test.

The first implementation is **intentionally fixed to the existing incident**:

- UUID: `c3b97d52-c988-47b9-addb-31ea49960b71`
- role: `github-sync`; action: `run_once`; old result: `indeterminate`
- trusted rollback snapshot:
  `/var/lib/obsidian-admin/operator-cutovers/cutover-1994763e9454-e1l_z2vc/`
- production root Queue: `/var/lib/obsidian-admin/queue`

Other incident UUIDs, roles and files cannot be chosen through CLI arguments.
They require new reviewed source and a separate human decision. Future
generalization is not part of this MVI.

## Trusted root source and preconditions

The operational entrypoint is `tools/reconcile_admin_operator_queue.py`.
**This source branch must be reviewed, merged and deployed by a different
human-approved change before executing either mode on production**. Pin its
exact reviewed Git blob SHA256, copy into a 0700 root-owned directory under
`/var/lib/obsidian-admin/operator-releases/<reviewed-commit>/`, mode 0400,
and verify all path components no-follow. The command is fixed to

```sh
/usr/bin/python3 -I -S -B \
  -X pycache_prefix=/dev/null/obsidian-admin-source-only \
  /var/lib/obsidian-admin/operator-releases/<approved-commit>/reconcile-admin-queue.py \
  --inspect --request-id c3b97d52-c988-47b9-addb-31ea49960b71
```

Only *after an explicit per-UUID human acceptance* is the second mode
permitted, with a reference to the exact human approval comment in this repo:

```sh
/usr/bin/python3 -I -S -B \
  -X pycache_prefix=/dev/null/obsidian-admin-source-only \
  /var/lib/obsidian-admin/operator-releases/<approved-commit>/reconcile-admin-queue.py \
  --reconcile --request-id c3b97d52-c988-47b9-addb-31ea49960b71 \
  --human-accept acknowledge_indeterminate_without_replay:c3b97d52-c988-47b9-addb-31ea49960b71 \
  --approval-reference 'https://github.com/upiscium/ObsidianAutomation/issues/442#issuecomment-<approved-comment-id>'
```

The approval reference is an immutable *audit pointer*, not a cryptographic
signature or a way to grant root authority remotely. The host operator must
verify the linked comment's identity and scope before running the command.

Required preconditions:

1. One reviewed LXC, root user and fixed Python isolated/no-site/no-bytecode
   mode. Manifest is root-owned, disabled, and its SHA mapping matches **all
   14** privileged Python modules and **all 10** managed systemd Units,
   including this PR's updated Queue module. The tool refuses an older
   installed Queue SHA. The standalone CLI additionally reconstructs the
   previously accepted full authority document from its new version by
   reversing only the reviewed revision and Queue-module digest, then
   compares its SHA-256 against the separately pinned prior authority.
   This pins every other field, module name, Unit name and digest, not just
   the manifest-reported inventory counts.
2. Root Worker `inactive/disabled`, socket absent; external nginx listener
   `inactive/disabled`, Dashboard loopback backend active. The relevant
   audit records must remain root-owned, 0600, single-link and no-symlink.
3. Both pre-existing host updater and Semantic Refresh lock inodes are
   acquired through the existing `cooperating_updater_gate` before any
   Queue mutation. The separate Queue admission lock is acquired afterward.
4. The exact prior Cutover `before-timers.json` and `failure.json` match
   source-pinned SHA256. **All five** timer ActiveState/UnitFileState values
   must equal their recorded originals. The GitHub role has **no active or
   queued Job** in *any* of its nine fixed services, and no MainPID or
   ControlPID. All related timer identities and Job fields are checked.
5. Original Queue `active.json`, request, processing and result are
   consistent for the exact UUID/action/service. Result is still literally
   `indeterminate`. No historical business success is claimed. The
   original journal is not removed or renamed.

An observation today cannot prove no effect occurred historically. The
original uncertain outcome **remains uncertain**. The operator must inspect
the historical Job/Journal evidence and approve continuing operations while
forbidding any replay of that UUID.

## Two-stage audited commit

The root CLI creates one immutable
`manual-evidence-<UUID>.json` in the protected Queue, containing:

- exact request, processing, result and prior active-marker SHA-256 values;
- boot ID, UTC observation time and exact human approval comment URL;
- bounded, positive inactive/disabled Worker, absent socket, root-owned
  authority disabled, idle role services and restored timer intent;
- `business_outcome=unknown_no_success_claim`.

The Queue then writes a separate immutable
`manual-reconcile-<UUID>.json` under the same lock, containing the exact
SHA-256 of that evidence record and the original three record hashes, an
explicit `acknowledge_unknown_no_replay` decision and the approval pointer.
**Only after the new file has been durably fsynced and the entire prior audit
history has been re-validated does the Queue remove `active.json`.**
No original request, processing claim or result record is altered. An old
receipt or submit with the **same UUID** continues to return
`indeterminate` even after new requests have been accepted; it never
enqueues a second action.

If the process is interrupted after evidence publication, the old
`active.json` remains and the Queue refuses new requests. A subsequent
explicit, identically approved root invocation may safely complete
finalization of the same already verified evidence. If evidence or original
hashes change, a Job is now active, credentials/permissions drift, the
stored approval pointer differs, or any marker is malformed, **fail closed**.

The dedicated CLI has no start/stop/enable/disable/Run once operation. It
never modifies authority flags, nginx rules, managed timer intent or Vault
content. Any later Worker enablement and business Job trial are separate
human approvals under Issue #403.

## Testing and release gates

- Synthetic tests cover original record preservation, correct manual proof,
  rejected fake approval/UUID/result, no replay after new active request,
  evidence mismatch, symlink/owner/mode violations, lock contention,
  interrupted marker cleanup, resumed finalization and fake-root role health.
- Test executable requires Python 3.11 or 3.13 with stdlib `tomllib` not
  needed; runs with fixed `-I -S -B` in production. No HTTP endpoint or
  AF_UNIX socket message is added for reconciliation.
- Require exact-HEAD GitHub CI, independent Correctness and Security review,
  and explicit **Human Merge Accept** before main integration.
- Require a **second Human approval** for actual root-only install,
  reconciliation of the historical UUID, and later Cutover/retrial. Do not
  remove the active marker or patch indeterminate manually as a workaround.
  A host reboot between publishing the manual evidence and finalizing its
  active marker changes the kernel boot ID. An old evidence record is never
  rewritten to forge a new boot ID. In this case an operator must recheck
  all live authority, Queue, updater locks, full systemd role idle status
  and timer intents, then explicitly supply --resume-across-reboot along
  with the same UUID-specific approved reference. The immutable original
  evidence boot ID is passed into Queue proof validation and retained in
  the durable proof. A missing or malformed old evidence record is rejected;
  the command does not silently resume work after a host reboot.

The advanced Python `.pyc` cache verification #402/#412 and TOML CIDR
allowlist #427 remain Post-RC/Post-GA work.
