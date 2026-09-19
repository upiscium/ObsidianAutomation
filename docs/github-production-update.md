# Obsidian GitHub production update transaction

## Purpose

Repeated production deployments of the Obsidian GitHub integration use a single
reviewed target commit instead of an implicit "latest main" update.

The normal operator entrypoint is:

```bash
obsidian-github-production-update \
  --target-sha <reviewed-merge-commit>
```

The target SHA is mandatory. The updater fetches `origin/main`, requires the
exact commit to exist, and requires that commit to be reachable from
`origin/main`. It never substitutes `origin/main` for the requested target.

## Transaction

The updater performs:

```text
preflight
  checkout is main + clean
  current SHA recorded
  target SHA fetched and validated
  timer enabled/active state recorded
        |
        v
disable --now obsidian-github-sync.timer
        |
        v
git reset --hard <target-sha>
        |
        v
force-reinstall production package
        |
        v
install repository-managed obsidian-github-*.service/.timer units
systemctl daemon-reload
        |
        v
production-smoke --profile safe
        |
        v
production-smoke --profile live
        |
        v
restore the original timer enabled/active state
        |
        v
persist deployment receipt
```

A dirty production checkout fails before the timer is touched.

Once the timer has been stopped, any later failure is fail-closed: the updater
best-effort disables/stops the timer again and does not automatically roll code
back. A canonical write may already have happened during live smoke, so changing
only the deployed code automatically would make the resulting state harder to
reason about.

## Smoke registry

`obsidian-github-production-smoke --profile safe` runs checks registered in
the installed production package. Safe checks must not require GitHub,
Nextcloud, or canonical writes and should use only the Python standard library
plus the installed package.

The first registered safe smoke fixes the #74 contract in production:

- HTTP 403 is `authority_rejection`;
- HTTP 412 is `etag_cas_conflict`;
- neither explicit response enters post-GET recovery.

Future changes that need a non-destructive deployment-specific assertion should
add it to the safe registry together with unit tests. This turns a one-off
operator command into a permanent regression check for every later deployment.

`obsidian-github-production-smoke --profile live` starts exactly one
`obsidian-github-compactor.service` cycle. Existing systemd dependencies run:

```text
compactor
  -> writer
       -> sync
            -> vault-pull
```

The smoke then requires `Result=success` and `ExecMainStatus=0` for all four
oneshot services.

Fault injection, credential corruption, migration markers, or other destructive
checks are not automatic production smokes. Keep those as explicit manual
canaries.

## Managed systemd units

After switching to the exact target SHA, the updater atomically installs every
regular file matching:

```text
examples/github-sync/obsidian-github-*.service
examples/github-sync/obsidian-github-*.timer
```

into `/etc/systemd/system`, then runs `systemctl daemon-reload`.

The five current core units are required to exist:

- `obsidian-github-sync-vault-pull.service`
- `obsidian-github-sync.service`
- `obsidian-github-writer.service`
- `obsidian-github-compactor.service`
- `obsidian-github-sync.timer`

This dynamic source selection lets a reviewed future revision add another
GitHub integration unit without requiring the previously-installed updater code
to know its filename in advance.

Private configuration, credentials, and `vault-pull.filters` are not copied by
the updater.

A revision that first introduces a new Unix service identity may require a
one-time authority bootstrap before running the updater live smoke. For the
status compactor, run
`sh examples/github-sync/bootstrap-compactor-authority.sh` as root before
deploying the revision that changes the timer target. Subsequent deployments
need no additional identity work.

## Deployment receipt

The default receipt directory is:

```text
/var/lib/obsidian-github-sync/deployments/
```

Every completed transaction stores a secret-free JSON record containing:

- previous production SHA;
- explicit target SHA;
- pre-deployment timer enabled/active state;
- safe/live smoke status;
- success/failure;
- failed stage when applicable;
- completion timestamp.

Raw command stderr, response bodies, credentials, environment data, and
credential-bearing URLs are not persisted.

If receipt persistence itself fails after the timer has already been restored,
the failure path disables/stops the timer again.

## First installation

The updater cannot install itself before it exists in the production venv.
Therefore only its first rollout uses the previous manual procedure:

```bash
systemctl disable --now obsidian-github-sync.timer

cd /opt/obsidian-github-sync/app
git fetch origin
git checkout main
git reset --hard <merge-commit-containing-production-updater>

/opt/obsidian-github-sync/venv/bin/pip install \
  --no-deps \
  --force-reinstall \
  /opt/obsidian-github-sync/app
```

Verify:

```bash
/opt/obsidian-github-sync/venv/bin/obsidian-github-production-update --help
/opt/obsidian-github-sync/venv/bin/obsidian-github-production-smoke --profile safe
```

The outer bootstrap intentionally leaves the timer disabled/inactive while code
and the venv are being replaced. If that timer was enabled+active immediately
before this one-time bootstrap stop, run the updater once against the same exact
SHA with:

```bash
/opt/obsidian-github-sync/venv/bin/obsidian-github-production-update \
  --target-sha <same-merge-commit> \
  --bootstrap-pre-disabled-timer
```

This flag is **first-install only**. It requires the timer to currently be
disabled and inactive, records that the operator pre-disabled an originally
enabled+active timer for bootstrap safety, and restores enabled+active only
after the full transaction passes. It must not be used for normal deployments.

Because the transaction is idempotent at the Git checkout/package level, this
first self-hosted run validates unit installation, live smoke, receipt
persistence, and timer restoration without briefly re-enabling the timer before
the updater has taken control.

## Normal operation

After the first installation:

```text
review PR
  -> merge manually
  -> record merge commit SHA
  -> obsidian-github-production-update --target-sha <merge SHA>
  -> require success receipt
  -> close the implementation Issue after production acceptance
```

Do not replace the target SHA with `origin/main` or another moving ref.
