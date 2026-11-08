# #472 D1/B1/B4 — Disposable real-kernel Owner↔Broker UID/GID acceptance

**Scope: test/docs-only, local-only, OFF by default.** Based on merged #567 at `main@e815709b5fe5087f7bbe65c05431a3c2e98a81fa`. No Commit/Push/PR/Ready/Merge, host principal or ACL change, systemd service, production socket, credential installation, Provider/Nextcloud/Vault I/O, Writer, deployment or activation.

## Why this gate matters

B2/B3's successful Owner observations previously mocked the kernel-connected `SO_PEERCRED` UID/GID. They proved request/response behavior but **not real distinct-process identity enforcement**. D1/B1/B4 adds an explicitly invoked, disposable integration test of the existing B1/B2/B3 source **without changing runtime modules**.

The test uses real Linux `fork`/`setresuid`/`setresgid`/`setgroups` and genuine `SO_PEERCRED`, never monkeypatching the connected peer or bypassing the Broker's authenticated read-only handler. It tests:

- Broker effective UID **21001**, Owner effective UID **21002**, unrelated UID **21003**, shared restricted IPC GID **21004**, and wrong primary Owner GID **21005**. These IDs exist **only inside a fresh user namespace**, mapping to pre-existing subordinate host IDs, not registered host users.
- A pre-provisioned Broker-owned 0710 IPC directory and 0600 lock, a separate Broker-private 0700 synthetic SQLite directory, and a Broker-owned 0660 Unix socket.
- Owner's A0 client can observe the nonsecret credential generation, a pending exact operation without a receipt, and a deterministic **local never-dispatched** `verified_no_effect` receipt. An unknown account is denied.
- An attacker UID with a valid IPC primary GID can connect to the socket, but receives **no Broker data**. The correct Owner UID with a wrong primary GID, while retaining IPC GID as a supplementary group, can also connect but receives **no Broker data**. This specifically exercises the independent UID and *primary* GID checks beyond filesystem ACLs.
- A denied socket can be closed before the client finishes `sendall` or `shutdown`; either EOF or connection reset / broken pipe is acceptable **only when no response bytes are received** and the listener acknowledges one accepted connection.

## Bounded execution on Adam

The verified environment has unprivileged user namespaces and an existing subordinate UID/GID mapping, with helpers `newuidmap` and `newgidmap`. Run explicitly in an **unprivileged** fresh user, mount, network and PID namespace:

```bash
cd /home/upiscium/Documents/Programs/PROJECT-OBSIDIAN-472-R2B2C-D1B1B4-UIDNS-ACCEPTANCE-20261108

nix develop "path:$PWD" --command \
  unshare --user --map-root-user --map-auto \
    --mount --net --pid --mount-proc --propagation private \
    --fork --kill-child=SIGKILL \
    env OA472_DISPOSABLE_UIDNS=1 PYTHONDONTWRITEBYTECODE=1 \
    python -m pytest -q -p no:cacheprovider \
    tests/test_472_broker_uidns_acceptance_d1b1b4.py
```

The test **refuses real host root**: its guard requires an explicit environment flag, namespace UID0 mapped to a *nonroot* host user, separate subordinate UID/GID ranges, and the test runner being **PID 1 inside its disposable PID namespace**. PID 1 is necessary to adopt/reap any orphaned Broker children after a coordinator failure; the guard disallows a partial namespace configuration that cannot provide that guarantee. Ordinary nonroot CI runs the guard tests and skips the privileged-role exercise. A machine without suitable subordinate mappings/mount namespaces does **not** get to claim a real cross-UID PASS.

The test creates a randomly named, private scratch root under `/tmp` owned by the initiating ordinary host account. Only inside its *private mount namespace* does it bind `/proc` and read-only-in-practice `/nix/store` into that scratch chroot, to support the existing SQLite FD verifier and Python runtime. It then forks a chrooted test coordinator and role processes, drops each role to a different namespace UID/GID, and uses AF_UNIX socket/control pipes. There is no TCP/HTTP service, network is isolated, no host user creation or systemd modification, and on a verified successful or safely failed test the disposable processes, mount namespace and scratch filesystem are cleaned up. On an **unreapable descendant**, the test aborts its PID namespace instead of performing unverified filesystem cleanup; the isolated scratch path may remain for manual inspection. The Broker may write its own **disposable synthetic SQLite records** only, never Provider/Vault state.

The Broker listener's request handling remains explicitly bounded in the test (no autonomous production accept loop); the coordinator sends one request trigger per acceptance. Failure paths use a coordinator-specific process group (established before any Broker fork), `SIGKILL` the entire group on timeout/error, and have the guarded PID-namespace init **reap the coordinator plus adopted Broker/Owner descendants before any bind unmount**. A bounded fault-injection trace deliberately hangs both coordinator and a forked Broker child, times out quickly, and requires no remaining child/zombie afterward. If a descendant cannot be reaped within the bounded cleanup deadline (e.g. uninterruptible filesystem I/O), the PID-namespace init performs **fatal `os._exit(78)`**, deliberately bypassing all Python `finally`, socket unlink, bind unmount and automatic `TemporaryDirectory` cleanup; this is a failed test and a possible leftover isolated scratch path requiring separate trusted operator inspection. Never treat incomplete cleanup as a PASS or proceed to ordinary teardown. A deterministic synthetic failure test intercepts the fatal exit to verify the branch without killing the test runner; a real uninterruptible kernel task is not induced. The test never attempts to clean or change unrelated host resources.

## Remaining limitations

This acceptance is **real kernel UID/GID separation only inside the isolated user namespace**. It does *not* prove production OS user/group configuration, actual installed service/executable identity, forwarded-FD prevention or service sandbox, Broker-exclusive credential custody, Provider-only egress, Owner-signed Plan/Claim and stable effect lock, provider idempotent CAS/PUT or operation-specific remote finality, power-loss recovery, or a production Vault canary.

In particular, a Broker-private `verified_no_effect` record is **not** a Provider-origin proof that no operation happened. It must not authorize Owner Journal settlement or Writer execution. Those remain separately gated after review of the actual deployment and effect path.
