# Experimental Management Dashboard: read-only status slice (#300)

**Status:** isolated implementation prototype; no production access, no control actions, no approved deployment. Root issue: #298; human-action and fault split: ObsidianCore #220 and ObsidianAutomation #309.

## Delivered scope

- Responsive Japanese HTML/CSS/JavaScript, stdlib HTTP server bound **only to 127.0.0.1** (no generic bind-address input, external CDN, secret entry, arbitrary command or service API).
- `GET /`, `GET /static/style.css`, `GET /static/app.js`, `GET /api/v1/status`.
- All POST/PUT/PATCH/DELETE/OPTIONS return 405; no timer control, arbitrary unit, credential management, Nextcloud/WebDAV, Vault or writer operation.
- The five systemd timer/oneshot pairs are a fixed, checked service registry. Each is classified by timer schedule vs latest **observed** service execution (never equate inactive oneshot with failure). Missing/malformed systemctl results remain **unknown**.
- `ai_pipeline` in the JSON API is an **independent** sanitized view of existing private `pre_review_status.build_status`: read-only authoritative latest-generation SQLite status, `awaiting_human_review` count, normal `deterministic_reject` count, transient retries, and separately counted actionable `blocked` / `retry_exhausted` incidents.
- AI status uses only fixed reason codes/counts and verified source timestamps, never arbitrary reason strings, model output, Vault content, credentials, journal, private paths, or exceptions.
- A normal Validator rejection **never raises an incident**. Unsupported or inaccessible AI status returns `unknown`, with **null counts** (not silently zero). Even `no_known_fault` describes only pre-review scheduling metadata, **not** AI provider uptime, Vault freshness or whole-system health.

### Sample status shape (illustrative, not a claim about production)

```json
{
  "version": 1,
  "authority": "observation_only",
  "services": [],
  "ai_pipeline": {
    "source": "pre_review_orchestration",
    "health": "failed",
    "reason_codes": ["blocked_generations"],
    "observed_at": "2026-10-11T12:00:00Z",
    "review_pending": 2,
    "normal_rejections": 7,
    "transient_failures": 0,
    "incidents": [
      {"code": "blocked_generations", "severity": "critical", "affected": 1, "active": true}
    ]
  }
}
```

Service entries and server-side `generated_at` are omitted from this illustration. The API returns them. A timestamp here means **status observation**, not when the failure originally occurred.

## Local development

Run from an isolated checkout on Linux with Python 3.11+ and systemd:

```sh
PYTHONPATH=src python3 -m obsidian_automation.admin_dashboard --port 8370
```

Open `http://127.0.0.1:8370/` on that same host. Without `--ai-root`, the AI panel intentionally displays **unknown**.

To enable a read-only AI source, an explicitly authorized local operator can start with `--ai-root /fixed/private/ai-artifact-root`; the web client **cannot** select this path. Run as a non-root reader permitted to observe the required SQLite and cadence records. A missing/inaccessible database or invalid projection returns `unknown`; do not grant broad writer credentials to make status collection pass.

No tested WireGuard/private ingress, systemd deployment unit or production dashboard exists yet. A loginless administrative interface must never be bound to untrusted/public networks.

## Safety / gaps

- Strict local Host/Origin validation, restrictive CSP, no inline scripts or external assets, no CORS, no caches, textContent-based UI, bounded public schema, generic redacted observation failure.
- This is **not** a complete health monitor: current phase does not independently verify AI provider connectivity, Nextcloud data freshness, GitHub Daily results, cleanup conflicts/stalls (#309), actual interval age, first/last incident occurrence, or a durable cross-service incident history.
- Existing pre-review statuses `blocked` and `retry_exhausted` are not automatically repaired or silently deleted; the data remains in authoritative lifecycle storage.
- The duplicated five-service status registry must be reconciled with `host_runtime_lifecycle.py` before a production control broker is implemented.
- Human Acknowledge #287 and the corresponding Core #205/#206 are **superseded**. Automatic deterministic-reject cleanup is #309, and genuine runtime errors stay visible here. This prototype does not delete rejected projections.

## Verification and remaining gates

The isolated prototype starts from the uncommitted read-only #300 worktree snapshot, copied without altering that source tree. Focused fixtures cover systemd waiting/elapsed/disabled/failed/unknown, no-observed oneshot, malicious Host/Origin, error redaction, CSP, no mutating routes, normal rejection vs blocked/exhausted/retryable, verified status serialization, and null/unknown fallback.

Before qualifying as the replacement for ObsidianCore operational signals:
1. Connect durable AI Provider, cleanup, GitHub and mirror incident evidence with validated age and bounded first/last occurrence; report unknown where not available.
2. Ensure the Management Dashboard can be accessed securely from the intended device over a reviewed private ingress, and has an accepted production start/stop design **without** a root web process.
3. Introduce typed, audited, effect-gated pause/resume/trigger broker separately; no generic systemctl or shell routes, no bypass of #294 stage/activate.
4. Complete independent Security/Correctness review, service identities/ACL tests, CI, explicit Architecture acceptance and separate Live deployment authorization.
5. Only then consider merging/promoting Core Review-only PR #221; there must be no operational visibility gap.

**No Live Vault, production systemd, Nextcloud or secrets may be touched under this prototype approval.**
