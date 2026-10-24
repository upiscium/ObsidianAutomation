# Snapshot B2 P1/P2 source-only prototype

This is **not deployed**. No service, credential provisioning, account setup,
Nextcloud/Gitea connection test, or production readiness is included. The
existing dashboard stays read-only unless a `SnapshotJob` is explicitly
injected. Legacy systemd operator routes are separate and unchanged.

`obsidian-snapshot-app --config <private TOML>` explicitly starts the loopback
dashboard and embedded tick thread under one unprivileged OS identity. A
private TOML must contain exactly `state` (absolute writable application
state directory), `rclone_remote` (`name:VaultFolder`), `git_remote` (HTTPS
URL to a pre-initialized private `.git` repository, **without** embedded
credentials), `branch` (existing branch), `policy` (snapshot helper policy
file), and `rclone_config` (private read-only Nextcloud rclone config). The
configured service identity needs read access to the Vault share and scoped
noninteractive Git authentication; neither is provisioned by this source.
Binding remains loopback; exposing via a trusted authenticated private ingress
requires an independent deployment review. No complete Connections wizard is
implemented; the settings API accepts only the configured `nextcloud` and
`gitea` connection names, never endpoint or credential values.

Settings default Off / 300 seconds. A manual run is allowed while Off. A
single owner process is locked in the state directory. Each run retains its
isolated mirror, bare cache, and Git worktree in `state/runs/<run_id>` for
review; **there is no automatic cleanup**. Budget disk capacity and arrange
human-reviewed retention before deployment. The worktree contains private
Vault data and must be protected as such. A crash during queued/running is
reported `unconfirmed` and never automatically replayed. Push replies are
reconciled against the exact remote branch ref. HTTP receipts acknowledge
submission only; `saved` requires a matching remote SHA. First-run empty
repositories, empty mirrors, branch ambiguity and unknown push results fail
closed. UI messages contain only bounded stage codes, statuses and hashes;
raw command errors are intentionally omitted.

Offline tests use a disposable fake rclone source and file:// bare Git fixture.
Real credentials, live network verification, account/path attestation,
credential helper isolation, disk retention, and LXC installation belong to
separately approved P3 work.
