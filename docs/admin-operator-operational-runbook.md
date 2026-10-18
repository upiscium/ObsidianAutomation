# #300 — LAN/VPN operator controls (reviewed source release contract)

**Status: source/test only. NOT installed, enabled, or cut over to production.**
The source branch includes the browser, AF_UNIX Broker, durable queue and worker,
but source completion is not an authority to start any managed service on the
live LXC. The only supported topology is ONE active execution LXC per Vault.

## Fixed service functions

Only the five fixed service/timer pairs in admin_status.SERVICES are exposed:
AI Vault Mirror, AI Pre-review, GitHub Project Sync, GitHub Daily Progress and
Core Promotion. The available actions are Pause (disable future timer
recurrence), Resume (reenable recurrence), and Run once (ask systemd Manager to
queue ONE fixed oneshot). Pause never cancels a running workflow. StartUnit
acknowledgement is NOT proof that a Gitea/GitHub/Nextcloud business transaction
succeeded. Each service owns its own CAS/ETag, retry and recovery guarantees.

The browser flow is preview -> short-lived ticket bound to service/action/UUIDv4
-> explicit Confirm -> fresh one-use CSRF + ticket -> non-root WebUI -> root
AF_UNIX intake -> durable request and processing evidence -> single Worker ->
audited receipt. A missing response must be reconciled by querying the SAME
UUID; a new UUID would be a new user action. Run once receipts use
systemd_job_accepted (Job path and boot ID), NOT completed. Unknown/partially
performed effects remain indeterminate and block replay.

The Unix Socket authenticates ONLY the dedicated installed Web Unix UID/GID
via Linux SO_PEERCRED and fixed private directory/socket ACLs. The Web process
cannot submit an arbitrary systemd unit, command, credential, filesystem path
or environment variable. All statuses are redacted and bounded.

## Accepted LAN/VPN authorization boundary

The operator explicitly selected the following network trust assumptions:
1. The service LXC resides on a trusted LAN.
2. The WebUI is reachable ONLY from that LAN.
3. All external clients reach that LAN through an authenticated secure VPN.

**No separate Web username/password, shared ingress header, or Web login screen
is required.** All clients admitted by the LAN/VPN network boundary are
therefore treated as authorized operators: an untrusted or compromised LAN
client is OUTSIDE this accepted threat model. If the LAN contains guests or
untrusted devices, this profile is NOT safe to deploy without separating the
management subnet or adding authentication.

The non-root Web backend MUST remain bound to 127.0.0.1 (and systemd restricts
its direct network access to loopback). LAN/VPN exposure must be provided by
a separately configured trusted reverse proxy on the SAME host/LXC, with
firewall/interface/source-CIDR allowlisting for the actual LAN and VPN
networks and NO unrestricted/public listener. The proxy must validate the
external Host and Origin, reject cross-origin writes, then consistently
rewrite upstream Host/Origin to the loopback endpoint. The Web backend also
enforces exact Host/Origin, CSRF, ticket scope, strict JSON size, timeouts and
no-cache/strict CSP. The obsolete X-Obsidian-Private-Ingress header is REJECTED
instead of used as additional operator authentication.

This is NETWORK trust, not proof of an individual person's identity: audit
records bind exact request IDs, actions and units but not per-user accounts.

## Startup trust: verify BEFORE root application imports

The effect-capable root Worker MUST NOT be started via a Python console script
or direct package import. The only installed ExecStart is:

    /usr/bin/python3 -I -S -B /usr/local/libexec/obsidian-admin-root-launch.py

That root-owned, non-group-writable bootstrap is **stdlib only**, validates
/etc/obsidian-admin/authority.json and fixed installed units, then checks the
entire STATIC privileged module import closure (currently 14 Python files,
including __init__.py, admin_broker_socket.py, admin_operation_preview.py and
admin_status.py) in the root-owned installed /opt/obsidian-automation/venv.
It verifies exact SHA256, stable inodes/owners/modes and no symlink path
components before execve() of the **fixed root-owned OS interpreter** /usr/bin/python3 with -I -S -B. The virtualenv/bin/python path is NOT executed. BOTH
interpreter stages run without site initialization and do not generate bytecode, so package .pth hooks
or sitecustomize cannot execute before exact-source admission. Every unreviewed __pycache__ directory or source-shadowing .pyc/.so/.pyd within the operator package is rejected before execve. The second
stage manually adds ONLY the hash-verified, root-owned site-packages
directory to its interpreter import path before loading the root runtime.
The bootstrap passes a **sealed Linux memfd**, bound to SHA256 of the validated private
authority bytes, through an inherited file descriptor. The Worker validates
the sealed descriptor and rehashes the authority before construction of any
root effect object. A plain environment variable is not sufficient; the
root console script is not packaged. A malicious host root account can still
construct its own trusted-execution inputs or import code; root is a trusted
actor, **not** an adversary this source attempts to sandbox. Startup revalidates
the root authority before creating a privileged socket; each live action
revalidates exact installed unit/module hashes under the updater lock.

The stage-only helper tools/prepare_admin_operator_authority.py is itself
stdlib only. It generates a root-owned 0600 exact-SHA authority file with
enabled=false and NEVER performs service effects. No root application import
is needed just to stage this disabled manifest.

## Actual single-LXC update/launcher exclusion

The root operator's local effect gate acquires BOTH stable cooperating updater
inodes in the reviewed order. The systemd Unit grants O_RDWR access to these
TWO root-controlled lock directories within its strict filesystem sandbox;
a read-only mount would make the reviewed fcntl lock protocol unusable.
The effect/socket path is /run/obsidian-admin/operation.sock, matching the
RuntimeDirectory=obsidian-admin provisioned by that same root Unit.

- /var/lib/obsidian-automation/deployments/host-update.lock
- /run/obsidian-automation/semantic-refresh-inhibited.lock

It rejects missing, replaced, busy, insecure and hardlinked locks and pending
host/semantic intent markers. It checks installed module and unit digests
under the same lock BEFORE submitting a systemd action. Run once additionally
re-observes the entire fixed SAME-DOMAIN chain (hidden dependency/OnSuccess
units as well as its timer) and refuses if a dependency is currently busy
or a Job is queued. A sibling's separate timer may be waiting normally,
but the selected timer must be fully paused. Exact source/identity gates
remain enforced, and systemd StartUnit uses job_mode=fail.

This is **not** a distributed transaction/lock and cannot exclude an
uncooperative root/manual launcher or a legacy updater that does NOT acquire
the reviewed stable locks. Root is a trusted host administrator outside the
unprivileged browser threat model. Two operator-certified, immutable root
cutover statements are now mandatory BEFORE effect authorization:

- coordinated_updaters_verified = true
- uncoordinated_launchers_retired = true

The stage-only helper writes BOTH false. A subsequent explicit Human Cutover
review must inventory the actual deployed updater executables, disable/retire
old uncoordinated versions, ensure relevant installed executables use the
shared locks and verify single-active-LXC topology before changing these
statements to true. These flags are an **operator attestation**, not automated
proof that every arbitrary privileged process on the host has cooperated.
Their absence or falsity prevents startup at both pre-import bootstrap and
post-import authority gates. Core Promotion uses its own nonblocking role
mutex and the root Broker MUST NOT acquire that mutex before invoking Core.

## Worker health and graceful shutdown

The operator socket advertises readiness ONLY when the explicitly qualified
Worker is alive, not stopping and without an unresolved worker failure.
Under production enforce_worker_readiness=True, it rejects NEW durable queue
submissions once the worker becomes unavailable, while immutable receipt
readbacks remain available. A crash/indeterminate/queue_recovery_required
blocks automatic retries and withdraws execute controls.

SIGTERM/SIGINT stops acceptance; the non-daemon worker is joined before the
root socket is closed to let an in-flight bounded systemd action settle.
If an external forced kill occurs after systemd accepted a job but before
durable receipt, the immutable processing claim remains unconfirmed and
must be manually reconciled. A systemd service's TimeoutStopSec is an external
force-stop bound, NOT a promise that every effect always finishes in time.

## Tests and exact acceptance boundaries

Source tests use synthetic UnitAdapters for HTTP -> WebUI -> Unix socket ->
durable queue -> Worker -> receipt, including Pause -> Run once -> Resume,
lost reply/replayed UUID, root default off, foreign UID and bad Origin/CSRF,
worker crash recovery and graceful stop. Additional isolated Linux tests in
docs/admin-systemd-isolated-acceptance.md prove systemd user-manager Job
coalescing, failure != admission, and actual cooperating updater flock
conflicts. They do NOT establish that the installed LXC's CURRENT units and
binaries are the reviewed release.

A further disposable *real Linux UID split* integration test,
tests/manual_admin_operator_crossuid_http.py, created a root-owned temporary
/run runtime directory and 0700 root queue/journal. An actual nonroot WebUI
performed confirmed HTTP Preview/CSRF -> root peer-verified Unix Socket ->
durable queue -> root Worker -> fake systemd adapter/Manager -> receipt for
Pause, Run once and Resume. No actual systemd unit or external provider
was modified in that test; the root fixture was shut down and removed.
The root-owned production Unit is not automatically installed or enabled.

Before declaring this usable on production, still required:
- A reviewed root-owned immutable installed release and source/Unit SHA.
- Real LAN/VPN reverse proxy/firewall source restriction validation.
- Real separate Web UID and root Worker installed systemd/AF_UNIX E2E, with
  correct root ownership, permissions, failclosed rollback and crash tests.
- Verified retirement of uncoordinated updater paths and single-LXC topology.
- A documented operator process to resolve indeterminate requests.
- Separate explicit Human Final Merge and production Cutover acceptance.
  Source-only integration PRs and successful CI do NOT authorize deployment.

## Approval request format for production

① **Implementation:** exact reviewed source/Unit hashes, proxy/firewall
   boundary, role identities, installer changes and disabled/retired updaters.
② **Approval requested:** explicit exact-SHA deployment and enabling the
   attested root Worker and WebUI controls (separate from main merge).
③ **Impact:** systemd timers/services, root/nonroot identities, LAN/VPN
   console reachability, immutable audit dirs; no Gitea/Nextcloud migration
   unless separately approved.
④ **If denied:** keep effect-disabled queue, current timers, old LXC,
   secrets and Vault unchanged. Keep the source PR as Draft/under review.
