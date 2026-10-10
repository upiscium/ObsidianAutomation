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
  systemd unit-file enumeration, and loaded-unit enumeration must all agree;
  any additional enabled, disabled, transient or loaded GitHub timer is a
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
  The EnvironmentFile parser accepts whitespace-indented assignments and
  binds every recognized absolute path; unsupported syntax fails closed.
  Stage also parses the reviewed managed Unit ExecStart/ExecStartPre/etc.
  arguments and SHA-256 binds files referenced directly by `--config`,
  `--rclone-config`, `--filter-file`, and `--password-file`. This explicitly
  covers GitHub watcher `config.toml`, rclone configuration/filter files and
  WebDAV password files, which are **not** EnvironmentFiles. Unknown file-like
  options, missing or nonregular direct files, variable-expanded/nonabsolute
  paths, and changed direct input hashes fail closed; neither file contents
  nor credentials are written to receipts or diagnostic logs.
- Saves an fsynced PREPARING marker **before** stopping managed timers.
- Disables/stops the GitHub Sync and Daily Progress timers and requires both
  disabled/inactive. No Core Promotion timer operation is permitted.
- Resets only the verified production checkout to SHA, installs package/units
  and Daily summarizer revision. The installed Python package module inventory
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
