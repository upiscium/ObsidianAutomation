# Core -> Live Vault Promotion Transport v0

## Purpose

Promotion Plan v0 proves what changed between two exact `ObsidianCore` commits and whether the current Live Vault snapshot is compatible with that change. Promotion Transport v0 is the trusted effect boundary that applies those reviewed system changes to the actual Nextcloud Live Vault.

```text
ObsidianCore base -> head
        ↓
Promotion Plan v0
        ↓
private ObsidianDeployment runner
        ↓
remote WebDAV preflight
        ↓
conditional create / update / delete
        ↓
remote exact-byte verification
        ↓
immutable receipt
        ↓
checkpoint advance
```

The transport is designed for a trusted LAN-side deployment process. GitHub does not receive a Nextcloud credential.

## Credential separation

Use a dedicated Nextcloud service account/app password for Core promotion.

Do not reuse or widen:

- the read-only Snapshot credential;
- the AI Sync credential that is intentionally limited to Read/Create;
- a user's primary Nextcloud password.

The promotion credential needs the permissions required to create, update, and delete files in the Vault because Core PRs can intentionally perform those operations. Its practical write authority is bounded by the deterministic Promotion Plan/public-projection policy in the transport, while the credential itself must be protected as deployment authority.

Store the username and app-password reference only in private `ObsidianDeployment` configuration. The password file must be a regular non-symlink file containing one non-empty line.

## Network boundary

The WebDAV base URL must use HTTPS. Embedded credentials, URL query strings, and fragments are rejected by the existing WebDAV target builder.

The transport does not follow HTTP redirects because it uses a direct `http.client` connection to the configured target host.

## Whole-plan preflight

Before any state-changing request is sent, every managed change is observed with a remote GET.

For each path:

### create

```text
remote absent        -> APPLY
remote == Core head  -> ALREADY_APPLIED
otherwise            -> CONFLICT
```

### update

```text
remote == Core base  -> APPLY
remote == Core head  -> ALREADY_APPLIED
otherwise            -> CONFLICT
```

### delete

```text
remote == Core base  -> APPLY
remote absent        -> ALREADY_APPLIED
otherwise            -> CONFLICT
```

If any path is `CONFLICT`, transport stops before the first mutation.

For update/delete operations in `APPLY`, the GET must also return a strong ETag. Missing or weak ETags fail closed before any mutation.

## Lost-update protection

State-changing requests use HTTP preconditions:

```text
create:
  PUT + If-None-Match: *

update:
  PUT + If-Match: <strong ETag from preflight GET>

delete:
  DELETE + If-Match: <strong ETag from preflight GET>
```

The preflight content SHA prevents applying a Core change over a semantically unrelated/currently diverged file. The ETag precondition closes the time-of-check/time-of-use window between preflight and mutation.

If another client changes the target after preflight, the conditional request must not silently overwrite it.

## Nextcloud parent directories

PUT requests include:

```text
X-NC-WebDAV-AutoMkcol: 1
```

so a reviewed Core create under a newly-added nested managed directory can be uploaded without a separate unconditional recursive `MKCOL` transaction.

## Exact-byte post-verification

A successful HTTP status is not sufficient evidence of completion.

After every mutation the transport performs another GET:

```text
create/update:
  remote SHA-256 must equal Promotion Plan after_sha256

delete:
  remote GET must return 404
```

If the server response to the mutation is lost or otherwise ambiguous, the same observation is used for recovery:

```text
remote == desired head state -> recovered
remote == previous base state -> no checkpoint advance; retry later
other remote state            -> conflict
```

This allows safe reruns after process/network failures without assuming that a lost response means either success or failure.

## Partial completion and reruns

A multi-file WebDAV promotion is not atomic. Some early files can be successfully changed before a later conditional mutation encounters a concurrent edit or transport failure.

The checkpoint therefore advances only after every path is remotely verified.

On the next run:

- paths already equal to Core head become `ALREADY_APPLIED`;
- remaining paths that still equal Core base remain `APPLY`;
- unrelated divergence becomes `CONFLICT`.

This gives the transaction convergent/idempotent recovery without rollback writes.

## Promotion checkpoint

Private deployment state maintains one canonical checkpoint file:

```json
{
  "record_version": 1,
  "source_repository": "upiscium/ObsidianCore",
  "last_observed_core_commit": "<commit>",
  "policy": {
    "version": "public-projection-roundtrip-v0",
    "sha256": "<exact public-export config SHA-256>"
  },
  "updated_at": "...Z"
}
```

A transport run requires:

```text
plan.base_commit == checkpoint.last_observed_core_commit
plan.policy == checkpoint.policy
```

The checkpoint is atomically replaced with `plan.head_commit` only after receipt persistence and complete remote verification.

### Bootstrap

Bootstrap is an explicit operator action:

```text
obsidian-core-promotion-checkpoint-init
```

Choose a Core commit known to represent the current publication baseline. The command refuses to overwrite an existing checkpoint.

The checkpoint is ordering/audit state, not the sole write-safety boundary: each mutation still carries per-file Core base/head SHA preconditions and remote ETag preconditions.

Production should serialize the complete promotion transaction with `flock` or an equivalent private deployment lock around plan construction, transport, and checkpoint handling.

## Receipt

After complete verification, the transport writes an immutable content-addressed receipt under the configured private receipt directory.

It binds:

- Promotion Plan SHA-256;
- exact Core base/head commits;
- exact promotion/public-export policy SHA-256;
- per-path action and before/after SHA-256;
- per-path result:
  - `applied`;
  - `already_applied`;
  - `recovered`;
- completion timestamp.

The receipt contains no password, authorization header, or raw credential material.

## CLI

Checkpoint bootstrap:

```bash
obsidian-core-promotion-checkpoint-init \
  --checkpoint /var/lib/obsidian-promotion/core.checkpoint.json \
  --core-commit <known-current-core-commit> \
  --config /etc/obsidian-deployment/public-export.toml
```

Transport:

```bash
obsidian-core-promotion-transport \
  --plan /var/lib/obsidian-promotion/current.plan.json \
  --core-repo /var/lib/obsidian-promotion/ObsidianCore \
  --config /etc/obsidian-deployment/public-export.toml \
  --base-url 'https://nextcloud.example/remote.php/dav/files/PROMOTER/ObsidianVault' \
  --username PROMOTER \
  --password-file /etc/obsidian-deployment/nextcloud-promotion.password \
  --checkpoint /var/lib/obsidian-promotion/core.checkpoint.json \
  --receipt-dir /var/lib/obsidian-promotion/receipts
```

Exit status:

```text
0  complete; receipt persisted and checkpoint advanced
2  malformed input, network/verification error, missing strong ETag, etc.
3  conflict; checkpoint not advanced
```

## Recommended private wrapper

`ObsidianDeployment` should own the production wrapper and secrets. A run should conceptually:

1. acquire one promotion lock;
2. fetch `ObsidianCore/main`;
3. read `last_observed_core_commit` from the private checkpoint;
4. build a plan from that commit to the exact fetched Core head;
5. verify the plan against the same Core checkout and pinned policy;
6. execute WebDAV transport;
7. release the lock.

Do not trigger this by giving a GitHub Actions job direct Nextcloud credentials. The trusted deployment side should poll/fetch public Core or otherwise receive only non-secret notification.

## Relationship to normal snapshot/publication

After a successful promotion, the existing read-only snapshot flow sees the changed Live Vault and commits it to private Gitea `ObsidianVault`. The normal Gitea publication then reprojects that state to `ObsidianCore`.

Because the Core head already contains the promoted bytes, that publication should converge without undoing the reviewed change.
