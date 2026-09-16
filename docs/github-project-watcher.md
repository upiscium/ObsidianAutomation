# GitHub Project Status Watcher v0.1

`obsidian-github-project-watch` observes GitHub repositories referenced by Obsidian Project notes and emits deterministic Project status proposals.

v0.1 is intentionally **read-only** with respect to the canonical Vault. It does not patch Project notes. The service reads a local pull-only Vault mirror, reads GitHub API state, stores observation state in SQLite, and writes JSON observations to stdout/journald.

## Responsibility boundary

The watcher is an integration service, not an AI service.

```text
Nextcloud Vault --pull-only--> local Project mirror
                                  |
GitHub API (read-only) -----------+
                                  v
                         Repository snapshot
                              + SQLite
                                  |
                                  v
                    Deterministic status policy
                                  |
                                  v
                       JSON status proposal
```

AI/LLM processing is not part of the status decision path. A future integration may forward structured GitHub activity to an AI processor for summaries, but that is a separate downstream concern.

## Project metadata

Only Project notes below `10-Project/` that explicitly opt in are watched:

```yaml
---
type: project
status: planning
github_repo: upiscium/Terreate
github_watch: true
---
```

`github_repo` must be an `owner/repository` name. GitHub observation cursors and baselines are not written into Vault frontmatter; they remain local SQLite state owned by the watcher.

## Status policy

The canonical Project statuses are:

- `planning`
- `running`
- `stopped`
- `done`
- `cancelled`

### planning / running

For a non-terminal, non-stopped Project:

- latest repository-wide push/force-push activity within the configured 7-day window -> `running`
- no repository-wide push/force-push activity within the configured 7-day window -> `planning`

The watcher uses GitHub Repository Activity rather than only the default branch commit list, so work pushed to feature branches also counts as current Project activity.

Open Issues and Pull Requests do not override the Commit rule during normal `planning` / `running` operation.

### stopped

`stopped` is human-controlled. GitHub activity never changes it.

### done / cancelled

A terminal status is preserved until activity **after the terminal baseline** is observed.

- new push/force-push Commit activity -> `running`
- newly-open Issue or Pull Request -> `planning`
- no new activity -> preserve `done` / `cancelled`
- Commit activity wins when Commit and Issue/PR activity occur in the same observation

When a Project first enters `done` or `cancelled`, that observation initializes a new baseline and never reactivates the Project immediately. This prevents old Open Issues or Pull Requests from being mistaken for post-completion activity.

If a status proposal is not yet applied, it remains pending in SQLite and is emitted again on later runs until the canonical Project status changes. This makes v0.1 dry-run operation observable without consuming an event permanently.

## Configuration

Use `configs/github-project-watcher.example.toml` as the base:

```toml
[watcher]
vault_root = "/srv/obsidian-github-sync/vault"
state_db = "/var/lib/obsidian-github-sync/state.sqlite3"
project_folder = "10-Project"
active_window_days = 7
github_api_base = "https://api.github.com"
github_token_env = "GITHUB_TOKEN"
request_timeout_seconds = 15
```

A GitHub token is optional for public repositories. For private repositories, set `GITHUB_TOKEN` in `/etc/obsidian-github-sync/credentials.env`. Use a fine-grained/read-only token limited to the repositories and GitHub metadata required by the watcher.

## LXC deployment

The intended production boundary is a dedicated unprivileged LXC named `obsidian-github-sync`.

Recommended layout:

```text
/opt/obsidian-github-sync/app/       repository checkout
/opt/obsidian-github-sync/venv/      Python virtualenv
/etc/obsidian-github-sync/config.toml
/etc/obsidian-github-sync/credentials.env
/etc/obsidian-github-sync/rclone.conf
/etc/obsidian-github-sync/vault-pull.filters
/var/lib/obsidian-github-sync/state.sqlite3
/var/lib/obsidian-github-sync/state/24-Locks/
/srv/obsidian-github-sync/vault/      local pull-only Vault mirror
```

Install runtime packages and the Python package:

```bash
apt install -y rclone
python3 -m venv /opt/obsidian-github-sync/venv
/opt/obsidian-github-sync/venv/bin/pip install /opt/obsidian-github-sync/app
```

### Pull-only Vault mirror

The watcher does not need the whole Vault. `examples/github-sync/vault-pull.filters` mirrors only `10-Project/**`.

The pull process reuses `obsidian-production-vault-pull`, which always runs `rclone sync` in the remote-to-local direction. The watcher unit additionally mounts the resulting local mirror read-only through systemd sandboxing.

For the credential boundary, use a dedicated Nextcloud identity whose `ObsidianVault` access is read-only when possible. Do not reuse a canonical writer credential.

Copy the filter and create the local state/mirror directories:

```bash
install -d -o obsidian-github-sync -g obsidian-github-sync /srv/obsidian-github-sync/vault
install -d -o obsidian-github-sync -g obsidian-github-sync /var/lib/obsidian-github-sync/state/24-Locks
install -m 0644 examples/github-sync/vault-pull.filters /etc/obsidian-github-sync/vault-pull.filters
```

Configure an rclone WebDAV remote named `nextcloud-github-sync` in `/etc/obsidian-github-sync/rclone.conf`. The example service expects the Vault at:

```text
nextcloud-github-sync:ObsidianVault
```

Test the mirror independently before starting the watcher:

```bash
systemctl start obsidian-github-sync-vault-pull.service
find /srv/obsidian-github-sync/vault/10-Project -type f -name '*.md' | head
```

### systemd cycle

Install the pull service, watcher service, and watcher timer:

```bash
install -m 0644 examples/github-sync/obsidian-github-sync-vault-pull.service /etc/systemd/system/
install -m 0644 examples/github-sync/obsidian-github-sync.service /etc/systemd/system/
install -m 0644 examples/github-sync/obsidian-github-sync.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now obsidian-github-sync.timer
```

The watcher service has `Requires=` and `After=` dependencies on `obsidian-github-sync-vault-pull.service`. Every 15-minute watcher cycle therefore refreshes the Project mirror first; a failed mirror refresh prevents that watcher run from using stale/incomplete input.

The watcher itself sees `/srv/obsidian-github-sync/vault` as read-only and only writes its local SQLite state below `/var/lib/obsidian-github-sync`.

## Manual dry-run verification

Refresh the mirror, then run one observation manually before enabling the timer:

```bash
systemctl start obsidian-github-sync-vault-pull.service
sudo -u obsidian-github-sync \
  /opt/obsidian-github-sync/venv/bin/obsidian-github-project-watch \
  --config /etc/obsidian-github-sync/config.toml
```

Example output:

```json
{"change": true, "current_status": "planning", "event": "project-status-observation", "project": "10-Project/Terreate.md", "proposed_status": "running", "reason": "latest commit is within 7 days", "repository": "upiscium/Terreate"}
```

v0.1 does not mutate the Vault even when `change` is true.

## Next stage

After dry-run observations have been verified against real repositories, add a separate mutation adapter that turns a validated status proposal into the existing canonical Vault mutation path. GitHub collection and status policy must remain independent of the mutation transport.
