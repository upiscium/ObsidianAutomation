# WebDAV user admin (WebUI for the Apache `htpasswd`)

Issue #631. A small page on the WebDAV VM to **create, delete and edit** the Basic-auth
users of the Apache that serves the Vault. It does nothing else.

Every valid user reaches the **whole Vault**; the only per-user setting is the access level below
(without `--groups` there is none and every valid user can read and write). Use one account per device or purpose, so a lost device is
revoked by deleting just its user.

## What it does

| Action | Effect |
| --- | --- |
| Add | Creates a user (name `^[a-z][a-z0-9._-]{2,31}$`) with a generated password (shown **once**) or a typed one (12-128 characters, no `:`), and an optional description (80 characters). |
| Reset password | New generated or typed password. Update the devices that use the account afterwards. |
| Edit description | Metadata only (kept in `users.json`); the users file is untouched. |
| Access level | Read & Write / Read Only / Write Only / Disable, chosen on creation (default Read & Write) and changed from the menu (a confirmation names the effect). Stored as Apache group lines in the `--groups` file; **Disable = in no group** (the password stays, so enabling again changes no device). |
| Lock / unlock | A toggle (🔒 in the list). A locked user cannot be deleted until it is unlocked. Metadata only (`users.json`). The `--protect` names (default `vault-oa`, `nc-vault-ui`) start out locked; once toggled, the stored state wins. |
| Delete | Needs a confirmation. Refused for a locked user; the last remaining user can never be deleted. |

The user name cannot be changed (a rename is a new user with a new password).

## Safety rules

- The administrator is a **separate account** with its own password file; a WebDAV user can
  never administer. Five failed logins from one client block that client for ten minutes. Behind
  NPM the client is the right-most `X-Forwarded-For` entry (the one NPM itself appended; an
  unparsable value falls back to the NPM address), so one LAN user cannot lock the others out,
  and the audit log names the real client. Control characters in a password are refused.
- Only listed client addresses (NPM `10.12.1.1`, localhost) and listed `Host` names are served.
- Every change needs the exact `Origin` and a one-use CSRF token. `Cache-Control: no-store` and
  a strict CSP are set; the assets are served behind the login too.
- Before every change the users file is copied to `<state>/backups/` (newest 30 kept). After
  it the result is read back (user present or absent, password verified with `htpasswd -v`);
  on a mismatch the backup is restored in place.
- `htpasswd -B` (bcrypt) does the hashing with the password on **stdin** (never an argument).
  It rewrites the file in place, so the Apache container, which bind-mounts that one file,
  keeps seeing it. Do not edit the file with `sed -i` or `mv` (that changes the inode).
- A generated password is returned once; it is never stored or logged. The audit log
  (`<state>/audit.log`) holds only time, action, user name, client address and result.

## Run

```sh
python -m obsidian_automation.webdav_user_admin \
  --htpasswd /opt/obsidian-webdav/secrets/htpasswd \
  --admin-htpasswd /etc/webdav-user-admin/admin.htpasswd --admin-user admin \
  --state /var/lib/webdav-user-admin --bind 10.12.2.9 --port 18092 \
  --allowed-host vault-admin.arc.upiscium.dev --allow-ip 10.12.1.1 --allow-ip 127.0.0.1
```

`--bind` must be loopback or RFC 1918. `--protect NAME` (repeatable) replaces the default
list of users that start out locked. No console script is added.

## Install on the VM (needs the owner's approval; nothing here is automatic)

1. User and directories: `useradd --system --no-create-home --shell /usr/sbin/nologin davadmin`;
   `install -d -m 0750 -o root -g davadmin /etc/webdav-user-admin`.
2. The users file: `chown davadmin:www-data /opt/obsidian-webdav/secrets/htpasswd` (mode stays
   `0640`, so the Apache container still reads it through the group). So that `davadmin` can
   reach that one file by name, give it **traverse-only** access to the two directories:
   `chgrp davadmin /opt/obsidian-webdav /opt/obsidian-webdav/secrets` and
   `chmod 0710` on both (group `--x`: no listing; the other files, e.g. `compose.yaml`, the
   password files, stay unreadable to it). `setfacl` was not installed on the VM; do not widen
   the modes to `0711`.
3. Code: the VM has no `python3-venv`, so extract the wheel (it is plain Python) instead:
   `python3 -c "import zipfile; zipfile.ZipFile('<wheel>').extractall('/opt/webdav-user-admin/lib')"`
   and run with `PYTHONPATH=/opt/webdav-user-admin/lib /usr/bin/python3 -m …`.
4. Administrator password: generate one, store it on the operator machine (0600), and write the
   hash: `printf %s "$PW" | htpasswd -i -B -c /etc/webdav-user-admin/admin.htpasswd admin` with
   mode `0640 root:davadmin`.
5. Service: `configs/webdav-user-admin.service.example` to
   `/etc/systemd/system/webdav-user-admin.service`, then `systemd-analyze verify`,
   `daemon-reload`, `enable --now`. The deployed unit also has `--allow-ip 10.12.2.9` (the VM
   itself, so a local check can reach the service); the example file shows the default list.
6. NPM (internal instance): a proxy host `vault-admin.arc.upiscium.dev` to
   `http://10.12.2.9:18092`, HTTPS forced, **no NPM access list and no NPM Basic auth** (the
   page does its own Basic login), plus the internal DNS record.
7. Check: unauthenticated `401`; create a throw-away user, authenticate it against the WebDAV
   (`PROPFIND` `207`), delete it, and see `401`.

Rollback: `systemctl disable --now webdav-user-admin`, restore the users file ownership
(`chown root:www-data`, mode `0640`) and the directory mode (`0700`), remove the NPM host.
The backups under `/var/lib/webdav-user-admin/backups/` can be copied back in place.

Deployed 2026-11-27: `/opt/webdav-user-admin/lib`, unit `webdav-user-admin.service`, state
`/var/lib/webdav-user-admin`, admin hash `/etc/webdav-user-admin/admin.htpasswd`, and the
pre-deploy copy `/opt/obsidian-webdav/secrets/htpasswd.bak-pre-useradmin-20261127`.

## Access levels (`--groups`)

| Level | Allowed methods | Typical use |
| --- | --- | --- |
| Read & Write | everything | a synced device |
| Read Only | `GET HEAD OPTIONS PROPFIND` | a viewer |
| Write Only | `PUT MKCOL DELETE MOVE COPY PROPPATCH LOCK UNLOCK OPTIONS` | a blind drop box. It cannot read or list, so Obsidian/Nextcloud sync does not work with it |
| Disable | none (`401`) | a paused device |

Apache side: `configs/apache-webdav-roles.conf.example` (needs `mod_authz_groupfile`). The group
file lives in its **own directory**, mounted into the container (for example
`/opt/obsidian-webdav/authz` → `/run/authz`, read-only), because the page replaces the file by
an atomic rename; the directory is owned by `davadmin`, the file is `0644`. Before every change
the file is copied to `<state>/backups/groups-*.bak` and read back; a mismatch restores it.

Switching on (needs the owner's approval; the Apache container is recreated, a few seconds
without WebDAV):

1. Create the directory and write every existing user into `rw:` first (so nothing changes),
   then start the page with `--groups <file>`.
2. Replace the `Require valid-user` block of the Apache config with the example, add the
   directory mount to `compose.yaml`, check the config with `apachectl -t` in the same image,
   and recreate the container.
3. Check one user per level (`GET`, `PUT`, `DELETE`) and a disabled user (`401`).

Rollback: restore the previous Apache config and `compose.yaml`, recreate the container.
