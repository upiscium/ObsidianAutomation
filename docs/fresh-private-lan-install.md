# Fresh private LAN Dashboard and restrictive-umask recovery (#298)

This document supplements the default loopback-only fresh installer. The existing
single-LXC app still uses exactly one nonroot service account and one embedded
scheduler for B2 Snapshot and B4 Daily Preview.

## Installing onto a new Debian 13 LXC

First inspect the plan with the actual private IPv4 of the new host:

```sh
python3 tools/install_snapshot_lxc.py --bind-address 10.12.2.15
```

After reviewing the generated plan, apply it explicitly:

```sh
python3 tools/install_snapshot_lxc.py --bind-address 10.12.2.15 --apply
```

The default (no bind argument) remains 127.0.0.1:8370. With the explicit
private bind, open http://10.12.2.15:8370/ from the trusted LAN/VPN. The
application uses that one private interface directly; no second ingress daemon
or general host-mode runner is introduced. Only exact RFC1918 IPv4 addresses
and 127.0.0.1 are accepted, not public IPs, names, or 0.0.0.0. The Host and
Origin headers must match the actual binding, and POST still requires one-use
CSRF. Privileged Operator functions are not enabled on LAN bindings.

WebUI has no user login. Never port-forward it to the Internet. Traffic over
private-IP HTTP is not TLS encrypted; when using untrusted networks or
entering passwords outside a trusted LAN, prefer the default loopback binding
with an authenticated SSH tunnel or a dedicated private HTTPS ingress.

## Root umask 077 regression

Fresh source installation previously inherited a root invoking shell's
umask 077 while creating the virtual environment. On obsidian-control
(Debian 13) the installer reported success but systemd failed with status
203/EXEC: root-owned venv directories were 0700 and the nonroot account
could not traverse them.

The fixed installer uses umask 022 only for its child package-install
commands (venv, pip, Debian packages). All app files remain root-owned and
nonroot-readable/executable; the persistent service state is created with an
explicit 0700 mode and owned by obsidian-snapshot. The service's nologin
identity does not change. No credential store or Vault files are widened.

The fresh installer refuses an existing application destination. Do not
re-run --apply as an update on an already installed host. Its source package
and systemd unit must instead be updated in a separate deployment step.

## Acceptance

With B2/B4 both unconfigured and Off, GET /api/v1/workflows returns five
entries, GET /api/v1/snapshot and GET /api/v1/daily show no last run, and
systemctl restart obsidian-snapshot.service recovers the same state.
The direct LAN WebUI can then configure shared Nextcloud+GitHub without
Gitea, or add Gitea later for B2 Snapshot. Preview success still does not
write Daily Notes; the #444 section-only Writer remains separate.
