# #403 disabled quiescence overlay — future root staging only

This is **not a deployment approval, authority activation, or Run once**. The
merged #440 module is the sole source replacement; no Unit, timer, Queue,
Dashboard, Vault, or Nextcloud is changed. The historical UUID
`c3b97d52-c988-47b9-addb-31ea49960b71` remains indeterminate and manually
reconciled **without a business-success claim**. Never replay it. A subsequent
GUI Pause → **new UUID** Run once ACK → Resume is a separate cutover decision.

## Independent coordinator gate

Only after independent source review, frozen SHA comparison and **separate
human staging approval verified by the coordinator** may a root operator
consider a future stage. A well-formed URL, the authorization argument below,
and this document do not prove approval. Revalidate the installed state and
source on the actual LXC; the historical audit is not a deployment preflight.
Stop on any mismatch. Do not use `prepare_admin_operator_authority.py` to
replace the existing authority (it is create-only).

The CLI has fixed production paths, exact old/new revision and digests. The
global `/opt/obsidian-automation/app` checkout deliberately remains at clean
`2b860f2fdec954ed363f6b23ed1df5f1597bbaf3`; it is **not** the source of
the new module. A separate protected root release bundle at
`/var/lib/obsidian-admin/operator-releases/fb8d5cb8aff3da22a0d93eb48fe2ba35b7fa1c30/disabled-quiescence/`
contains only `admin_workflow_quiescence.py` and `release.json`. Every release
directory is root-owned `0700`, each file root-owned `0400`, single-link,
nofollow. `release.json` canonically pins the merged commit, exact Git source
path, Git blob ID `66ecad9a46030516457405157f9c9a3f660899c4` and
independent source SHA
`7e13c28727a9fbc2cb74082aacba8fca544d092ff6e36aeef2635813996edcf7`.
The operator/coordinator must separately prove that these two immutable bundle
files were extracted from the reviewed Git commit and freeze/verify their exact
release identity before placing them on the host. A claimed commit in JSON is
**not** itself proof of origin or human approval. The tool checks exact bundle
members, descriptor bytes, Git blob ID and source SHA; it never reads a new
module from the stale global checkout. A wrong checkout HEAD is rejected, not
automatically upgraded.
It independently checks old authority SHA
`aafc54c00d84e8ebd248630c3054c3b082494dd7086270f4158c8ecf2fa5e121`,
old module SHA `dadf7400ae15d4be1f6ba6949be10716d642f8acbf53ffa4f732526d20358321`,
the 14-module/10-Unit closure, and candidate authority SHA
`662cfb3b030d845f16384b3f413cdd7267291aff89f5d6878bc6a70c1d352359`.
The latter is reconstructed canonically, not trusted as an instruction to
ignore the inventory. The three authority booleans remain false.

On the **actual production host, only after that separate authorization**, use
the installed reviewed tool under the isolated root system interpreter:

```text
/usr/bin/python3 -I -S -B -X pycache_prefix=/dev/null/obsidian-admin-source-only /var/lib/obsidian-admin/operator-releases/fb8d5cb8aff3da22a0d93eb48fe2ba35b7fa1c30/stage-admin-disabled-quiescence.py
```

The command shown above is illustrative: the separately approved frozen CLI
must itself be installed and hash-verified in the protected root release
area, not executed directly from the stale global checkout. The coordinator
must separately provision and verify this independently pinned CLI; the
example CLI path is not created by this source-only change. No production
execution is approved by this runbook. The default is inspection. It acquires
both updater locks but makes no writes.
Check the result `PRECHECK_PASS` and corroborate it with independent read-only
host observations. Stage is an explicitly distinct invocation:

```text
/usr/bin/python3 -I -S -B -X pycache_prefix=/dev/null/obsidian-admin-source-only /var/lib/obsidian-admin/operator-releases/fb8d5cb8aff3da22a0d93eb48fe2ba35b7fa1c30/stage-admin-disabled-quiescence.py --stage-disabled --authorization OA403-DS1-disabled-overlay
```

**Neither command has been run on production by this work packet.** Do not
copy them to a host absent review/approval. No arbitrary path/revision/Unit
parameters are accepted. This code never calls systemctl start/stop/enable/
disable/reload/daemon-reload. The sole systemctl use is bounded `show` for the
five timers, 28 fixed workflow services and root operator service. It refuses
jobs, live GitHub-domain services, an enabled Worker, a socket, active Queue, pending runtime
or inhibit marker, unsafe parents/files, and both occupied updater locks. It
never imports the installed application module.

Debian 13 exposes exactly six requested `show` fields on timers:
LoadState, ActiveState, SubState, Job, FragmentPath and UnitFileState.
The 28 services and root Worker must instead return those six **plus**
MainPID and ControlPID; missing either service PID is a refusal, not an
assumed zero. The type-specific parser refuses extra, duplicate, malformed
or non-ASCII properties. Synthetic tests exercise this real-shaped 34-Unit
response; they are not a production root acceptance run.

The Queue preflight expects exactly the historical completed Pause UUID
`75e533e0-bdba-439e-80cb-d53754faa6c0` (three durable records) and the
five-record manually reconciled indeterminate Run once chain, plus the
root:obsidian-admin-web admission.lock. Request/processing/result and that
lock are root:obsidian-admin-web `0600`; manual records are root:root `0600`.
An unknown or orphan record, unsafe identity, or changed digest is a gate,
not a cleanup instruction. The unrelated Core Promotion service may remain
failed/failed with no Job or PID; the root Worker and all nine affected GitHub
services must be idle. All five timer intents are still captured and unchanged.
This is a **domain-scoped, non-effectful disabled source stage**, not a global
maintenance drain or relaxed Run once admission. Core Promotion effects and
its own role-local lock are untouched.

The fixed authority directory `/etc/obsidian-admin` is root:obsidian-admin-web
`0710`, and the fixed Queue directory `/var/lib/obsidian-admin/queue` is
root:obsidian-admin-web `0700`. The stager accepts that group **only for these
two canonical ancestors**; all other required parents retain root:root,
nofollow and non-writable checks. When the root Worker is stopped, systemd's
`/run/obsidian-admin` RuntimeDirectory normally does not exist. Only the
fixed `operation.sock` check may treat that missing directory as Socket
absence, after checking `/run` root:root `0755`. If RuntimeDirectory exists,
it must be root:obsidian-admin-web `0710` and have no Socket. Symlinks,
unexpected modes/groups, and absent *other* required parents are refusal
conditions; this procedure creates no runtime directory.

The stager reads bounded `/proc/self/mountinfo` under the two updater locks.
For every fixed installed module, Unit, authority, release, Queue, lock,
runtime and stage target (including its ancestors), the only expected mount
boundaries are `/` and `/run`. A direct or ancestor overlay/bind mount is
refused even if it reports the **same device** as the backing filesystem.
The single reviewed `/run` boundary must be a tmpfs mounted at its root.
Malformed or unavailable mountinfo and a change in the relevant mount records
between preflight, install and recovery are refusal gates. Unrelated mount
records are parsed but do not need to be absent. This is a sampled, root-host
boundary: it is not an atomic lock against an uncooperative privileged mount
actor. Revalidate mount topology in any separately approved live preflight.

All eight historical Queue JSON records are parsed with recursive duplicate-
key rejection and exact canonical UTF-8/LF encoding, matching the grounded
read-only record audit. The two linked histories remain immutable. Tests of
the CLI entrypoint use a no-I/O Stage spy and synthetic interpreter/UID values;
they grant no production privilege or mutation.

**Review decision candidate (no change in this source revision):** the current
disabled-only stager still refuses *active* unrelated AI/Core services while
allowing an idle/failed Core observation. The documented Run once predicate
and #442 reconciliation are GitHub-*domain* scoped, whereas the two updater
locks are only cooperating exclusion and do not themselves authorize effects.
There is no explicit accepted #403 source-stage policy here proving whether
an unrelated active service must block this non-effectful replacement. Retain
the stricter existing predicate pending a separate human security/operations
decision; do not infer permission to relax it from this runbook.

## Durable intent and interrupted stages

The protected root-owned `0700` stage directory is fixed at
`/var/lib/obsidian-admin/operator-stages/disabled-quiescence-fb8d5cb8aff3`.
Its `0600` before-bytes, rollback receipt and SHA-linked `pending.json`
capture five timer enable/active/substate intents and both historical Queue
proof digests, and are synced before either target replacement. Each sibling temp
is synced before rename and its parent is synced after rename. Completion is
written only after external read-back of both files, all 13 unchanged modules,
10 Units, 5 timer intents and both Queue proofs. `pending.json` is retained
as forensic evidence; `completed.json` or `rolled-back.json` supplies terminal
state. There is no automatic rollback on exceptions or power loss.
The root-owned `0700` operator-stages parent is created and synced on first
stage only, never during inspection. A crash before durable `pending.json`
leaves a manual-investigation gate without having replaced either target.

Any `BLOCKED` result or crash after stage directory creation requires a
coordinator-led recovery decision, **not another stage**. Preserve records,
recheck the two held locks, source pin, Worker/socket, Queue and timer states.
If both module and authority already match the candidate and no completion
receipt exists, the separately authorized `--finalize --authorization
OA403-DS1-disabled-overlay` repeats read-back and writes completion. If either
file is old or candidate, the separately authorized `--rollback
--authorization OA403-DS1-disabled-overlay` restores verified before-bytes
and writes a rollback receipt. Both actions reject third-party bytes, timer/
Queue drift or conflicting terminal receipts; retry an interrupted rollback
with the same action. A completed stage cannot be rolled back by this tool:
escalate for a new bounded review. Do not remove pending evidence or temp
artifacts to force a retry. Unknown partial state requires manual investigation
without overwriting it.

Staging is not sufficient for GUI operation. Subsequent privileged release,
attestation, Queue/Worker activation and business run require their own
approvals and preflight. The historical observations and hashes above can
expire; do not claim success without independent host read-back.
