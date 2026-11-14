# #303 Phase 2 — logical folder mapping (design draft)

**Status:** P2-0 to P2-3 implemented for obsidian-control (#584); the rest is design. This is a separate gate after V1, which is
live with root selection only. Suggest tracking it in its own issue.

## Problem

Inside the Vault root, each role lives at a fixed relative folder:

| Role | Current folder |
| --- | --- |
| daily | `00-DailyNote` |
| ai | `04-AI` |
| idea | `05-Idea` |
| project | `10-Project` |
| knowledge | `11-Knowledge` |
| system | `98-System` |

These names are hard-coded in 36 source files, 35 Python modules plus
`status_manager.js` (2026-11-13 inventory), and in configs, schemas and
examples:

- `10-Project`: 18 modules
- `11-Knowledge`: 13 modules
- `98-System`: 10 modules
- `00-DailyNote` and `05-Idea`: 4 modules each
- `04-AI`: 1 module

Users with a different Vault layout cannot use the automations without
renaming folders.

## Goal

A versioned `vault_bindings` record maps each role to one folder relative to
the active Vault root. Every producer and consumer derives its paths,
allowlists and filters from that one record.

## Non-goals

- Moving or renaming existing notes.
- Changing the Vault root (V1 does that).
- Per-job overrides.
- Multiple Vaults.

## Schema (proposal)

```json
{"version": 1,
 "root_pin": "<oc:id chain of the active Vault root, from V1>",
 "roles": {"daily": "00-DailyNote", "ai": "04-AI", "idea": "05-Idea",
           "project": "10-Project", "knowledge": "11-Knowledge", "system": "98-System"},
 "role_ids": {"daily": "<oc:id>", "...": "..."}}
```

**Defaults.** The defaults equal today's names. A missing record means
exactly today's behaviour.

**Validation**, reusing V1/A:
- Each path is relative and uses the new-candidate grammar.
- Paths must be unique and must not nest inside each other.
- No casefold or NFKC collisions.
- Every role is an existing plain directory (or one inside the same
  accepted share) under the pinned root, with its own `oc:id` recorded.

## P2-1 implementation (read-only)

`src/obsidian_automation/vault_bindings.py`. Nothing here saves a record
or changes a Job. Jobs keep the `vault_layout` defaults until P2-2/P2-3.

- **Record:** `vault_bindings.json` in the state directory, 0600, with exactly
  the keys `version`, `root_pin`, `roles` and `role_ids`. It is read with the
  V1 private-JSON reader (no symlink, FIFO or oversize file).
- **Missing record:** today's names.
- **Unreadable or invalid record:** an error. Callers fail closed and never
  fall back to the defaults.
- **Shape checks (no I/O):** all six roles, the V1 new-candidate grammar
  (ASCII, at most 16 components, no dot names), no duplicates or nesting
  after casefold/NFKC folding. Sibling prefixes such as `Daily` and
  `DailyIdeas` are allowed.
- **Preview:** for each role, the V1 walk of `<active root>/<folder>`. It
  needs a pinned active root (`root_unpinned` / `root_pin_mismatch`
  otherwise). Each role must resolve below that pin, and the refusals are:
  - **Another share below the root** (`unsafe_target`): a role must stay in
    the root's own storage, a plain directory or one inside the root's own
    share. The verifier gained `no_share_root_below` for this; its default
    keeps the V1 rules unchanged.
  - `duplicate_folder`: two names for one server folder.
  - `nested_folder`: roles nested on the server, even under different names.
  - `identity_changed`: a stored role folder recreated under the same name.
    Preview cannot accept the new folder. P2-4 must add an explicit,
    confirmed re-pin for a folder the user recreated on purpose.
  - `root_changed`: the root was recreated, or the stored mapping is for
    another root.
  - `path_too_long`: root plus folder beyond 100 characters or 16
    components. This is checked before any I/O.
- **Output:** folder labels, reason codes and a `changed` flag per role. It
  contains no IDs, URLs or ETags.
- **Record from a valid Preview:** returned to the server only. It grants
  nothing: the P2-4 save must re-check the pins and take the V1 Job fence.
  Jobs may run during a Preview because nothing changes.

## P2-2 implementation (consumers on obsidian-control)

- **Logical and physical paths:**
  - Inside a Job, role paths stay logical: today's names, in local mirrors,
    plans, watcher paths and receipts. Stored history therefore never changes
    with a mapping.
  - Only the Nextcloud pull maps a role to its physical folder from the
    record. There are two such pulls:
    - `vault_format_source.pull_system_markdown` (Formatter, `system`);
    - `github_daily_setup.pull_project_notes` (B3/B4, `project`).
  - The copier gets `folder=` only for a mapped folder, so the default call
    is unchanged.
- **Per-role pins:** `vault_bindings.RoleGuard` wraps the V1 root guard.
  - **Without a record:** the root guard's result, with no extra provider I/O.
  - **With a record:** the root must be pinned to the record's root, and each
    role folder the Job reads must still have its recorded `oc:id`.
    Otherwise the Job refuses.
  - **Roles used per Job** (`snapshot_app.job_guards`):
    - Formatter: `system`;
    - B3: `project`;
    - B4: `project` and `daily`.
  - **One snapshot per run:** the pull uses the mapping the guard verified
    for that run, not a second read of the record.
  - **Root pin:** a root pin that changes during the check gives
    `unavailable`.
- **Producers not yet mapped (until P2-3):** B4 refused while a record moved
  `daily` or `project`. P2-3 lifted this; see below.
- **Generations:** with a record, a digest of it is folded into the
  Formatter and B3/B4 source generations. Plans and phase receipts admitted
  under another mapping then stop matching. Without a record, the
  generations are unchanged.
- **Still open for P2-4:** the publish PUT runs after the guard in the same
  B4 run. A mapping save, and a root activation, must therefore hold the V1
  Job fence for the whole guarded run, pull and publish included.
- **Bad record:** the pull stops with the existing reason codes
  `formatter_source_pull_failed` and `project_mirror_pull_failed`. It never
  falls back to the defaults.
- **Not on this host (separate decision):** the AI pipeline modules
  (`knowledge_index`, `context_bundle`, `ai_input_planner`, `semantic_*`) run
  on the AI host. They read a local mirror whose folders are provisioned by
  `tools/provision_automation_authority.py`, and that host has no
  `vault_bindings.json`. Mapping there needs the record delivered to the AI
  host and the mirror re-provisioned. Their logical paths stay today's names.
- **Snapshot (B2):** mirrors the whole Vault root and uses no role folder.
  It keeps the root guard only. This is intended, not a missed consumer.
- **For P2-3:** every writer that turns a logical path into a remote URL must
  go through the record, and only after the guard pins that role. This
  covers:
  - the Daily writer (`DAILY_ROOT`);
  - the Project status mutation and worker;
  - the Formatter writer and effect journal;
  - B3 WebDAV readback;
  - the Project links that B4 renders.

  Formatter plans and receipts carry logical `98-System/...` paths. Any
  writer that consumes them must translate them first.

## P2-3 implementation (producers on obsidian-control)

- **The only Vault writer on this host is the B4 Daily publisher.** The
  Formatter has no Writer injected, and B3 is an observer.
- **Daily publisher:**
  - **Paths:** it keeps the logical Daily path (`00-DailyNote/...`) in the
    projection and the receipt. Only the WebDAV URL uses the Daily role's
    physical folder (`vault_layout.physical_path`), for the GET before, the
    PUT and the read-back GET.
  - **Pins right before the PUT:** the publisher runs inside the B4
    `RoleGuard` again. The root and role pins are re-checked just before
    the PUT, and the write goes to the folder that check verified.
  - **Bad record:** the publisher fails before any request.
  - **Refusal before the PUT:** a pin refusal at that point is recorded as
    `("publication", "vault_root_refused")`, which means nothing was sent.
    Any other publisher failure stays `outcome_unconfirmed`.
  - **Remaining gap:** a few requests (the GET before) remain between that
    check and the PUT. A folder recreated in that window could still be
    written. This is inherent to a check-then-write over WebDAV. The P2-4
    save takes the Job fence, so the record itself cannot change then.
- **B4 no longer refuses a mapping.** It reads Project notes and writes the
  Daily note through the record, and its Daily body links only to GitHub
  URLs, never to Project paths.
- **Writers not wired on this host:**
  - Project status mutation and overview;
  - Formatter writer and effect journal;
  - B3 readback;
  - promotion transports;
  - review intake and Human projection.

  They keep logical paths, and their allowlists still check logical paths.
  Before any of them is wired to a mapped Vault, its URL must go through
  `physical_path` under a `RoleGuard` for that role, or the guard must list
  the role as `unmapped`.

## Migration rules

1. **Immutable history.** Existing job receipts, projections, CAS entries
   and review decisions keep their stored relative paths and hashes.
   Nothing rewrites them.
2. **Quiesce first.** A mapping change uses the V1 Job fence: refuse while
   any run is queued or running. In-flight items finish under the old
   mapping.
3. **New work only.** New runs use the new mapping. Pending review items
   from before stay bound to their old path, and the UI says so.
4. **Pin per role.** The Job guard checks the root pin and each used
   role's `oc:id` before acting. A missing or recreated folder makes the
   Job refuse.
5. **No implicit writes.** Canonical PUT/DELETE derived from an unreviewed
   path change is refused. The Writer admits only paths under the current
   mapping, and only after review.
6. **Rollback.** One step back to the previous mapping, with the same
   pending-pin pattern as the V1 root switch.

## Work breakdown (each slice: synthetic tests, independent review, owner gates)

| Slice | Content |
| --- | --- |
| P2-0 | Path inventory per module (producer or consumer, read or write); turn the constants into one `vault_layout` accessor that still returns today's names. No behaviour change |
| P2-1 | Schema, validation and preview (read-only), default record == today |
| P2-2 | Consumers read the accessor: Snapshot filters, B3/B4 readers, Formatter scope, AI input planner |
| P2-3 | Producers and Writer allowlists from the accessor; Review CAS path checks |
| P2-4 | WebUI mapping editor (preview-first, confirm, fence, per-role pins, rollback) |
| P2-5 | Disposable E2E: rename a role folder and verify no history rewrite and no unreviewed writes |

## Open questions for the owner

- Should the AI-derived state (`04-AI`) leave the Vault entirely, as the
  #303 issue text suggests, instead of being mapped? Decided 2026-11-13:
  keep it in the mapping for now (#584).
- Is the role list complete? Yes for `src/`; see
  `docs/oa303-phase2-path-inventory.md` (P2-0).
