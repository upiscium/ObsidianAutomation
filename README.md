# ObsidianAutomation

Reusable automation software for maintaining and publishing an Obsidian Vault while keeping private Vault data and production deployment details outside this public repository.

The repository includes deterministic Vault publication/synchronization components and the production AI lifecycle used to generate, validate, review, and conditionally publish Knowledge Notes while preserving explicit authority boundaries.

## Start here

For production deployment and first-host setup, see [Installation](docs/installation.md).

For the Human-facing AI workflow, including `04-AI` projection, Human Review, post-review delivery, and Completed cleanup, see [Getting started](docs/getting-started.md).

The detailed projection contract is documented in [Human-facing AI lifecycle projection](docs/human-ai-projection.md).

## Optional fixed-service operator console (#300)

The independently released #300 operator console supports five fixed systemd
timer/service pairs, with read-only status and explicitly confirmed
Pause/Resume/Run once requests. Operation receipts distinguish systemd Job
admission from service-level GitHub/Gitea/Nextcloud success. This is a
separately reviewed LAN/VPN-only operator feature, not the unfinished
#298/#311 single-LXC setup Wizard, Connections, Vault Gateway, folder mapping,
or Snapshot/Publisher migration. **Only one automation execution LXC per
managed Vault is supported**; multi-instance active control is unsupported.
The privileged Worker is OFF until its installed source, Unix ACLs, shared
updater locks and private ingress pass exact-revision acceptance.

See [the #300 standalone release scope](docs/releases/300-standalone-20261018.md)
and [operator deployment boundary](docs/admin-operator-operational-runbook.md).

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

## Public Publisher v0

`obsidian-public-publish` is the transaction helper intended for a trusted Gitea Runner. It requires a clean `ObsidianCore` checkout, applies the projection, runs repository validation/tests, and creates one local commit only after validation succeeds. It intentionally never runs `git push`.

The production design and example Gitea workflow are documented in `docs/gitea-publication.md` and `examples/gitea/public-projection.yml`.

## Live Vault Snapshot v0

`obsidian-vault-snapshot` turns a stable local mirror of the Live Vault into one private Git snapshot commit. The helper itself has no network access and never pushes.

The recommended production topology uses a dedicated unprivileged Linux container. That container pulls the Vault directly from Nextcloud over WebDAV into a local mirror, verifies the pull, runs the snapshot helper, and pushes only the resulting private Gitea commit. The Proxmox host does not need to mount or authenticate to Nextcloud.

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
