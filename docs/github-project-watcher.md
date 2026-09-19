# GitHub Project Status Watcher v0.1

`obsidian-github-project-watch` observes GitHub repositories referenced by Obsidian Project notes and emits deterministic Project status proposals.

v0.1 is intentionally **read-only** with respect to the canonical Vault. It does not patch Project notes. The service reads a local pull-only Vault mirror, reads GitHub API state, stores observation state in SQLite, and writes JSON observations to stdout/journald.

## Responsibility boundary

The watcher is an integration service, not an AI service.

```text
Nextcloud Vault
      |
      | read-only Nextcloud identity
      v
obsidian-github-mirror
      |
      | local 10-Project mirror
      v
obsidian-github-sync <------ GitHub API (read-only)
      |
      v
Repository Snapshot + SQLite
      |
      v
Deterministic Status Policy
      |
      v
JSON Status Proposal
```

The mirror and watcher use separate Unix identities, primary groups, and credentials:

- `obsidian-github-mirror`: can read Nextcloud and update only the local Vault mirror.
- `obsidian-github-sync`: can read the local mirror and GitHub, and can write only its local SQLite state.
- `obsidian-github-vault`: shared supplementary group used only to read the local mirror.

The watcher identity cannot read the Nextcloud rclone credential. The mirror identity cannot read the GitHub token.

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

- latest default-branch commit within the configured 7-day window -> `running`
- no recent default-branch commit within the configured 7-day window -> `planning`

Feature-branch-only pushes do not change Project status. The watcher deliberately uses the
current default-branch head so one commit-list request replaces multiple repository
activity-type requests.

Open Issues and Pull Requests do not override the Commit rule during normal `planning` / `running` operation.

For a single-page repository snapshot, GitHub reads are serialized and bounded to three
REST requests:

```text
GET /repos/<owner>/<repo>/commits?per_page=1
GET /repos/<owner>/<repo>/issues?state=open&per_page=100&page=1
GET /repos/<owner>/<repo>/pulls?state=open&per_page=100&page=1
```

Issue/PR pagination can add requests only when a repository has more than 100 matching
open items. Requests remain serial to avoid increasing secondary-rate-limit pressure.

### stopped

`stopped` is human-controlled. GitHub activity never changes it.

### done / cancelled

A terminal status is preserved until activity **after the terminal baseline** is observed.
A default-branch HEAD SHA change counts as Commit activity even if a force-push moves the
branch to a commit with an older timestamp.

- changed default-branch HEAD SHA -> `running`
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
/opt/obsidian-github-sync/app/       repository checkout, root-owned
/opt/obsidian-github-sync/venv/      Python virtualenv, root-owned

/etc/obsidian-github-sync/config.toml
/etc/obsidian-github-sync/credentials.env      # GitHub only

/etc/obsidian-github-mirror/rclone.conf        # Nextcloud only
/etc/obsidian-github-mirror/vault-pull.filters

/var/lib/obsidian-github-sync/state.sqlite3
/var/lib/obsidian-github-mirror/state/24-Locks/

/srv/obsidian-github-sync/vault/                # local pull-only Vault mirror
```

### Service identities

Create one shared mirror-read group and two isolated service identities. Each identity keeps its own primary group; the shared group carries no credentials.

```bash
groupadd --system obsidian-github-vault

useradd --system --user-group \
  --home-dir /var/lib/obsidian-github-mirror \
  --create-home \
  --shell /usr/sbin/nologin \
  obsidian-github-mirror

# Skip this useradd when the watcher user already exists.
useradd --system --user-group \
  --home-dir /var/lib/obsidian-github-sync \
  --create-home \
  --shell /usr/sbin/nologin \
  obsidian-github-sync

usermod -aG obsidian-github-vault obsidian-github-mirror
usermod -aG obsidian-github-vault obsidian-github-sync
```

The local mirror is owner-writable and shared-group-readable. The setgid bit keeps files under the mirror in `obsidian-github-vault`; the mirror service uses `UMask=0027` so they are not world-readable.

```bash
install -d -o obsidian-github-mirror -g obsidian-github-vault -m 2750 \
  /srv/obsidian-github-sync/vault
install -d -o obsidian-github-mirror -g obsidian-github-mirror -m 0750 \
  /var/lib/obsidian-github-mirror/state/24-Locks
install -d -o obsidian-github-sync -g obsidian-github-sync -m 0750 \
  /var/lib/obsidian-github-sync
install -d -o root -g obsidian-github-sync -m 0750 \
  /etc/obsidian-github-sync
install -d -o root -g obsidian-github-mirror -m 0750 \
  /etc/obsidian-github-mirror
```

Install runtime packages and the Python package:

```bash
apt install -y rclone
python3 -m venv /opt/obsidian-github-sync/venv
/opt/obsidian-github-sync/venv/bin/pip install /opt/obsidian-github-sync/app
chown -R root:root /opt/obsidian-github-sync
chmod -R go-w /opt/obsidian-github-sync
```

### Pull-only Vault mirror

The watcher does not need the whole Vault. `examples/github-sync/vault-pull.filters` mirrors only `10-Project/**`.

The pull process reuses `obsidian-production-vault-pull`, which always runs `rclone sync` in the remote-to-local direction. The watcher unit additionally exposes the resulting local mirror read-only through systemd sandboxing.

Use a dedicated Nextcloud identity with read-only access to only the canonical `10-Project` share. Do not reuse a canonical writer credential and do not grant the mirror account the whole Vault when a direct Project-only share is available.

Copy the filter into the mirror-only configuration directory:

```bash
install -o root -g obsidian-github-mirror -m 0640 \
  examples/github-sync/vault-pull.filters \
  /etc/obsidian-github-mirror/vault-pull.filters
```

Configure an rclone WebDAV remote named `nextcloud-github-sync` in `/etc/obsidian-github-mirror/rclone.conf` and set the file to `root:obsidian-github-mirror` mode `0640`. The watcher identity is not a member of that group and therefore cannot read this credential.

When `10-Project` is shared directly to the mirror account, it appears at the rclone remote root:

```text
nextcloud-github-sync:
└── 10-Project/
```

The example pull service therefore uses the remote root:

```text
nextcloud-github-sync:
```

and `vault-pull.filters` keeps only `10-Project/**`. This preserves the local layout expected by the watcher:

```text
/srv/obsidian-github-sync/vault/10-Project/...
```

Test the remote shape before enabling the pipeline:

```bash
sudo -u obsidian-github-mirror \
  rclone lsd nextcloud-github-sync: \
  --config /etc/obsidian-github-mirror/rclone.conf
```

Then test the mirror independently:

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

The watcher service has `Requires=` and `After=` dependencies on `obsidian-github-sync-vault-pull.service`. Every 5-minute watcher cycle therefore refreshes the Project mirror first; a failed mirror refresh prevents that watcher run from using stale/incomplete input.

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
