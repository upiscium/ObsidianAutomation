# GitHub Project status canonical mutation v0

## Purpose

This path turns one validated `project-status-observation` emitted by `obsidian-github-project-watch` into a narrowly-scoped canonical Project `status` update.

It is deliberately separate from the AI Knowledge production lifecycle. No LLM decision, Human review artifact, or `create_note` mutation is reused for this deterministic integration.

## Authority boundary

```text
obsidian-github-sync LXC
  GitHub read-only
  Vault mirror read-only
  emits exact observation JSON
          |
          | future narrow ingress
          v
AI Writer host / obsidian-ai-sync
  GET canonical Project
  validate binding + expected status
  replace status line only
  PUT + If-Match strong ETag
  exact-byte GET verification
  durable receipt
          |
          v
Nextcloud Live Vault
```

The watcher LXC never receives the Nextcloud writer credential. The writer-side CLI is intended to run only under the existing Sync Transport authority that already owns that credential.

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

## Receipts

`obsidian-github-project-status-apply` emits a JSON receipt containing:

- exact canonical proposal SHA-256;
- Project path and repository;
- expected / desired status;
- result (`applied`, `recovered`, or `already_desired`);
- before / after content SHA-256;
- completion timestamp.

With `--receipt`, the receipt is created exclusively. Reprocessing the same proposal may reuse an existing receipt only when its `proposal_sha256` matches.

## CLI

```bash
obsidian-github-project-status-apply \
  --proposal /path/to/observation.json \
  --base-url 'https://nextcloud.example/remote.php/dav/files/<writer>/ObsidianVault' \
  --username '<writer>' \
  --password-file /etc/obsidian-ai/nextcloud-writer.password \
  --receipt /var/lib/obsidian-ai/state/30-Receipts/<proposal-sha>.github-status.json
```

The CLI returns:

- `0`: applied, recovered, or idempotently already desired;
- `2`: malformed input / unavailable authority / ambiguous non-conflict error;
- `3`: stale proposal or canonical conflict.

## Canary sequence

Do not connect automatic cross-LXC ingress immediately.

1. Capture one real watcher observation from CT 30004.
2. Transfer that exact JSON manually to the Writer host without granting the watcher writer credentials.
3. Run the writer-side CLI against a disposable/canary Project first.
4. Verify only `status:` changed and a receipt is durable.
5. Verify stale status, `stopped`, repository mismatch and ETag races fail closed.
6. Only then implement a narrow automatic proposal ingress.
