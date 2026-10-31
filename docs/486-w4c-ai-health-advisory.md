# OA486 W4C R1 — AI Pre-review health advisory

## Scope and authority

Source-only addition to `/workflow-control` on the supplied merged #497 baseline
`c755ec560f4a8b353a9635a821bcd3820ae41534`. The separate AI Pre-review section
uses an existing real HTTP status source without broadening the strict
`webui-workflow-control/v1` DTO or compositor. B1 remains pending; its card,
integration, actions and the B1–B5 connection summary are unchanged. Producer
health `ok` is not workflow success, a connected adapter, provider readiness,
note generation, Review approval or a Vault effect.

## Manual read and provenance

- Initial state is **Not requested**. Only **Read AI pipeline status** requests
  the advisory; no added load-time fetch, polling, forms, inputs or POSTs. The
  existing catalog still reads on load and its own refresh button.
- Fixed same-origin `GET /api/v1/status`, `credentials: omit`, `cache: no-store`,
  `redirect: error`, `Accept: application/json`. An AbortController and one
  10-second deadline bound fetch **and** JSON decoding. A new click clears old
  advisory results, aborts the previous active read and advances a generation
  guard; late success, rejection or timeout cannot replace the newest state.
- Existing provenance: `admin_dashboard.py`'s `/api/v1/status` route calls
  `_StatusSingleFlight.get()`, returning integer `version: 1`,
  `authority: observation_only` and `ai_pipeline` from
  `admin_ai_status.load_ai_health()`. The loader reads a bounded, sanitized
  pre-review producer projection, not a live provider or job database.
- The status route also collects independent systemd observations. Manual-only
  use avoids extra automatic expensive reads. Its existing single-flight policy
  permits one collection and shares a recent JSON result for at most one second;
  busy/failed collection can return 503. No backend changes are made here.

The frontend accepts only plain built-in objects with the expected version and
authority. It displays allowlisted freshness and health enums, four nonnegative
safe-integer counters (review, operational failures, retry-pending failures and
deterministic rejections), and strictly boolean backpressure/reminder flags.
Retry-pending does not confirm remaining retry budget or an active retry;
Validation rejection is not an outage. All values use textContent with fixed
labels. Source paths, incident codes, services, errors, generated timestamps,
provider names/URLs, HTML and raw JSON are never displayed by the advisory.

Freshness is the **backend's** policy: the default threshold is 15 minutes;
age at or beyond that threshold is stale. The browser does not independently
verify the source or recompute age. Stale, invalid and unavailable observations
show fixed text without historical counters. Malformed envelopes/data, HTTP or
JSON errors and timeouts show **AI producer observation unavailable** and clear
counts. Advisory failure does not erase the independent control catalog;
catalog failure or refresh does not erase the independent advisory. A successful
manual read is a snapshot, not a continuously current health claim.

## Validation handoff and limits

Only `live.html`, `live.js`, `tests/webui_live_v1.test.cjs` and this document are
in scope. The backend route, DTO/model, compositor, packaging and offline demo
are unchanged; existing packaged HTML/JS wildcards cover the asset edits.
There are no live provider calls, Writer changes, B3/B5 integration, admission,
Review/apply operations, credentials, deployment or production-service changes.

The VM suite retains its original 12 checks and adds 13 advisory checks:
**25 default checks**, or **26** with the existing `--catalog-stdin` mode.
Its original `PASS …` and `webui_live_v1: … checks passed; controllable transport,
no network` output format is retained. New cases cover manual-only transport,
fixed options, redaction/type validation, freshness, abort/deadline including
delayed JSON, overlapping generations, independent failures and mock versus
HTTP provenance. Fixtures are synthetic; the VM is not a real browser.

Recommended coordinator validation (not executed in this source-only session):

```sh
node tests/webui_live_v1.test.cjs
node tests/webui_design_v1.test.cjs
python -m pytest tests/test_admin_dashboard.py tests/test_admin_ai_status.py tests/test_webui_control_get.py tests/test_webui_control_read.py
```

Use the repository's approved test environment. Review the four-file diff and
confirm the supplied base HEAD independently. No execution pass, real
browser accessibility check, live producer/provider verification or Fresh-LXC
E2E is claimed. Changes are left as uncommitted source; publication requires
separate explicit authorization.
