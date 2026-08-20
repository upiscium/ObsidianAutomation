# Snapshot observability

`obsidian-vault-snapshot` emits one stable metrics line for every invocation after argument parsing.

Example successful snapshot:

```text
Snapshot metrics: result=success started_at=2026-08-20T01:00:00Z finished_at=2026-08-20T01:00:13Z duration_seconds=13.214 changed=true dry_run=false commit_sha=<sha> manifest_sha256=<sha256> changes=1
```

No-op runs use `result=no-op`, dry runs use `result=dry-run`, and helper failures use `result=failure` on stderr. Failure metrics intentionally use `-` for fields that are unavailable because the snapshot did not complete.

The duration uses a monotonic clock, while the explicit timestamps are UTC wall-clock values for correlation with systemd journal entries.

## Scope

This measurement covers the deterministic `obsidian-vault-snapshot` helper only: source stability checks, hashing, staging, destination comparison, and local Git commit creation.

It intentionally does not measure deployment-specific work such as Nextcloud/rclone synchronization, Gitea fetch/push, disposable worktree creation, or systemd scheduling. Those operations belong to the private deployment wrapper and should be timed separately when end-to-end transaction latency is required.

Keeping these scopes separate allows Vault-size-dependent helper cost to be tracked without moving production endpoints, paths, or credentials into this public repository.
