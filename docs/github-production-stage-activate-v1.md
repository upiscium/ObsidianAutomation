# GitHub Production Two-Phase Deployment v1 (#294)

Status: DRAFT / prototype; approval of this design is NOT approval to execute
a deployment, change Writer/Vault state, restore timers, or apply Nextcloud CAS.

## Why this exists

Previously `obsidian-github-production-update --target-sha SHA` performed
an **unconditional Live Smoke** after safe checks. That Live Smoke launches
compactor -> GitHub writer -> sync -> vault-pull via systemd dependencies.
Publishing reviewed code and exercising real production effects must be
separate authorities. Stage/Activate add such a boundary while keeping the
legacy full transaction available **only with a new explicit live-effects flag**.

## One-time install-free Stage bootstrap (first deployment only)

The current production executable is built from a revision **before** the
`stage` subcommand was added. Running that installed CLI cannot bootstrap the
two-phase flow, while running the historical full updater just to install a
new CLI would trigger an **unapproved Live Smoke**. Do not use either route.

The reviewed target includes `tools/bootstrap_github_stage.py`: a standalone
Python stdlib-only entrypoint, invoked from the **exact merged Git commit**.
It first validates a clean production `main` checkout, exact 40-character
SHA-1 commit, target reachable from the locally verified `origin/main`, and
fast-forward ancestry. It reads only three allowed `src/obsidian_automation/`
Python **Git objects** (`__init__`, `github_production_update`,
`github_production_staging`) after verifying their Git mode, exact blob hash
and byte limit. Bootstrap explicitly disables Git replacement objects,
refuses any local replacement refs, strips inherited Git configuration overrides,
and uses a vetted root-owned temp parent instead of inherited TMPDIR.
It loads the modules from an owner-private, temporary directory with
isolated Python imports; **the production checkout and installed package do
not need to change before Stage itself acquires its transaction lock**.
The private directory is removed on normal return; a hard loss might leave
a private orphan that requires deliberate inspection, not age-only deletion.

### Read-only source/Stage inspection

After an operator has separately approved refreshing local Git references
(`git fetch origin main` does **not** switch the installed checkout),
substitute the exact merged, independently reviewed full SHA:

```bash
set -euo pipefail
APP=/opt/obsidian-automation/app
VENV=/opt/obsidian-automation/venv
TARGET='<exact-40-character-reviewed-merged-SHA>'
test -z "$(env -i PATH=/usr/bin:/bin GIT_NO_REPLACE_OBJECTS=1 \
  GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null \
  /usr/bin/git -C "$APP" for-each-ref --format='%(refname)' refs/replace)"
env -i PATH=/usr/bin:/bin GIT_NO_REPLACE_OBJECTS=1 \
  GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null \
  /usr/bin/git -c core.fsmonitor=false -c core.hooksPath=/dev/null \
  -C "$APP" show "$TARGET:tools/bootstrap_github_stage.py" |
  /usr/bin/python3 -I -B - inspect \
    --target-sha "$TARGET" \
    --app-root "$APP" \
    --venv-root "$VENV" \
    --bind-config-file /etc/obsidian-github-summarizer/config.env
```

The bootstrap requires `python3 -I -B` (isolated imports without bytecode
writes) and trusted absolute operator paths. This sample is **inspect-only**;
the approved Stage invocation must be separately assembled from the exact
source and authority receipts.

This does **not** update code, stop timers, invoke Writer/Smoke or create a
stage receipt. It returns `source_attested_no_effects` and an existing
transaction marker status; it is **not** a claim that the entire service/unit
runtime preflight has passed. Unmerged Draft PR heads and unexpected branch,
dirty checkout, rollback, malformed/symlinked Git source fail closed.

### First Stage, only under a separate operator approval

The exact same `git show` pipeline may instead be called with the `stage`
subcommand and `--approve-stage-effects` **only after explicit external
approval of that exact target SHA and change scope**. The Stage command
may disable managed timers, reset the production Git checkout, remove validated
ignored build artifacts, install the package/units and write durable Stage
state. The flag is a local guard; it is **not** the approval itself.
The bootstrap deliberately exposes **no** Activate, live Writer, Daily timer,
force-deploy, alternative Git SHA or rollback command.

Once a successful Stage returns its SHA-addressed receipt, use the now
installed reviewed CLI's `inspect` command for the later stage transaction
readback. Do not restore Sync/Daily timers by inference. **Activate requires a
separate exact-receipt Live Smoke approval** and any allowed Sync timer
restoration requires explicit separate consent. Daily publication requires
its own independent Writer/Nextcloud CAS authorization.

### Interrupted Stage / failed bootstrap recovery

| Durable marker | Meaning | Operator recovery gate |
|---|---|---|
| `none` | No committed Stage control marker; first read-only source inspection may be repeated | Verify checkout, installed package, timers and any orphan temporary state before authorizing a new Stage |
| `preparing` | Timers may have been stopped; installation may be partly applied | Keep Writer/Daily inactive; collect unit/package/receipt metadata and verify upstream/canonical refs. **No automatic retry or checkout reset** |
| `failed` | Stage or Activate encountered an error; effects may already have happened | Diagnose the failed cutpoint; explicitly authorize repair/rollback or a new staged transaction. If timer stop or post-stop inspection failed, the operator receives `TIMER_CONTAINMENT_UNCONFIRMED`; do not assume timers are inactive |
| `staged` | Safe Stage receipt exists; both managed timers should be inert | Validate exact receipt, code/ACL/config/unit drift; separately approve (or refuse) Activate |
| `activating` | Live GitHub Writer Smoke may have run partially or fully | **Do not replay Smoke** without independently reconciling published/canonical effects and receiving new consent |
| `activated` | Activation receipt was committed | Confirm exact receipt, permitted timer state and downstream project/daily acceptance independently |

The bootstrap error result explicitly says
`failed_manual_reconciliation_required` and
`stage_effects_may_have_occurred=true` if a separately authorized Stage
raised an error; it cannot claim that the operation rolled back. A bootstrap
failure before Stage authorization has `blocked` status and no Stage authority.
No recovery method silently removes an existing marker, resumes a failed
transaction, changes a timer, replays Writer or rewrites canonical Evidence.

## Stage

Command shape (operator sets exact, reviewed full SHA and verified host paths):

`obsidian-github-production-update stage --target-sha SHA --app-root /opt/obsidian-automation/app --venv-root /opt/obsidian-automation/venv --bind-config-file /etc/obsidian-github-summarizer/config.env`

This is **documentation, not a command to run before approval**.

- Requires root, owned non-symlink deployment receipt directory and a private
  0700 staging directory. Acquires a nonblocking advisory transaction lock.
- Refuses an existing staging transaction unless its prior status is activated.
  A failed, preparing, or activating state requires explicit reconciliation.
- Validates a clean main checkout, exact target commit reachable from
  origin/main, and current production HEAD as an ancestor of the target.
  Non-fast-forward updates and rollbacks require a separate recovery contract;
  this normal Stage intentionally refuses them. **Every installed**
  `obsidian-github-*.service` must be confirmed inactive before/during staging,
  not merely the Writer/Sync shortlist. Both installed and loaded GitHub
  **service inventories** are enumerated through systemctl: unknown externally
  installed or transient services are rejected even when their unit file does
  not appear in the configured systemd directory.
  The **only allowed GitHub timers** are
  the known Sync and Daily Progress timers. The installed-files inventory,
  systemd unit-file enumeration must match the expected set exactly. The
  loaded-unit inventory may omit known disabled/inactive timers, but must be
  a subset of the installed allowlist. Any additional unknown or transient
  GitHub timer is a
  fail-closed **pre-effect** blocker. Repeat these checks after timer stop,
  daemon reload and safe smoke. No unknown timer is automatically stopped.
- Fingerprints the exact authorized config files (SHA-256 only, not content).
  By default the summarizer config is required; additional security-relevant
  service config paths may be supplied explicitly. The installed managed
  services are also examined for exact absolute `EnvironmentFile=` bindings;
  required files must exist, and even the absence of an optional file is
  recorded. A changed or newly created optional config blocks Activate.
  The complete installed `obsidian-github-*` Unit namespace, including older
  managed units, is also bound; adding/removing an installed Unit blocks Activate.
  For every managed unit, the effective systemd `LoadState` and
  `FragmentPath` must match the trusted installed file and `DropInPaths`
  must be empty. Pending per-unit `.d` override directories **and global
  service.d/timer.d overrides** are rejected across all directories listed
  by `systemd-analyze unit-paths` (as well as the configured unit directory),
  even if not loaded yet. This applies before and after Stage and Activate;
  no drop-in directives are silently excluded from the revision contract.
  When installing new GitHub Units, Stage renders the reviewed legacy
  /opt/obsidian-github-sync/venv/bin and WorkingDirectory paths to the
  explicitly approved app/venv roots, matching consolidated-host rendering.
  Any remaining unrendered legacy runtime path is rejected. The exact
  installed bytes are SHA-bound in the Stage receipt, and rechecked by Activate.
  The EnvironmentFile parser accepts whitespace-indented assignments and
  binds every recognized absolute path; unsupported syntax, including
  systemd %-specifiers and variable-expanded paths, fails closed.
  Stage also scans the reviewed managed Unit `ExecCondition`,
  `ExecStart`, `ExecStartPre`, `ExecStartPost`, `ExecReload`,
  `ExecStop` and `ExecStopPost` arguments. Files referenced directly by
  `--config`, `--rclone-config`, `--filter-file`, and
  `--password-file` are SHA-256 bound. This explicitly covers GitHub
  watcher `config.toml`, rclone configuration/filter files and WebDAV
  password files, which are **not** EnvironmentFiles. Because systemd
  performs C-style escape decoding that differs from POSIX `shlex`,
  this v1 scanner **rejects residual backslash escapes after legitimate
  line continuations have been joined**, and refuses %-specifiers and
  shell -c wrappers, rather than guessing which file the runtime will read.
  Such units need a separately reviewed
  parser extension before staging. Unknown file-like options, missing or
  nonregular direct files, variable-expanded/nonabsolute paths, and changed
  direct input hashes fail closed; neither file contents nor credentials
  are written to receipts or diagnostic logs.
- **Before the PREPARING marker or timer changes**, run a read-only,
  fail-closed preflight of the **fixed allowlist** of ignored setuptools
  build directories (`build/`, `dist/`, and root/src
  `obsidian_automation.egg-info/`). Verify the root and **every descendant**
  is an expected, root-owned regular file or non-group/world-writable
  directory on the checkout filesystem. Symlinks, special files, hardlinked
  artifacts, other-owner files, unexpectedly mounted subdirectories (including
  same-device bind mounts from Linux `/proc/self/mountinfo`), and any
  inspection errors are refused. Bound the entire cleanup set to at most
  4,096 descendant entries and 128 MiB of regular-file payloads. Refusal
  leaves both timer states unchanged and no PREPARING transaction.
- Saves an fsynced PREPARING marker **before** stopping managed timers.
- Disables/stops the GitHub Sync and Daily Progress timers and requires both
  disabled/inactive. No Core Promotion timer operation is permitted.
- Resets only the verified production checkout to SHA. Under the approved
  Stage lock and with both timers inert, **revalidate** the exact ignored
  build namespace and then remove only these validated derived directories
  before pip builds the replacement package (related #253). A path changed
  after preflight still fails closed; ordinary Stage failure leaves timers
  inert and requires operator reconciliation. Linux descriptor-safe rmtree
  and stable trusted root-owned checkout ancestors are prerequisites.
  This cleanup is a filesystem mutation **only within an approved Stage**;
  neither this document nor the read-only preflight authorizes removal.
  The pip build/install subprocess inherits an explicit `umask 0022`
  rather than a restrictive operator shell umask; the updater restores the
  caller's previous umask on success **and on a pip failure**.
  After installation, the Stage interpreter reads the exact canonical
  `PRIMARY_USERS` role inventory from the reviewed authority provisioner
  **using AST/literal parsing without executing the provisioner**. Every
  `obsidian-*` identity runs a bounded `runuser` Python `-I -B` smoke
  to import the installed package and verify that all SHA-bound GitHub
  console entrypoints are readable/executable. No Writer command is executed.
  A role permission/import failure leaves Stage failed and timers inert.
  Before Activate's effect-capable live smoke, the same role import/access
  test and the recorded role inventory are revalidated. This closes the
  updater-side #254 read/import permission gate, while actual service unit
  execution and model inference remain separately authorized tests.
  Then it installs the units and Daily summarizer revision.
  The installed Python package module inventory
  and each module byte hash must match the exact checked-out source; deployed
  GitHub console-script entrypoints are also SHA-256 bound in the Stage receipt.
  This prevents a clean Git HEAD from concealing installed venv code drift.
  Stage then runs ONLY the GitHub safe and Daily safe smoke profiles.
  The `--profile live` command is absent by design.
- Verifies timers are still inert, effect-capable services remain idle and
  configuration digests remain unchanged. Writes an immutable, canonical,
  SHA-256-addressed stage receipt with prior/target revisions, config and
  installed-unit hashes, timer states and safe smoke results.
- Publishes a STAGED state marker only after the receipt is fully durable.
  Both managed timers remain inert during operator review.

**Crash recovery:** a SIGKILL or host loss leaves an earlier PREPARING
marker; retries refuse further effects until an independently authorized
reconciliation. A failed stage also leaves a FAILED marker. There is no
automatic rollback or permission to remove a partially applied stage.

## Activate

Command shape:

`obsidian-github-production-update activate --stage-sha256 EXACT_STAGE_DIGEST --approve-live-github-writer --app-root /opt/obsidian-automation/app --venv-root /opt/obsidian-automation/venv --bind-config-file /etc/obsidian-github-summarizer/config.env`

The flag does **not** grant Daily Writer or Nextcloud CAS publication.
The operator must explicitly approve running GitHub Writer/Sync live smoke
for this exact stage. A ChatGPT consent to **design** does not supply this.

- Requires an exact, immutable stage receipt digest and STAGED control marker.
- Checks stage-to-runtime path identity, code checkout, deployed units,
  explicit config file digests and installed revision binding.
- Requires both managed timers disabled/inactive, the **complete allowed
  timer inventory** unchanged, all managed units free of effective or pending
  systemd drop-ins, and all effect-capable services idle. Any unknown timer,
  added drop-in or fragment rebind blocks Live Smoke.
- Can optionally restore **only the GitHub Sync timer** with
  `--restore-sync-timer`, and only if the original timer state authorized it.
- **Daily Progress timer restoration is explicitly forbidden in Activate.**
  Its re-enablement requires a separate date-scoped Summary/Writer/CAS review
  and separately approved operational action, even when it was previously
  enabled. The default is to leave both timers disabled/inactive.
  No implicit restoration from the recorded prior state.
- Writes durable ACTIVATING marker **before** running the approved live
  GitHub smoke. If a call fails or is interrupted, a second activation
  cannot replay potentially non-idempotent effects without reconciliation.
- Verifies post-activation timer state, then records activation receipt
  and ACTIVATED marker. No Writer/Timer permission is inferred from a
  successful safe Stage.

## Compatibility and operator obligations

- Legacy full deployment syntax remains available only when the operator
  includes `--approve-legacy-live-effects` in addition to the reviewed
  `--target-sha`. The legacy implementation is otherwise unchanged.
- New Stage/Activate CLI requires explicit `--app-root` and `--venv-root`
  because the old defaults may not match consolidated production. On the
  inspected host the current checkout was under
  `/opt/obsidian-automation/app`. Always recheck actual paths and the
  pinned updater authority before running.
- Installed code and config file paths are trusted operator input.
  The staging records are root-private, no-follow, bounded, canonical JSON.
  The immutable receipt is fsynced before SHA-addressed publication.
  The control is atomically replaced and fsynced. Crashes during publication
  may strand an orphan temp entry; **never** age-delete by default.
- The stage lock serializes only participating updater processes. An
  independent administrator manually starting a Writer/timer, changing
  systemd unit/drop-in files, or running daemon-reload can race the checks;
  this is an explicit trusted-root operator boundary. Pre/post checks
  mitigate drift but cannot defend against an adversarial privileged systemd
  administrator acting between every individual check.
- This patch does not rewrite previously published 2026-10-07/08 Evidence,
  invent completed Summary refs, run Daily Writer, or grant Nextcloud CAS.
- Offline unit tests simulate Git and systemd commands in isolated
  temporary directories, including denied approval, failed safe smoke,
  Live Smoke failures, corruption, config/unit/revision drift and timer
  state verification. They do NOT demonstrate real host deployment.

## Required gates before operational use

1. Independent correctness/security review, including atomic receipt failure
   cutpoints, config manifest completeness and trusted directory boundaries.
2. Full Python CI 3.11/3.13 plus local 3.12 and authority checks.
3. Reviewed SHA after #291/#293 integration and code package publication
   authority; verify deployed host, service roles, actual root paths and
   rollback/recovery procedure.
4. Separate explicit approval for Stage effects; a second approval to run
   Live GitHub Writer Smoke against the exact Stage receipt; a **third**
   separate publication authorization for Daily Writer/Nextcloud CAS.
5. Confirm post-stage inert timers and preserved Core Promotion state.
   Keep actual production unmodified during prototype evaluation.

Connection preference: Adam RDC direct shell with the existing
`SSH_AUTH_SOCK=/run/user/1000/ssh-agent` and strict SSH host-key checking.
No tmux forwarding assumption and no ad-hoc rsync to live source trees.


## R5 review remediation scope and remaining release gates (2026-10-11)

The P1 bootstrap replacement-ref hazard and incorrect consolidated-host Unit
paths have code and isolated regression coverage. Bootstrap source retrieval
itself must use the sanitized, replacement-disabled command in this runbook:
sanitizing only the code after import cannot attest the script being imported.
Stage validates all rendered Unit inputs before the first Unit replacement;
installed bytes and effective Unit paths remain subject to the Stage receipt.

This is **not** an independent review verdict or authorization to merge,
Stage, Activate, run live GitHub Writer, or restore any timer. At a minimum:

- Establish a common host-wide transaction lock/pending-intent admission with
  the consolidated host lifecycle updater. The separately authorized legacy
  all-in-one updater must be retired or governed as break-glass against active
  or unresolved Stage transactions, not silently treated as equivalent authority.
- Expand fault-injection coverage for receipt publication and directory fsync,
  interrupted preparing/activating state and Sync timer arming after restoration.
  If timer stop cannot be verified, treat the environment as containment-
  unconfirmed and reconcile it manually before any new effects.
- Independently re-review the exact final commit for security, correctness,
  compatibility and the Git/checkout trust boundary, with real Reviewer output
  and explicit outstanding-risk disposition.
- Merge only on separate authorization; then conduct a read-only production
  compatibility check and request **distinct exact-SHA Stage**, **exact-receipt
  Activate/Writer**, and **timer restoration** authorities. Daily Writer/CAS
  publication acceptance remains independent.

## R7 shared updater authority and crash-hardening candidate (2026-10-11)

DRAFT implementation, not a deployment authorization. This supersedes the
previous "common lock not implemented" code-level gap while keeping real-host
and independent review gates open.

- The single host-wide lock inode is
  /var/lib/obsidian-automation/deployments/host-update.lock.
  GitHub Stage, Activate, the legacy effectful GitHub updater and the
  target-owned consolidated host lifecycle hold this nonblocking flock for
  their entire effectful transaction. Stage takes it before its private
  transaction lock; no other path may obtain these locks in reverse order.
  The host receipt directory must be root-owned and not group/world-writable.
  Root operation cannot redirect this path through the approved CLI.
  The production Stage receipt directory is likewise fixed to
  /var/lib/obsidian-github-sync/deployments; custom test-only roots cannot
  bypass the real unresolved-Stage admission gate.
- An existing /var/lib/obsidian-automation/deployments/pending-runtime.json
  blocks Stage, Activate and legacy GitHub updates, including malformed and
  failed intents. Under the same lock, consolidated host lifecycle and legacy
  updater inspect the separate GitHub Stage control at
  /var/lib/obsidian-github-sync/deployments/staged-github-production-v1/.
  If that directory exists, its private control must be valid and activated;
  preparing, staged, activating, failed, missing/corrupt controls or a
  suspicious symlink block host/legacy effects until manual reconciliation.
  Legacy --approve-legacy-live-effects remains an explicit break-glass route,
  not an alternative Activate receipt.
- Before PREPARING, Stage fsyncs the control directory's parent deployment
  directory on **every Stage attempt**, not just the first creation. The
  directory may already exist from a failed previous fsync without having
  durable parent metadata. Any failed parent fsync blocks before timer effects,
  and a retry must successfully repeat it. Marker/receipt publication
  separately fsyncs its own directory. No power-loss guarantee is inferred
  from the mere existence of a directory after an error.
- When restoring a previously active GitHub Sync timer, Activate requires
  SubState=waiting with a **recognized, strictly positive systemctl-rendered
  monotonic time span** (e.g. 5min or 1month 2w 5d 13h 51min 2.256ms),
  or SubState=running. Empty/zero/infinite, malformed, unexpected or future
  unrecognized formats fail closed; arbitrary nonempty strings do not count
  as finite. Merely enabled/active is not sufficient, and failures are not
  marked activated. This is a state/readiness check, not a guarantee the
  scheduler will actually execute at the expected time.
- The managed Unit scanner rejects dashed-prefix drop-ins and unsupported
  shell/systemd pipe execution wrappers. Bootstrap Git validation uses a
  trusted absolute /usr/bin/git with sanitized Git environment. Stage Git/pip
  subprocesses do not inherit arbitrary pip/Python/dynamic-loader overrides;
  only the exact Adam SSH agent path may be passed as an approved transport
  capability. Stage's pip install additionally uses
  `--no-index --no-build-isolation --no-cache-dir --no-deps`: no build
  backend is fetched from an external package index during privileged Stage.
  The existing root-owned venv must already contain a reviewed setuptools
  backend compatible with the pinned pyproject requirement (currently
  setuptools>=75), plus any build prerequisites. Missing prerequisites fail
  closed with the managed timers kept inert and manual recovery required.
  This is an offline invocation constraint, not a cryptographic attestation
  of the preinstalled backend. The distinct legacy full-live updater is
  explicitly break-glass and does not inherit Stage's deterministic build
  admission contract.

BOOTSTRAP: The first Stage bootstrap loads only the pinned three GitHub updater
modules. Its exact target checkout must include the reciprocal host lifecycle
gate, and previously launched host updater processes must be quiescent before
Stage authorization. This does not protect against a privileged administrator
deliberately running stale pre-gate binaries or manually changing systemd.

Independent security/correctness re-review, crash/fault-injection and actual
host filesystem/ACL/systemd compatibility remain distinct acceptance gates,
followed by separate explicit merge, Stage, Activate, Writer, timer restoration
and Daily CAS authorizations. Passing simulation tests is not proof of
production power-loss recovery.

## R10 candidate: privileged build-environment admission (2026-10-12)

Before writing PREPARING or stopping either GitHub timer, root-mode Stage is
restricted to the canonical /opt/obsidian-automation/venv and validates
the complete trust path from / through the venv. Expected venv directories,
bin/pip, pyvenv.cfg, Python launcher symlinks, resolved root-owned
/usr/bin/python3.X binaries, and all installed site-packages entries are
checked for owner/mode/type. Files must have nlink=1, directory and file
symlinks in site-packages are rejected, and Python executable symlinks are
accepted only if they resolve to root-owned, non-group/world-writable CPython
binaries under /usr/bin. The pip script must use a reviewed venv
Python shebang. Symlinked files in unexpected locations, writable
.pth startup hooks, foreign-owned modules, unsupported backend identities,
multiple site-package directories, or setuptools below version 75 fail closed.

The bounded inventory is SHA-256 fingerprinted before any timer effect. This
binds the pip script, Python launcher targets, pyvenv config and installed
non-application import modules and package metadata. The app distribution
itself is excluded from this stable-build digest because Stage reinstalls it
and separately attests its Python code and GitHub entrypoints. The build
digest plus pip/setuptools/wheel versions is attached to the immutable Stage
receipt; post-install and post-safe-smoke checks reject unexpected build-code
changes. Activate repeats the trust and digest checks before marking
activating or running a live Writer.

This is a local root-managed source trust check, not a cryptographic
certificate that a privileged administrator installed the correct official
wheel. It assumes no malicious actor already controls root or a trusted
system Python/interpreter and that root-owned directory ancestors cannot be
replaced by an adversary during the transaction. The operator still must
verify the installed backend's origin and expected hash against its approved
wheelhouse/distro provenance before the first Stage. The legacy break-glass
updater is not covered by this Stage build-environment receipt.

Negative fixtures cover writable pip/modules/startup files, symlinks,
unexpected interpreters, hardlinks, missing/old setuptools, and malformed
build receipts. These isolated tests do not exercise production power-loss
semantics or grant Stage/Activate authority.

## R11 candidate: import-closure and Stage receipt versioning (2026-10-12)

The v2 Stage receipt is intentionally **not backward-compatible** with the
earlier Draft-only v1 staging receipt. The host-wide control marker remains
version 1, preserving its separately reviewed host lifecycle admission
contract. A v1 Stage receipt without a build manifest cannot authorize the
new Activate. If an old staged transaction is ever encountered, preserve its
marker and receipts for manual evidence reconciliation; do not rewrite the old
receipt, silently retry a Writer or re-run Stage. A newly authorized, reviewed
Stage after reconciliation must produce a fresh v2 receipt and separate
explicit Live approval.

Root-owned pyvenv.cfg must declare include-system-site-packages=false
exactly once. A venv that could import additional system site-packages not
covered by the build manifest fails before PREPARING. The normal production
Python launcher symlinks to root-owned /usr/bin/python3.X remain supported.

Installed application import inventory now rejects unreviewed native extension
modules (.so/.pyd), arbitrary executable/data files, symlinks and orphan
bytecode. Only source-matched *.py and optional corresponding CPython
__pycache__ files may exist. Python source bytes still must match the pinned
Git checkout exactly. A compact SHA-256 aggregate of permitted installed
bytecode paths and bytes is included in the app code manifest and must match
after SAFE smoke and before Live Activate. This is a change-detection contract,
not proof that the bytecode was compiled by an independently verified compiler;
the trusted root-owned install assumption remains explicit.

These source-level checks cannot replace real-host package and filesystem
compatibility testing or authenticate a malicious privileged administrator.
They do not authorize deletion of old transactions, a package reinstall,
Live Smoke, timer restoration or merge.

## R12 root-mode simulation and v1 refusal acceptance (2026-10-12)

The GitHub Actions `root-stage-fixtures` job runs only three explicitly
selected `require_root=True` Stage/Activate simulations. Each uses ephemeral
`tmp_path` fixtures and a FakeRunner; no production checkout, real systemd,
Writer, Vault, Nextcloud or production installed package is touched. The
simulated installed package is created and modified only inside `tmp_path`.
The job checks its effective UID and verifies through JUnit output that all
three cases executed with zero SKIPs, rather than treating ordinary nonroot
CI skips as a successful security gate. The normal Python 3.11/3.13 suite remains unprivileged;
the root-only job uses an isolated GitHub-hosted runner with checkout credentials
disabled. Fork-origin PRs are refused by a pre-checkout guard and cannot
trigger elevated Python tests automatically; such contributions need a
separately reviewed trusted root-fixture execution rather than a silent SKIP.
The existing sudo-based `authority-fixture` CI job follows the same pre-checkout
fork-origin refusal policy and disables checkout credential persistence; neither
privileged job silently accepts unreviewed fork code. These checks do not make
an editable CI workflow tamper-proof or authorize a production deployment.

The legacy Stage receipt v1 rejection test also publishes an isolated v1-format
receipt, switches the temporary durable `staged` control to that v1 digest, and
invokes Activate with affirmative simulated approval. Activate must reject the
legacy schema before any live Writer command, preserve the unresolved control,
and leave both timers inert. No implicit receipt migration is authorized.
