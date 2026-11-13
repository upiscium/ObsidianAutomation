# #303 Phase 2 — logical folder mapping (design draft)

**Status:** design only, no code. This is a separate gate after V1, which is
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
