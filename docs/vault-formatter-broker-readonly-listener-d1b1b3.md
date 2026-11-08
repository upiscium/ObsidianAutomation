# #472 D1/B1/B3 — Broker preprovisioned read-only Unix listener

**Status:** LOCAL-ONLY / SOURCE-ONLY / UNWIRED. Based on merged main `79dd08d09c2f5940058bc4441cdf4c4b86847a94` (PR #565). No Commit/Push/PR/Ready/Merge, installation, activation, deployment, credential provision, Provider/Vault I/O, or external authority.

## Exact scope and requirements

`src/obsidian_automation/vault_format_broker_readonly_listener.py` is a **separately invoked, one-connection-per-call** Linux AF_UNIX socket binder / acceptor for the existing **read-only** `BrokerReadOnlySession`. The previously merged B2 module (`vault_format_broker_readonly_session.py`) is also minimally refactored: its private `_serve_connected` handles an already accepted descriptor **without closing it**, while its public `serve_once` closes the descriptor itself and raises a fixed failure if that close is indeterminate. This is NOT a daemon, new executable, autonomous loop, systemd unit, actual installed privileged Broker, Provider transport, credential issuer, signed Claim validator or Writer/Owner Journal controller. The existing runtime does not import the listener.

The **trusted operator must preprovision**, without this module doing so:

- A distinct nonroot Broker OS principal and distinct nonroot Owner principal, with a restricted IPC group. Credential and Provider egress permissions remain outside this module.
- A canonical absolute Broker-owned socket directory, exact mode **0710**, group set to designated IPC GID; no symlink ancestor or writable untrusted path component. Root-owned world-writable sticky `/tmp` is accepted for **disposable tests only**.
- An existing, Broker-owned **0600 regular single-link** `broker-readonly-listener.lock` in the socket directory, and an **absent socket pathname**. The listener never creates or replaces the lock, silently removes stale sockets, or accepts a pre-existing name.
- An intended socket path below the checked Unix path-length limit, owned by the Broker, with final mode **0660** and designated IPC group. A live listener may only change that socket's group to the already-selected GID; it cannot chown to a new UID or install a new IPC policy.

`BrokerUnixReadOnlyListener.open()` obtains an exclusive nonblocking `flock` on the preprovisioned lock, pins the parent directory and lock inode/owner/mode, checks socket absence, creates and binds one AF_UNIX SOCK_STREAM pathname, verifies the socket inode, ownership, group and mode, and starts listening with a bounded backlog. A second listener or stale path is rejected, not replaced.

`serve_once()` holds an in-process gate, verifies the pinned parent, lock and socket immediately **before and after** accepting at most one connection, and calls the B2 session's `_serve_connected` method, which does **not** close the descriptor. The listener has **sole ownership of closing the accepted socket**. Any indeterminate accepted-descriptor close fails and poisons the listener even if a response was transmitted; a second close cannot establish successful cleanup. The session independently checks the **connected** kernel `SO_PEERCRED` Owner UID/GID before reading request bytes; it supports only `credential.observe` and `operation.finality`. B3 adds no mutations, retry, Provider/Vault access, receipt issuance, or terminal effect authority. An explicit empty accept expires after a bounded interval and returns false; there is no listener-run loop.

`close()` first closes the listener socket, then checks that the directory, named lock and socket still match the originally pinned inodes and policy before unlinking **only the originally bound socket** by its pinned directory fd and syncing the directory. Any uncertainty about close, inode, lock, parent identity or cleanup blocks successful acknowledgement and poisons the instance **before releasing the in-process gate**: this prevents a competing open from rebinding the socket between an indeterminate cleanup and the failure latch. **No automatic removal of a foreign/stale socket**. A separately authorized operator must inspect an indeterminate state. Only disposable test code may clean its own scratch directory.

## Verified synthetic-only cases

`tests/test_472_broker_readonly_listener_d1b1b3.py` uses isolated temporary directories and Broker-private SQLite, **no useradd/chown of host resources, service setup, credentials, Provider, or Vault**. It checks exact directory/socket mode and inode, pre-provisioned lock, lock contention, empty bounded accept, read-only A0-compatible credential/finality responses, forbidden PUT, actual same-UID kernel peer rejection, wrong modes, absent/hardlinked/symlinked/replaced lock, stale existing socket, renamed/replaced socket, replaced parent, symlink ancestors, invalid paths, process identity change, close-while-handling concurrency, bind chmod failure, uninspectable post-bind inode, failed socket close and socket race between preflight and accept, and synthetic indeterminate accepted-peer close that must fail rather than return success. A separate B2 direct-session test requires its own close failure to propagate as a fixed error. A deterministic failed-setup test injects post-bind socket chmod failure followed by cleanup directory fsync failure and inspects the latch **at gate release**; the test failed against the pre-correction implementation and passes only when the latch is set while the gate is still held.

Positive Broker→Owner observation requests **mock the expected connected Owner UID/GID**, since the local test account has only one real UID. The negative same-UID case uses real kernel `SO_PEERCRED`. **An actual distinct-UID positive acceptance test is NOT completed or implied**, nor does SO_PEERCRED authenticate a trusted executable, forwarded FD origin, approved Owner Claim or authenticated Provider result. A future disposable-root-capable environment may independently validate true distinct UIDs and socket access without user/group installation, but this must be authorized and documented separately.

## Future gates, not included here

- Real Broker-owned socket/UID/GID/group/ACL + Owner client acceptance on disposable isolated OS principals with pinned executable provenance and no FD forwarding; privilege/capability and service lifecycle review.
- Owner-side exact signed Claim and immutable Plan validation, stable Owner-held effect flock, Broker-exclusive Provider credential custody and egress.
- Durable Provider CAS/PUT and idempotency, operation-specific no-effect/effect finality, uncertainty and restart recovery, power-loss tests, audited live 98-System canary.
- No `verified_no_effect` Broker-local receipt from B1/B2/B3 is Provider proof, Owner Journal settlement permission or Writer lease.

This component may be merged as source without activating any of these behaviors. Real deployment and Provider writes require separate explicit authorization.
