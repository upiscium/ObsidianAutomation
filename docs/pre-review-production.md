# Pre-review production rollout and acceptance

## Scope

This runbook deploys the #64 pre-review pipeline to the existing single AI Writer
host. It stops at `awaiting_human_review`.

It does not connect automatic execution or transport:

```text
Generator -> Validator -> Reader -> Evaluator -> awaiting_human_review
                                                STOP
```

The production updater never starts Executor, WebDAV transport, or any canonical
write worker.

## Production paths

```text
application checkout:
  /opt/obsidian-ai/ObsidianAutomation

venv:
  /opt/obsidian-ai/venv

state:
  /var/lib/obsidian-ai/state

revision managed by updater:
  /etc/obsidian-ai/pre-review-revision.env

private deployment configuration:
  /etc/obsidian-ai/pre-review-generator.env
  /etc/obsidian-ai/pre-review-evaluator.env

deployment receipts:
  /var/lib/obsidian-ai/deployments
```

Private Generator/Evaluator files contain only deployment-specific
OpenAI-compatible provider settings. The reviewed Git revision is kept in the
separate updater-owned revision file.

## Required production identities

Existing:

- `obsidian-ai-sync`
- `obsidian-ai-reader`
- `obsidian-ai-generator`
- `obsidian-ai-validator`
- `obsidian-ai-evaluator`
- `obsidian-ai-reviewer`
- `obsidian-ai-executor`

Wave D adds:

- `obsidian-ai-status`

`obsidian-ai-status` reads only the orchestration metadata database and writes
an aggregate projection. It does not read Context, Untrusted, Validation,
Evaluation, Review, Execution, Transport, Receipts, or credentials.

## Authority bootstrap

Before the first updater run, execute the bootstrap directly from the exact
reviewed merge commit without changing production HEAD:

```bash
TARGET=<reviewed-merge-commit>
APP=/opt/obsidian-ai/ObsidianAutomation

git -C "$APP" fetch origin main
git -C "$APP" rev-parse --verify "$TARGET^{commit}"

git -C "$APP" show   "$TARGET:examples/ai/bootstrap-pre-review-authority.sh"   | sudo env AI_ROOT=/var/lib/obsidian-ai/state sh
```

Expected final line:

```text
PASS: pre-review production authority configured
```

This creates/configures only:

- `02-Orchestration`;
- `02-Orchestration/recipes`;
- `02-Orchestration/status`;
- `24-Locks/read-view`;
- the credential-free `obsidian-ai-status` identity;
- narrow ACL entries for those paths.

It does not grant canonical Vault or credential authority.

## Private provider configuration

Create the two private files separately from repository code:

```text
/etc/obsidian-ai/pre-review-generator.env
/etc/obsidian-ai/pre-review-evaluator.env
```

Each file has the deployment-specific form:

```text
OPENAI_BASE_URL=https://.../v1
# optional when the endpoint requires bearer authentication
OPENAI_API_KEY=...
```

The base URL may be supplied as either the authority root or the `/v1` root;
the worker normalizes it to `/v1`. Remote plain HTTP is rejected; HTTP is
accepted only for loopback endpoints. The API key is never passed on the command
line or persisted in recipe/status/receipt artifacts.

Do not put tokens, Nextcloud credentials, arbitrary commands, model names, or
the reviewed revision into the job recipe through these files. Provider model
identity remains pinned by the submitted immutable recipe.

## First updater bootstrap

The updater does not exist in the currently installed venv before its first
release. The shared venv is also used by the existing mirror helper, so stop the
recurring mirror before replacing the package:

```bash
sudo systemctl disable --now obsidian-ai-vault-pull.timer
sudo systemctl stop obsidian-ai-vault-pull.service
```

Install the reviewed updater from a temporary detached worktree. Do not use an
editable install and do not move production HEAD yet:

```bash
TARGET=<reviewed-merge-commit>
APP=/opt/obsidian-ai/ObsidianAutomation
BOOT=$(mktemp -d /var/tmp/obsidian-pre-review-bootstrap.XXXXXX)
rmdir "$BOOT"

git -C "$APP" fetch origin main
git -C "$APP" worktree add --detach "$BOOT" "$TARGET"

sudo /opt/obsidian-ai/venv/bin/pip install   --no-deps   --force-reinstall   "$BOOT"

git -C "$APP" worktree remove --force "$BOOT"
```

Then hand control to the exact-SHA transaction:

```bash
sudo /opt/obsidian-ai/venv/bin/obsidian-pre-review-production-update   --target-sha "$TARGET"   --bootstrap-mirror-pre-disabled
```

The bootstrap flag is first-install only. It requires the mirror timer to be
currently disabled/inactive and records that it was originally expected to be
enabled+active. A successful transaction restores it.

## Exact-SHA update transaction

Normal later deployments use:

```bash
sudo /opt/obsidian-ai/venv/bin/obsidian-pre-review-production-update   --target-sha <reviewed-merge-commit>
```

The updater performs:

```text
clean main checkout + exact target verification
        |
        v
record pre-review timer state
record mirror timer state
        |
        v
stop recurring pre-review chain (when already installed)
stop mirror timer + mirror service
        |
        v
git reset --hard <exact target>
        |
        v
pip install --no-deps --force-reinstall <production checkout>
        |
        v
write exact pre-review-revision.env
install reviewed pre-review systemd units
systemctl daemon-reload
        |
        v
force pre-review timer disabled/inactive
        |
        v
safe smoke (no provider, no Nextcloud, no canonical write)
        |
        v
restore existing mirror timer state
restore pre-review timer state only on later deployments
        |
        v
persist secret-free deployment receipt
```

On first installation, `obsidian-pre-review.timer` remains disabled/inactive
even after success. Automation is enabled only after the acceptance gate below.

Any failure after recurring services are stopped is fail-closed: the updater
leaves the relevant timers disabled rather than running an uncertain mixed
revision.

## Safe smoke

The updater automatically runs:

```bash
obsidian-pre-review-production-smoke   --profile safe   --expected-revision <exact-sha>
```

The safe profile verifies:

- exact updater-owned revision;
- all six reviewed pre-review units;
- distinct non-root service identities;
- Generator -> Validator -> Reader -> Evaluator -> Status dependency chain;
- Validator/Reader/Status network isolation where applicable;
- absence of Executor/WebDAV/Execution/Transport/Receipt authority markers.

It does not contact the OpenAI-compatible provider or Nextcloud.

## Limited operational status

The timer targets:

```text
obsidian-pre-review-status.service
  -> obsidian-pre-review-evaluator.service
       -> obsidian-pre-review-reader.service
            -> obsidian-pre-review-validator.service
                 -> obsidian-pre-review-generator.service
```

After the chain, `obsidian-ai-status` writes:

```text
/var/lib/obsidian-ai/state/02-Orchestration/status/pre-review-status.json
```

The projection contains only aggregate orchestration state:

- current job count;
- count per state;
- pipeline health;
- Human Review wait age/reminder;
- backpressure state.

It contains no job ID, Context SHA, Proposal SHA, mutation SHA, recipe SHA,
content, endpoint, credential, or Review decision.

Read it with:

```bash
/opt/obsidian-ai/venv/bin/obsidian-pre-review-status   --status-file   /var/lib/obsidian-ai/state/02-Orchestration/status/pre-review-status.json   --json
```

Health semantics:

- `OK`: no orchestration failure requiring retry/operator intervention;
- `WARNING`: at least one current `retryable_failure`;
- `CRITICAL`: current `blocked` or `retry_exhausted`.

A Human Review wait older than 24 hours is a reminder, not a pipeline failure.

## Disposable production canary

Before enabling the timer, run the canary in a **new** path below `/var/tmp`
or `/tmp`. The CLI refuses the real production state/Vault paths.

Example:

```bash
TARGET=<deployed-review-sha>

sudo /opt/obsidian-ai/venv/bin/obsidian-pre-review-production-canary   --scratch-root /var/tmp/obsidian-pre-review-canary-$TARGET   --generator-base-url <generator-openai-compatible-url>   --evaluator-base-url <evaluator-openai-compatible-url>   --generator-model <reviewed-generator-model>   --evaluator-model <reviewed-evaluator-model>   --deployed-revision "$TARGET"
```

If bearer authentication is required, export the credentials only for the
manual canary invocation:

```bash
export OPENAI_GENERATOR_API_KEY='...'
export OPENAI_EVALUATOR_API_KEY='...'
```

The canary covers:

1. real OpenAI-compatible Generator -> Validator -> Reader -> Evaluator progression;
2. stop at `awaiting_human_review`;
3. no Review/Execution/Transport/Receipt artifact;
4. duplicate submit idempotency;
5. orphaned attempt crash/resume at every pre-review boundary;
6. injected provider failure and three-attempt exhaustion;
7. Human Review backpressure at eight current waits;
8. host-local mirror read-view lock serialization.

Scratch evidence is preserved on failure. Add `--cleanup-on-success` only when
the successful scratch artifacts are not needed for inspection.

The canary never imports or calls Executor or WebDAV transport code.

## Production identity idle-chain acceptance

With the production timer still disabled, run one manual chain cycle:

```bash
sudo systemctl start obsidian-pre-review-status.service

sudo systemctl show   obsidian-pre-review-generator.service   obsidian-pre-review-validator.service   obsidian-pre-review-reader.service   obsidian-pre-review-evaluator.service   obsidian-pre-review-status.service   --property=Result,ExecMainCode,ExecMainStatus
```

With no production jobs, every stage should exit successfully as an idle
oneshot and the aggregate status projection should be readable.

This validates the actual production users/systemd sandbox/ACLs without creating
a canonical mutation.

## Enablement gate

Enable recurrence only after all of these pass:

- exact-SHA deployment receipt is success;
- mirror timer restored successfully;
- pre-review timer is still disabled;
- safe smoke passes;
- disposable provider canary passes;
- production identity idle-chain passes;
- status projection is readable and contains no sensitive identifiers;
- no automatic Executor/Transport connection exists.

Then:

```bash
sudo systemctl enable --now obsidian-pre-review.timer
```

Verify:

```bash
systemctl is-enabled obsidian-pre-review.timer
systemctl is-active obsidian-pre-review.timer
systemctl is-enabled obsidian-ai-vault-pull.timer
systemctl is-active obsidian-ai-vault-pull.timer
```

## Failure handling

Do not enable the pre-review timer after a failed updater or canary.

A failed exact-SHA update intentionally leaves recurring services disabled when
the package revision may be uncertain. Inspect the deployment receipt, repair
the reported stage, then re-run the exact reviewed target.

Do not recover a pre-review failure by starting Executor, WebDAV transport, or
by creating a Human approval record automatically.
