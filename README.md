# ObsidianAutomation — Three-service profile

ObsidianAutomation is a **single, non-root, LAN/VPN-only application** for three
fixed Obsidian Vault workflows. It has exactly three WebUI service panels:

| Workflow | Source → destination | Control |
| --- | --- | --- |
| Snapshot | Live Vault → private Gitea Git snapshot | Enable/disable, interval, Run Once, verified result |
| Core Promotion | reviewed GitHub ObsidianCore → canonical Vault | Enable/disable, interval, Run Once, verified result |
| GitHub Daily | GitHub Issue/PR activity → yesterday's JST Daily Note | Enable/disable, interval, Run Once, verified result |

No AI Knowledge, External ChatGPT Import, GitHub Project Writer, Vault
Formatter, root Operator console, or general-purpose command runner is exposed
or scheduled by this profile. The GitHub reader used by Daily is **not** the
retired GitHub Project Writer.

## Runtime

The one process `obsidian-snapshot-app` owns all three independent persistent
SQLite Job controllers and the private management UI. First use requires
Nextcloud, Gitea (for Snapshot), and GitHub (for Daily) credentials as
applicable. Core Promotion additionally requires a reviewed baseline checkpoint.

```sh
obsidian-snapshot-app \
  --state /var/lib/obsidian-snapshot \
  --bind 127.0.0.1 \
  --daily-publish
```

Binding to a trusted RFC1918 LAN address is supported. The app intentionally
does **not** implement a Web login: network access must be restricted to a
trusted LAN and authenticated VPN. The default without `--daily-publish`
is Daily Preview only, **not** Vault publication.

Each service has explicit configuration and schedule status, a one-use-CSRF
settings action, a request-ID-bound Run Once action, and a separate verified
execution result. A `202` receipt only proves admission, not completion.
Ambiguous Core Promotion effects block further execution until reconciled.

## Core Promotion setup

The first Core Promotion checkpoint is an explicit operation: choose an
**already reflected** generated public-projection commit in
`upiscium/ObsidianCore`; verify the live Vault and the exact allowlist policy
before entering its complete 40-character SHA in the UI. The new adapter
reuses the existing conditional WebDAV promotion, deterministic policy checks
and managed `.obsidian/appearance.json` compatibility. **Do not** initialize
from an arbitrary latest commit or infer that an old-host checkpoint is safe
to reuse without checking its exact source and policy.

The bundled public-export policy remains coupled to the Gitea publication
bridge. `obsidian-public-export` and `obsidian-public-publish` remain as
supporting commands for the currently separate Gitea Runner; they are not
independent WebUI services. Do not retire the Runner until a different
publisher has been independently verified.

## Source-only / production boundary

Changing this repository does not deploy or activate jobs on
`obsidian-control`. Production setup and rollback are documented in
[Three-service deployment and acceptance](docs/three-services.md). The old
`--vault-root-ui` / `--vault-bindings-ui` command-line switches have been
retired; update the service unit **when** doing an independently approved
production cutover. Existing Vault contents and private SQLite state must be
preserved; do not delete them as part of the source purge.

## Development

```sh
nix develop
pytest -q
```

The build publishes five fixed console scripts: the three-service app, the
existing public exporter/publisher support tools, and Core Promotion
bootstrap/run recovery commands. Its remaining internal Python modules are
libraries required by these three workflows, not additional active services.
