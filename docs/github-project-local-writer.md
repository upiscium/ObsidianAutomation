# GitHub Project local writer topology

## Purpose

GitHub-backed Project automation is self-contained in the dedicated `obsidian-github-sync` LXC while keeping observation and canonical-write credentials separated by Unix identity.

```text
obsidian-github-mirror
  Nextcloud read-only credential
          |
          v
/srv/obsidian-github-sync/vault/10-Project
          |
          v
obsidian-github-sync
  GitHub read-only observation
  deterministic status + overview proposals
          |
          v
/var/lib/obsidian-github-pipeline/25-Execution
          |
          v
obsidian-github-writer
  dedicated canonical 10-Project writer credential
  status-only Project CAS
  sibling Status.md create/update CAS
          |
          v
/var/lib/obsidian-github-pipeline/27-Transport
```

The existing AI Writer LXC and `obsidian-ai-sync` authority are not part of this path.

## Nextcloud authority

Use the dedicated Nextcloud account `obsidian-github-writer` and share only canonical `10-Project` with:

```text
Read   = yes
Update = yes
Create = yes
Delete = no
Share  = no
```

The share permission bitmask is `7` (`Read=1 + Update=2 + Create=4`).

`Update` is required for the existing Project `status:` CAS. `Create` is required only to create a missing sibling `Status.md`; the automation exposes no canonical delete operation. Do not grant access to unrelated Vault roots.

When `10-Project` is shared directly, the DAV root is normally:

```text
https://<nextcloud>/remote.php/dav/files/obsidian-github-writer
```

and canonical paths remain:

```text
10-Project/<project>/<project>.md
10-Project/<project>/Status.md
```

Verify the actual share mount name before production use.

The mirror account remains read-only. The existing AI Writer credential remains unchanged for its own independent pipeline.

## Unix identities and handoff group

The credential groups remain separate. `obsidian-github-pipeline` carries only local request/result handoff access and contains no credentials.

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

Local pipeline roots:

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

Intended local authority:

- `obsidian-github-sync`: owner-write `25-Execution`; cannot write `27-Transport`;
- `obsidian-github-writer`: read-only `25-Execution`; owner-write `27-Transport` and `24-Locks`;
- `obsidian-github-mirror`: no pipeline-state access;
- `obsidian-github-pipeline`: no credential files.

## Writer credential

Store the dedicated App Password at:

```text
/etc/obsidian-github-writer/webdav-password
```

with:

```bash
chown root:obsidian-github-writer /etc/obsidian-github-writer/webdav-password
chmod 0640 /etc/obsidian-github-writer/webdav-password
```

Configure `/etc/obsidian-github-writer/config.env` from `examples/github-sync/writer-config.env.example`, then:

```bash
chown root:obsidian-github-writer /etc/obsidian-github-writer/config.env
chmod 0640 /etc/obsidian-github-writer/config.env
```

## Proposal types

The watcher side produces two deterministic local request types from one GitHub observation.

### Project status proposal

Only a pending status transition is content-addressed:

```text
25-Execution/<proposal-sha256>.github-status.json
```

The status worker fresh-GETs the canonical Project, verifies binding and expected status, changes only the top-level `status:` line with strong ETag `If-Match`, and exact-byte verifies the result.

### Project overview desired state

Every watched Project has one stable desired-state request:

```text
25-Execution/<sha256(project-path)>.github-overview.json
```

It contains the current open Issue/PR numbers and titles from the same GitHub snapshot already used by the watcher. The overview worker fresh-GETs canonical state and maintains sibling `Status.md` using conditional create/update. See `docs/github-project-overview.md` for the managed-block and checkbox-preservation contract.

## systemd cycle

The timer targets the writer unit. Dependencies impose:

```text
obsidian-github-sync.timer
  -> obsidian-github-writer.service
       -> obsidian-github-sync.service
            -> obsidian-github-sync-vault-pull.service
```

One activation therefore performs:

```text
remote Project mirror refresh
  -> one GitHub observation
  -> status + overview local enqueue
  -> Project status worker
  -> Status.md overview worker
```

Install/update the reusable units with:

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

## Credential-isolation gate

Before enabling canonical updates:

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

Stop the timer for the first deployment:

```bash
systemctl disable --now obsidian-github-sync.timer
systemctl start obsidian-github-writer.service
systemctl show obsidian-github-writer.service -p Result -p ExecMainStatus
journalctl \
  -u obsidian-github-sync-vault-pull.service \
  -u obsidian-github-sync.service \
  -u obsidian-github-writer.service \
  -n 120 --no-pager
```

A first overview cycle for a Project with no `Status.md` should report `outcome=created`. Subsequent cycles with unchanged GitHub and unchanged canonical rendering should settle to `already_desired`.

Test at least one checkbox and a note edit before enabling the timer. Both must survive the next overview refresh.

Then enable production recurrence:

```bash
systemctl enable --now obsidian-github-sync.timer
```

## Security boundary

This is process/Unix-identity isolation inside one LXC, not host-level isolation. LXC root can reach all three credentials. The accepted boundary is that the watcher process has no canonical writer credential, while the writer process has no GitHub or mirror credential.
