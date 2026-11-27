# WebDAV user admin (WebUI for the Apache `htpasswd`)

Issue #631. A small page on the WebDAV VM to **create, delete and edit** the Basic-auth
users of the Apache that serves the Vault. It does nothing else.

Every valid user can read and write the **whole Vault** (Apache uses `Require valid-user`;
there are no per-user rights). Use one account per device or purpose, so a lost device is
revoked by deleting just its user.

## What it does

| Action | Effect |
| --- | --- |
| Add | Creates a user (name `^[a-z][a-z0-9._-]{2,31}$`) with a generated password (shown **once**) or a typed one (12-128 characters, no `:`), and an optional description (80 characters). |
| Reset password | New generated or typed password. Update the devices that use the account afterwards. |
| Edit description | Metadata only (kept in `users.json`); the users file is untouched. |
| Delete | Needs a confirmation. Protected accounts (`vault-oa`, `nc-vault-ui` by default) and the last remaining user cannot be deleted. |

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
protected list. No console script is added.

## Install on the VM (needs the owner's approval; nothing here is automatic)

1. User and directories: `useradd --system --no-create-home --shell /usr/sbin/nologin davadmin`;
   `install -d -m 0750 -o root -g davadmin /etc/webdav-user-admin`.
2. The users file: `chown davadmin:www-data /opt/obsidian-webdav/secrets/htpasswd` (mode stays
   `0640`, so the Apache container still reads it through the group) and
   `chmod 0711 /opt/obsidian-webdav/secrets` (so `davadmin` can reach that one file; the other
   files in the directory stay unreadable to it).
3. Code: `python3 -m venv /opt/webdav-user-admin/venv` and
   `pip install --no-deps --no-index <obsidian_automation wheel>`.
4. Administrator password: generate one, store it on the operator machine (0600), and write the
   hash: `printf %s "$PW" | htpasswd -i -B -c /etc/webdav-user-admin/admin.htpasswd admin` with
   mode `0640 root:davadmin`.
5. Service: `configs/webdav-user-admin.service.example` to
   `/etc/systemd/system/webdav-user-admin.service`, then `systemd-analyze verify`,
   `daemon-reload`, `enable --now`.
6. NPM (internal instance): a proxy host `vault-admin.arc.upiscium.dev` to
   `http://10.12.2.9:18092`, HTTPS forced, **no NPM access list and no NPM Basic auth** (the
   page does its own Basic login), plus the internal DNS record.
7. Check: unauthenticated `401`; create a throw-away user, authenticate it against the WebDAV
   (`PROPFIND` `207`), delete it, and see `401`.

Rollback: `systemctl disable --now webdav-user-admin`, restore the users file ownership
(`chown root:www-data`, mode `0640`) and the directory mode (`0700`), remove the NPM host.
The backups under `/var/lib/webdav-user-admin/backups/` can be copied back in place.
