# Gitea-driven ObsidianCore publication

This document describes the production boundary for publishing the public
`ObsidianCore` projection from a private `ObsidianVault` hosted on a trusted,
LAN-only Gitea instance.

## Trust model

- `ObsidianVault` on Gitea is the private canonical source.
- `ObsidianAutomation` on GitHub contains reusable, public automation software.
- `ObsidianCore` on GitHub is a generated public projection plus repository-owned files.
- The Gitea instance and its repositories/users are inside the trusted LAN boundary.
- The Gitea runner is registered at **instance level** but uses a dedicated
  `obsidian-publisher` label for routing.
- The label is routing, not an authorization boundary. If untrusted repositories or
  users are ever introduced to the Gitea instance, reduce runner scope or isolate jobs
  before continuing to use a host runner.

The production runner is intentionally separated from the NixOS development host.
For v0 it runs in a dedicated **Debian 13 unprivileged LXC**. The LXC itself is the
runtime isolation boundary; no Proxmox host directories are bind-mounted into it.
The runner executes jobs in host mode inside that LXC and does not require Docker or
Nix.

The publication helper never pushes. It applies the allowlisted projection, validates
`ObsidianCore`, and creates a local commit. The Gitea workflow only pushes if the
helper created a new commit.

## Required runner software

The Debian runner environment needs:

- Gitea Runner
- Git
- Python 3.11 or newer
- Node.js 20 or newer
- OpenSSH client
- CA certificates

The example workflow installs `ObsidianAutomation` into an isolated Python virtual
environment for each publication job. Production does not call `nix develop`.

## Required Gitea configuration

Enable Actions for the private `ObsidianVault` repository.

Configure these repository-level values:

### Variable

`OBSIDIAN_AUTOMATION_REF`

Pin this to a released tag or immutable commit of `upiscium/ObsidianAutomation`.
Do not use a floating `main` value in production.

### Secrets

`OBSIDIAN_CORE_DEPLOY_KEY`

A private SSH deploy key whose public half is installed on the GitHub
`upiscium/ObsidianCore` repository with write access. Do not reuse this key for any
other repository.

`OBSIDIAN_CORE_KNOWN_HOSTS`

A trusted `known_hosts` entry for GitHub SSH. Provision this from a trusted source so
that the workflow does not learn the SSH host key dynamically during a publication
job.

The example workflow is `examples/gitea/public-projection.yml`. Copy it into the
private Vault as `.gitea/workflows/public-projection.yml`.

## Instance-level runner registration

Register the dedicated Debian LXC as an instance-level Gitea Runner using the label:

```text
obsidian-publisher:host
```

The workflow requests the routing label as:

```yaml
runs-on: obsidian-publisher
```

Use the Gitea instance LAN address when registering the runner; do not use `localhost`
or `127.0.0.1` unless Gitea itself runs inside the same LXC.

The runner process should run as an unprivileged Linux user and be managed by systemd.
The runner registration state and working directory should be kept under a dedicated
path such as `/var/lib/gitea-runner`.

## Publication transaction

On each push to `ObsidianVault/main`:

1. Gitea checks out the private Vault.
2. The workflow clones `ObsidianAutomation` and checks out the pinned revision.
3. The workflow creates a Python virtual environment and installs that revision.
4. The workflow clones `ObsidianCore` using the repository-scoped deploy key.
5. `obsidian-public-publish` applies the allowlist-only projection.
6. The helper runs:
   - `git diff --check`
   - `node 98-System/99-dev/validate-repo.mjs`
   - `node --test 98-System/99-dev/test/*.test.mjs`
7. If validation fails, no commit is created and no push is attempted.
8. If the projection is unchanged, no commit is created and no push is attempted.
9. If validation succeeds and content changed, the helper creates one local commit.
10. The workflow pushes that commit to `ObsidianCore/main`.

The existing GitHub Actions validation on `ObsidianCore` remains a second independent
post-push check.

## Recovery

A failed publication never changes the private canonical Vault. If a publication
commit reaches GitHub but later needs to be reverted, revert the generated
`ObsidianCore` commit and fix either the private Vault content or publication policy
before the next Gitea run. Otherwise the next successful projection will regenerate
the canonical public state.
