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

The publication helper never pushes. It applies the allowlisted projection, validates
`ObsidianCore`, and creates a local commit. The Gitea workflow only pushes if the
helper created a new commit.

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

## Production runner

Use a dedicated Linux VM for the production runner rather than the NixOS development
host. The intended v0 deployment is a small Debian 13 VM running `act_runner` in host
mode. The VM itself is the isolation boundary.

Gitea generally recommends containerized jobs because host mode provides no job-level
encapsulation. In this deployment, host mode is accepted because the Gitea instance is
LAN-only/trusted and the VM exists only to run CI jobs. If this trust assumption
changes, switch the runner to Docker/DinD or otherwise isolate each job.

Do not place the runner on the same machine as the Gitea server if a separate VM is
available.

### Required host software

The publication workflow requires:

- `bash`
- `git`
- `python3` >= 3.11
- `node` >= 20
- `ssh`
- CA certificates
- standard core utilities

Debian 13 provides a sufficiently new Python and Node.js from the normal repositories.
For example:

```bash
sudo apt update
sudo apt install -y \
  ca-certificates \
  curl \
  git \
  nodejs \
  openssh-client \
  python3
```

No Nix installation is required on the production runner. `flake.nix` remains a
development and manual-verification environment only.

### Runner registration

Create an unprivileged `act_runner` service account and install a stable `act_runner`
binary according to the Gitea documentation. Obtain the **instance-level** registration
token from the Gitea admin Actions runner settings, then register the runner with the
dedicated host label:

```bash
act_runner register \
  --no-interactive \
  --instance https://gitea.example.lan/ \
  --token '<instance-registration-token>' \
  --name obsidian-publisher \
  --labels 'obsidian-publisher:host'
```

Do not commit the registration token or runner registration state. Run `act_runner`
as the unprivileged service user with `/var/lib/act_runner` as its working directory.
The runner can then be managed by systemd using the service pattern documented by
Gitea.

## Publication transaction

On each push to `ObsidianVault/main`:

1. Gitea checks out the private Vault.
2. The workflow clones `ObsidianAutomation` and checks out the pinned revision.
3. The workflow clones `ObsidianCore` using the repository-scoped deploy key.
4. Python directly invokes `obsidian_automation.public_publish`; production does not
   enter a Nix development shell.
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
