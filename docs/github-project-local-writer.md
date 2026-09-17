# GitHub Project status local writer topology

## Purpose

GitHub-backed Project status synchronization is self-contained in the dedicated `obsidian-github-sync` LXC while retaining process-level authority separation.

The LXC contains three Unix identities:

```text
obsidian-github-mirror
  Nextcloud read-only mirror credential
          |
          v
/srv/obsidian-github-sync/vault/10-Project
          |
          v
obsidian-github-sync
  GitHub read-only observation
  deterministic status proposal
          |
          v
/var/lib/obsidian-github-pipeline/25-Execution
          |
          v
obsidian-github-writer
  dedicated Nextcloud 10-Project update credential
  canonical GET + status-only CAS PUT
          |
          v
/var/lib/obsidian-github-pipeline/27-Transport
```

The existing AI Writer LXC and `obsidian-ai-sync` authority are not part of this path.

## Nextcloud authority

Use a dedicated Nextcloud account such as `obsidian-github-writer`.

Share only the canonical `10-Project` folder to this account with:

```text
Read   = yes
Update = yes
Create = no
Delete = no
Share  = no
```

For the Nextcloud share permission bitmask this is `3` (`1 + 2`). Existing Project files should expose update/write capability through WebDAV. Do not grant this account access to unrelated Vault roots.

The existing mirror account remains read-only. The existing AI Writer credential remains unchanged and create-only for its own pipeline.

When `10-Project` is shared directly to `obsidian-github-writer`, its DAV root is normally:

```text
https://<nextcloud>/remote.php/dav/files/obsidian-github-writer
```

and the watcher Project path remains:

```text
10-Project/<project>/<project>.md
```

Verify the actual share mount name before production use.

## Unix identities and shared handoff group

The credential groups remain separate. A third group carries only local request/result handoff access and never contains credentials.

```bash
groupadd --system obsidian-github-pipeline 2>/dev/null || true

groupadd --system obsidian-github-writer 2>/dev/null || true
if ! id obsidian-github-writer >/dev/null 2>&1; then
  useradd \
    --system \
    --gid obsidian-github-writer \
    --home-dir /var/lib/obsidian-github-writer \
    --create-home \
    --shell /usr/sbin/nologin \
    obsidian-github-writer
fi

usermod -aG obsidian-github-pipeline obsidian-github-sync
usermod -aG obsidian-github-pipeline obsidian-github-writer
```

Create the local pipeline roots:

```bash
install -d -o root -g obsidian-github-pipeline -m 0750 \
  /var/lib/obsidian-github-pipeline

install -d -o obsidian-github-sync -g obsidian-github-pipeline -m 2750 \
  /var/lib/obsidian-github-pipeline/25-Execution

install -d -o obsidian-github-writer -g obsidian-github-pipeline -m 2750 \
  /var/lib/obsidian-github-pipeline/27-Transport

install -d -o obsidian-github-writer -g obsidian-github-writer -m 0750 \
  /var/lib/obsidian-github-pipeline/24-Locks

install -d -o root -g obsidian-github-writer -m 0750 \
  /etc/obsidian-github-writer
```

The intended filesystem authority is:

- `obsidian-github-sync`: owner-write to `25-Execution`; group-read only to `27-Transport`;
- `obsidian-github-writer`: group-read only to `25-Execution`; owner-write to `27-Transport` and `24-Locks`;
- `obsidian-github-mirror`: no access to pipeline state;
- `obsidian-github-pipeline`: no credential files.

## Writer credential

Store only the dedicated GitHub writer App Password in:

```text
/etc/obsidian-github-writer/webdav-password
```

with:

```bash
chown root:obsidian-github-writer /etc/obsidian-github-writer/webdav-password
chmod 0640 /etc/obsidian-github-writer/webdav-password
```

Copy `examples/github-sync/writer-config.env.example` to:

```text
/etc/obsidian-github-writer/config.env
```

and set the real DAV root and username. The file contains no password but should still be bounded to the writer identity:

```bash
chown root:obsidian-github-writer /etc/obsidian-github-writer/config.env
chmod 0640 /etc/obsidian-github-writer/config.env
```

## Proposal queue

`obsidian-github-project-watch-enqueue` runs the normal deterministic watcher and writes only `change=true` / `pending=true` observations to the local request directory.

Each request is canonicalized and content-addressed:

```text
25-Execution/<proposal-sha256>.github-status.json
```

The watcher keeps its normal JSON journal output. If queue persistence fails after SQLite state is updated, the pending proposal is emitted again on the next watcher run and enqueue is retried.

## Writer worker

`obsidian-github-project-status-worker` scans queued requests and verifies the filename against the canonical proposal SHA-256 before using the writer credential.

For each new request it performs the existing status-only CAS transport:

1. fresh canonical GET;
2. Project / repository / `github_watch` / expected status validation;
3. strong ETag requirement;
4. replace only top-level `status:` in fresh canonical bytes;
5. `PUT + If-Match`;
6. exact-byte GET verification;
7. durable result in `27-Transport`.

Successful results are content-addressed by the same proposal digest:

```text
27-Transport/<proposal-sha256>.github-status.transport-result.json
```

Canonical conflicts are terminal for that exact proposal and are persisted separately as:

```text
27-Transport/<proposal-sha256>.github-status.rejection.json
```

Transient transport/authority errors do not create a terminal result, so the service returns failure and the proposal can be retried after the deployment issue is corrected.

## systemd cycle

Install all four reusable units:

```bash
install -m 0644 \
  examples/github-sync/obsidian-github-sync-vault-pull.service \
  /etc/systemd/system/

install -m 0644 \
  examples/github-sync/obsidian-github-sync.service \
  /etc/systemd/system/

install -m 0644 \
  examples/github-sync/obsidian-github-writer.service \
  /etc/systemd/system/

install -m 0644 \
  examples/github-sync/obsidian-github-sync.timer \
  /etc/systemd/system/

systemctl daemon-reload
```

The timer targets the writer unit. Dependencies impose the full order:

```text
obsidian-github-sync.timer
  -> obsidian-github-writer.service
       Requires/After obsidian-github-sync.service
         Requires/After obsidian-github-sync-vault-pull.service
```

Thus one timer activation performs:

```text
remote -> local Project mirror refresh
  -> GitHub observation + durable enqueue
  -> writer-side canonical CAS
```

## Credential-isolation gate

Before enabling canonical updates, verify all three identities:

```bash
sudo -u obsidian-github-sync \
  test -r /etc/obsidian-github-writer/webdav-password \
  && echo 'FAIL: watcher can read writer credential' \
  || echo 'PASS: writer credential isolated from watcher'

sudo -u obsidian-github-writer \
  test -r /etc/obsidian-github-sync/credentials.env \
  && echo 'FAIL: writer can read GitHub credential' \
  || echo 'PASS: GitHub credential isolated from writer'

sudo -u obsidian-github-writer \
  test -r /etc/obsidian-github-mirror/rclone.conf \
  && echo 'FAIL: writer can read mirror credential' \
  || echo 'PASS: mirror credential isolated from writer'

sudo -u obsidian-github-mirror \
  test -r /etc/obsidian-github-writer/webdav-password \
  && echo 'FAIL: mirror can read writer credential' \
  || echo 'PASS: writer credential isolated from mirror'
```

Also verify local handoff direction:

```bash
sudo -u obsidian-github-writer \
  test -r /var/lib/obsidian-github-pipeline/25-Execution \
  && echo 'PASS: writer can read requests'

sudo -u obsidian-github-writer \
  test -w /var/lib/obsidian-github-pipeline/25-Execution \
  && echo 'FAIL: writer can rewrite requests' \
  || echo 'PASS: requests are read-only to writer'

sudo -u obsidian-github-sync \
  test -w /var/lib/obsidian-github-pipeline/27-Transport \
  && echo 'FAIL: watcher can forge transport results' \
  || echo 'PASS: transport results are read-only to watcher'
```

## Canary

Before enabling the timer, run one explicit cycle:

```bash
systemctl start obsidian-github-writer.service
systemctl show obsidian-github-writer.service -p Result -p ExecMainStatus
journalctl \
  -u obsidian-github-sync-vault-pull.service \
  -u obsidian-github-sync.service \
  -u obsidian-github-writer.service \
  -n 100 --no-pager
```

For a stale `running -> planning` Project, the first successful cycle should show:

```text
mirror refreshed
project-status-enqueued
writer completed outcome=applied
```

The next cycle should refresh the mirror, observe canonical `planning`, and stop emitting that transition.

Only after this canary passes should the recurring timer be enabled:

```bash
systemctl enable --now obsidian-github-sync.timer
```

## Security boundary

This design provides process/Unix-identity isolation inside one LXC, not host-level isolation. Root compromise of the LXC can reach all three credentials. The accepted boundary is therefore:

> the watcher process has no canonical writer credential, while the dedicated writer process has no GitHub or mirror credential.

If host-level compromise isolation becomes a requirement, the writer identity can later be moved into a separate LXC without changing the proposal/result contracts.
