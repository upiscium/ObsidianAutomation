# Fresh Snapshot LXC bootstrap

This guide describes the **new-host** path for a Debian 13 LXC. It does not
retrofit or migrate an existing Snapshot host. The installer creates one
non-login service account, persistent state, a Python virtual environment, and
one enabled systemd service (`obsidian-snapshot.service`). The service is
configured with `Restart=on-failure`; no timer or second application service is
installed.

## Before installing

1. Create a fresh Debian 13 LXC with systemd and network access to the Debian
   package repositories.
2. Obtain this repository at a reviewed commit. Do not run the bootstrap from a
   floating branch or an unreviewed checkout.
3. Confirm the LXC is dedicated to this single Snapshot runtime. This installer
   intentionally refuses to overwrite its account, application, state, or unit
   destinations if any already exist.

The installer itself uses only the Python standard library. It installs
`python3`, `python3-venv`, Debian's local setuptools/wheel packages, `git`,
`rclone`, and `ca-certificates` through APT. The application and its build backend are then installed from the local
checkout and Debian's `/usr/share/python-wheels`; pip is invoked with
`--no-index` so Python package installation does not use PyPI.

## Review the generated plan

From the reviewed checkout, inspect the plan first:

```bash
python3 tools/install_snapshot_lxc.py
```

This is plan-only by default. It prints JSON describing package commands, the
service account, paths, source checkout, state directory, and the exact systemd
unit. It does not call APT, `useradd`, `systemctl`, or write files. Verify the
source path and service definition are what you intend before applying.

## Apply on the fresh LXC

Only after reviewing the plan, apply it as root:

```bash
python3 tools/install_snapshot_lxc.py --apply
```

The installer fails before changes if its canonical account or destinations
already exist. It then installs the local source into
`/opt/obsidian-snapshot/venv`, creates `/var/lib/obsidian-snapshot` owned by the
`obsidian-snapshot` system account, installs
`/etc/systemd/system/obsidian-snapshot.service`, and enables/starts that one
service. A failed action stops the sequence; the installer does not attempt an
automatic rollback.

The service runs the package's `obsidian-snapshot-app` entry point as the
non-root account with `--state /var/lib/obsidian-snapshot`. Its `HOME` and
working directory point at that private directory. Open the loopback dashboard
at `http://127.0.0.1:8370/` locally (or through a separately controlled private
ingress). No password-based Web login is provided: **never expose the port to
the public Internet**. Supply a Nextcloud WebDAV URL ending in
`/remote.php/dav/files/<user>/`, the existing Vault folder, one app-password
account, and an existing private Gitea HTTPS `.git` repository with a token.
Click **接続を検証して保存**; invalid/unreachable connections leave the prior settings
unchanged. Secrets are stored locally under the service account and are not
read back into the UI. Gitea's authenticated read-only API must confirm that
the exact repository is private; the app checks privacy again immediately before
each changed snapshot push. A token that can read but cannot write may pass
setup: the UI labels write access **unverified** until a snapshot push is
confirmed at the remote ref. Snapshot starts Off and saving new connection
settings returns the schedule to Off. Click Run now for a manual snapshot even
while Off; enabling the schedule requires its separate save action.
For an empty existing private repository the first run creates its first branch;
this UI does **not** create a Gitea repository. Do not use a public repository.

## Offline test contract

The installer tests build a plan without invoking its command-line `--apply`
path. Execution-path tests use a temporary target root and an injected fake
command runner; APT, user/group tools, and systemd are never run by those tests.
This is a logic-level check, not acceptance on a real Debian LXC. No real
installation should be performed as part of the repository test suite.

After an operator applies the plan, inspect the service and its journal on the
target LXC:

```bash
systemctl status obsidian-snapshot.service
journalctl -u obsidian-snapshot.service --no-pager
```

## Limits

- This is a fresh-install-only bootstrap, not an updater or migration tool.
- It installs from the already obtained local source checkout; it does not
  select, fetch, or authenticate a Git revision for you.
- The package set and build-wheel availability must match the target Debian
  release. The guide currently targets Debian 13 and its local Python wheelhouse.
- Local wheelhouse must contain a build backend satisfying `setuptools>=75`;
  if unavailable the installer fails without fetching PyPI packages.
- Unit tests do not verify Debian APT behavior, filesystem ownership on an LXC,
  the runtime application's external configuration, or real systemd lifecycle.
- Privacy may change between the immediate pre-push API check and Git push;
  no cross-protocol atomic guarantee exists. The disposable E2E substitutes
  a local bare Git repository for HTTPS Gitea and a fake rclone for Nextcloud.
