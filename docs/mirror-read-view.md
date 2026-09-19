# Host-local mirror read view v0

## Purpose

Reader derives immutable Index and Context artifacts from the pull-only local
Vault mirror. The mirror refresher uses `rclone sync`, so a Reader scan that
runs concurrently with refresh could otherwise observe a mixture of old and new
local bytes.

Wave B of #64 adds one narrow host-local read-view guard:

```text
<ai-root>/24-Locks/read-view/mirror-read.lock
```

It serializes local mirror mutation against Reader operations that inspect
`11-Knowledge`.

## What this guarantees

While Reader holds the mirror read-view lock:

- the production mirror refresher cannot enter its `rclone sync` section;
- stale-index verification and selected source re-read see one stable local
  mirror view;
- Index construction cannot span two mirror refresh states.

The resulting Index/Context artifacts are immutable and content-addressed, so
the lock is released before later LLM inference.

## What this does not guarantee

The lock is **not** a distributed snapshot or remote freshness proof.

It does not prove that:

- Nextcloud has no newer edit;
- another host is not editing the canonical Vault;
- the local mirror corresponds to a particular remote transaction;
- a canonical mutation has or has not occurred.

It only stabilizes one host's already-present local mirror bytes against that
host's pull-only refresher.

Existing Index staleness checks remain mandatory. If the mirror refreshed after
an Index was built, retrieval acquires the read-view lock and rebuilds the
current inventory. A mismatch fails closed instead of using that stale Index.

## Lock topology

The two operational locks have distinct authority:

```text
24-Locks/
├── canonical-io.lock
└── read-view/
    └── mirror-read.lock
```

`canonical-io.lock` remains writable only by the existing trusted production
effect identities. Reader cannot open it.

`read-view/` is a narrow subdirectory writable only by:

- `obsidian-ai-sync`;
- `obsidian-ai-reader`.

Other semantic/effect identities cannot open the read-view lock.

## Lock order

Mirror refresh needs both protections and uses this fixed order:

```text
canonical_io_lock
  -> mirror_read_lock
      -> rclone sync
```

Reader operations acquire only `mirror_read_lock`.

Reader therefore cannot participate in a lock cycle involving
`canonical_io_lock`. Existing per-mutation lock ordering remains:

```text
canonical_io_lock
  -> per-mutation lock
```

No code path may acquire `canonical_io_lock` while already holding
`mirror_read_lock`.

## Reader lock scope

Index construction:

```text
mirror_read_lock
  -> scan active Knowledge bytes
release
  -> store immutable 04-Index artifact
```

Generator Context retrieval:

```text
rank immutable Index
mirror_read_lock
  -> verify Index against current local mirror
  -> re-read selected source bytes
  -> verify source hashes against Index
release
  -> store immutable 05-Context artifact
  -> later Generator inference
```

Evaluation Context retrieval follows the same pattern and stores the immutable
`14-Evaluation-Context` only after the mirror lock is released.

The lock is intentionally not held during Generator/Evaluator network calls.

## Failure behavior

Unsafe/missing lock directories, symlink lock files, lock-open failures, or lock
acquisition failures are fatal for the Reader operation. Retrieval must not
silently continue without the guard.

A stale Index also remains a deterministic failure requiring a new Index before
retry.

## Deployment boundary

This repository contains the reusable ACL fixture and gates. Production ACL
changes remain deployment authority and are introduced separately from code
merge.

The required production permission change is narrow:

- Reader gains traverse-only access to `24-Locks`;
- Reader and Sync gain rwx only on `24-Locks/read-view`;
- Reader remains unable to create/open `canonical-io.lock` or per-mutation
  locks.
