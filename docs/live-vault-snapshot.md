# Live Vault to private Gitea snapshot

This document describes the boundary for turning a locally synchronized Live Obsidian Vault into versioned snapshots in a private Gitea `ObsidianVault` repository.

## Authority model

- The **Live Vault** is the user-editable copy synchronized through Nextcloud/Remotely Save.
- Snapshot automation treats the Live Vault as **read-only**.
- The private Gitea `ObsidianVault` repository is the version/audit/automation authority.
- `ObsidianAutomation` contains reusable deterministic code only.
- Internal paths, Gitea endpoints, credentials, systemd units, and production pinning belong in private deployment configuration.

The Live Vault must not itself be the Git working tree. `obsidian-vault-snapshot` rejects identical or nested source/destination roots.

## Stable snapshot transaction

`obsidian-vault-snapshot` performs the following transaction:

1. Require a clean destination Git repository root.
2. Hash all managed Live Vault files into a deterministic manifest.
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

The helper never writes to the Live Vault, performs network access, or pushes Git commits.

## Snapshot policy

See `configs/vault-snapshot.example.toml`.

`exclude` defines Live-only/transient content that should not exist in the managed private snapshot. A path excluded from the source is absent from staging, so an existing managed copy in the destination is deleted.

`repository_owned` defines private-repository/deployment content that must not be overwritten or deleted by the Live Vault mirror. Typical examples are:

- `.gitea/**`
- `.gitignore`

Git metadata is always repository-owned.

The v0 example policy mirrors the current private-Vault ignore boundary for Obsidian workspace state, downloaded theme assets, distributed plugin assets, Various Complements history, and Obsidian trash.

## Recommended production layout

Use a dedicated snapshot host or container that can read the locally synchronized Vault. Do not mount or expose the Live Vault to the Gitea Actions runner merely for this snapshot path.

A typical layout is:

```text
/srv/obsidian-live/                  read-only source for snapshot service
/var/lib/obsidian-snapshot/repo.git  bare private Gitea cache
/var/lib/obsidian-snapshot/run-*     disposable Git worktree
/etc/obsidian-snapshot/policy.toml   private snapshot policy
```

The snapshot service account should have:

- read access to the Live Vault;
- write access to its own state directory;
- a credential scoped to pushing the private `ObsidianVault` repository;
- no write access to the Live Vault.

## Recommended publisher wrapper

Keep a bare cache and create a disposable worktree for each run. Conceptually:

```bash
set -euo pipefail

state=/var/lib/obsidian-snapshot
live=/srv/obsidian-live
bare="$state/repo.git"
work="$(mktemp -d "$state/run-XXXXXX")"

cleanup() {
  git --git-dir="$bare" worktree remove --force "$work" 2>/dev/null || true
  rm -rf "$work"
}
trap cleanup EXIT

# Serialize runs with flock in the production wrapper.
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
- `flock` or an equivalent lock so runs never overlap
- `OnUnitActiveSec=5min`
- `Persistent=true`
- network-online ordering for the Gitea push step

The systemd unit and all internal paths/endpoints should live in the private deployment repository rather than this public repository.

## Failure behavior

- If the Live Vault keeps changing, the helper fails before modifying the destination.
- If the source changes during the staging copy, the helper fails before modifying the destination.
- If the snapshot has no Git-visible change, no commit is created.
- If push fails, the private Gitea branch is unchanged.
- If the wrapper crashes after destination mutation, the disposable worktree is discarded.
- A successful push to private Gitea can then trigger the existing Public Projection workflow independently.
