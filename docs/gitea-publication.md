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

## Instance-level runner on NixOS

NixOS provides the `services.gitea-actions-runner` module. A minimal host-mode runner
for this workflow can be configured as follows:

```nix
{ pkgs, ... }:
{
  services.gitea-actions-runner.instances.obsidian-publisher = {
    enable = true;
    name = "obsidian-publisher";
    url = "https://gitea.example.lan/";

    # Environment file containing:
    # TOKEN=<instance-level-runner-registration-token>
    tokenFile = "/run/secrets/gitea-actions-runner-token";

    labels = [
      "obsidian-publisher:host"
    ];

    hostPackages = with pkgs; [
      bash
      coreutils
      curl
      gawk
      git
      gnused
      nodejs_22
      openssh
      wget
      nix
      cacert
    ];
  };
}
```

Obtain the instance-level registration token from the Gitea admin Actions runner
settings. Keep the token file outside the Nix store and restrict its filesystem
permissions.

The host runner executes workflow commands directly on the NixOS host. This is an
intentional v0 choice because the Gitea instance is LAN-only and trusted. It should
not be treated as sandboxing.

## Publication transaction

On each push to `ObsidianVault/main`:

1. Gitea checks out the private Vault.
2. The workflow clones `ObsidianAutomation` and checks out the pinned revision.
3. The workflow clones `ObsidianCore` using the repository-scoped deploy key.
4. `obsidian-public-publish` applies the allowlist-only projection.
5. The helper runs:
   - `git diff --check`
   - `node 98-System/99-dev/validate-repo.mjs`
   - `node --test 98-System/99-dev/test/*.test.mjs`
6. If validation fails, no commit is created and no push is attempted.
7. If the projection is unchanged, no commit is created and no push is attempted.
8. If validation succeeds and content changed, the helper creates one local commit.
9. The workflow pushes that commit to `ObsidianCore/main`.

The existing GitHub Actions validation on `ObsidianCore` remains a second independent
post-push check.

## Recovery

A failed publication never changes the private canonical Vault. If a publication
commit reaches GitHub but later needs to be reverted, revert the generated
`ObsidianCore` commit and fix either the private Vault content or publication policy
before the next Gitea run. Otherwise the next successful projection will regenerate
the canonical public state.
