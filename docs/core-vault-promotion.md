# ObsidianCore -> Live Vault Promotion v0

## Purpose

`ObsidianCore` is a public projection of selected system files from the private Obsidian environment, but it is also the repository where system changes are reviewed through GitHub pull requests.

The existing publication path is intentionally one-way:

```text
Nextcloud Live Vault
        ↓ read-only snapshot
private Gitea ObsidianVault
        ↓ public projection
GitHub ObsidianCore
```

A GitHub PR merged into `ObsidianCore` therefore does not, by itself, change the Live Vault. Promotion is the explicit reverse path for reviewed system changes.

Promotion is **not bidirectional sync**. It is a bounded, optimistic-concurrency transaction over only the files already managed by the public projection policy.

## Authority model

The existing authority remains:

- Nextcloud Live Vault: user-editable sync authority.
- private Gitea `ObsidianVault`: snapshot / audit / automation authority.
- GitHub `ObsidianCore`: public projection and review surface for system code/configuration.
- private `ObsidianDeployment`: deployment authority and home for private endpoints, credentials, service definitions, and production checkpoint state.
- GitHub `ObsidianAutomation`: reusable deterministic promotion logic; no production credentials.

A promotion must ultimately affect the Nextcloud Live Vault. Committing only to the Gitea `ObsidianVault` snapshot would be incorrect because the next read-only snapshot would overwrite that commit from the unchanged Live Vault.

## Target topology

```text
GitHub ObsidianCore
  PR merge
     ↓
trusted ObsidianDeployment runner
  clone exact Core commits
     ↓
Promotion Plan v0
     ↓
fresh Live Vault observation
     ↓
Reconciliation
  APPLY / ALREADY_APPLIED / CONFLICT
     ↓
Nextcloud promotion transport   # next implementation stage
     ↓ conditional remote mutation + verification
Nextcloud Live Vault
     ↓
normal read-only snapshot
     ↓
Gitea ObsidianVault
     ↓
normal public projection
     ↓
GitHub ObsidianCore converges
```

The trusted deployment side pulls from public GitHub. GitHub must not receive a private Gitea or Nextcloud write credential merely to trigger promotion.

## Round-trip allowlist

Promotion v0 reuses the exact public-export TOML policy. A path is promotion-eligible only when all of the following are true:

1. it matches `include`;
2. it does not match `exclude`;
3. it does not match `repository_owned`;
4. it is a safe relative POSIX path;
5. the Core object is a regular Git blob, not a symlink/submodule/tree.

With the current policy this includes the managed parts of:

```text
98-System/**
Dashboard.md
.obsidian/app.json
.obsidian/appearance.json
.obsidian/community-plugins.json
.obsidian/core-plugins.json
.obsidian/daily-notes.json
.obsidian/graph.json
.obsidian/hotkeys.json
.obsidian/types.json
.obsidian/text-generator.json
.obsidian/snippets/**
```

Repository-owned files such as `.github/**`, `.gitignore`, `README.md`, and `LICENSE` never round-trip to the Vault.

This makes the public projection policy the single ownership boundary in both directions instead of maintaining a second drifting path list.

## Promotion Plan v0

`obsidian-core-promotion-plan` takes:

```text
--core-repo
--base-ref
--head-ref
--config
--output
```

The base and head refs are resolved to immutable Git commit IDs. Base must be an ancestor of head.

For every managed changed path the plan records:

```json
{
  "action": "create | update | delete",
  "path": "98-System/...",
  "before_sha256": "... or null",
  "after_sha256": "... or null"
}
```

The full plan also binds:

- source repository identity `upiscium/ObsidianCore`;
- exact base commit;
- exact head commit;
- promotion policy version;
- SHA-256 of the exact public-export policy file.

The plan contains hashes, not credentials and not deployment authority.

A managed Core symlink or other non-regular Git object fails closed. A managed file larger than 2 MiB also fails closed in v0.

## Checkpoint semantics

The deployment side maintains a durable `last_observed_core_commit` checkpoint.

Conceptually:

```text
base = last_observed_core_commit
head = current ObsidianCore/main
```

The checkpoint must advance only after one of these outcomes:

1. every managed change is already present in the Live Vault; or
2. every pending change was successfully transported to Nextcloud and remotely verified.

It must not advance on conflict, ambiguous remote outcome, validation failure, or transport failure.

The checkpoint belongs in private deployment state. It must not be controlled by GitHub input.

## Reconciliation v0

`obsidian-core-promotion-reconcile` compares a canonical plan with an exact local observation of the current Live Vault state.

Each changed path is classified independently.

### create

```text
current absent          -> APPLY
current == after        -> ALREADY_APPLIED
otherwise               -> CONFLICT
```

### update

```text
current == before       -> APPLY
current == after        -> ALREADY_APPLIED
otherwise               -> CONFLICT
```

### delete

```text
current == before       -> APPLY
current absent          -> ALREADY_APPLIED
otherwise               -> CONFLICT
```

`ALREADY_APPLIED` is necessary for the normal Vault -> Core publication path. A Vault-originated change can appear later in the Core commit history; promotion must recognize that the Live Vault already equals the Core head rather than treating the publication as a competing write.

## Conflict meaning

A conflict means the current Live Vault is neither the exact Core base state nor the exact Core head state for a managed path.

Example:

```text
Core base:  task.js = A
Core head:  task.js = B
Live Vault: task.js = C
```

The promotion system must not overwrite `C` with `B` automatically. The checkpoint remains unchanged and Human review is required.

This is an optimistic-concurrency boundary, not a three-way semantic merge engine.

## Why a fresh Live observation is still not write authority

Reconciliation is a preflight. The Live Vault can change after it is observed.

Therefore the Nextcloud transport stage must independently re-check the exact remote bytes immediately before each mutation and must use conditional remote operations where supported. A successful transport must verify the remote bytes after mutation before the promotion checkpoint advances.

The local snapshot/reconciliation result alone must never authorize an unconditional overwrite.

## CLI exit behavior

`obsidian-core-promotion-plan`:

- `0`: canonical plan created;
- `2`: invalid repository/ref/policy/path/object or other fail-closed error.

`obsidian-core-promotion-reconcile`:

- `0`: no conflict (`APPLY` and/or `ALREADY_APPLIED` only);
- `2`: malformed/unsafe plan or observation error;
- `3`: at least one `CONFLICT`.

## Current implementation boundary

This stage implements:

- exact Core commit diffing;
- public-projection-derived promotion allowlist;
- canonical Promotion Plan v0;
- SHA-256 preconditions;
- symlink/non-regular-object rejection;
- Live snapshot reconciliation;
- `APPLY / ALREADY_APPLIED / CONFLICT` semantics.

It intentionally does **not** yet write Nextcloud.

The next stage is the trusted promotion transport in `ObsidianDeployment`: remote precondition verification, create/update/delete, exact-byte post-verification, receipt/checkpoint handling, and failure recovery.
