# #300 Phase H — Preview-only operator request path (NOT live service control)

**Development-only, no deployment.** This slice connects the loopback Dashboard
to an **optional separate privileged Unix peer-verified preview process**.
The preview process is NOT installed or activated and has **no effectful command
handler**, no general systemd control, and does not construct OperationBroker.
Existing Pause, Resume and Run once remain disabled in the Dashboard and
in the default read-only capabilities projection. This slice does not authorize
production service control, source merging or user-visible promises of execution.

## Browser -> WebUI (unprivileged)

- Existing GET /api/v1/status, GET /api/v1/operation-capabilities, Host,
  Origin, CSP, loopback-only listener and observation freshness behavior remain.
- New **GET /api/v1/operation-session** returns a fresh HMAC-SHA256-authenticated
  CSRF token containing a random 128-bit nonce and short expiry, signed with
  a process-local 256-bit secret. It expires within 120 seconds. Token issuance
  is **stateless** (cross-site unreadable GETs cannot fill an issuance pool);
  only successfully consumed nonces use a bounded, time-expiring cache to
  prevent replay. Tokens are never put in a cookie, URL, localStorage or audit
  log. HEAD does not mint tokens.
- New **POST /api/v1/operation-previews** is *not* an operation-execution route.
  It requires the exact local Host, a **present and matching Origin**, safe
  Sec-Fetch-Site when supplied, and a one-use CSRF token in the single
  X-Obsidian-CSRF header. No CORS wildcard is added. The handler accepts a
  strictly bounded (at most 512 bytes), one-deadline JSON request with only
  version=1, UUIDv4 request_id, a fixed service_id and one of
  pause / resume / run_once. Extra fields, duplicate JSON keys, credentials,
  arbitrary unit names/commands, missing or duplicate Content-Length, content
  encodings, and chunked transfer are rejected.
  The Dashboard bounds accepted concurrent HTTP handlers and applies an
  **absolute two-second deadline across the request line and all headers**,
  not just the POST body, so slow-drip connections cannot exhaust unbounded
  worker threads. The POST body has its own strict one-second total deadline.
  The expensive existing GET /api/v1/status path is also **single-flight**:
  at most one fixed-systemd/AI read model is evaluated concurrently, and
  another request receives a fixed status_busy response without waiting or
  spawning more systemctl reads. A successfully built immutable JSON
  observation may be reused for at most one second; failed refreshes never
  return a previous healthy projection. All responses still use no-store
  because the server's short deduplication is not browser freshness proof.
  GET /api/v1/operation-capabilities likewise uses a separate single-flight,
  at-most-one-second fixed-schema cache. When that optional Unix socket is
  slow or unavailable, extra callers receive the explicitly false
  broker_unavailable projection immediately instead of occupying handler
  threads or escalating authority.
- The browser will not recognize or render a preview as executable even if
  a compromised/misconfigured local responder claims can_execute=true.
  All successful/failed preview projections force authority=preview_only and
  can_execute=false. No POST /api/v1/operations, receipt mutation, start/stop,
  credential/Vault write or privileged root request exists.
- The UI provides an accessible service/action selector and Preview button.
  An Execute button is **always disabled**; the preview never claims that a
  current systemd status proves fresh or safe production execution.
  The status observer failure clears the earlier preview/selection.
  In-flight preview results are discarded if a newer status refresh or
  service/action selection invalidated the response, preventing stale
  compatibility messages from reappearing after a failed observation.

## Unprivileged WebUI -> OS-local Unix socket

The WebUI's preview callback forwards only the typed fixed request to
the fixed local Unix socket. It never accepts a browser-supplied timestamp,
shell argument, filesystem path, credential, current systemd property,
pre-authorized flag or effect permission. The socket client independently
checks version, exact response fields, request UUID/service/action identity
and can_execute=false, and rejects spoofed positive projections.

The existing ReadOnlyBrokerSocket class remains strictly capabilities-only.
An optional sibling OperationPreviewSocket shares its tested AF_UNIX framing,
bounded request, pinned directory/socket inode and OS SO_PEERCRED UID/GID
authorization. It accepts one additional method, **preview** only. There is
no apply, submit, execute, confirm or receipt access through this listener.

## Privileged OS-local preview process (not installed)

The optional python -m obsidian_automation.admin_preview_daemon entrypoint
requires root and an existing non-root service identity named
obsidian-admin-web (or another operator-specified Unix web user in a
separately reviewed installation configuration). Its peer UID and GID come
from the OS account database, never browser data. The socket binds only at
the fixed constant pathname under /run/obsidian-automation, with conservative
ACLs, and handles SIGTERM/SIGINT by graceful shutdown and inode-checked
cleanup. No matching systemd unit, runtime ACL, sudo/polkit permission or
production setup has been installed by this source slice.

The process constructs a fresh **server-owned** fixed systemd status
projection, validates request syntax and compatibility against that
projection using bounded, fixed-property systemd reads (32 KiB and
three-second limit per allowed unit) and the existing preflight model, and
returns
preview_only / can_execute:false whether compatible or rejected. The
preflight validation's deployment_quiescent=True parameter is used only
as a *schema/preview hint*: the returned candidate still requires actual
deployment and worker exclusion, live root revalidation, durable audit and
independent privileged authorization before any future execution.

## Remaining effect and deployment gates

This is a **no-effect vertical slice**. The actual #300 privileged operation
worker, durable accepted-then-executed submission and receipt/reconciliation
channel, full timer/OnSuccess job-exclusion boundary, unit revision attestation,
role mutex/credential separation and disposable real systemd/cross-UID tests
are NOT done here. In particular, a request accepted by PreviewSocket does
not reserve a timer slot or authorize a later Run once. The root process
must not directly expose a general shell or systemd unit name.

Only **one active service-execution LXC per managed Vault** is supported.
Multiple simultaneous instances against the same Vault are unsupported and
have no behavior/data-integrity guarantees. Cross-LXC distributed locking is
not a #300 requirement, while *within-LXC* updater and worker locks, remote
CAS/ETag, #294 Stage/Activate, #309 Sync cleanup and operator approval remain.

Tests include disposable Linux AF_UNIX roundtrips with current UID, strict
SO_PEERCRED checks, process shutdown/cleanup, loopback HTTP with exact
Host/Origin, expired/reused CSRF, malicious body and header rejection, spoofed
positive backend projections, and no write route. They do not prove actual
production root-to-Web service identities, package revision trust, private
WireGuard ingress or installed systemd behavior.
