# ObsidianAutomation

Reusable automation software for maintaining and publishing an Obsidian Vault while keeping private Vault data and production deployment details outside this public repository.

The first implementation target is a deterministic Public Exporter that generates the public `ObsidianCore` projection from an explicitly allowlisted subset of a private Vault.

## Trust boundary

This repository may contain reusable automation code, schemas, tests, prompts, and container definitions. It must not contain production credentials, private Vault contents, internal endpoints, runner registration data, or environment-specific secrets.

The Public Exporter and Publisher are deterministic and do not use an LLM. The private Vault is the source of projection content; public-repository-owned files are preserved separately.

## Nix development environment

On NixOS or another system with flakes enabled, enter the development environment with:

```bash
nix develop
```

With direnv installed, the repository also provides `.envrc`:

```bash
direnv allow
```

The development shell provides Python 3.11, pytest, Git, Node.js 22, `obsidian-public-export`, and `obsidian-public-publish` from the current working tree.

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

`obsidian-public-publish` is the transactional wrapper intended for Gitea Runner usage. The destination must be a clean `ObsidianCore` Git repository root.

```bash
obsidian-public-publish \
  --source /path/to/private-vault \
  --destination /path/to/ObsidianCore \
  --config configs/public-export.example.toml
```

When projection content changes, it validates `ObsidianCore` and creates one local Git commit. It deliberately never runs `git push`. A validation failure creates no commit, and an unchanged projection is a no-op.

The instance-level Gitea Runner deployment boundary and an example private-Vault workflow are documented in `docs/gitea-publication.md` and `examples/gitea/public-projection.yml`.

## Current scope

The current scope covers deterministic private-Vault-to-public-Projection generation and a Gitea-driven publication path. AI/LLM workflows, Nextcloud-to-Gitea snapshot automation, and canonical Vault mutation remain out of scope.
