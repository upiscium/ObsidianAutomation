# AI Writer pull-only mirror operation

## Purpose

The Nextcloud Live Vault remains the canonical editing authority. The AI Writer local Vault at `/var/lib/obsidian-ai/vault` is only a pull-only mirror used for retrieval, validation, and execution planning.

Canonical writes and mirror refreshes must not overlap. A refresh that races a remote create can otherwise observe an intermediate remote state while the execution lifecycle is still being finalized.

## Global canonical I/O lock

Both of these production actions use the same lock:

```text
<ai-root>/24-Locks/canonical-io.lock
```

- `obsidian-production-knowledge-webdav-worker`
- `obsidian-production-vault-pull`

The lock protects the remote/local I/O boundary only. Existing per-mutation locks remain responsible for Intent / Request / Result / Receipt lifecycle state.

The required lock order is:

```text
global canonical I/O lock
  -> per-mutation production lock
```

The mirror helper takes the global lock and then the narrow host-local mirror
read-view lock:

```text
canonical_io_lock
  -> mirror_read_lock
      -> rclone sync
      -> rclone check
```

Reader never acquires `canonical_io_lock`. It acquires only
`24-Locks/read-view/mirror-read.lock` while scanning/verifying local mirror
bytes. Both rclone commands run while the canonical and read-view locks are
held, so Reader does not observe the local mirror during either mutation or
verification. See [Host-local mirror read view](mirror-read-view.md).

The locks serialize this host's I/O; they do not freeze the remote. The check
is a bounded-time observation, not an atomic remote snapshot: the remote can
change during or after refresh/verification, and independent readers do not
receive a permanent freshness guarantee.

## Pull-only helper

Example:

```bash
obsidian-production-vault-pull \
  --ai-root /var/lib/obsidian-ai/state \
  --vault-root /var/lib/obsidian-ai/vault \
  --remote nextcloud-ai:ObsidianVault \
  --rclone-config /etc/obsidian-ai/rclone.conf \
  --filter-file /etc/obsidian-ai/vault-pull.filters
```

The helper intentionally exposes no upload or bidirectional mode. After input
validation, its rclone invocations are fixed to:

```text
rclone sync <remote> <local-vault> \
  --config <config> \
  --filter-from <filter-file> \
  --ignore-times \
  --delete-after \
  --delete-excluded

rclone check <remote> <local-vault> \
  --config <config> \
  --filter-from <filter-file> \
  --download
```

The sync's `--ignore-times` prevents rclone's default size/modtime comparison
from skipping same-size content changes. The subsequent `check --download`
compares content rather than relying on size-only checks. The check uses the
same named remote, local destination, config, and filter as the sync; it has no
delete or upload flags. The helper reports success only when both commands
exit zero.

The production AI filter includes `00-DailyNote/**`, `05-Idea/**`,
`10-Project/**`, and `11-Knowledge/**`; see
`examples/ai/vault-pull.filters`. Daily and Idea are Reader-only Semantic
Corpus material in v1 and do not change automatic Generation selection yet.
Project Notes remain Reader input and are not added to the Evaluation
Knowledge corpus.

The source must be a named rclone remote. Local-path sources are rejected. The
local Vault, deployment-private rclone config, and exact-SHA managed filter
file must already exist; config/filter symlinks are rejected. Filter-excluded
destination files are deleted from the local rebuildable mirror so narrowing
the reviewed filter converges the local corpus exactly. This does not delete
anything from the remote Nextcloud source. A non-zero exit or spawn failure
from either command is a failed refresh and is never reported as success.

Each rclone subprocess has a 600-second timeout and the sync-plus-check phase
shares a 900-second deadline. Each subprocess receives the lesser of 600
seconds and the remaining shared budget. The shared deadline starts only after
both locks have been acquired; waiting to acquire either lock is not bounded by
these timeouts. If a command times out or the shared budget expires, the helper
fails closed without retrying. It does not roll back or clean up the local
mirror, so a partial refresh may remain and must be handled by a later refresh
or operator procedure.

This stronger refresh costs additional I/O: `--ignore-times` may transfer
every included file on each sync, and `check --download` reads remote content
again to verify it. The check does not make the sequence atomic and cannot
guarantee that remote content remains unchanged after verification. Neither
the command timeouts nor shared execution budget bound the time spent waiting
for locks. This code and its documentation are not a production deployment;
deployment remains under the private deployment authority.

## Credential boundary

Run the mirror helper as `obsidian-ai-sync`, the same production identity that owns the Nextcloud transport credential.

Do not grant Nextcloud credentials to Reader, Generator, Validator, Evaluator, Reviewer, or Executor identities.

The mirror helper does not itself parse or expose Nextcloud credentials; rclone reads the supplied config file under the Sync identity.

## systemd example

Reusable examples are provided at:

```text
examples/ai/obsidian-ai-vault-pull.service
examples/ai/obsidian-ai-vault-pull.timer
```

The example cadence is:

```text
OnBootSec=2min
OnUnitActiveSec=5min
Persistent=true
```

Cadence, remote name, paths, and deployment hardening remain deployment policy. Production deployment configuration and credentials belong in the private deployment authority, not this repository.

## Production acceptance

Before enabling the timer in production:

1. run one manual helper refresh;
2. verify the expected canonical target exists in the local mirror;
3. compare the local content SHA-256 against the canonical remote content SHA-256;
4. confirm `canonical-io.lock` remains inaccessible to Reader;
5. confirm `24-Locks/read-view` is writable only by Sync and Reader;
6. enable and start the timer;
7. verify a later timer run exits successfully.

When testing serialization, hold the global lock during a synthetic transport or refresh and confirm the other operation cannot enter its I/O section concurrently.
