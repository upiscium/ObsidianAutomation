# Core Promotion Production Wiring v0

## Purpose

This document wires the deterministic Core Promotion Plan/Transport into a production service without moving private credentials to GitHub or widening existing Snapshot/AI credentials.

The complete convergent topology is:

```text
GitHub ObsidianCore
  reviewed managed-path PR merge
        ↓ public fetch
private promotion service
  checkpoint -> plan -> conditional WebDAV
        ↓
Nextcloud Live Vault
        ↓ read-only snapshot
private Gitea ObsidianVault
        ↓ guarded publication
GitHub ObsidianCore
        ↓
projection acknowledgement / convergence
```

## Critical publication race

Without an additional guard, the following race is possible:

```text
Core managed PR merge: A -> B
        ↓
Live Vault is still A
        ↓
normal Vault snapshot/publication runs before Promotion
        ↓
old publication would write A back to Core
```

That would erase the reviewed Core change before the promotion service sees it.

`obsidian-public-publish` therefore has a convergence guard in this production stage.

It finds the last generated public-projection commit and computes the net managed-path drift from that projection to current Core HEAD.

```text
Vault projection differs from Core
AND Core has unacknowledged managed drift
    -> fail closed; do not mutate/commit Core

Vault projection equals Core
AND Core has unacknowledged managed drift
    -> create empty generated acknowledgement commit

Core drift is repository-owned only
    -> normal Vault publication remains allowed
```

Future generated publication commits contain:

```text
Obsidian-Projection: v1
```

as a commit-message marker. Legacy commits with exact subject:

```text
Sync public projection from ObsidianVault
```

remain accepted as the initial projection baseline.

This gives the following sequence after a reviewed Core change:

```text
Core A -> reviewed B
publication before Promotion -> BLOCKED
Promotion writes B to Live Vault
snapshot sees B
publication sees Vault == Core B
publication creates empty acknowledgement commit
future Vault publications proceed from acknowledged B
```

## Production identity

Use a dedicated unprivileged Linux account:

```text
obsidian-core-promoter
```

Do not run the service as:

- the Gitea Actions runner;
- the Snapshot user;
- any AI Writer identity;
- root.

The service needs:

- outbound HTTPS to public GitHub;
- outbound HTTPS to the configured Nextcloud WebDAV endpoint;
- read/write access only to `/var/lib/obsidian-core-promotion`;
- read access to its pinned application files and `/etc/obsidian-core-promotion`.

## Dedicated Nextcloud credential

Create a dedicated Nextcloud user/service account such as:

```text
obsidian-core-promoter
```

Share only the Obsidian Vault folder with that account and grant the file mutation permissions required for reviewed Core changes:

```text
read
create
update
delete
```

Do not grant sharing or administrative authority.

Generate a dedicated app password. Do not reuse:

- the read-only Snapshot credential;
- the AI Sync Read/Create credential;
- the user's primary password.

Store the app password only in:

```text
/etc/obsidian-core-promotion/nextcloud.password
```

with access restricted to the promotion service identity.

## Required revision pinning

The Gitea public-projection workflow and the promotion service must use the same reviewed `ObsidianAutomation` revision/policy.

Before enabling the promotion timer:

1. merge the production-wiring change;
2. set Gitea `OBSIDIAN_AUTOMATION_REF` to that immutable merge commit;
3. allow/verify the guarded publisher revision is active;
4. deploy the same immutable revision to the promotion service;
5. copy the exact `configs/public-export.example.toml` from that revision to the private promotion configuration;
6. bootstrap the promotion checkpoint;
7. enable the timer.

Do not enable Core promotion while the Gitea publisher is still running the unguarded pre-Promotion revision.

## Recommended filesystem layout

```text
/opt/obsidian-core-promotion/
├── ObsidianAutomation/       pinned public checkout
└── venv/                     installed package

/etc/obsidian-core-promotion/
├── promotion.env             non-secret URL/user only
├── public-export.toml        exact policy bytes from pinned revision
└── nextcloud.password        app password only

/var/lib/obsidian-core-promotion/
├── ObsidianCore/             public GitHub cache
├── checkpoint.json           private ordered promotion checkpoint
├── plans/                    immutable content-addressed plans
├── receipts/                 immutable successful receipts
└── promotion.lock            complete-transaction lock
```

The state root is owned by `obsidian-core-promoter` and mode `0700`.

## Installation example

Create the service identity:

```bash
useradd \
  --system \
  --user-group \
  --home-dir /var/lib/obsidian-core-promotion \
  --no-create-home \
  --shell /usr/sbin/nologin \
  obsidian-core-promoter

install -d -o obsidian-core-promoter -g obsidian-core-promoter -m 0700 \
  /var/lib/obsidian-core-promotion
install -d -o root -g obsidian-core-promoter -m 0750 \
  /etc/obsidian-core-promotion
install -d -o root -g root -m 0755 \
  /opt/obsidian-core-promotion
```

Install an immutable `ObsidianAutomation` revision:

```bash
AUTOMATION_SHA='<merged production-wiring commit>'

git clone https://github.com/upiscium/ObsidianAutomation.git \
  /opt/obsidian-core-promotion/ObsidianAutomation
git -C /opt/obsidian-core-promotion/ObsidianAutomation \
  checkout --detach "$AUTOMATION_SHA"

python3 -m venv /opt/obsidian-core-promotion/venv
/opt/obsidian-core-promotion/venv/bin/pip install --no-deps \
  /opt/obsidian-core-promotion/ObsidianAutomation
```

Install the exact policy bytes from the same revision:

```bash
install -o root -g obsidian-core-promoter -m 0640 \
  /opt/obsidian-core-promotion/ObsidianAutomation/configs/public-export.example.toml \
  /etc/obsidian-core-promotion/public-export.toml
```

Create `/etc/obsidian-core-promotion/promotion.env` from `examples/promotion/promotion.env.example` and install it mode `0640`, owned by `root:obsidian-core-promoter`.

Write only the dedicated app password to `nextcloud.password` and install it mode `0640`, owned by `root:obsidian-core-promoter`.

## Production runner

`obsidian-core-promotion-run` owns the full recurring transaction.

It does the following under one exclusive file lock:

1. create or verify a local `ObsidianCore` cache;
2. require its origin to be exactly the canonical public Core repository;
3. require a clean working tree;
4. fetch only `origin/main` into `refs/remotes/origin/main`;
5. load and validate the private checkpoint;
6. require exact public-export policy SHA equality;
7. return `up_to_date` before reading the password or contacting WebDAV when checkpoint == fetched head;
8. build a deterministic Promotion Plan from checkpoint to fetched head;
9. persist the plan content-addressed under `plans/`;
10. run Promotion Transport v0;
11. persist the receipt;
12. advance the checkpoint only after complete remote verification.

The production CLI does not expose a Core repository URL override. Public GitHub Core is the fixed source.

## Bootstrap

Bootstrap is deliberately separate from recurring execution:

```text
obsidian-core-promotion-bootstrap
```

It fetches the canonical Core repository itself and requires the supplied baseline commit to:

- be a full exact commit SHA;
- exist in the fetched repository;
- be an ancestor of current `ObsidianCore/main`;
- have a subject beginning with `Sync public projection from ObsidianVault`;
- bind the exact current public-export policy SHA.

It refuses to overwrite an existing checkpoint.

Choose a generated projection commit known to correspond to the current Live Vault/publication baseline. Do not bootstrap from a reviewed Core-only PR commit merely because it is current HEAD.

Example:

```bash
runuser -u obsidian-core-promoter -- \
  /opt/obsidian-core-promotion/venv/bin/obsidian-core-promotion-bootstrap \
    --core-commit '<known generated projection commit>'
```

Then run one manual no-op check before enabling the timer:

```bash
systemctl start obsidian-core-promotion.service
journalctl -u obsidian-core-promotion.service -n 100 --no-pager
```

The expected initial result is usually:

```json
{"result":"up_to_date"}
```

unless reviewed Core commits exist after the selected generated baseline.

## systemd

Copy:

```text
examples/promotion/obsidian-core-promotion.service
examples/promotion/obsidian-core-promotion.timer
```

into `/etc/systemd/system/`, then:

```bash
systemctl daemon-reload
systemctl enable --now obsidian-core-promotion.timer
```

The example timer polls every five minutes with a small randomized delay. Public Core fetching requires no GitHub write credential or token.

The service uses `StateDirectory=obsidian-core-promotion` and a hardened one-shot sandbox. It does not share the generic Gitea runner identity.

## Failure behavior

### GitHub unavailable

No plan or remote mutation occurs. Checkpoint remains unchanged.

### Core history rewritten / checkpoint not ancestor

Promotion Plan fails closed. Human investigation is required.

### publication races before Promotion

The guarded publisher refuses to overwrite unacknowledged managed Core drift.

### remote preflight conflict

No WebDAV mutation occurs. Checkpoint remains unchanged.

### partial WebDAV completion

Checkpoint remains at the previous Core commit. The next run converges already-applied paths and retries remaining base-state paths.

### policy drift

The runner fails before the `up_to_date` short circuit or any WebDAV mutation. Production policy changes require explicit checkpoint/policy migration rather than silent rebinding.

## Production E2E

After installation, use a harmless managed-path PR in `ObsidianCore` and verify:

```text
1. PR merge appears at Core/main.
2. Gitea publication cannot revert the unpromoted managed change.
3. promotion runner produces a content-addressed plan and receipt.
4. exact bytes appear in Nextcloud Live Vault.
5. normal read-only snapshot commits the Live state to Gitea ObsidianVault.
6. guarded publication sees Vault == Core and writes a projection acknowledgement if needed.
7. subsequent promotion run reports up_to_date.
```

Then test deletion of the same harmless file through a second reviewed Core PR to cover both create and delete production paths.
