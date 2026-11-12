# ADR #303 P3 — Nextcloud coherence feasibility and the restricted V1 contract

- Status: **Accepted by owner on 2026-11-12 (policy only).** Implemented as
  source only. Not activated, not deployed and not published.
- Base: `main@1248ee85156f`. The policy decision itself used no provider request.
  F1/P2 were not re-run. The P3 runs on the disposable instance are recorded below.

## Context

V1/D (switching the Vault root) needs C. C means that from the first read
until the root-binding CAS completes, these all stay consistent:

- the connection revision;
- the parent/child resource identities;
- every sibling collision verdict;
- every alias/mount classification.

Edits from other Nextcloud clients count too.

## Evidence

The fixed digests below match the owner-private files (checked read-only on
2026-11-12). `tests/test_303_p2_evidence_pins.py` pins them and skips with
`NOT_REVERIFIED` when the files are absent.

| Item | SHA-256 |
| --- | --- |
| F1 manifest | `61d9d0ae…b1be` |
| F1 receipt | `7b5316f0…bf4c` |
| P2 receipt | `6e839f3e…a574` |
| P2 PHP client | `de78bf31…0737` |
| P2 host guard | `f7420299…d07d` |

**F1:** 5 MKCOL and 4 create-only PUT on the disposable Nextcloud 35.0.2, all
HTTP 201, 230 bytes in total. This does not prove that the server rejects a
conflicting conditional PUT.

**P2:** 7 PROPFIND requests, all HTTP 207. They returned 9 distinct resources
in 14 occurrences. Of 98 property occurrences, 89 were returned and 9 were
missing/404. `oc:fileid` and the ETag each appeared 14 times. The receipt keeps
counts and hashes only, not property values.

## Mechanism classification (Nextcloud 35, public docs; not verified on our instance)

| Candidate mechanism for C | Class | Reason |
| --- | --- | --- |
| Server snapshot / epoch spanning a tree | **NOT_SUPPORTED** | No documented DAV snapshot or epoch |
| Exclusive tree lock held through the CAS | **NOT_SUPPORTED** | WebDAV `LOCK` is per resource, is itself a write, and does not stop other clients' edits in sibling directories |
| Conditional update (`If-Match`) on the root | **NOT_SUPPORTED** for C | It guards one resource's body, not a directory tree read |
| Propagated directory ETag | **FEASIBLE_WITH_MORE_EVIDENCE** as a *change signal only* | Misses A→B→A changes and edits made after the last read; it can never prove C |
| `oc:id` = padded `oc:fileid` + instance ID | **FEASIBLE_WITH_MORE_EVIDENCE** (for I) | P3-D1: stable across move, rename and restart on one instance |
| `nc:mount-type` / `nc:is-mount-root` | **FEASIBLE_WITH_MORE_EVIDENCE** (for X) | P3-D2: user shares only; group folders, external storage and backend symlinks not covered |
| Depth:1 completeness | **NOT_SUPPORTED** for storage; **FEASIBLE_WITH_MORE_EVIDENCE** for Nextcloud's index (L) | P3-D3: unscanned on-disk children are silently absent |

## Gate register (nothing VERIFIED; mirrored in `CURRENT_PROVIDER_PROOF`)

| Gate | State |
| --- | --- |
| A | UNKNOWN |
| I | **OBSERVED_WITH_LIMITS** (P3-D1) |
| X | **OBSERVED_WITH_LIMITS** for user shares only (P3-D2); group folders, external storage and symlinks UNKNOWN |
| L | **OBSERVED_WITH_LIMITS** for Nextcloud's index (P3-D3); **FAILED** for storage completeness |
| C | UNKNOWN; expected NOT_SUPPORTED |
| R | **OBSERVED_WITH_LIMITS** (P3-D5; plus synthetic tests for our own revision) |

## Decision

The options were:

- **(a)** Ship only the read-only preview and keep V1/D blocked.
- **(b)** A restricted V1 that does not claim atomicity.

**The owner chose (b) on 2026-11-12.** Its contract:

1. **Identity pin.** Activation records the `oc:id` chain to the selected root.
2. **Revalidate at activation.** All Jobs are quiesced in the Dashboard lock
   order. The root is re-walked. The previewed leaf `oc:id` must still match.
   The local revision CAS changes `vault_folder` only.
3. **Revalidate before each Job run.** `check_root_pin` must return `ok` or
   `legacy_unpinned`.
   - **Wired** in `snapshot_app` through `JobRootGuard`, before every Vault
     read:
     - the Snapshot runner;
     - the B4 runner (which also gates publication, since publication
       follows the Preview in the same run);
     - the Formatter source;
     - both B3 collectors.
   - Each refusal is recorded through the Job's existing failure path:
     - Snapshot: `failed/sync`;
     - B4: `source_admission/source_unavailable`;
     - Formatter: `source_or_plan_failed`;
     - B3: `unknown/collector_unavailable`.
   - When nothing was ever activated (today), the guard allows the run with
     no provider I/O, whatever the Job's own configuration.
   - Once a root is pinned, the guard sends one Depth:0 `PROPFIND` of the
     root per run and compares its `oc:id`. If the register does not admit
     that read, the Job refuses.
   - The pin is written as `pending_activated` or `pending_rolled_back`
     before the config CAS. Only `activated`/`rolled_back` pins are trusted,
     so every crash point makes the Jobs refuse rather than run unpinned.
   - Rollback is one step. It is allowed only after `activated` or
     `pending_activated`. After `pending_rolled_back` it answers
     `rollback_uncertain`, because a retry could swap the roots back.
     Recovery is a new activation. If that rollback landed, the preview
     answers `already_selected` for the current root, so recovery means
     activating another root and then rolling back to it. Jobs stay
     refused until then.
   - A sticky `vault_root_pinning.enabled` marker turns a lost binding file
     into `mismatch`. Back up and restore it together with
     `vault_root_binding.json`. A `blocked` attempt removes a marker it
     created itself.
   - A rollback to a root that was never pinned returns `legacy_unpinned`,
     the same as before any activation.
   - **Shared connection save.** `POST /api/v1/nextcloud/connections` sets
     the folder by explicit operator input after its own probe. Under the
     same Job locks it now records that folder as an unpinned root (outcome
     `connection_saved`, returning to today's behaviour). The record is
     never a pinned chain, and rollback is refused afterwards. If writing it
     fails, the stale pin stays and every Job keeps refusing.
4. **Not guaranteed.** Remote edits made between the final read and the CAS,
   or after it. New collisions or aliases created inside the root later.
   The UI must say this.

Even under (b), A/I/X/L/R must be VERIFIED. While no gate is VERIFIED,
(b) still makes zero provider calls. The code never writes
`C=VERIFIED` and never sets a coherence flag in restricted mode.

## P3 experiments

**Synthetic (mock transport, done):** S1 rotation, S2 ETag change, S3/S4
`oc:id` mismatch, S5 mount target, S6 property returned as 404, S7 transport
failures, S8 unproven provider makes zero calls. The restricted mode adds:
the owner flag is required, every non-C gate is required, the change signal
still blocks, and the ID chain is withheld unless the result is
`candidate_valid`.

**Disposable provider (each needs separate approval, no new resources):**

| ID | Gate | Writes to the fixture |
| --- | --- | --- |
| P3-D1 | I: `oc:id` stability over restart and rename/move | MOVE ×2 |
| P3-D2 | X: share to a 2nd synthetic user, then read `nc:mount-type` | user + share |
| P3-D3 | L: as run, unscanned on-disk children vs. the seeded expected set (the planned ACL-hidden child was not tested) | 3 direct disk writes + `files:scan` |
| P3-D4 | C: A→B→A, delete/recreate, change after last read | MOVE/DELETE/MKCOL |
| P3-D5 | R: as run, a user-password reset between two walks (not an app password, not mid-walk) | password reset |

P3-D4 is expected to confirm NOT_SUPPORTED. It does not unblock C.

## P3-D1 / P3-D2 results (disposable Nextcloud 35.0.2, 2026-11-12)

The owner approved this run ("Docker環境でテストを進めて"). It used the
existing disposable stack only, with the in-container PHP client.
- Plan and client SHA-256 `0b179b9c…f6befa`: owner-local
  `oa303-p3-d1-d2-plan-20261112.md`.
- Receipts: `~/.local/state/oa303-nextcloud-disposable/p3-20261112/`.
- No new image, container, network or port. No retry.

**P3-D1 (I).**
- Requests: 8 PROPFIND (all 207), 4 MOVE (all 201), 1 container restart.
- `oc:fileid` and `oc:id` of the moved file stayed the same across:
  - a cross-parent MOVE (`10-Project/alpha.md` → `11-Knowledge/`)
  - a directory rename (`98-Empty` → `98-Empty-renamed`)
  - a container restart
  - the moves back
- The same held for every other row: no path's `oc:id` changed.
- `oc:id` equals the 8-digit zero-padded `oc:fileid` + `instanceid` on every
  row, matching `occ config:system:get instanceid`.
- The F1 layout was restored.

**P3-D2 (X).**
- Steps: created a synthetic user `oa303_test_viewer`, made 1 read share of
  `10-Project` (OCS 200), and ran 4 PROPFIND (all 207).
- Viewer side:
  - The received share appears as `nc:mount-type=shared` with
    `is-mount-root=true`.
  - Its children are also `shared` with `is-mount-root=false`.
- **Both accounts see the same `oc:id`** for the shared folder. A share is
  therefore an alias of one resource, which is exactly what V1/A's
  duplicate-ID and alias checks guard against.
- The owner side shows no mount marker for an outgoing share.
- Local folders and files are `""` / `false`.

**Limits.**
- One instance, one version, one storage backend.
- Not tested: group folders (not installed), external storage (app on, no
  mount), backend symlinks, ACL-hidden children (L), races (C), credential
  rotation (R).
- Hence OBSERVED_WITH_LIMITS, not VERIFIED.

**Left in place** (cleanup needs a separate G6 approval):
- the viewer user and its skeleton files
- the read share
- the container `/tmp/oa303_viewer_pass`

## P3-D3 / P3-D5 results (disposable Nextcloud 35.0.2, 2026-11-12)

The owner approved this run ("D3とD5を進めてほしい").
- Same stack and boundaries as above. The D2 viewer user and share were reused.
- The PROPFIND-only client is SHA-256 `30db07a9…31bd95`.
- Receipts: `p3-20261112/d3.jsonl` and `d5.jsonl`, owner-private.
- The admin secret was not touched.

**P3-D3 (L).**
- Writes: as www-data, wrote `disk-only.md`, `ALPHA.md` and `disk-only-dir/`
  directly into the data dir under `10-Project`.
- Requests: 4 PROPFIND (all 207) around one path-limited `occ files:scan`.
- **Before the scan,** both the admin and viewer listings showed only the 2
  seeded files.
  - The 3 entries that existed on disk were silently absent. The self row,
    the IDs and the ETag gave no sign of this.
  - The cause is that `filesystem_check_changes` is unset (never).
- **After the scan,** all 5 appeared.
  - `alpha.md` and `ALPHA.md` coexist in one folder with different `oc:id`s.
  - V1/A blocks that level with `casefold_collision`, but only once both
    names are in the index.
- Not tested: ACL-hidden children, external storage, group folders.

**P3-D5 (R).** Viewer account only.
- Requests: 1 PROPFIND (207), then `occ user:resetpassword` (rc 0), then 2
  more PROPFIND.
- The old password got **401** immediately, and no listing was recorded.
- The new password got 207. All 11 rows had the same paths and `oc:id`.
- Not tested: app passwords/tokens, session cookies, or a rotation during
  one verifier walk under load.

**Left in place** (no cleanup, by owner decision):
- the 3 scanned entries
- the viewer user and share
- `/tmp/oa303_viewer_pass` and `/tmp/oa303_viewer_pass.old` in the container

## Decision on L scope (owner, 2026-11-12, option A)

L means **complete with respect to Nextcloud's index (the authorized
view)**, not the server's storage.

**Assumption.** The Vault is written only through Nextcloud clients
(desktop/mobile/WebDAV). Those writes update the index.

**Not guaranteed:**
- files placed directly on the server's disk;
- external storage that has not been rescanned;
- case twins that are not yet indexed.

The verifier docstring and the UI caveat state the same three points.

**Option (b) rejected.** The alternative was to keep L as storage
completeness and leave V1/D blocked forever.

This decision changes what L must prove. It does not mark L VERIFIED.
Activation still needs A/I/X/L/R VERIFIED.
