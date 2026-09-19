# Source-side production bootstrap v1

## Purpose

The production bootstrap must be able to update itself without requiring the
currently installed Python package to understand the target deployment format.

The stable contract is:

```text
installed standalone launcher
  -> fetch explicit full target SHA
  -> verify target is an exact fetched commit reachable from origin/main
  -> create detached temporary worktree at target
  -> execute target/tools/production_bootstrap.py
  -> target code updates production checkout/package
  -> target code atomically replaces the launcher
```

The launcher and target bootstrap use only the Python standard library. A future
target may change its internal deployment implementation while keeping this
source-handoff contract. Package build dependencies are installed only from the
Debian local wheelhouse; production bootstrap does not depend on PyPI availability.

## Canonical writer-side paths

The new consolidated writer-side LXC uses:

```text
/opt/obsidian-automation/app
/opt/obsidian-automation/venv
/usr/local/sbin/obsidian-automation-update
/var/lib/obsidian-automation/deployments
```

These paths are for the new `obsidian-automation` trust domain. Existing
Publisher / AI / GitHub Sync production paths remain unchanged until controlled
migration.

## Fresh host

Install the local build wheelhouse packages first:

```bash
apt-get update
apt-get install -y python3-setuptools-whl python3-wheel-whl
```

On Debian 13 these provide the build backend wheels under `/usr/share/python-wheels`.
The bootstrap installs `setuptools>=75` and `wheel` into the production venv with
`pip --no-index --find-links /usr/share/python-wheels`, then installs
ObsidianAutomation with `--no-build-isolation --no-index --no-deps`.

The initial bootstrap is intentionally source-side. Obtain an exact reviewed
ObsidianAutomation checkout in a temporary directory and run its bootstrap file:

```bash
sudo python3 tools/production_bootstrap.py \
  --target-sha <reviewed-full-merge-sha> \
  --profile automation
```

The current v1 foundation:

1. creates the production Git checkout if absent;
2. validates a full target SHA;
3. requires the target to be reachable from fetched `origin/main`;
4. executes the target commit's own bootstrap copy from a detached worktree;
5. resets the production checkout to the exact target;
6. creates the production venv if absent;
7. force-reinstalls the package non-editably;
8. atomically installs/replaces `obsidian-automation-update`;
9. writes a secret-free bootstrap receipt.

It does **not yet enable or migrate production services**. The receipt records
`host_activation=not_attempted`. Declarative identities, ACLs, subsystem units,
timer-state transactions, and cutover are subsequent host-lifecycle stages.

## Subsequent self-update

After the first bootstrap:

```bash
sudo obsidian-automation-update \
  --target-sha <reviewed-full-merge-sha> \
  --profile automation
```

The installed launcher still executes the target commit's bootstrap, not the
currently installed package updater.

## Fail-closed rules

- floating refs such as `main` / `origin/main` are rejected as deployment targets;
- production checkout must remain on `main` and clean before handoff;
- target source root must itself be clean and exactly at the requested target;
- symlinked bootstrap/launcher sources are rejected;
- package installation is non-editable;
- launcher replacement is atomic;
- target-child failures propagate only a bounded validated bootstrap error code/message; arbitrary child stderr is not relayed;
- raw command stderr/stdout is not persisted in receipts;
- no credential values are read or created by this bootstrap foundation.

This bootstrap is not permission to run duplicate production writers. New-host
service activation remains gated by the consolidation migration acceptance.
