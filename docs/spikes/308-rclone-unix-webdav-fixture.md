# Spike #308 — rclone WebDAV over a local Unix socket

- **Status:** Disposable feasibility evidence / partial pass; not a production acceptance
- **Date:** 2026-10-11
- **Evidence location:** Adam scratch checkout of ObsidianAutomation (outside production paths)
- **Version:** rclone 1.75.1, obtained through Nix
- **Related:** #298, #299, #302, #308, ADR-0001

## Scope and limits

This test used **only synthetic local files**. The source was an ordinary local directory exported by `rclone serve webdav`; it was **not a Nextcloud server**. No production Nextcloud, AI, Gitea, GitHub credentials, private Vault content, running LXC or systemd production unit was used.

The purpose was to determine whether the existing rclone CLI structure (`sync named-remote: local --filter-from ... --delete-after --delete-excluded`) can read through a Unix-domain WebDAV endpoint without exposing a remote credential to the calling mirror job.

## Fixture

Five source files were constructed under `source/ObsidianVault`:

- `00-DailyNote/2026/10/2026-10-11.md` (human-edited-style text)
- `04-AI/50-Review/review.md` (excluded)
- `05-Idea/seed.md`
- `10-Project/Project.md`
- `11-Knowledge/日本語.md`

A mirror filter included Daily, Idea, Project and Knowledge roots, excluding all others. The rclone client config contained **no remote password**:

```ini
[fixture-gw]
type = webdav
url = http://localhost
vendor = rclone
unix_socket = /scratch/fixture/gateway.sock
```

The server was started with `umask 077`:

```sh
rclone serve webdav "$FIXTURE/source" \
  --addr "unix://$FIXTURE/gateway.sock" \
  --read-only \
  --dir-cache-time 0s --poll-interval 0s
```

Client (full command shape preserved):

```sh
rclone sync fixture-gw:ObsidianVault "$FIXTURE/mirror" \
  --config "$FIXTURE/client-rclone.conf" \
  --filter-from "$FIXTURE/ai-filter" \
  --delete-after --delete-excluded

rclone check fixture-gw:ObsidianVault "$FIXTURE/mirror" \
  --config "$FIXTURE/client-rclone.conf" \
  --filter-from "$FIXTURE/ai-filter" --download --one-way
```

## Observed results

| Test | Observed |
| --- | --- |
| Local WebDAV client using Unix socket | PASS, named `fixture-gw:` remote |
| Initial filtered download | Four files, 98 B total |
| Initial exact downloaded comparison | 0 differences, 4 matches |
| Remote `04-AI` exclusion | Local 04-AI absent; source file still present |
| Unfiltered `rclone lsf -R` on the socket | Could still list `04-AI/50-Review/review.md`; the client-side filter is **not** an access restriction |
| Socket permissions | `0700`; `chmod 000` caused client listing failure |
| Synthetic remote write attempt | CLI failure; direct PUT HTTP 404; source file not created |
| Modification/deletion behind default WebDAV serve | **STALE**: second sync incorrectly reported nothing to transfer; exact check detected 2 differences |
| Server restarted with `--dir-cache-time 0s --poll-interval 0s` | Project file updated, deleted Idea removed locally |
| Final exact downloaded comparison | 0 differences, 3 matches |

The default `rclone serve webdav` help reports a **5 minute VFS directory cache**. A refresh that changes its upstream data behind the VFS can therefore be stale even when the calling `rclone sync` exits successfully. Disabling the server's directory cache resolved this specific synthetic reproduction but is not by itself proof of correct freshness or acceptable performance in production.

The `--read-only` flag and local Unix socket provided a non-mutating fixture. **The returned 404 is not an independent authorization proof**; a policy-enforcing Gateway must explicitly deny remote mutation and prove that denial under all tested verbs/scopes.

## Feasibility assessment

**Confirmed**:
- rclone's WebDAV backend exposes `unix_socket` / `--webdav-unix-socket`;
- rclone's WebDAV server can listen at `unix:///path`;
- a credentialless rclone client can successfully sync from a local DAV server over that socket with the existing command structure;
- ACLs on the Unix socket can prevent callers from connecting at all.

**Not established**:
- actual Nextcloud `PROPFIND`, hash, ETag, `OC:` vendor fields, ownership, external shares, partial reads;
- service identities and per-role socket/namespace capabilities;
- whether `rclone serve webdav` proxying a real Nextcloud remote preserves all validation checks (including snapshot double-fetch/check);
- concurrency, lock-order preservation, failure/restart recovery, high-volume performance, mixed permissions;
- canonical writes, CAS, exact-case deletes, and broader Nextcloud credential confidentiality if the Gateway is compromised.

## Architecture alternatives that must be compared

1. **Direct one-credential fan-out:** One operator-managed dedicated Nextcloud service account, root-owned credential store, `systemd LoadCredential=` to service-specific rclone/WebDAV clients. Minimal code change but all credential recipients effectively have broad remote authority.
2. **Gateway-owned mirror pull:** Credential-bearing dedicated worker performs sync and shares read-only verified mirrors. Avoid handing remote secrets to readers; design safe local writes/ACL.
3. **One or more scope-limited local DAV sockets:** Credential-bearing Gateway/sidecars expose read-only or narrower views to existing rclone clients. Socket ACL/peer credential or capability enforcement must be meaningful, not just "localhost".
4. **Strict multi-account:** Keep remote per-share ACL isolation, but provision/manage it behind the same GUI.

Product usability (one visible connection) is possible with more than one implementation. Choose the least complex mechanism whose **stated** security guarantees meet the accepted threat model; do not falsely equate local-only guards and Nextcloud server ACLs.

## Remaining gates

- Exact-version support in target deployment.
- Test against disposable Nextcloud-like upstream and service users, then failure injections.
- Validate freshness and nextcloud semantics; compare hashes and final remote bytes.
- Revalidate canonical writer invariants and `#287` cleanup scope, `#294` approval boundary.
- Independent security/correctness review.
- **No production cutover, credential rotation, user-account deletion or Vault migration permitted by this spike.**

Official technical references: [rclone WebDAV](https://rclone.org/webdav/), [rclone serve WebDAV](https://rclone.org/commands/rclone_serve_webdav/), [systemd Credentials](https://systemd.io/CREDENTIALS/).
