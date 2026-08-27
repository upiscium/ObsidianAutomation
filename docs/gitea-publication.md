# Gitea-driven ObsidianCore publication

This document describes the production boundary for publishing the public
`ObsidianCore` projection from the private Obsidian environment through the trusted,
LAN-only Gitea `ObsidianVault` snapshot repository.

## Trust model

- The **Nextcloud Live Vault** is the user-editable sync authority.
- Gitea `ObsidianVault` is the private snapshot / audit / automation authority fed by the read-only Live Vault snapshot path.
- `ObsidianAutomation` on GitHub contains reusable, public automation software.
- `ObsidianCore` on GitHub is a generated public projection plus repository-owned files and is also the review surface for managed system changes.
- Core -> Live Vault changes use the separate Promotion path; publication must never silently overwrite a reviewed Core change that has not yet converged through Promotion.
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

The publication helper never pushes. It applies or acknowledges the allowlisted projection,
validates `ObsidianCore`, and creates a local commit when necessary. The Gitea workflow
only pushes if the helper created a new commit.

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

When Core Promotion is enabled, this ref must include the guarded publication logic
and should match the reviewed `ObsidianAutomation` revision deployed by the private
promotion service. Update the guarded publisher **before** enabling the promotion timer.

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

## Generated projection baseline

New publication commits include the commit-message marker:

```text
Obsidian-Projection: v1
```

The publisher uses the latest marked commit as the generated projection baseline.
For migration from the pre-Promotion publication history, an exact legacy subject:

```text
Sync public projection from ObsidianVault
```

is accepted as the initial baseline.

Only paths managed by the public-export policy participate in the Core drift guard.
Repository-owned changes such as `.github/**`, `README.md`, or `LICENSE` do not block
normal Vault publication.

## Core Promotion convergence guard

A reviewed Core managed-path change can temporarily make Core newer than the Live Vault.
Without a guard, an unrelated Vault snapshot could publish the old Live bytes and erase
that reviewed Core change before Promotion runs.

The publisher therefore computes both:

1. the projection changes required to make Core match the current Gitea Vault snapshot; and
2. the net managed-path drift from the last generated projection baseline to current Core HEAD.

The decision is:

```text
projection differs from Core
AND unacknowledged managed Core drift exists
    -> fail closed before modifying Core

projection already equals Core
AND unacknowledged managed Core drift exists
    -> create an empty generated acknowledgement commit

no unacknowledged managed Core drift
    -> ordinary publication behavior
```

The empty acknowledgement commit is intentional. After Promotion has written the reviewed
Core bytes to the Live Vault and the normal read-only snapshot has captured them, the Gitea
projection contains no file diff against Core. The acknowledgement advances the generated
projection baseline without rewriting any content, so later independent Vault edits can again
publish normally.

This is not an approval mechanism. It only prevents the Vault -> Core projection from racing
with the separate Core -> Live Vault Promotion transaction.

## Publication transaction

On each push to `ObsidianVault/main`:

1. Gitea checks out the private Vault snapshot.
2. The workflow clones `ObsidianAutomation` and checks out the pinned revision.
3. The workflow creates a Python virtual environment and installs that revision.
4. The workflow clones `ObsidianCore` using the repository-scoped deploy key.
5. `obsidian-public-publish` loads the exact allowlist policy and evaluates the convergence guard before applying any projection mutation.
6. If unacknowledged managed Core drift would be overwritten, the helper fails closed and no push is attempted.
7. Otherwise the helper applies the allowlist-only projection when a file diff exists.
8. The helper runs, when validation is required:
   - `git diff --check`
   - `node 98-System/99-dev/validate-repo.mjs`
   - `node --test 98-System/99-dev/test/*.test.mjs`
9. If Core already equals the Vault projection but unacknowledged managed drift has converged through Promotion, the helper creates one empty generated acknowledgement commit.
10. If there is neither a projection diff nor an acknowledgement to record, no commit is created.
11. If validation succeeds and a commit is required, the helper creates exactly one local generated projection/acknowledgement commit.
12. The workflow pushes that commit to `ObsidianCore/main`.

The existing GitHub Actions validation on `ObsidianCore` remains a second independent
post-push check.

## Interaction with Core Promotion

The safe production ordering is:

```text
1. deploy this guarded publisher revision to Gitea
2. verify publication is running the pinned guarded revision
3. deploy the same reviewed ObsidianAutomation revision to the private promotion service
4. bootstrap the promotion checkpoint from a known generated projection commit
5. perform a manual no-op promotion run
6. only then enable the promotion timer
```

Do not enable Promotion while Gitea still runs the legacy unguarded publisher.

After a successful Core promotion:

```text
Core reviewed head
    -> Promotion writes exact bytes to Nextcloud Live Vault
    -> read-only snapshot commits Live state to Gitea ObsidianVault
    -> guarded publisher observes Vault == Core
    -> generated acknowledgement commit advances the projection baseline
```

## Recovery

A failed publication never changes the Nextcloud Live Vault or the Gitea snapshot checkout
that triggered the workflow. If a generated Core publication commit reaches GitHub but later
needs to be reverted, fix the authoritative Live Vault/publication state and allow the normal
snapshot/publication path to converge again.

If publication is blocked by unacknowledged managed Core drift, do not force-push or manually
copy older Vault bytes over Core. Inspect or complete the Core Promotion transaction. Once the
Live Vault and subsequent Gitea snapshot match the reviewed Core bytes, the guarded publisher
can acknowledge convergence automatically.
