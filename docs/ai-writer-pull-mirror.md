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

The mirror helper takes only the global lock.

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

The helper intentionally exposes no upload or bidirectional mode. Its rclone invocation is fixed to:

```text
rclone sync <remote> <local-vault> \
  --config <config> \
  --filter-from <filter-file> \
  --delete-after
```

The source must be a named rclone remote. Local-path sources are rejected. The local Vault, rclone config, and filter file must already exist; config/filter symlinks are rejected.

A non-zero rclone exit is a failed refresh and is never reported as success.

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
4. confirm `canonical-io.lock` is owned/accessible only according to the existing `24-Locks` ACL policy;
5. enable and start the timer;
6. verify a later timer run exits successfully.

When testing serialization, hold the global lock during a synthetic transport or refresh and confirm the other operation cannot enter its I/O section concurrently.
