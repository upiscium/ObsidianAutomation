# Spike #308: Authenticated Nextcloud → rclone UDS gateway and conditional writes

- **Recorded:** 2026-10-11 (Asia/Tokyo)
- **Status:** Authenticated disposable functional tests PASS, **production qualification NOT granted**
- **Environment:** Adam non-production scratch, Nextcloud 35.0.1 / PHP 8.4.26 / SQLite / 127.0.0.1:31847; rclone 1.75.1
- **Scope:** Only a test account `fixtureowner` and a uniquely named `ObsidianVault-fixture-308` folder.
- **Related:** [#308](https://github.com/upiscium/ObsidianAutomation/issues/308), [#299](https://github.com/upiscium/ObsidianAutomation/issues/299), [ADR-0001](../adr/0001-single-host-management-dashboard.md), [anonymous baseline](308-nextcloud35-loopback-fixture.md)

## Isolation and credential preparation

Nextcloud 35.0.1 was previously installed from an SHA-256-verified official source archive into a disposable scratch directory. The PHP development server was bound **only** to localhost and was not TLS-enabled; no production Nextcloud endpoint or secrets were used.

A local Python script generated a fresh, random, test-only password in memory, passed it via `NC_PASS` to `occ user:resetpassword --password-from-env fixtureowner`, and wrote an `0600` rclone WebDAV config. No plaintext was printed. `rclone obscure` in this config is **reversible obfuscation, not secure encryption**; protection comes from file/OS permissions. Credentials were never checked into Git.

## Authenticated direct rclone test

The client remote configuration bound `vendor=nextcloud` to `http://127.0.0.1:31847/remote.php/dav/files/fixtureowner`.

1. `rclone lsf fixture-nextcloud:` successfully listed the fixture account's default files.
2. `rclone copy` uploaded synthetic `00-DailyNote`, `04-AI/50-Review`, `10-Project`, `11-Knowledge` files into the dedicated `ObsidianVault-fixture-308` remote subtree.
3. `rclone sync fixture-nextcloud:ObsidianVault-fixture-308 <local> --filter-from <AI filter> --delete-after --delete-excluded` populated **three** permitted local files and omitted `04-AI`. `rclone check --download --one-way` returned **0 differences / 3 matching**.
4. The actual remote `04-AI/50-Review` file remained. The account/credential could still list it, confirming that *rclone client filters are not remote authorization*.

## Nextcloud-backed Unix-domain WebDAV gateway

A separate process held the only real Nextcloud test credential and ran:

```sh
rclone serve webdav fixture-nextcloud:ObsidianVault-fixture-308/10-Project \
  --config <secret-rclone-config> \
  --addr unix://<scratch>/project-gateway.sock \
  --read-only --dir-cache-time 0s --poll-interval 0s
```

A client used only an independent nonsecret config:

```ini
[nc-project-gateway]
type = webdav
url = http://localhost
vendor = rclone
unix_socket = /scratch/project-gateway.sock
```

Observed results:

| Check | Evidence |
| --- | --- |
| Socket permissions | `0700` in a private scratch dir |
| List and read `Project.md` | Exactly matched direct Nextcloud bytes |
| `/../04-AI/50-Review/review.md` attempt | HTTP 404 |
| PUT via read-only Unix DAV server | HTTP 404; no new file |
| External direct Nextcloud update of Project.md | Gateway sync downloaded updated bytes, then `rclone check --download` found 0 differences (2 matching files while transient test file existed) |
| External direct Nextcloud deletion of `obsolete.md` | Gateway sync removed local copy, exact check found 0 differences / 1 matching |

This confirms feasibility of a **small authenticated Nextcloud-backed read-only rclone UDS facade**, not a general authenticated per-role gateway. All processes ran under the *same* Unix user. No cross-UID or adversarial symlink tests, concurrency, large corpus, cache restart recovery, or real-world throughput measurements were performed. The cache-off flags addressed an earlier reproduced local VFS freshness issue; cache semantics need a reviewed long-running design.

## Critical: generic `rclone copyto` is unsafe for conditional canonical creation

**Reproduced on three synthetic test files**, once initially and twice as separate repeat cases:

1. An existing remote file was present and readable.
2. A second `rclone copyto <different local> fixture-nextcloud:<same path> --header-upload 'If-None-Match: *' --retries 1 --low-level-retries 1` was run.
3. Nextcloud properly responded to the conditional PUT with **HTTP 412 Precondition Failed**.
4. The rclone command returned nonzero **and the previously existing remote file was now missing**.
5. Local PHP HTTP logs for `repeat-conditional-3.md` demonstrated:

```text
GET     /.../repeat-conditional-3.md  → 200
PUT     /.../repeat-conditional-3.md  → 412
DELETE  /.../repeat-conditional-3.md  → 204
PROPFIND /.../repeat-conditional-3.md → 404
```

**Inference supported by direct request log:** rclone's upload failure cleanup sent a `DELETE` after a rejected `PUT`, thereby deleting a pre-existing file. This is a destructive transport behavior in the tested version/configuration, *not* a failure of Nextcloud's HTTP 412 enforcement. Never implement the canonical create-only `If-None-Match` contract by adding a generic rclone upload header. Preserve the repository's dedicated `webdav_create.py`/typed transport with exact readback. Do not assume all versions behave this way; regression tests must pin the behavior under the target versions.

## Direct HTTP WebDAV CAS contrast (PASS)

A test-only Python `http.client` implementation used the same fixture account over loopback, issuing direct HTTP requests and **no rclone upload pipeline or cleanup**. The following results were observed for `10-Project/http-cas-test.md`:

| Request | HTTP | Remote byte integrity |
| --- | --- | --- |
| GET before create | 404 | Missing as expected |
| PUT with `If-None-Match: *` | 201 | Initial bytes exactly present via subsequent GET |
| Repeated PUT with `If-None-Match: *` | 412 | Initial bytes still exactly present |
| PUT with current strong `If-Match` ETag | 204 | New bytes exactly present |
| PUT with stale `If-Match` ETag | 412 | New bytes still exactly present |

The initial ETag was strong (not `W/`). The direct HTTP test concluded `DIRECT_WEBDAV_CAS_FIXTURE_PASS` without any compensating `DELETE`. This validates Nextcloud 35's conditional semantics in the disposable fixture; it does **not** establish whether a new Gateway preserves them without extensive further tests.

## Architecture consequences for #302

- **One Nextcloud account is achievable.** The user-facing credential-count goal does not require a mandatory new Gateway in Standard v1.
- **Simplest Standard implementation:** GUI central secret storage and validated connection profiles, with the same account credential delivered to only the already remote-facing OS identities. Minimal rewriting, but each recipient's remote blast radius is broad and must be disclosed. Strict retains multi-account remote-side ACLs.
- **Optional hardening:** an owner-only credential and scoped read-only Unix DAV sockets can be used for rclone *reads*; it may be valuable if untrusted roles must not read the credential or other Vault folders. It adds separate availability, freshness, locking and maintenance obligations.
- **Canonical mutations remain typed HTTP/CAS:** never funnel create/update/delete through unrestricted rclone `copyto`. Review approval/receipt/path ownership, strong ETags, and `If-None-Match` semantics must survive the one-account change.
- The product requirement is one operator-configured connection, not one OS user or one monolithic process. The internal choice must balance security guarantees, code/maintenance cost and rollback complexity.

## Open qualification gates

- True distinct Unix service identities, private socket/credential access control, cross-root negative tests, symlink/path normalization and method restrictions;
- multiple Vaults, concurrency and long-running real Nextcloud freshness/invalidations;
- outage, interrupted sync, large data performance and crash recovery;
- explicit independent Security/Correctness verdict on the final ADR and production implementation;
- no systemd or production LXC cutover before separate operator acceptance.

**No production Nextcloud account, private Vault, LXC, Gitea server or production runtime was changed.**
