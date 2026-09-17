# Managed appearance promotion

## Purpose

The generic Core Promotion Transport remains an exact-byte transport. Production recurring promotion adds one narrow semantic exception for:

```text
.obsidian/appearance.json
```

This exception exists because Obsidian and sync clients can reserialize the JSON or preserve device/private appearance state while the reviewed Core change is still semantically at the previous managed state. Treating those harmless differences as byte divergence blocks the entire promotion transaction, including unrelated create/update operations.

## Boundary

The generic CLI remains:

```text
obsidian-core-promotion-transport
```

and retains exact SHA-256 preflight/post-write verification for every path.

The recurring production CLI is:

```text
obsidian-core-promotion-run
```

and uses `managed_promotion_transport` for `.obsidian/appearance.json` updates only. Every other path delegates to the existing exact-byte transport logic.

## Managed appearance state

Production treats these scalar keys as Core-managed:

```text
theme
cssTheme
```

For `enabledCssSnippets`, the current Core-managed set is:

```text
obsidian-core
obsidian-core-mobile
callout-colors
expense-dashboard-lite
mobile-home-buttons
monthly-expanse
task-button
task-controls
task-status
work-time
```

The legacy names remain in the managed set so a reviewed migration can reliably remove them instead of accidentally preserving them as local/private snippets.

The following remote state is preserved:

- JSON keys outside the managed appearance contract;
- enabled snippets outside the managed snippet set;
- formatting, indentation, key order, and final-newline differences.

A future change that adds another Core-managed appearance key or snippet must update this contract and its tests in the same reviewed revision. Unknown Core-owned keys fail closed because Core `appearance.json` is required to contain exactly the currently supported managed keys.

## Preflight

For an `appearance.json` update, production reads Core base, Core head, and the current remote JSON object.

- exact remote base bytes -> `apply`;
- exact remote head bytes -> `already_applied`;
- managed semantic projection equals Core base -> `apply`;
- managed semantic projection equals Core head -> `already_applied`;
- any other managed projection -> `conflict`.

Malformed JSON, duplicate snippet names, invalid field types, or unsupported Core appearance shape fail closed.

Preflight still completes for all plan entries before any remote mutation. A real appearance conflict or a generic exact-byte conflict therefore prevents all mutations.

## Mutation and verification

When semantic application is required, production starts from the remote JSON object and changes only:

- `theme`;
- `cssTheme`;
- the managed subset of `enabledCssSnippets`.

Unmanaged keys/snippets are retained. The update uses the strong ETag observed during preflight with `If-Match`.

After PUT, production GETs the file again and verifies the managed semantic projection against Core head. Exact remote bytes are not required for this path because a server/client may serialize the same JSON differently.

A concurrent write that changes the ETag is rejected. Ambiguous network outcomes are recovered only when the desired managed projection is observable remotely.

Promotion receipts continue to record the Promotion Plan's `before_sha256`/`after_sha256`. For this one semantic path, the actual remote file may intentionally have a different full-file SHA because unrelated remote keys/snippets are preserved.

## Production upgrade

Do not edit or re-bootstrap the existing checkpoint when deploying this transport change.

Stop recurring promotion first:

```bash
systemctl stop obsidian-core-promotion.timer
systemctl stop obsidian-core-promotion.service || true
systemctl reset-failed obsidian-core-promotion.service
```

After the reviewed ObsidianAutomation PR is merged, install that immutable merge commit into the existing pinned checkout/venv. For an existing checkout:

```bash
AUTOMATION_SHA='<merged ObsidianAutomation commit>'

git -C /opt/obsidian-core-promotion/ObsidianAutomation fetch --no-tags origin
git -C /opt/obsidian-core-promotion/ObsidianAutomation checkout --detach "$AUTOMATION_SHA"

/opt/obsidian-core-promotion/venv/bin/pip install --no-deps --force-reinstall \
  /opt/obsidian-core-promotion/ObsidianAutomation
```

Do not replace `/etc/obsidian-core-promotion/public-export.toml` unless the reviewed revision actually changes the public-export policy bytes. The checkpoint binds that exact policy SHA and must continue to match it.

Run one manual cycle while the timer remains stopped:

```bash
systemctl start obsidian-core-promotion.service
journalctl -u obsidian-core-promotion.service -n 100 --no-pager
```

Verify the checkpoint advances to the intended ObsidianCore head and verify the promoted files in the Live Vault. Only then resume recurring execution:

```bash
systemctl enable --now obsidian-core-promotion.timer
```

## Incident acceptance for Issue #83

For the incident that motivated this change, the existing checkpoint remains:

```text
7e9618e6798e8c16d4dfe5b32ceaacd2da4bf4d9
```

and the intended ObsidianCore head is:

```text
5e244468fd22be6c2c285419986c0d3ac6e6d361
```

The Live appearance currently has the managed base state with `obsidian-core` enabled. A successful one-shot cycle must:

1. semantically update appearance so `obsidian-core` and `obsidian-core-mobile` are managed/enabled while preserving unrelated state;
2. create `.obsidian/snippets/obsidian-core-mobile.css`;
3. create `98-System/90-config/styles/obsidian-core-mobile.css`;
4. complete all remaining planned changes;
5. write a successful receipt;
6. advance the checkpoint to `5e244468fd22be6c2c285419986c0d3ac6e6d361`.

If the current remote managed appearance no longer matches either the recorded Core base or head projection, production must still stop with a conflict instead of guessing.