# ObsidianAutomation

Reusable automation software for maintaining and publishing an Obsidian Vault while keeping private Vault data and production deployment details outside this public repository.

The repository includes deterministic Vault publication/synchronization components and the production AI lifecycle used to generate, validate, review, and conditionally publish Knowledge Notes while preserving explicit authority boundaries.

## Start here

For production deployment and first-host setup, see [Installation](docs/installation.md).

For the Human-facing AI workflow, including `04-AI` projection, Human Review, post-review delivery, and Completed cleanup, see [Getting started](docs/getting-started.md).

The detailed projection contract is documented in [Human-facing AI lifecycle projection](docs/human-ai-projection.md).

## Supported runtime topology and concurrency limits

**Supported configuration: exactly one active ObsidianAutomation service-execution LXC per managed Vault.** Multiple cooperating services and distinct Unix identities *within that one LXC* are supported; multiple independently active ObsidianAutomation deployments against the **same Vault** are not.

**Concurrent multi-instance access to the same Vault is unsupported. Its behavior and data integrity are not guaranteed.** This includes running two writer/scheduler instances, keeping an old LXC actively serving while a replacement LXC starts, or treating a second LXC/VM as an automatically active hot standby. Such configurations can cause duplicate work, conflicting writes, stale status projections, repeated publication or cleanup effects. This project does not provide cross-host distributed locks, leader election, or automatic failover.

A standby instance may exist only **while inactive**. For a migration, stop scheduling and drain existing effects on the old instance, reconcile durable state and any incomplete remote operations, then activate the replacement as the sole service-execution instance. Re-enabling both is outside the supported configuration.

**Single-LXC does not remove local safety requirements.** Shared-host service controls, local deployment updates, timers, one-shot jobs and publishing still need appropriate local exclusion, durable audit/idempotency, per-role Unix permissions and remote CAS/ETag protection. They cannot rely on an operator-facing warning alone to prevent conflicts *inside* the supported host.

This is the operator-approved scope for the upcoming management Dashboard (#298/#300); parts of that Dashboard are still development-only and are not a live operations service. See [the single-host ADR](docs/adr/0001-single-host-management-dashboard.md) and [service broker limits](docs/admin-operation-broker.md).

## Trust boundary

This repository may contain reusable automation code, schemas, tests, prompts, and container definitions. It must not contain production credentials, private Vault contents, internal endpoints, runner registration data, or environment-specific secrets.

The Public Exporter is deterministic and does not use an LLM. The private Vault is the source of projection content; public-repository-owned files are preserved separately.

## Nix development environment

On NixOS or another system with flakes enabled, enter the development environment with:

```bash
nix develop
```

With direnv installed, the repository also provides `.envrc`:

```bash
direnv allow
```

The development shell provides Python, pytest, Git, Node.js, `obsidian-public-export`, `obsidian-public-publish`, and `obsidian-vault-snapshot` from the current working tree.

Nix is for development and manual verification only. Production automation is expected to run independently in dedicated Linux containers and does not require Nix.

## Public Exporter v0

The exporter treats the destination repository as two trust domains:

- paths matched by `repository_owned` are preserved and never managed by the exporter;
- every other destination file belongs to the generated projection and is removed if it is no longer present in the allowlisted source projection.

Git metadata (`.git` and `.git/**`) is always protected even if it is omitted from the configuration.

The example policy is in `configs/public-export.example.toml`. Optional `exclude` patterns remove operational or private files from the selected source projection even when a broader `include` pattern matches them; an already-exported excluded path is removed from the destination on the next apply.

### Preview a projection

```bash
obsidian-public-export \
  --source /path/to/private-vault \
  --destination /path/to/ObsidianCore \
  --config configs/public-export.example.toml \
  --dry-run
```

The dry run prints `ADD`, `UPDATE`, and `DELETE` operations without changing the destination.

### Apply a projection

After reviewing the dry-run output:

```bash
obsidian-public-export \
  --source /path/to/private-vault \
  --destination /path/to/ObsidianCore \
  --config configs/public-export.example.toml
```

v0 rejects path traversal, symlinks in managed paths, missing required allowlist entries, and collisions with repository-owned paths.

## Public Publisher v0 — existing helper, legacy Runner integration

`obsidian-public-publish` is a deterministic transaction helper: it
requires a clean `ObsidianCore` checkout, applies the reviewed projection,
runs validation/tests and creates a local commit; it does **not** run
`git push` itself.

The existing Gitea Runner workflow documented in
[the previous publication design](docs/gitea-publication.md) and
[example workflow](examples/gitea/public-projection.yml) is **historical
integration**, not the #298 single-LXC target. The target will use this
reviewed logic in a narrowly scoped local Publisher role **inside the sole
active service-execution LXC**, with no generic host-mode Actions Runner
or second required service LXC. That target publisher is still under
development and not yet accepted for production.

## Live Vault Snapshot v0

`obsidian-vault-snapshot` turns a stable local mirror of the Live Vault into one private Git snapshot commit. The helper itself has no network access and never pushes.

The **legacy** snapshot topology used a separate unprivileged Linux container to pull the Vault via WebDAV, verify its local mirror and create a private Git snapshot. For the #298 single-LXC target, the Snapshot role runs as a separately restricted Unix service identity **within the sole active service-execution LXC**. The Proxmox host does not need to mount or authenticate to Nextcloud. A second actively running Snapshot/Automation LXC acting on the same Vault is not part of the supported configuration.

The design is documented in `docs/live-vault-snapshot.md`; the example policy is `configs/vault-snapshot.example.toml`.

## Current scope

The repository currently covers:

- deterministic public projection and publication of the managed ObsidianCore surface;
- private Live Vault snapshot support;
- pull-only AI input mirroring;
- automatic Input Planner -> Generator -> Validator -> Reader -> Evaluator processing;
- Human-facing `04-AI/**` lifecycle projection;
- fail-closed Human Review Intake;
- separated Executor -> Sync -> Executor canonical Knowledge creation;
- post-review `60-Execution -> 70-Transport -> 80-Completed` projection;
- terminal Human-facing projection cleanup while retaining `80-Completed`;
- exact-SHA production bootstrap/update and authority verification;
- GitHub Project status synchronization and Core promotion workflows.

Production credentials, private Vault contents, environment-specific endpoints, and deployment secrets remain outside this repository.

For installation and operational acceptance, follow [Installation](docs/installation.md) and [Pre-review production rollout and acceptance](docs/pre-review-production.md).
