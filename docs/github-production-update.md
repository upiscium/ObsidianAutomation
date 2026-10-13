# Obsidian GitHub production update transaction

## Purpose

Repeated production deployments of the Obsidian GitHub integration use a single
reviewed target commit instead of an implicit "latest main" update.

The **preferred staged deployment** is described in
[GitHub Production Two-Phase Deployment v1](github-production-stage-activate-v1.md).
Its `stage` operation runs safe-only smoke and keeps managed timers inert;
`activate` requires distinct exact-stage and live effect approval.

The historic **single-transaction live deployment** remains available only
with explicit permission to perform the live GitHub Writer/Sync smoke:

```bash
obsidian-github-production-update \
  --target-sha <reviewed-merge-commit> \
  --approve-legacy-live-effects
```

Do not use this full transaction as a side-effect-free staging operation.
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
  GitHub sync + Daily Progress timer state recorded
        |
        v
disable --now every existing managed GitHub timer
        |
        v
git reset --hard <target-sha>
        |
        v
force-reinstall production package
        |
        v
install repository-managed obsidian-github-*.service/.timer units
install exact Daily summarizer revision binding
systemctl daemon-reload
        |
        v
production-smoke --profile safe
Daily production smoke --profile safe
        |
        v
production-smoke --profile live
        |
        v
restore each timer's original state
(new Daily timer stays disabled when it did not exist before)
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

The current required unit set includes the existing five-unit Project status
pipeline plus the Daily Progress chain:

- `obsidian-github-sync-vault-pull.service`
- `obsidian-github-sync.service`
- `obsidian-github-writer.service`
- `obsidian-github-compactor.service`
- `obsidian-github-sync.timer`
- `obsidian-github-daily-schedule.service`
- `obsidian-github-daily-collect.service`
- `obsidian-github-daily-summary.service`
- `obsidian-github-daily-render.service`
- `obsidian-github-daily-apply.service`
- `obsidian-github-daily-progress.timer`

This dynamic source selection lets a reviewed future revision add another
GitHub integration unit without requiring the previously-installed updater code
to know its filename in advance.

Private configuration, credentials, and `vault-pull.filters` are not copied by
the updater.

A revision that first introduces a new Unix service identity may require a
one-time authority bootstrap before running the updater live smoke. The
currently-deployed checkout may not contain that bootstrap yet, so execute the
script from the exact reviewed target commit without moving production HEAD:

```bash
git -C /opt/obsidian-github-sync/app fetch origin main
git -C /opt/obsidian-github-sync/app \
  show <reviewed-target-sha>:examples/github-sync/bootstrap-compactor-authority.sh \
  | sh
```

For the status compactor this creates only the local credential-free identity
and narrow request-directory ACL. Then run the normal
`obsidian-github-production-update --target-sha <reviewed-target-sha>`.
Subsequent deployments need no additional identity work.

## Deployment receipt

The default receipt directory is:

```text
/var/lib/obsidian-github-sync/deployments/
```

Every completed transaction stores a secret-free JSON record containing:

- previous production SHA;
- explicit target SHA;
- pre-deployment enabled/active state for every managed GitHub timer;
- existing GitHub safe/live smoke status;
- Daily Progress safe-smoke status and explicit `daily_live_canary:
  not_attempted` until a controlled canary is run;
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
systemctl disable --now obsidian-github-daily-progress.timer 2>/dev/null || true

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


## Daily Project Progress production activation

The updater installs the Daily Progress units but does not grant credentials or
enable a newly-introduced Daily timer automatically.

Before the first live Daily canary:

1. merge/promote the ObsidianCore companion that places exactly one visible
   `# Project Progress` H1 in newly-created Daily Notes;
2. run the reviewed authority provisioner so
   `obsidian-github-summarizer`, `obsidian-github-renderer`, and
   `obsidian-github-daily-writer` plus their stage ACLs exist;
3. create `/etc/obsidian-github-summarizer/config.env` from the reviewed
   example, readable only through the summarizer config boundary;
4. create a dedicated Nextcloud account shared only to `00-DailyNote` with
   Read + Update permissions, then install its config/password below
   `/etc/obsidian-github-daily-writer`;
5. keep `obsidian-github-daily-progress.timer` disabled;
6. enqueue an explicit canary date and start the dependency chain manually;
7. verify the full artifact chain with:

```bash
obsidian-github-daily-production-smoke \
  --profile live \
  --date YYYY-MM-DD
```

The live smoke is read-only with respect to pipeline control: it re-reads the
Evidence, grounded-summary, Projection, transport ref, and exact transport-result
artifact and requires all SHA bindings plus a successful
`applied|recovered|already_desired` outcome.

Only after that explicit canary passes should the Daily timer be enabled.

The timer schedules a new previous-day job at approximately 00:10 Asia/Tokyo and
also wakes at every `:40` to retry any historical pending dates. Stage refs are
immutable, so successful stages are not rerun during retries.
