# GitHub Project Status Watcher v0.1

`obsidian-github-project-watch` observes GitHub repositories referenced by Obsidian Project notes and emits deterministic Project status proposals.

v0.1 is intentionally **read-only** with respect to the canonical Vault. It does not patch Project notes. The service reads a local read-only Vault mirror, reads GitHub API state, stores observation state in SQLite, and writes JSON observations to stdout/journald.

## Responsibility boundary

The watcher is an integration service, not an AI service.

```text
GitHub API (read-only)
        ↓
GitHub Collector
        ↓
Repository snapshot + SQLite observation state
        ↓
Deterministic Project status policy
        ↓
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

- latest Commit within the configured 7-day window -> `running`
- no Commit within the configured 7-day window -> `planning`

Open Issues and Pull Requests do not override the Commit rule during normal `planning` / `running` operation.

### stopped

`stopped` is human-controlled. GitHub activity never changes it.

### done / cancelled

A terminal status is preserved until activity **after the terminal baseline** is observed.

- new Commit -> `running`
- newly-open Issue or Pull Request -> `planning`
- no new activity -> preserve `done` / `cancelled`
- Commit wins when Commit and Issue/PR activity occur in the same observation

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

A GitHub token is optional for public repositories. For private repositories, set `GITHUB_TOKEN` in `/etc/obsidian-github-sync/credentials.env`. The token should have read-only access only to the repositories and metadata required by the watcher.

## LXC deployment

The intended production boundary is a dedicated unprivileged LXC named `obsidian-github-sync`.

Recommended layout:

```text
/opt/obsidian-github-sync/app/       repository checkout
/opt/obsidian-github-sync/venv/      Python virtualenv
/etc/obsidian-github-sync/config.toml
/etc/obsidian-github-sync/credentials.env
/var/lib/obsidian-github-sync/state.sqlite3
/srv/obsidian-github-sync/vault/      read-only local Vault mirror
```

Install the package:

```bash
python3 -m venv /opt/obsidian-github-sync/venv
/opt/obsidian-github-sync/venv/bin/pip install /opt/obsidian-github-sync/app
```

Install the example units:

```bash
install -m 0644 examples/github-sync/obsidian-github-sync.service /etc/systemd/system/
install -m 0644 examples/github-sync/obsidian-github-sync.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now obsidian-github-sync.timer
```

The timer runs every 15 minutes. The service is a `Type=oneshot` job and writes observations to journald.

## Manual dry-run verification

Run one observation manually before enabling the timer:

```bash
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
