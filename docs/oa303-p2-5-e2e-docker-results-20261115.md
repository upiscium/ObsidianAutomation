# #584 P2-5 E2E results — local Docker (2026-11-15)

**Result:** every case run passed: M0 to M10, with M6 on its second attempt
(see "Cases"). B4 and B3 were not run live; see "Not run live".

## Setup

- **App:** `python:3.13-slim` (Debian 13), with a wheel built from
  `main` `5cdb8ec`, which includes P2-0 to P2-4.
  - It runs `obsidian-snapshot-app --state /e2e/state` on `127.0.0.1:8370`.
  - It has no systemd. It is on the isolated Docker network only.
- **Nextcloud:** the disposable stack (35.0.2), behind a TLS proxy
  (`nginx:alpine`, alias `nc.oa303.test`) with a CA created for this run.
- **Account:** the synthetic viewer `oa303_test_viewer`, never a production
  account. Its password stays inside the containers and is never printed.
- **Fixture:** created as the synthetic owner, inside the share
  `OA303-Synthetic-20261110/11-Knowledge`, which is shared with the viewer.
  - The folder `P25/` holds the six role folders, `Ops/` and `Journal/`.
  - `P25/98-System/clean.md` is already formatted.
  - `P25/Ops/raw.md` needs one blank-line fix.
  - The Vault root is switched to `11-Knowledge/P25` through the V1 UI flow,
    so the root is pinned.
- **Job exercised:** only the Formatter, in dry-run mode with no Writer.
- **Receipts:** `~/.local/state/oa303-nextcloud-disposable/p25-20261115/`
  (`cases.jsonl`, `fixture.jsonl`). They contain no secrets and no IDs.

## Cases

| Case | What | Result |
| --- | --- | --- |
| M0 | App without `--vault-bindings-ui`: GET is 404, POST is 405 | pass |
| M1 | Save connection, enroll, then switch the root to `P25` (Preview + confirm) | pass |
| M2 | No mapping: the card shows `default`; the Formatter reads `98-System`, with 0 to change | pass |
| M3 | Preview, then save `system → Ops`: `saved`, `requires_job_reenable`, state `ok` | pass |
| M4 | The Formatter follows the mapping: 1 to change. The plan path is the logical `98-System/raw.md`, not `Ops/raw.md` | pass |
| M5 | No Vault write: the PROPFIND response for `Ops/raw.md`, ETag included, is unchanged after the runs | pass |
| M6 | A mapping locks the root. A V1 switch to `11-Knowledge` is refused with `mapping_present`. A connection-form change of the Vault folder is refused with 409 `mapping_present`. An unchanged save is 200 | pass (2nd attempt) |
| M7 | The owner deletes and recreates `Ops`. The Formatter run fails, and the Preview reports `identity_changed` with no handle | pass |
| M8 | Re-pin (`accept_recreated: ["system"]`), then save: the Formatter runs again, with 1 to change | pass |
| M9 | Rollback restores the record with the old `Ops` ID, so the Formatter refuses. This is the documented limit: rollback does not re-read the server | pass |
| M10 | Reset: the record is removed, state is `default`, and the Formatter reads `98-System` again, with 0 to change | pass |

**M6 first attempt:** the driver previewed the already-active `P25`, which
cannot be a switch target, and stopped with `preview_required`. The
connection-form half of the case had already passed (409). The case was
rerun with `11-Knowledge` as the target, and that attempt passed. Both
attempts are in `cases.jsonl`.

**Refusal reason:** in M7 and M9 the Formatter run records the generic
`source_or_plan_failed`, not a role-pin-specific code.

## Not run live

- **B4 Daily publisher and B3:** they need GitHub, which this synthetic
  account does not have. The Daily writer's mapped GET/PUT/GET, the re-check
  before the PUT and `vault_root_refused` are covered by
  `tests/test_303_p2_3_producers.py`.
- **Browser UI:** the card was not opened in a browser in this run. The routes
  it calls were exercised over HTTP.

## Follow-ups

- Cleanup of the E2E containers, the CA, `trusted_domains` and the `P25/`
  fixture needs approval.
- Before a production deploy, check read-only that all six default role
  folders exist in the production Vault (O2-A).
