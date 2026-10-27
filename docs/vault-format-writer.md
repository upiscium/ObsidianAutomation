# Guarded Live Vault Formatter Writer (Phase 1, #471)

Parent: [Epic #469](https://github.com/upiscium/ObsidianAutomation/issues/469).
Depends on [read-only Formatter #470](https://github.com/upiscium/ObsidianAutomation/issues/470).
Operations and management UI belong to [#472](https://github.com/upiscium/ObsidianAutomation/issues/472).

## Status and authority

This code is an internal opt-in API, not a CLI, installed systemd service,
automatic scheduler, Gitea publisher or live credential enrollment.

- No automatically enabled write path or credential lookup.
- Calling `apply_plan` with defaults is no-network, no-write.
- Even with `enabled=True`, writes are refused unless
  `cas_server_verified=True`, an eligible selection is explicit, and
  the caller supplies an HTTPS connection and private receipt directory.
- `cas_server_verified` represents a separate provider acceptance gate.
  This Boolean is NOT proof that real Nextcloud enforces `If-Match`.
  Deployment approval and actual real-provider CAS tests remain outstanding.
- Snapshot's read-only boundary, Core Promotion, Gitea snapshot,
  public projection allowlist and Daily jobs are unchanged.

## Transaction

1. #470 generates a deterministic plan for eligible `98-System/**` Markdown,
   bound to a version, original and desired hashes, and safe changed lines.
2. The Writer re-admits that plan and explicit selected `would_change` paths.
   It never trusts plan metadata as the bytes to upload.
3. Before any PUT, it GETs all selected files. HTTP 200, current SHA-256,
   size, actual safe reformat output, changed lines and strong ETag must
   agree with the immutable plan.
4. If any preflight fails, NO selected file receives a PUT.
   Files already at the desired hash require no PUT.
5. It persists an immutable `attempting` receipt before each conditional
   `PUT If-Match: <strong ETag>`. Rejected CAS or authentication stops
   further writes. Multiple files are not an atomic transaction.
6. Every PUT, including timeout or 5xx, requires a fresh GET whose
   body exactly matches desired SHA-256. Only then is it `applied` or
   `recovered`; otherwise the effect is `indeterminate`, never success.
   Receipt files are now created relative to a retained private directory
   descriptor. The writer checks ancestor permissions and the original
   directory identity before every remote mutation; if its pathname is
   replaced mid-preflight, the operation is blocked with no PUT.
7. Receipts are private and immutable, with operation ID, plan SHA,
   endpoint/account binding SHA, path, before/after SHA, status and timestamp.
   Raw content, URLs, raw ETags, passwords and tokens are not logged.
   A synced operation marker blocks replay of the same operation ID;
   retry with a new identity requires a fresh observation and no blind PUT.

`vault_modified` is true only for verified applied/recovered bytes, false
when no mutation occurred, and null for indeterminate remote outcomes.

## Production activation gates — NOT CLEARED

- Establish a bounded writer authority in the one-LXC service design.
  Never expand Snapshot or Gitea Runner to a Vault write credential.
- Real Nextcloud conditional PUT must demonstrably reject stale ETags
  *without modifying canonical content*. This is important because an
  earlier rclone conditional upload path deleted an existing target.
- Only `98-System/**` eligible Markdown paths can be selected; nontext,
  binary, symlink, hidden and unknown formats remain untouched.
- On a separately approved disposable/production canary, preview exact
  diff, run one conditional PUT, inspect GET readback and private receipt.
- Verify idempotence, crash/timeout ambiguity, collision/race behavior,
  rollback, periodic scheduling and scope before independently authorizing
  Writer activation (#472).
- Neither merging source nor passing CI grants any production access.

## Synthetic-only tests

```sh
python3 -m pytest -q tests/test_vault_format_writer.py tests/test_vault_formatter.py
```

The local test transport never contacts Nextcloud. It covers dry-run
defaults, attestation gates, tampered plans, bad paths, unreadable state,
weak ETags, GET-before-all-PUT, CAS conflicts, success readback, ambiguous
network effects, partial results, replay and secret-free receipts.
