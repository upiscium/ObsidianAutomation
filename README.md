# ObsidianAutomation

Reusable automation software for maintaining and publishing an Obsidian Vault while keeping private Vault data and production deployment details outside this public repository.

The current deterministic pipeline covers both stable snapshots of a synchronized Live Vault into private Git and allowlist-only publication from the private Vault into `ObsidianCore`.

## Trust boundary

This repository may contain reusable automation code, schemas, tests, prompts, and container definitions. It must not contain production credentials, private Vault contents, internal endpoints, runner registration data, or environment-specific secrets.

The snapshot and publication tools are deterministic and do not use an LLM. The Live Vault is read-only to snapshot automation, the private Vault is the source of public projection content, and repository-owned files are preserved separately.

## Nix development environment

On NixOS or another system with flakes enabled, enter the development environment with:

```bash
nix develop
```

With direnv installed, the repository also provides `.envrc`:

```bash
direnv allow
```

The development shell provides Python, pytest, Git, Node.js, `obsidian-vault-snapshot`, `obsidian-public-export`, and `obsidian-public-publish` from the current working tree.

Nix is for development and manual verification only. Production snapshot/publisher hosts are expected to run independently and do not require Nix.

## Live Vault Snapshot v0

`obsidian-vault-snapshot` turns a locally synchronized Live Vault into one stable local Git snapshot commit without ever writing to the Live Vault or pushing to a remote.

The source and destination must be separate, non-nested roots. Before touching the private Git worktree, the helper requires repeated matching content manifests, copies the source into a temporary staging tree, and verifies the source/staging manifests again. If synchronization is still changing files, the run fails before destination mutation.

The example policy is in `configs/vault-snapshot.example.toml`.

```bash
obsidian-vault-snapshot \
  --source /path/to/live-vault \
  --destination /path/to/private-ObsidianVault-checkout \
  --config configs/vault-snapshot.example.toml \
  --dry-run
```

A changed real snapshot creates one local commit with a `Source-Manifest-SHA256` provenance trailer. No-op snapshots create no commit. The recommended private deployment uses a bare cache plus a disposable worktree for each scheduled run; see `docs/live-vault-snapshot.md`.

## Public Exporter v0

The exporter treats the destination repository as two trust domains:

- paths matched by `repository_owned` are preserved and never managed by the exporter;
- every other destination file belongs to the generated projection and is removed if it is no longer present in the allowlisted source projection.

Git metadata (`.git` and `.git/**`) is always protected even if it is omitted from the configuration.

The example policy is in `configs/public-export.example.toml`.

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

## Current scope

The deterministic Git-backed path is being completed before any AI/LLM workflow or canonical Vault write path. AI proposal, evaluation, and execution remain out of scope for this stage.
