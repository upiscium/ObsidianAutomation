# GitHub Project status canonical mutation v0

## Purpose

This path turns one validated `project-status-observation` emitted by `obsidian-github-project-watch` into a narrowly-scoped canonical Project `status` update.

It is deliberately separate from the AI Knowledge production lifecycle. No LLM decision, Human review artifact, or `create_note` mutation is reused for this deterministic integration.

## Authority boundary

The existing production authority split is retained:

```text
obsidian-github-sync LXC
  GitHub read-only
  Vault mirror read-only
  emits exact observation JSON
          |
          | future narrow ingress
          v
25-Execution exact request
          |
          | read-only to Sync
          v
AI Writer / obsidian-ai-sync
  shared canonical I/O lock
  GET canonical Project
  validate binding + expected status
  replace status line only
  PUT + If-Match strong ETag
  exact-byte GET verification
          |
          +--> 27-Transport exact result
          |
          v
Nextcloud Live Vault

future Executor/reconciler
  verifies request/result binding
  -> final audit receipt in 30-Receipts
```

Only `obsidian-ai-sync` holds the Nextcloud writer credential. The watcher LXC never receives it.

Sync does **not** write a final receipt. It writes only a transport result in the existing Sync-owned `27-Transport` authority. Final receipt creation remains a separate later-stage responsibility, matching the existing production authority model.

## Input contract

The input is the watcher JSON object itself. A mutation is admissible only when:

- `event == project-status-observation`;
- `change == true`;
- `pending == true`;
- `project` is below `10-Project/` and ends in `.md`;
- `repository` is `owner/name`;
- `current_status` is canonical and is not `stopped`;
- `proposed_status` is only `planning` or `running`;
- current and proposed statuses differ;
- timestamps, Git SHA and Issue/PR arrays satisfy the watcher contract.

`done` / `cancelled` may appear only as the expected status for a deterministic watcher reactivation. Automation can never set `done`, `cancelled`, or `stopped`.

The canonicalized watcher object is SHA-256 hashed. That digest binds the transport result to the exact request.

## Canonical preflight

The writer never uploads bytes copied from the watcher mirror. It performs a fresh GET against the canonical WebDAV Project and requires:

- `type: project`;
- `github_watch` still enabled;
- `github_repo` still equals the proposal repository;
- current canonical status still equals `current_status` from the proposal;
- current canonical status is not `stopped`;
- a strong ETag is available before mutation.

If the canonical status already equals the desired status, the operation is an idempotent `already_desired` no-op.

If any other status or binding changed after observation, the operation fails closed as a conflict.

## Mutation semantics

The implementation parses the current canonical bytes and changes only the top-level `status:` line. All current body text and all other frontmatter are preserved from the writer-side GET.

The PUT uses:

```text
If-Match: <strong ETag from canonical GET>
Content-Type: text/markdown; charset=utf-8
```

The writer then GETs the Project again and requires exact bytes equal to the desired bytes computed from the preflight GET. A desired status with different surrounding bytes is not accepted as successful exact-byte verification.

This prevents a stale watcher mirror from overwriting unrelated Human edits and prevents a race between canonical GET and PUT.

The effect and Project-mirror refresh are serialized through the existing host-local `canonical-io.lock` by requiring `--state-root` and using `canonical_io_lock`.

## Transport result

`obsidian-github-project-status-apply` emits and durably stores a Sync-authored transport result containing:

- exact canonical proposal SHA-256;
- Project path and repository;
- expected / desired status;
- outcome (`applied`, `recovered`, or `already_desired`);
- before / after content SHA-256;
- completion timestamp.

The result is intended for `27-Transport`, which is writable by Sync in the existing authority matrix. It is **not** a final success receipt.

`--result` is required and uses exclusive creation. Reprocessing the same proposal may reuse an existing result only when its `proposal_sha256` matches.

If the remote effect succeeds but the process crashes before result persistence, a retry will observe the desired canonical status and can durably record `already_desired` without falsely claiming that the retry performed the original write.

## CLI

A production invocation on the AI Writer has the form:

```bash
obsidian-github-project-status-apply \
  --proposal /var/lib/obsidian-ai/state/25-Execution/<proposal-sha>.github-status.json \
  --state-root /var/lib/obsidian-ai/state \
  --base-url 'https://nextcloud.example/remote.php/dav/files/<writer>/ObsidianVault' \
  --username '<writer>' \
  --password-file /etc/obsidian-ai/webdav-password \
  --result /var/lib/obsidian-ai/state/27-Transport/<proposal-sha>.github-status.transport-result.json
```

The CLI returns:

- `0`: applied, recovered, or idempotently already desired, with durable transport result;
- `2`: malformed input / unavailable authority / ambiguous non-conflict error;
- `3`: stale proposal or canonical conflict.

## Manual canary sequence

Do not connect automatic cross-LXC ingress immediately.

1. Capture one real watcher observation from CT 30004.
2. Transfer that exact JSON manually to the Writer host without granting the watcher writer credentials.
3. Stage it under a location readable by Sync; for production-equivalent testing use a controlled file under `25-Execution`.
4. Run the writer-side CLI against a disposable/canary Project first.
5. Store the transport result under `27-Transport`.
6. Verify only `status:` changed and the transport result is durable.
7. Verify stale status, `stopped`, repository mismatch and ETag races fail closed.
8. Only then implement a narrow automatic proposal ingress plus a separate result reconciler/final receipt stage.
