"""Small WebUI to manage the Basic-auth users of the WebDAV Apache (Issue #631).

Runs on the WebDAV VM, next to the Apache container that reads the same
``htpasswd`` file. It creates users, resets a password, edits a description and
deletes users; nothing else. Python standard library only; the password hashing
is left to the ``htpasswd`` tool (bcrypt), which rewrites the file *in place*, so
the single-file bind mount of the Apache container keeps seeing it.

Safety rules (tested):

* the administrator is a separate account with its own password file; a WebDAV
  user can never administer; failed logins are throttled per client address;
* only listed client addresses (NPM, localhost) and listed Host names are served;
* every change needs the exact Origin and a one-use CSRF token;
* a backup of the file is taken before every change, the result is read back
  (user present or absent, password verified) and the backup is restored on a
  mismatch;
* protected accounts cannot be deleted, nor can the last remaining user;
* a generated password is returned once and is never stored, logged or listed;
  the audit log holds only time, action, user name, client address and result.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import ipaddress
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path

NAME = re.compile(r"[a-z][a-z0-9._-]{2,31}\Z")
_USER_ROUTE = re.compile(r"/api/v1/users/([a-z][a-z0-9._-]{2,31})/(password|description|delete)\Z")
MIN_PASSWORD, MAX_PASSWORD = 12, 128
MAX_DESCRIPTION = 80
BACKUPS_KEPT = 30
DEFAULT_PROTECTED = ("vault-oa", "nc-vault-ui")
MAX_BODY = 4096
THROTTLE_FAILURES, THROTTLE_SECONDS = 5, 600
CSRF_SECONDS, CSRF_MAX = 600, 64
_JSON = "application/json; charset=utf-8"
_ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/static/webdav-admin.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/webdav-admin.css": ("app.css", "text/css; charset=utf-8"),
}
_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


class UserAdminError(ValueError):
    def __init__(self, code: str, http: int = 400):
        super().__init__(code)
        self.code, self.http = code, http


def _utc(clock) -> str:
    return datetime.fromtimestamp(clock(), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _valid_description(value: object) -> str:
    if (type(value) is not str or len(value) > MAX_DESCRIPTION
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)):
        raise UserAdminError("invalid_description")
    return value.strip()


def _valid_password(value: object) -> str:
    if (type(value) is not str or not MIN_PASSWORD <= len(value) <= MAX_PASSWORD
            or ":" in value or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)):
        raise UserAdminError("invalid_password")
    return value


class UserStore:
    """The htpasswd file plus a small private metadata file (descriptions, times)."""

    def __init__(self, htpasswd: Path, state: Path, *, protected=DEFAULT_PROTECTED,
                 hash_tool: str = "htpasswd", clock=time.time, cost: int = 10):
        self.htpasswd, self.state = htpasswd, state
        self.protected = frozenset(protected)
        self.bin, self.clock, self.cost = hash_tool, clock, cost
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        (state / "backups").mkdir(mode=0o700, exist_ok=True)
        self._lock = threading.Lock()
        self._seq = 0

    # ------------------------------------------------------------ low level

    def _run(self, args: list[str], password: str | None = None) -> bool:
        try:
            done = subprocess.run(
                [self.bin, *args], input=None if password is None else password + "\n",
                capture_output=True, text=True, timeout=20, check=False)
        except (OSError, subprocess.SubprocessError):
            return False
        return done.returncode == 0

    def names(self) -> list[str]:
        try:
            text = self.htpasswd.read_text(encoding="utf-8")
        except OSError:
            raise UserAdminError("htpasswd_unreadable", 503) from None
        found = []
        for line in text.splitlines():
            name, sep, _ = line.partition(":")
            if sep and name and not line.startswith("#"):
                found.append(name)
        return found

    def _verify(self, name: str, password: str) -> bool:
        return self._run(["-v", "-i", str(self.htpasswd), name], password)

    def _backup(self) -> Path:
        self._seq += 1
        stamp = datetime.fromtimestamp(self.clock(), timezone.utc).strftime("%Y%m%d-%H%M%S")
        target = self.state / "backups" / f"htpasswd-{stamp}-{self._seq:04}.bak"
        try:
            data = self.htpasswd.read_bytes()
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as out:
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
        except OSError:
            raise UserAdminError("backup_failed", 503) from None
        old = sorted((self.state / "backups").glob("htpasswd-*.bak"))
        for stale in old[:-BACKUPS_KEPT]:
            try:
                stale.unlink()
            except OSError:
                pass
        return target

    def _restore(self, backup: Path) -> None:
        # Rewrite in place: the Apache container bind-mounts this very inode.
        data = backup.read_bytes()
        fd = os.open(self.htpasswd, os.O_WRONLY | os.O_TRUNC)
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())

    def _meta(self) -> dict:
        path = self.state / "users.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            raise UserAdminError("metadata_unreadable", 503) from None
        return value if type(value) is dict else {}

    def _save_meta(self, meta: dict) -> None:
        path = self.state / "users.json"
        temp = path.with_suffix(".tmp")
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                json.dump(meta, out, ensure_ascii=False, sort_keys=True)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temp, path)
        except OSError:
            raise UserAdminError("metadata_unwritable", 503) from None

    def audit(self, action: str, user: str | None, ok: bool, ip: str, code: str | None = None) -> None:
        record = {"ts": _utc(self.clock), "action": action, "user": user, "ok": ok, "ip": ip}
        if code:
            record["code"] = code
        try:
            fd = os.open(self.state / "audit.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as out:
                out.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        except OSError:
            pass  # an audit failure must not turn a safe refusal into an effect

    # ----------------------------------------------------------- operations

    def list_users(self) -> list[dict]:
        with self._lock:
            names, meta = self.names(), self._meta()
        out = []
        for name in sorted(names):
            entry = meta.get(name, {})
            out.append({"name": name, "description": entry.get("description", ""),
                        "protected": name in self.protected,
                        "created": entry.get("created"), "updated": entry.get("updated")})
        return out

    def create(self, name: object, password: object, description: object) -> dict:
        if type(name) is not str or not NAME.match(name):
            raise UserAdminError("invalid_name")
        text = _valid_description("" if description is None else description)
        generated = password is None
        secret = secrets.token_urlsafe(18) if generated else _valid_password(password)
        with self._lock:
            if name in self.names():
                raise UserAdminError("user_exists", 409)
            self._write_password(name, secret)
            meta = self._meta()
            now = _utc(self.clock)
            meta[name] = {"description": text, "created": now, "updated": now}
            self._save_meta(meta)
        return {"name": name, "password": secret if generated else None}

    def set_password(self, name: str, password: object) -> dict:
        generated = password is None
        secret = secrets.token_urlsafe(18) if generated else _valid_password(password)
        with self._lock:
            if name not in self.names():
                raise UserAdminError("user_not_found", 404)
            self._write_password(name, secret)
            meta = self._meta()
            entry = meta.setdefault(name, {"description": "", "created": None})
            entry["updated"] = _utc(self.clock)
            self._save_meta(meta)
        return {"name": name, "password": secret if generated else None}

    def _write_password(self, name: str, secret: str) -> None:
        backup = self._backup()
        ok = self._run(["-i", "-B", "-C", str(self.cost), str(self.htpasswd), name], secret)
        if not ok or name not in self.names() or not self._verify(name, secret):
            self._restore(backup)
            raise UserAdminError("write_failed", 500)

    def set_description(self, name: str, description: object) -> dict:
        text = _valid_description(description)
        with self._lock:
            if name not in self.names():
                raise UserAdminError("user_not_found", 404)
            meta = self._meta()
            entry = meta.setdefault(name, {"description": "", "created": None})
            entry["description"], entry["updated"] = text, _utc(self.clock)
            self._save_meta(meta)
        return {"name": name}

    def delete(self, name: str, confirm: object) -> dict:
        if confirm != name:
            raise UserAdminError("confirmation_mismatch")
        if name in self.protected:
            raise UserAdminError("user_protected", 409)
        with self._lock:
            names = self.names()
            if name not in names:
                raise UserAdminError("user_not_found", 404)
            if len(names) <= 1:
                raise UserAdminError("last_user", 409)
            backup = self._backup()
            if not self._run(["-D", str(self.htpasswd), name]) or name in self.names():
                self._restore(backup)
                raise UserAdminError("write_failed", 500)
            meta = self._meta()
            meta.pop(name, None)
            self._save_meta(meta)
        return {"name": name}


class AdminAuth:
    """The administrator: a separate password file; failures are throttled per address."""

    def __init__(self, admin_htpasswd: Path, admin_user: str, *, hash_tool: str = "htpasswd",
                 clock=time.time):
        self.file, self.user, self.bin, self.clock = admin_htpasswd, admin_user, hash_tool, clock
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def blocked(self, ip: str) -> bool:
        now = self.clock()
        with self._lock:
            recent = [t for t in self._failures.get(ip, []) if now - t < THROTTLE_SECONDS]
            self._failures[ip] = recent
            return len(recent) >= THROTTLE_FAILURES

    def fail(self, ip: str) -> None:
        with self._lock:
            self._failures.setdefault(ip, []).append(self.clock())

    def clear(self, ip: str) -> None:
        with self._lock:
            self._failures.pop(ip, None)

    def check(self, user: str, password: str) -> bool:
        if (user != self.user or not password or len(password) > 256
                or any(ord(ch) < 32 or ord(ch) == 127 for ch in password)):
            return False
        try:
            done = subprocess.run([self.bin, "-v", "-i", str(self.file), user],
                                  input=password + "\n", capture_output=True, text=True,
                                  timeout=20, check=False)
        except (OSError, subprocess.SubprocessError):
            return False
        return done.returncode == 0


class CsrfTokens:
    def __init__(self, clock=time.time):
        self.clock, self._tokens, self._lock = clock, {}, threading.Lock()

    def issue(self) -> str:
        token = secrets.token_urlsafe(24)
        now = self.clock()
        with self._lock:
            self._tokens = {k: v for k, v in self._tokens.items() if v > now}
            if len(self._tokens) >= CSRF_MAX:
                self._tokens.pop(next(iter(self._tokens)))
            self._tokens[token] = now + CSRF_SECONDS
        return token

    def consume(self, token: str) -> bool:
        with self._lock:
            expires = self._tokens.pop(token, None)
        return expires is not None and expires > self.clock()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 8

    def __init__(self, address, *, store: UserStore, auth: AdminAuth, allowed_hosts,
                 allow_ips, clock=time.time):
        super().__init__(address, Handler)
        self.store, self.auth = store, auth
        self.allowed_hosts = frozenset(allowed_hosts)
        self.allow_ips = frozenset(str(ipaddress.ip_address(ip)) for ip in allow_ips)
        self.csrf = CsrfTokens(clock)


class Handler(BaseHTTPRequestHandler):
    server_version = "WebdavUserAdmin"
    sys_version = ""

    def log_message(self, fmt, *args):  # never log headers or bodies
        sys.stderr.write("webdav_user_admin %s %s\n" % (self.address_string(), fmt % args))

    # ---------------------------------------------------------------- output

    def _send(self, status: int, body, mime: str = _JSON, extra: dict | None = None) -> None:
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", _CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status: int, code: str, extra: dict | None = None) -> None:
        self._send(status, {"error": code}, extra=extra)

    def _client_ip(self) -> str:
        """The real client behind a trusted proxy (NPM), for the throttle and the audit.

        The peer must be an allowed address; the right-most X-Forwarded-For entry is the
        one the proxy itself appended, so a client cannot choose it. Anything unparsable
        falls back to the peer, so one bad value cannot hide an attacker.
        """
        peer = self.client_address[0]
        values = self.headers.get_all("X-Forwarded-For", [])
        if len(values) != 1:
            return peer
        last = values[0].split(",")[-1].strip()
        try:
            return str(ipaddress.ip_address(last))
        except ValueError:
            return peer

    # ----------------------------------------------------------------- guard

    def _guard(self) -> bool:
        server = self.server
        if self.client_address[0] not in server.allow_ips:
            self._error(403, "source_rejected")
            return False
        ip = self._client_ip()
        if self.headers.get("Host", "") not in server.allowed_hosts:
            self._error(403, "host_rejected")
            return False
        if server.auth.blocked(ip):
            self._error(429, "too_many_attempts", {"Retry-After": str(THROTTLE_SECONDS)})
            return False
        header = self.headers.get("Authorization", "")
        user = password = ""
        if header.startswith("Basic "):
            try:
                user, _, password = base64.b64decode(header[6:], validate=True).decode("utf-8").partition(":")
            except (binascii.Error, UnicodeDecodeError, ValueError):
                user = password = ""
        if not server.auth.check(user, password):
            server.auth.fail(ip)
            server.store.audit("login", None, False, ip, "bad_credentials")
            self._error(401, "authentication_required",
                        {"WWW-Authenticate": 'Basic realm="WebDAV user admin", charset="UTF-8"'})
            return False
        server.auth.clear(ip)
        return True

    def _write_guard(self) -> bool:
        host = self.headers.get("Host", "")
        origins = self.headers.get_all("Origin", [])
        if len(origins) != 1 or origins[0] not in (f"https://{host}", f"http://{host}"):
            self._error(403, "origin_rejected")
            return False
        tokens = self.headers.get_all("X-Admin-CSRF", [])
        if len(tokens) != 1 or not self.server.csrf.consume(tokens[0]):
            self._error(403, "csrf_rejected")
            return False
        return True

    def _body(self) -> dict:
        length = self.headers.get_all("Content-Length", [])
        types = self.headers.get_all("Content-Type", [])
        if (len(length) != 1 or not length[0].isascii() or not length[0].isdecimal()
                or int(length[0]) > MAX_BODY or len(types) != 1
                or not types[0].startswith("application/json")):
            raise UserAdminError("invalid_request")
        try:
            value = json.loads(self.rfile.read(int(length[0])).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise UserAdminError("invalid_request") from None
        if type(value) is not dict:
            raise UserAdminError("invalid_request")
        return value

    # -------------------------------------------------------------- routing

    def do_GET(self):
        if not self._guard():
            return
        if self.requestline.split(" ")[1] != self.path or "?" in self.path:
            self._error(404, "not_found")
            return
        try:
            if self.path in _ASSETS:
                filename, mime = _ASSETS[self.path]
                self._send(200, files("obsidian_automation").joinpath(
                    "webdav_admin", filename).read_bytes(), mime)
            elif self.path == "/api/v1/session":
                self._send(200, {"csrf": self.server.csrf.issue()})
            elif self.path == "/api/v1/users":
                store = self.server.store
                self._send(200, {"users": store.list_users(), "protected": sorted(store.protected)})
            else:
                self._error(404, "not_found")
        except UserAdminError as exc:
            self._error(exc.http, exc.code)
        except Exception:
            self._error(503, "read_unavailable")

    def do_POST(self):
        if not self._guard():
            return
        path = self.path
        store, ip = self.server.store, self._client_ip()
        target = _USER_ROUTE.fullmatch(path)
        if path != "/api/v1/users" and target is None:
            self._error(404, "not_found")
            return
        if not self._write_guard():
            return
        action, user = "create", None
        try:
            payload = self._body()
            if target is None:
                if set(payload) - {"name", "password", "description"} or "name" not in payload:
                    raise UserAdminError("invalid_request")
                user = payload["name"] if type(payload["name"]) is str else None
                result = store.create(payload["name"], payload.get("password"),
                                      payload.get("description"))
            else:
                user, action = target.group(1), target.group(2)
                if action == "password":
                    if set(payload) - {"password"}:
                        raise UserAdminError("invalid_request")
                    result = store.set_password(user, payload.get("password"))
                    action = "reset_password"
                elif action == "description":
                    if set(payload) != {"description"}:
                        raise UserAdminError("invalid_request")
                    result = store.set_description(user, payload["description"])
                    action = "edit_description"
                else:
                    if set(payload) != {"confirm"}:
                        raise UserAdminError("invalid_request")
                    result = store.delete(user, payload["confirm"])
            store.audit(action, user, True, ip)
            self._send(200, result)
        except UserAdminError as exc:
            store.audit(action, user, False, ip, exc.code)
            self._error(exc.http, exc.code)
        except Exception:
            store.audit(action, user, False, ip, "unexpected_exception")
            self._error(503, "operation_unavailable")

    do_HEAD = lambda self: self._error(405, "method_not_allowed")
    do_PUT = lambda self: self._error(405, "method_not_allowed")
    do_DELETE = lambda self: self._error(405, "method_not_allowed")


def make_server(*, store: UserStore, auth: AdminAuth, bind: str, port: int, allowed_hosts,
                allow_ips=("127.0.0.1",), clock=time.time) -> Server:
    ip = ipaddress.ip_address(bind)
    rfc1918 = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    if (ip.version != 4 or ip.is_unspecified or ip.is_multicast
            or not (ip.is_loopback or any(ip in ipaddress.ip_network(net) for net in rfc1918))):
        raise ValueError("private_bind_required")
    if not allowed_hosts:
        raise ValueError("allowed_host_required")
    return Server((bind, port), store=store, auth=auth, allowed_hosts=allowed_hosts,
                  allow_ips=allow_ips, clock=clock)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="WebUI for the WebDAV Apache users")
    parser.add_argument("--htpasswd", type=Path, required=True,
                        help="Apache users file (rewritten in place)")
    parser.add_argument("--admin-htpasswd", type=Path, required=True,
                        help="Separate file with the single administrator entry")
    parser.add_argument("--admin-user", default="admin")
    parser.add_argument("--state", type=Path, required=True,
                        help="Private state: descriptions, backups, audit log")
    parser.add_argument("--bind", required=True, help="Private LAN IPv4 or loopback")
    parser.add_argument("--port", type=int, default=18092)
    parser.add_argument("--allowed-host", action="append", default=[], required=True,
                        help="Host header value to serve (repeatable), e.g. vault-admin.arc.upiscium.dev")
    parser.add_argument("--allow-ip", action="append", default=[],
                        help="Client address allowed to connect (repeatable); default 127.0.0.1")
    parser.add_argument("--protect", action="append", default=None,
                        help="User that cannot be deleted (repeatable); default: " + ", ".join(DEFAULT_PROTECTED))
    parser.add_argument("--hash-tool", default="htpasswd")
    args = parser.parse_args(argv)
    for path in (args.htpasswd, args.admin_htpasswd, args.state):
        if not path.is_absolute():
            parser.error("paths must be absolute")
    store = UserStore(args.htpasswd, args.state,
                      protected=args.protect if args.protect is not None else DEFAULT_PROTECTED,
                      hash_tool=args.hash_tool)
    auth = AdminAuth(args.admin_htpasswd, args.admin_user, hash_tool=args.hash_tool)
    try:
        server = make_server(store=store, auth=auth, bind=args.bind, port=args.port,
                             allowed_hosts=args.allowed_host,
                             allow_ips=args.allow_ip or ["127.0.0.1"])
    except ValueError as exc:
        parser.error(str(exc))
    with server:
        server.serve_forever(poll_interval=0.25)
    return 0


if __name__ == "__main__":
    sys.exit(main())
