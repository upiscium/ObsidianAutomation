# Experimental read-only Dashboard (#300)

**Status:** implementation prototype, not accepted or deployed. This is a deliberately small, non-mutating first vertical slice of the larger [#298](https://github.com/upiscium/ObsidianAutomation/issues/298) single-LXC UX redesign.

## Scope

- A mobile-responsive HTML/CSS/JavaScript page, loaded entirely from the Python stdlib HTTP server, with no CDN or externally loaded resources.
- `GET /api/v1/status` returns only a bounded fixed registry of five systemd timer/oneshot pairs, their sanitized schedule state, last observed execution result, and coarse health:
  - `obsidian-ai-vault-pull.timer` → `obsidian-ai-vault-pull.service`
  - `obsidian-pre-review.timer` → `obsidian-pre-review-status.service`
  - `obsidian-github-sync.timer` → `obsidian-github-compactor.service`
  - `obsidian-github-daily-progress.timer` → `obsidian-github-daily-apply.service`
  - `obsidian-core-promotion.timer` → `obsidian-core-promotion.service`
- The same status response includes an `ai_pipeline` aggregate sourced exclusively from the pre-existing **non-authoritative** `/var/lib/obsidian-ai/state/02-Orchestration/status/pre-review-status.json` projection. It exposes only schema-validated health, Human Review queue count, separate operational-failure and deterministic-rejection counts, Backpressure and reminder flags.
- A separate `ai_mirror` aggregate derives from the pre-existing fixed-unit `mirror_status.collect_status` diagnostic: latest systemd attempt, monotonic age since latest completed successful attempt, active runtime duration and finite diagnostic reasons. The consumer explicitly reports `durable_success_history=false` and `remote_verified=false`; it never claims actual remote Nextcloud freshness or a durable history.
- Freshness is explicit: projection age under 15 minutes is `fresh`; older data is `stale` and its old review/error counts are **not** presented as current. A missing, malformed, overlarge, symlinked or unreadable file is `unavailable` (never healthy). Far-future timestamps are `invalid`; minor clock skew up to 60 seconds is clamped to zero age.
- `GET /`, `GET /static/style.css`, `GET /static/app.js`. All other paths return 404.
- POST/PUT/PATCH/DELETE/OPTIONS return 405; no start/stop/restart/reload/run-once, credential update, or Vault mutation endpoints exist.

The output is **observation only**. A systemd oneshot `inactive` status is not automatically an error. The current implementation classifies explicit service failures, a successfully completed latest systemd attempt, a timer deliberately paused, no-run history, elapsed timers without a next event, unknown or uninstalled units independently. It does *not* treat an observed successful systemd exit as proof that Nextcloud, the private Vault or an AI result is current and correct. The API does not include Vault paths, credentials, raw journal, ExecStart, command lines, URLs, or systemd stderr.

## Local development use

From this checkout, on an ordinary Linux host with systemd and Python 3.11+:

```sh
PYTHONPATH=src python3 -m obsidian_automation.admin_dashboard --port 8370
```

Then open `http://127.0.0.1:8370/` on that same host. Installers can later use the registered `obsidian-admin-dashboard` CLI entry point. The server **always binds 127.0.0.1** regardless of command input; remote-phone access requires a separately reviewed private ingress or tunnel. Do not expose this experimental endpoint to the public Internet or broadly trusted LAN without a complete management ingress review.

## Security properties and limits

- Fixed `systemctl show` unit registry and explicit allowlisted property names; no request-supplied unit name, arbitrary command, shell, or filesystem traversal.
- Per-call subprocess timeout and output bound; failure and malformed properties become `unknown`, never a fabricated healthy state.
- The AI projection is read through `O_NOFOLLOW` component-by-component directory traversal, `fstat` regular-file/hardlink checks and a 64 KiB ceiling; no parsing of job database, Vault, LLM output, receipt or secret files. Only fixed typed counters are returned, never raw `reasons`, planner policy strings or other free text. The eventual non-root Dashboard service must be granted **read-only access to this one projection** and needed directory traversal; it must not receive general `02-Orchestration`, credential or Vault permissions.
- Localhost-only socket; exact Host/Origin checks; no CORS wildcard; restrictive CSP disallowing inline scripts and remote dependencies, frame embedding, form submissions; Cache-Control: no-store; no raw exception data.
- JavaScript uses `textContent` rather than `innerHTML` when rendering status values, preventing interpretation of an unexpected service label as HTML.
- This is **not yet** a secure general-purpose admin API: the eventual credentials manager, local privileged broker, WireGuard access control, CSRF coverage for new mutations, audit trail, status age/freshness policy and human approval gates require their own review before any mutating UI is enabled.

## Acceptance evidence

Nix-provided Python 3.11 + pytest with `PYTHONPATH=src`, no external services needed:
- The existing MVP test baseline was **1,181 tests PASS**. With this #300 follow-up, **1,196 tests PASS in 20.69 seconds** on the isolated Adam checkout, including the new bounded AI projection's fresh/stale/future/missing/malformed cases, symlink/hardlink rejection, and HTTP API sanitized read model. This is a source regression result, not a production deployment or independent security review. GitHub CI and integration-branch acceptance remain separate.

The test mocks do not establish actual production systemd health or correct management of existing live timers. No CI, independent security review, or production deployment acceptance has been claimed.

## Follow-up to complete #300

1. AI `pre_review_status` and `mirror_status` read-only observations are implemented in separate bounded Dashboard slices. Next add a **dedicated durable GitHub pipeline read model** (#316) for independently proven success/failure and incident history. The existing systemd display and Mirror diagnostic provide only the latest attempt, never verified remote state or durable history.
2. Unify the service registry with runtime/provisioning logic instead of maintaining duplicate lists long-term (current prototype duplicates known names deliberately to prevent dynamic command injection).
3. Add a narrowly scoped local operation broker for approved `pause`, `resume`, `run_once` with idempotency, lock and persistent audit, and do not bypass #294 deployment stage/activate or #309 exact-bound automatic rejection cleanup.
4. Confirm trustworthy origin when opened from a private WireGuard-connected phone and add real deployment-unit hardening; never silently expand the host bind address.
5. Carry out independently qualified Security and Correctness review before changing the production profile or merging.

**This document and implementation do not authorize or perform production service activation or Vault writes.**

## Corrected incident and successful-run semantics (subsequent child PR)

The AI status projection must not describe a normal deterministic Validation
rejection as an incident. Only blocked generations and retry-exhausted
generations contribute to the actionable operational error count; a retryable_failure is an observed failed attempt, shown separately as a warning pending another claim (remaining retry budget is not proven). Fixed,
bounded incident codes and counts are available for blocked and exhausted
categories, never original model output or raw diagnostic text.

The pre-review status has a redundant aggregate health level and detailed
state counters. If these contradict each other, the Dashboard returns
invalid/unknown with hidden counters rather than treating the apparently green
aggregate as proof of health. A missing or stale source behaves similarly:
no previous counts are presented as current. An ordinary rejected candidate,
even if numerous, is not an outage and needs no Human Acknowledge.

The pending-failure status may persist even after the final permitted failed attempt until another claim happens, or indefinitely if workers stop. The Dashboard must neither claim retry is actively running nor suppress the warning. Settling exhaustion at the actual last failed attempt rather than a later claim is tracked in #319; this read-only status change deliberately does not alter the authoritative worker state machine. A systemd oneshot that succeeded at some unspecified time is now described as
observed_success (freshness unverified), not healthy. A verified latest run
failure has priority over an unrelated field that is unknown. The original
non-mutating five-unit systemctl allowlist, fixed-path read model, and
localhost-only web server are unchanged.

The prior test's credential-looking PASSWORD assignment was a synthetic
redaction canary, not a real secret. It is replaced by an explicit nonsecret
unexpected-field canary so generic secret scanners do not confuse test data
with live credentials. The historic scan alert may still reference the
pre-existing commit and does not, by itself, establish credential exposure.

This amendment does not add provider outage monitoring, actual Nextcloud
cleanup conflict detection, cross-service incident history, service control,
remote ingress or production deployment. Those remain separate acceptance gates.

## Malformed AI projection isolation

The AI status reader treats both strict JSON decoder errors (invalid UTF-8,
duplicate JSON keys, non-object JSON) and typed projection validation failures
as an unavailable AI observation. It must **not** fail the combined HTTP status
endpoint or hide independent systemd service results. Local synthetic API
fixtures exercise these decoder paths and verify HTTP 200 with explicit unknown
AI health while the fixed service observations remain intact.

## Masked timer observability

When systemd reports a mask (`LoadState=masked` or `UnitFileState=masked` / `masked-runtime`) **and** confirms the timer is `inactive/dead`, the Dashboard shows `blocked`. Masking does not necessarily stop a previously active timer; `active/waiting`, `active/running`, or transitional masked states remain `unknown` rather than being misrepresented as quiescent. A verified `ActiveState=failed` remains a failure. Malformed, missing, or contradictory properties—including `LoadState=not-found` paired with a masked unit file—also remain `unknown`. These observations grant no authority to unmask or control a timer.


## Vault Mirror latest-attempt observation slice (2026-10-15, Draft)

The WebUI now displays a separate **Vault Mirror** diagnostics panel alongside
the five systemd service cards and AI Review pipeline aggregate. This reuses
the existing `mirror_status.collect_status` read-only systemd monitor rather than
inventing success evidence. Only the fixed source identifier, latest attempt
class, bounded monotonic durations, health class and a finite reason-code
allowlist reach the browser; failed/malformed diagnostic reads become `unknown`.
The browser renders with `textContent` only and clears stale mirror values when
an API refresh fails.

The time since the last successful systemd pull is **not** an attestation that
Nextcloud remote state is current. The response explicitly fixes
`durable_success_history=false` and `remote_verified=false`; even a report of
`health=ok` applies only to the fixed-unit latest attempt and age threshold.
A disabled or stopped timer is still distinguished by the independent service
card, not silently reported as a failed remote write.

Local isolated Adam worktree `feat/300-dashboard-mirror-observation` from
Dashboard integration commit `9927d63` (not main) passed 103 targeted status/UI
tests and **1238/1238 full pytest tests** using the project's Nix Python 3.11
environment, plus Python compileall, JavaScript node syntax validation and
`git diff --check`. No host timer/service, Vault, Nextcloud, credential or
production configuration was modified. GitHub CI and independent review remain
separate gates; final merge continues only via integration Draft PR #311.
