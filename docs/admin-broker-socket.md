# #300 Phase C — peer-verified local broker IPC (read-only only)

**Status:** experimental source only, not accepted/deployed. No production
systemd effect, root listener service, or write HTTP endpoint is introduced.

## Boundary

The non-root WebUI will eventually communicate with a dedicated privileged
local broker. This slice deliberately implements only a READ-ONLY protocol.
The root listener is not installed, and the broker does not execute actions.

ReadOnlyBrokerSocket uses Linux AF_UNIX/SOCK_STREAM and enforces:
- existing pinned non-symlink directory, root-owned and not group/world-writable;
- socket inode owned by root, mode 0660, optional fixed socket group;
- OS-configured peer UID and optional effective GID, checked with SO_PEERCRED
  before even parsing request bytes;
- length-prefixed JSON (1 to 2048 bytes), duplicate keys / invalid UTF-8 /
  unknown version / malformed payload all refused;
- one fixed supported operation: capabilities (read-only).
  Receipt lookup is deliberately NOT exposed on this single-request listener:
  a stuck filesystem/callback could otherwise block all clients. A separate
  bounded worker design is required before any receipt IPC is added.

Unsupported apply, pause, resume, run_once, file path, unit/command names,
secret fields, and all arbitrary requests produce fixed safe rejections.
Unknown or unauthorized peers are disconnected without details. An
exclusive UID-owned 0600 flock lease lives in the pinned socket directory
for the whole listener lifetime, preventing accidental competing listeners
from racing the creation/shutdown sequence. Uncooperative processes with
the owning/root UID remain part of the trusted host boundary. The entire
request frame shares a single monotonic deadline, so a byte-dripping peer
cannot extend the receive timeout indefinitely. Receipt callbacks are not accepted and cannot stall the listener. After a
failed bind setup, only the newly created matching
socket inode is eligible for cleanup. Shutdown does not unlink a replaced
inode or a socket in a directory whose write permissions became untrusted.
Root/same-UID processes remain inside the trusted host authority boundary.

The only capabilities currently returned are:

    {"version":1,"authority":"observation_only",
     "operations":{"pause":false,"resume":false,"run_once":false},
     "reason":"cross_updater_exclusion_unqualified"}

The loopback Dashboard additionally exposes a READ-ONLY endpoint
GET /api/v1/operation-capabilities. It returns broker_unavailable when the
socket cannot be queried. False/unknown remains false; a spoofed positive or
malformed socket response is treated as unavailable. No POST endpoint is added.
This status is NOT authorization for any future write operation.

The fixed default production socket locator is
/run/obsidian-automation/admin-control.sock, but no service creates or binds it.
Tests instead use disposable current-UID sockets in /tmp, not the production
path. There is no receipt-lookup callback or browser receipt endpoint in this
slice. The durable broker-side lookup remains implemented by #367, but
its privilege and latency boundary is not wired to this socket.

## Authority still blocking real control

The #367 broker has effects_enabled=False by default and its maintenance gate
has not been qualified against all real updaters. This read-only socket NEVER
invokes OperationBroker.apply, regardless of any caller-supplied object.

The future effect gate must exclude all relevant launch/update paths:
1. Host runtime exact-SHA update and pending-runtime journal.
2. AI pre-review updater, refresh inhibitor and workflow dependencies.
3. GitHub update / sync / Daily launchers and pending effects.
4. Core Promotion writer and its deployment/update paths.
5. Concurrent timer starts, chained oneshot jobs and recovery.
6. A replacement/standby LXC only after the prior active runtime has been
   quiesced and stopped. Both concurrently active against one Vault are
   **unsupported**.

**Supported configuration: one active service-execution LXC per managed
Vault.** Simultaneous operation of multiple ObsidianAutomation instances
against one Vault is unsupported and does **not** carry behavior or
data-integrity guarantees. The MVP does not need a distributed cross-LXC
lock, leader election or cross-host fencing system. A local flock does not
make the unsupported configuration safe, and we do not claim that it does.

To enable controls on the supported one-LXC topology, all **local** active
updaters and job/worker paths still need verified common exclusion. Until
those within-LXC lock/idle contracts are qualified, service controls remain
disabled. The #294 Stage/Activate gate and #309 Sync cleanup remain independent
authority boundaries, not generalized systemd operations.

## Deployment/acceptance still required

- Dedicated hardened privileged process, socket activation/permissions, exact
  peer UID/GID and cross-UID Linux acceptance on a disposable host.
- Authoritative updater lock/cutover proof, installed unit SHA/FragmentPath
  verification and full dependency quiescence (not just final oneshot idle).
- Separate WebUI CSRF, Origin, request bounds, rate limits, explicit action
  confirmation, auditing and durable idempotency before any write route.
- Actual systemd lifecycle, crash and competing-updater E2E; full CI,
  Security/Correctness independent review, human final merge and separate
  production cutover acceptance.

Source tests use disposable Unix sockets with the current user, verify
SO_PEERCRED positive/negative UID/GID matching, lease contention, hostile
frames and route rejection, socket inode protection, first-stat bind failures,
and read-only HTTP fallback. They do NOT
qualify a root daemon, multi-UID service or live effect.
