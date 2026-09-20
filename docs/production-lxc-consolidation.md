# Production LXC consolidation and private migration inventory

## Target topology

Writer-side automation is consolidated by trust domain, not by feature:

```text
obsidian-automation
  Core Promotion
  AI Writer / pre-review
  GitHub Sync / local writer / compactor
  future private Importer

gitea-runner
  workflow execution only; separate LXC

obsidian-snapshot-taker
  read-only Nextcloud snapshot authority only
```

The Snapshot LXC remains separate. Its read-only Nextcloud credential is never
copied into the writer-side automation LXC.

Inside `obsidian-automation`, feature and authority boundaries remain separate
Unix identities with POSIX ACLs and systemd sandboxing. Consolidating containers
must not widen credential readability.

## Value-free inventory

Before copying any private configuration, inspect each existing writer-side LXC
with:

```bash
obsidian-production-migration-inventory --role publisher
obsidian-production-migration-inventory --role ai
obsidian-production-migration-inventory --role github-sync
```

Run only the role matching that LXC.

The command never opens the declared credential/config files. It uses filesystem
metadata only and reports:

- a fixed logical ID;
- the fixed manifest path;
- presence;
- file type;
- owner/group;
- permission mode;
- coarse size class;
- declared expected field names;
- the intended migration action.

It does **not** report file contents, hashes, endpoints, usernames, passwords,
token prefixes, SSH key material, symlink targets, or arbitrary directory
listings.

Review the report before sharing it. Paths, service-user names, and structural
metadata may still be operationally private.

## Publisher boundary

The dedicated `gitea-runner` LXC registers a fresh Gitea Runner; the automation
host does not run a workflow executor. See `gitea-runner-production.md` for the
source-side binary/config/unit staging and registration boundary. Never copy
opaque registration state.

The existing Publisher LXC runs `gitea-runner.service`; cutover must stop that old unit before starting the newly registered runner on the dedicated Runner LXC.

The following are Gitea repository configuration, not LXC files:

- `OBSIDIAN_CORE_DEPLOY_KEY`;
- `OBSIDIAN_CORE_KNOWN_HOSTS`;
- `OBSIDIAN_AUTOMATION_REF`.

Do not copy these repository Actions values into the new host filesystem.

The Publisher-side inventory also covers Core Promotion when deployed on the
existing writer host:

```text
/etc/obsidian-core-promotion/promotion.env
/etc/obsidian-core-promotion/public-export.toml
/etc/obsidian-core-promotion/nextcloud.password
/var/lib/obsidian-core-promotion
/etc/systemd/system/obsidian-core-promotion.service
/etc/systemd/system/obsidian-core-promotion.timer
```

Core Promotion configuration/credential entries are conditional because older
Publisher hosts may not have the promotion service deployed. If either the
promotion service/timer or its state root is present, treat `promotion.env`,
`public-export.toml`, `nextcloud.password`, and the durable state root as one
migration boundary. The state root must be copied only while the old promotion
timer/service is quiesced. The public Git cache inside the state root is
rebuildable, but checkpoint/plans/receipts are not.

## AI boundary

The initial manifest includes:

```text
/etc/obsidian-ai/rclone.conf
/etc/obsidian-ai/vault-pull.filters
/etc/obsidian-ai/webdav-password
/etc/obsidian-ai/pre-review-generator.env
/etc/obsidian-ai/pre-review-evaluator.env
/etc/obsidian-ai/pre-review-revision.env
/var/lib/obsidian-ai/state
/var/lib/obsidian-ai/deployments
/var/lib/obsidian-ai/vault
```

The revision environment is derived and should be recreated by the deployment
lifecycle. The local Vault is a pull-only replica and should normally be rebuilt.
Durable lifecycle state must be migrated only while old writer-side automation
is quiesced.

## GitHub Sync boundary

The initial manifest includes:

```text
/etc/obsidian-github-sync/config.toml
/etc/obsidian-github-sync/credentials.env
/etc/obsidian-github-mirror/rclone.conf
/etc/obsidian-github-mirror/vault-pull.filters
/etc/obsidian-github-writer/config.env
/etc/obsidian-github-writer/webdav-password
/var/lib/obsidian-github-sync
/var/lib/obsidian-github-pipeline
/var/lib/obsidian-github-mirror
/srv/obsidian-github-sync/vault
```

The writer `config.env` is required by the production writer unit and contains
the non-secret Nextcloud base URL / username binding; it must migrate or be
recreated alongside the writer password.

Rebuildable mirrors should normally be rebuilt on the new LXC. Durable queue /
SQLite / request-result state requires a quiesced migration if continuity is
needed.


## Consolidated authority provisioning

Before any credential or durable state is migrated, the target-owned bootstrap creates the local authority boundary for the consolidated writer LXC.

Provisioned identities include:

- a dormant compatibility `gitea-runner` account (no runner binary, unit, registration or jobs);
- `obsidian-core-promoter`;
- `obsidian-ai-sync/reader/generator/validator/evaluator/status/reviewer/executor`;
- `obsidian-github-mirror/sync/writer/compactor`.

GitHub handoff groups remain separate from credential groups:

- `obsidian-github-vault` — mirror read handoff only;
- `obsidian-github-pipeline` — local request/result handoff only.

The provisioner creates only empty directory roots and POSIX ACLs. It does not create `rclone.conf`, WebDAV passwords, provider env files, GitHub tokens, promotion passwords, systemd units, or timers.

AI lifecycle authority preserves the existing stage separation: Generator writes Untrusted, Reader writes Index/Context, Validator writes Validation/Evaluation Request, Evaluator writes Evaluation, Reviewer writes Review, Executor writes Execution/Receipts, and Sync writes Transport/Vault mirror. Status receives orchestration metadata/status projection access only.

Top-level AI `vault` and `state` roots intentionally receive no default ACL entries; default ACLs begin at the known stage directories so a future unknown directory does not inherit broad authority accidentally.

At this phase the bootstrap receipt must show:

```json
{
  "authority_provisioning": "passed",
  "host_activation": "not_attempted"
}
```

## Private config / credential stream transfer

After the credential-free authority boundary passes, migrate only the declared
private files before quiescing any old writer.

Use the same reviewed ObsidianAutomation revision on both ends. The transfer
helper has an explicit allowlist for each source role:

```text
publisher
  Core Promotion promotion.env
  Core Promotion public-export.toml
  Core Promotion nextcloud.password

ai
  rclone.conf
  vault-pull.filters
  webdav-password when deployed
  pre-review-generator.env
  pre-review-evaluator.env

github-sync
  config.toml
  credentials.env when present
  mirror rclone.conf
  mirror vault-pull.filters
  writer config.env
  writer webdav-password
```

The AI revision env is derived and is not transferred. Gitea Runner registration
state is not transferred. Durable state and rebuildable mirrors are not members
of the private-config bundle.

The source command writes a binary bundle to stdout and fixed metadata-only
status to stderr. The destination accepts only known logical IDs and installs
files atomically with fixed owner/group/mode. It refuses symlink sources,
unknown entries, partial Core Promotion config, truncated/trailing bundle data,
and an existing destination whose bytes differ.

A retry with identical bytes is idempotent. The tool may compare existing
destination bytes internally for that purpose, but never prints secret values or
secret hashes.

The intended transport is a direct trusted stream such as SSH:

```text
old role LXC exporter stdout
        |
        | authenticated SSH transport
        v
new obsidian-automation importer stdin
```

Do not redirect the bundle to a shell-visible temporary file unless recovery
procedures explicitly require an encrypted staging artifact.

After each role import, run the built-in readability verification. It checks
owner/group/mode and positive/negative read access for the service identities
without printing file contents.

This phase does not stop the old services and does not install or start new
production units, so canonical writer authority remains only on the old LXCs.

## Secret transfer

Do not paste credential values into GitHub, chat, shell history, or command-line
arguments.

The migration procedure must:

1. bootstrap users/directories/ACLs on the new LXC first;
2. stop the relevant old producer/writer service before copying durable state;
3. transfer each approved private file over a trusted root-to-root channel;
4. install it directly with the intended owner/group/mode;
5. prove unrelated service identities cannot read it;
6. keep the corresponding new writer timer disabled until acceptance passes.

Do not bulk-copy `/etc`, service-user homes, venvs, or old repository checkouts.

## Inert production unit staging

After private config/credential migration passes, stage the production systemd
units on the new consolidated LXC while it is still non-serving.

The staging tool reads the reviewed canonical unit examples from the exact target
checkout and installs 17 units:

- AI Vault mirror + Input Planner + human projection + pre-review pipeline: 10 units;
- GitHub Sync pipeline: 5 units;
- Core Promotion: 2 units.

Runtime paths are rewritten only from the legacy per-LXC prefixes to:

```text
/opt/obsidian-automation/venv/bin
/opt/obsidian-automation/app
```

The tool also recreates the derived AI revision binding:

```text
/etc/obsidian-ai/pre-review-revision.env
```

with the exact reviewed deployment SHA.

This command is deliberately **staging-only**. It refuses to operate if any
managed production timer is enabled/active or any managed service is active.
After installation it reloads systemd and requires all four recurring timers to
remain disabled/inactive:

- `obsidian-ai-vault-pull.timer`;
- `obsidian-pre-review.timer`;
- `obsidian-github-sync.timer`;
- `obsidian-core-promotion.timer`.

No production service is started. Gitea Runner is not part of this unit staging
because it is hosted on a separate execution-plane LXC and remains inactive until
its own cutover. No runner binary/unit/registration belongs on the automation host.

Once the new host has passed this inert staging gate, a later cutover transaction
may quiesce old writers, transfer durable state, run canaries, and enable the new
timers.

## Cutover

Never run equivalent canonical writers on old and new LXCs simultaneously.

A safe sequence is:

```text
bootstrap new LXC
  -> value-free inventory old LXC
  -> provision identities and ACLs
  -> install private config/credentials
  -> migrate only required durable state
  -> rebuild mirrors
  -> safe/shadow acceptance
  -> stop old recurring writer
  -> final state copy if needed
  -> enable new recurring writer
  -> production health verification
  -> keep old LXC powered off but recoverable
```

The source-side bootstrap/self-update implementation is tracked separately and
must be accepted before production cutover.

## Final acceptance and retirement

The deployment topology has three trust domains, not three interchangeable
workers. #111 records the completed role migration; #110 tracks the generic
automation-host update transaction. Merge/deploy #110 before treating the parent
#109 lifecycle as fully accepted. A PR/CI result does not substitute for real-host
acceptance of that new lifecycle.

Retire old containers only after backup and fresh production evidence: complete
pipeline stages with invocation timestamps, not just stale `Result=success`.
A `Type=oneshot` process can legitimately be `activating` during execution.
An idle AI chain does not prove non-empty generation/review-stop behavior.

After cutover, old durable state is stale. Do not simply re-enable an old writer:
first quiesce the new writer, transfer/reconcile its latest durable state and
credentials, validate the target, then switch authority. Never overwrite current
state with an old backup in order to roll software back. Keep snapshot credentials
out of both writer and runner domains.

The legacy dormant runner account in automation authority fixtures is retained
for negative access checks and compatibility, not permission to run jobs there.
Do not delete accounts recursively on a serving host; no credential/home purge is
part of this topology update.
