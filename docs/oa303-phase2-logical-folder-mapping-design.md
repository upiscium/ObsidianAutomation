# #303 Phase 2 — logical folder mapping (design draft)

**Status:** P2-0 and P2-1 implemented (#584); the rest is design. This is a separate gate after V1, which is
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
