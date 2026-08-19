# Live Vault to private Gitea snapshot

This document describes the boundary for turning a Live Obsidian Vault synchronized through Nextcloud/Remotely Save into versioned snapshots in a private Gitea `ObsidianVault` repository.

## Authority model

- The **Live Vault** on Nextcloud is the user-editable sync authority.
- Snapshot automation must only obtain a read-only view of the Live Vault.
- The private Gitea `ObsidianVault` repository is the version/audit/automation authority.
- `ObsidianAutomation` contains reusable deterministic code only.
- Internal paths, Nextcloud/Gitea endpoints, credentials, systemd units, and production pinning belong in private deployment configuration.

`obsidian-vault-snapshot` itself performs no network access. The production container first pulls a local mirror from Nextcloud, then passes that local mirror to the deterministic snapshot helper.

## Recommended production topology

Keep the whole snapshot path inside one dedicated unprivileged Linux container. The Proxmox host does not need to mount or authenticate to Nextcloud.

```text
Nextcloud Live Vault
        │ WebDAV / read-only service account
        ▼
Snapshot LXC
├─ rclone
│   └─ /var/lib/obsidian-snapshot/live-mirror
├─ pinned ObsidianAutomation
├─ obsidian-vault-snapshot
├─ /var/lib/obsidian-snapshot/repo.git
├─ disposable Git worktree
└─ push credential scoped to private Gitea ObsidianVault
        │
        ▼
private Gitea ObsidianVault
        │ push event
        ▼
existing Public Projection workflow
```

Do not expose the Live Vault to the general Gitea Actions runner merely for this snapshot path.

## Nextcloud read boundary

Prefer a dedicated Nextcloud service account for the snapshot container. Share only the Vault folder with that account and disable edit rights on the internal share. Generate an app password for WebDAV access rather than storing the user's primary password.

Configure rclone with a WebDAV remote using the Nextcloud vendor. Keep the rclone configuration readable only by the snapshot service account.

Conceptually:

```text
remote name: nextcloud-vault
backend: WebDAV
vendor: Nextcloud
URL: https://nextcloud.example/remote.php/dav/files/SNAPSHOT_USER/
user: SNAPSHOT_USER
password: dedicated app password
```

The remote path used by the service should point only at the shared Vault folder.

## Remote fetch transaction

The remote fetch stage writes only to a local mirror. A conservative v0 transaction is:

1. `rclone sync` the read-only Nextcloud Vault into the local mirror.
2. Wait a short settle interval.
3. Run the same `rclone sync` again so changes that happened during the first traversal converge locally.
4. Run `rclone check` between the remote Vault and the local mirror.
5. Only after the check succeeds, run `obsidian-vault-snapshot` against the local mirror.

Example:

```bash
set -euo pipefail

remote="nextcloud-vault:ObsidianVault"
live="/var/lib/obsidian-snapshot/live-mirror"

mkdir -p "$live"
rclone sync "$remote" "$live"
sleep 10
rclone sync "$remote" "$live"
rclone check "$remote" "$live"
```

If the remote changes during this process and the check does not converge, the run fails and the private Git repository is not modified. A later timer run retries from the current Nextcloud state.

The local mirror is not itself a Git worktree and is never used as a write-back source for Nextcloud.

## Stable snapshot transaction

After the remote fetch stage, `obsidian-vault-snapshot` performs the following local transaction:

1. Require a clean destination Git repository root.
2. Hash all managed local-mirror files into a deterministic manifest.
3. Wait `settle_seconds` and hash again.
4. Retry until two consecutive manifests match or `stability_attempts` is exhausted.
5. Copy the stable source into a temporary staging tree.
6. Hash the staging tree and the source again.
7. Continue only if both still match the previously stable manifest.
8. Compute ADD / UPDATE / DELETE operations against the private Git worktree.
9. Apply the mirror only to managed paths.
10. Stage with `git add -A` and run `git diff --cached --check`.
11. If Git has no staged change, create no commit.
12. Otherwise create one local commit containing a `Source-Manifest-SHA256` trailer.

The helper never writes to the local mirror, performs network access, or pushes Git commits.

## Snapshot policy

See `configs/vault-snapshot.example.toml`.

`exclude` defines Live-only/transient content that should not exist in the managed private snapshot. A path excluded from the source is absent from staging, so an existing managed copy in the destination is deleted.

`repository_owned` defines private-repository/deployment content that must not be overwritten or deleted by the Live Vault mirror. Typical examples are:

- `.gitea/**`
- `.gitignore`

Git metadata is always repository-owned.

The v0 example policy mirrors the current private-Vault ignore boundary for Obsidian workspace state, downloaded theme assets, distributed plugin assets, Various Complements history, and Obsidian trash.

## Recommended container layout

```text
/var/lib/obsidian-snapshot/live-mirror   local Nextcloud pull target
/var/lib/obsidian-snapshot/repo.git      bare private Gitea cache
/var/lib/obsidian-snapshot/run-*         disposable Git worktree
/etc/obsidian-snapshot/policy.toml       private snapshot policy
/etc/obsidian-snapshot/rclone.conf       Nextcloud WebDAV credential
```

The dedicated snapshot service account should have:

- read/write access to its local state directory;
- read access to the Nextcloud Vault through the dedicated WebDAV account;
- a credential scoped to pushing the private Gitea `ObsidianVault` repository;
- no Nextcloud write permission to the Live Vault.

## Recommended publisher wrapper

Keep a bare cache and create a disposable worktree for each run. Conceptually:

```bash
set -euo pipefail

state=/var/lib/obsidian-snapshot
live="$state/live-mirror"
bare="$state/repo.git"
work="$(mktemp -d "$state/run-XXXXXX")"
remote="nextcloud-vault:ObsidianVault"

cleanup() {
  git --git-dir="$bare" worktree remove --force "$work" 2>/dev/null || true
  rm -rf "$work"
}
trap cleanup EXIT

# Serialize the complete fetch + snapshot + push transaction with flock.
rclone sync "$remote" "$live"
sleep 10
rclone sync "$remote" "$live"
rclone check "$remote" "$live"

git --git-dir="$bare" fetch origin +refs/heads/main:refs/heads/main
git --git-dir="$bare" worktree add --detach "$work" refs/heads/main
before="$(git -C "$work" rev-parse HEAD)"

obsidian-vault-snapshot \
  --source "$live" \
  --destination "$work" \
  --config /etc/obsidian-snapshot/policy.toml

after="$(git -C "$work" rev-parse HEAD)"
if [ "$before" != "$after" ]; then
  git -C "$work" push origin HEAD:main
fi
```

Initialize the bare cache once with the private Gitea repository and preserve its `origin` remote. Production should pin `ObsidianAutomation` to an immutable tag or commit rather than a floating `main`.

A push race with another writer should be allowed to fail as a non-fast-forward update. The next scheduled run can fetch the new canonical `main`, rebuild a fresh worktree, and snapshot again.

## Scheduling

A five-minute systemd timer is a reasonable initial cadence. The snapshot helper also has its own short content-settling window, so the timer does not need filesystem-event debouncing.

Recommended properties:

- `Type=oneshot`
- execution as a dedicated unprivileged service account
- `flock` or an equivalent lock around the complete fetch/snapshot/push transaction
- `OnUnitActiveSec=5min`
- `Persistent=true`
- network-online ordering for both Nextcloud pull and Gitea push

The systemd unit and all internal paths/endpoints should live in the private deployment repository rather than this public repository.

## Failure behavior

- If Nextcloud cannot be read or remote/local verification fails, the private Git worktree is never created for that run.
- If the local mirror keeps changing, the helper fails before modifying the Git destination.
- If the local mirror changes during the staging copy, the helper fails before modifying the Git destination.
- If the snapshot has no Git-visible change, no commit is created.
- If push fails, the private Gitea branch is unchanged.
- If the wrapper crashes after destination mutation, the disposable worktree is discarded.
- A successful push to private Gitea can then trigger the existing Public Projection workflow independently.
