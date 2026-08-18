# ObsidianAutomation

Reusable automation software for maintaining and publishing an Obsidian Vault while keeping private Vault data and production deployment details outside this public repository.

The first implementation target is a deterministic Public Exporter that generates the public `ObsidianCore` projection from an explicitly allowlisted subset of a private Vault.

## Trust boundary

This repository may contain reusable automation code, schemas, tests, prompts, and container definitions. It must not contain production credentials, private Vault contents, internal endpoints, runner registration data, or environment-specific secrets.

The Public Exporter is deterministic and does not use an LLM. The private Vault is the source of projection content; public-repository-owned files are preserved separately.

## Development environment

### NixOS / Nix

The repository provides a flake-based development shell for Linux.

With direnv:

```bash
direnv allow
```

`.envrc` activates the default flake devShell with `use flake`.

Without direnv:

```bash
nix develop
```

The devShell provides Python 3.11, pytest, Git, and the `obsidian-public-export` development wrapper. The wrapper runs the current working-tree source directly, so an editable pip install is not required for normal development and testing.

```bash
pytest -q
obsidian-public-export --help
```

### Generic Python

```bash
python -m pip install -e '.[dev]'
```

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

## Current scope

The exporter only creates the local public working-tree projection. Automated pushes to `ObsidianCore`, Gitea runner integration, secret scanning, and AI/LLM workflows are intentionally out of scope for v0.
