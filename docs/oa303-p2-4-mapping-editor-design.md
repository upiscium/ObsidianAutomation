# #584 P2-4 — role mapping editor (design proposal)

**Status:** implemented (`vault_bindings_editor.py`, `vault_bindings.js`).
The owner chose D1-A, D2-A and D3-A (2026-11-14). Builds on P2-0 to P2-3
(#588, #590, #591, #592). This is the first slice that can **save** a mapping
record, so it is the first one that can change production behaviour.

**Implementation notes** (where the code differs from, or adds to, the
proposal below):

- **Reset:** `POST /api/v1/vault-bindings/resets {confirm: true}` goes back
  to today's names, with no record and no provider I/O.
  - It is the way out of `stale_root`, `unreadable` and an unfinished
    (`pending`) change.
  - A reset can be rolled back, unless the root has changed since
    (`root_changed`).
- **Re-pin:** `accept_recreated` is sent with the Preview rather than with
  the save. Every role it lists must report `identity_changed` without it,
  otherwise `invalid_accept`. Other recreated roles still block the Preview.
- **Rollback when the previous state was the defaults:** this is always
  allowed, because today's names need no root.
- **D2-A:** `activate_vault_root` and `rollback_vault_root` refuse with
  `mapping_present` while a record or a pending change exists.
- **Settled root only:** Preview and save refuse with `root_unsettled` while
  the V1 root pin is `pending_*`, so an unfinished V1 switch can still be
  completed.
- **Interrupted change:** a reset finishes or drops it and keeps the
  rollback point from before that change, never the unfinished record.
- **Rate limit:** save, reset and rollback share the Preview rate limit.
- **Other open handles:** a successful save drops them.
- **Known limits:**
  - **Rollback and reset do not re-read the server.** If a restored folder
    was recreated meanwhile, the Jobs refuse with `mismatch`, but the card
    shows `ok`, because the status compares root pins only. A new save with
    a re-pin fixes it.
  - **Every default folder must exist.** Under D1-A, `ai`, `idea` and
    `knowledge` keep today's names but are still verified and pinned (P2-1),
    so all six folders must exist to save. The owner chose to keep this (O2-A,
    2026-11-14); check that the six folders exist, read-only, before deploy.
- **Connection forms (O1-A, 2026-11-14):** while a record or a pending change
  exists, both connection saves refuse with `409 mapping_present` if they
  would change `webdav_url`, `nextcloud_user` or `vault_folder`. These are
  the server, account and folder the mapping is bound to. A password-only
  change is still allowed. The form says to reset the mapping first.

## Goal

From the WebUI, the owner can:
- see the current role → folder mapping;
- edit it, preview it, and save it with an explicit confirmation;
- re-pin a role folder that was recreated on purpose;
- undo the last save in one step.

The record format, validation and Preview are P2-1. The Jobs that read the
record are P2-2 and P2-3.

## Scope of editable roles (owner decision D1)

Only Jobs on obsidian-control follow the record. The AI-host pipeline still
uses today's names (P2-2 design note). If the owner remapped `knowledge`,
Formatter/B3/B4 would be unaffected, but the AI host would keep reading
`11-Knowledge`. That split is silent and confusing.

| Option | Editable roles | Result |
| --- | --- | --- |
| **A (recommended)** | `daily`, `project`, `system` | Every editable role is honoured by every Job that uses it. `ai`, `idea` and `knowledge` stay at today's names, shown read-only with the note "AI ホスト側が未対応". |
| B | all six | The AI host ignores three of them until a later slice. The UI must warn about this on every save. |

## Save flow

It follows the V1 root switch: the Job fence, a pending marker and one-step
rollback.

1. **Preview** (`POST /api/v1/vault-bindings/previews`, P2-1 logic).
   - **Response:** per role, the folder label, a reason code and a `changed`
     flag, plus an opaque **handle** valid for 300 s.
   - **Server side only:** the handle stores the exact record the Preview
     built, including the `oc:id`s it observed.
2. **Save** (`POST /api/v1/vault-bindings/saves`).
   - **Body:** `{handle, confirm: true, accept_recreated: [roles]}`.
   - **Refused unless:** the handle is live, the Preview was `valid`, and
     `confirm` is true.
   - **Inside `DashboardJobFence.quiesced()`** (the same lock order as V1):
     1. Refuse `jobs_in_flight` if any run is queued or running.
     2. Re-run the Preview walk. The fresh record must equal the handle's
        record, so a folder that changed since the Preview refuses as
        `stale_preview`.
     3. Write `vault_bindings.pending` (0600). While it exists, every
        `RoleGuard` returns `mismatch`, so the Jobs refuse.
     4. Copy the current record to `vault_bindings.previous.json`. With no
        record, store a "defaults" marker.
     5. Atomically replace `vault_bindings.json`.
     6. Run `fence.after_change()`, as V1 does: pause Daily, invalidate the
        Formatter, and mark the Snapshot as saved.
     7. Remove the pending marker.
   - **Crash safety:** a crash at any step leaves the pending marker, so the
     Jobs refuse (fail-closed). The UI then shows "保存が完了していません"
     with a retry or rollback.
3. **After a save** the Jobs stay paused until the owner re-enables them, as
   after a V1 root switch (`requires_job_reenable`). The generations already
   change with the record (P2-2), so old plans and receipts stop matching.

**Why the fence is enough** (the P2-2 review item): `quiesced()` holds every
Job's lock and refuses while any run is queued or running. A save can
therefore never land between a Job's guard check and its pull or publish.

## Re-pin a recreated folder (`identity_changed`)

- **Preview:** the role is shown as "同じ名前で作り直されたフォルダです", and
  the save stays blocked.
- **Accepting it:** the owner ticks "このフォルダを新しく固定する" for that
  role. The save then sends `accept_recreated: ["daily"]`, and only roles
  that had `identity_changed` in this Preview may be listed.
- **What is stored:** the new `oc:id`. This is recorded in the save receipt
  as the role names only, with no IDs.
- **Not covered:** any other refusal, such as a nested folder, another share
  or a duplicate, cannot be overridden.

## Rollback (`POST /api/v1/vault-bindings/rollbacks`, `{confirm: true}`)

- **What it restores:** one step back, to `vault_bindings.previous.json`, or
  to "no record" (today's names) when the previous state was the defaults.
  It does no provider I/O, like V1.
- **Same steps as a save:** it takes the fence, writes the pending marker,
  replaces the record, runs `after_change()` and removes the marker.
- **One step only:** a rollback cannot be rolled back. A new save is needed
  instead.
- **If a previous folder was recreated since:** the Jobs refuse with
  `mismatch` after the rollback, and the UI says so. A new save with a
  re-pin fixes it.

## Interaction with the V1 root switch (owner decision D2)

A record is bound to the root it was saved for (`root_pin`). After a V1 root
switch, or a free-text change in the connection form, every role-guarded Job
refuses with `mismatch`.

| Option | Behaviour |
| --- | --- |
| **A (recommended)** | A root switch is refused with `mapping_present` while a record exists. The owner first resets the mapping to today's names (a save of the defaults, or a rollback to "no record"), then switches the root, then maps again. Explicit, and no Job is ever left refusing by surprise. |
| B | A root switch is allowed. The Jobs refuse until the mapping is saved again for the new root. The UI shows "割り当てを再設定してください". |

The connection form (free text) cannot be refused the same way, because it
is the recovery path. Under either option, a record whose `root_pin` no
longer matches shows the "再設定" state.

## API and UI

- **Feature flag:** `--vault-bindings-ui`, OFF by default, like
  `--vault-root-ui`. Without it, every route returns 404 or 405.
- **Routes:**
  - `GET /api/v1/vault-bindings`, which returns the current mapping and:
    - `is_default`;
    - `state` (`ok`, `pending`, `stale_root` or `unreadable`);
    - `can_rollback`;
    - the editable roles (D1).
  - `POST .../previews`, `POST .../saves` and `POST .../rollbacks`.
- **Protections:** the same Origin, CSRF and rate limit as V1. The responses
  contain no IDs, URLs or ETags.
- **UI:** a new card "役割フォルダの割り当て" below "Vaultフォルダの選択".
  - One row per role: the current folder, an input box for editable roles,
    and the Preview status.
  - Buttons: 「確認する」 (Preview), then 「保存する」, which shows a confirm
    dialog listing the changed roles. 「1つ前に戻す」 is shown when a
    rollback is possible.
  - Free-text input (D3, recommended): validated by Preview. The V1 folder
    picker could be reused later.

## Tests and E2E

- **Unit and HTTP tests:**
  - the save, rollback and re-pin paths;
  - every crash point (pending marker left, Jobs refuse);
  - `stale_preview`, `jobs_in_flight` and handle expiry;
  - the root-switch interaction (D2);
  - the routes with the flag off.
- **P2-5 E2E** on the local Docker Nextcloud:
  - rename `98-System` to another name and map it, then check the
    Formatter plan;
  - map Daily, then check that B4 writes the mapped Daily note;
  - recreate a folder: the Jobs refuse, then a re-pin fixes it;
  - roll back;
  - confirm that history and receipts keep logical paths;
  - confirm that no write happens without review.

## Production

- **Deploy:** merging P2-4 changes nothing until it is deployed with
  `--vault-bindings-ui` and the owner saves a mapping. Deploy and enabling
  the flag each need approval.
- **Until a save, behaviour is today's.** Without a record the Jobs keep today's names (P2-2), so
  a deployed but unused editor changes nothing.
