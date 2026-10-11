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
- Localhost-only socket; exact Host/Origin checks; no CORS wildcard; restrictive CSP disallowing inline scripts and remote dependencies, frame embedding, form submissions; Cache-Control: no-store; no raw exception data.
- JavaScript uses `textContent` rather than `innerHTML` when rendering status values, preventing interpretation of an unexpected service label as HTML.
- This is **not yet** a secure general-purpose admin API: the eventual credentials manager, local privileged broker, WireGuard access control, CSRF coverage for new mutations, audit trail, status age/freshness policy and human approval gates require their own review before any mutating UI is enabled.

## Acceptance evidence

Nix-provided Python 3.11 + pytest with `PYTHONPATH=src`, no external services needed:
- 24 focused tests for timer/oneshot state classification, fixed command allowlist, malicious Host/Origin, no mutating routes, CSP, JSON redaction and observer failure.
- The existing full `tests/` suite including the new tests completed **1181 PASS in 19.84 seconds** on the isolated Adam checkout.

The test mocks do not establish actual production systemd health or correct management of existing live timers. No CI, independent security review, or production deployment acceptance has been claimed.

## Follow-up to complete #300

1. Integrate existing `pre_review_status` / `mirror_status` and GitHub pipeline bounded projections for last verified success / health reasons; avoid copying raw operation receipts.
2. Unify the service registry with runtime/provisioning logic instead of maintaining duplicate lists long-term (current prototype duplicates known names deliberately to prevent dynamic command injection).
3. Add a narrowly scoped local operation broker for approved `pause`, `resume`, `run_once` with idempotency, lock and persistent audit, and do not bypass #294 deployment stage/activate or #287 Human Review.
4. Confirm trustworthy origin when opened from a private WireGuard-connected phone and add real deployment-unit hardening; never silently expand the host bind address.
5. Carry out independently qualified Security and Correctness review before changing the production profile or merging.

**This document and implementation do not authorize or perform production service activation or Vault writes.**
