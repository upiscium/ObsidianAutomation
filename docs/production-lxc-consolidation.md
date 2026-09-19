# Production LXC consolidation and private migration inventory

## Target topology

Writer-side automation is consolidated by trust domain, not by feature:

```text
obsidian-automation
  Publisher / Gitea Runner
  Core Promotion
  AI Writer / pre-review
  GitHub Sync / local writer / compactor
  future private Importer

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

The new host should normally register a fresh Gitea Runner rather than copying
opaque runner registration state.

The following are Gitea repository configuration, not LXC files:

- `OBSIDIAN_CORE_DEPLOY_KEY`;
- `OBSIDIAN_CORE_KNOWN_HOSTS`;
- `OBSIDIAN_AUTOMATION_REF`.

Do not copy these repository Actions values into the new host filesystem.

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
/etc/obsidian-github-writer/webdav-password
/var/lib/obsidian-github-sync
/var/lib/obsidian-github-pipeline
/var/lib/obsidian-github-mirror
/srv/obsidian-github-sync/vault
```

Rebuildable mirrors should normally be rebuilt on the new LXC. Durable queue /
SQLite / request-result state requires a quiesced migration if continuity is
needed.

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
