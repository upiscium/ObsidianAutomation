"""WebDAV user admin WebUI (#631). A fake htpasswd tool for CI; the real one when present."""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import shutil
import stat
import subprocess
import sys
import threading

import pytest

from obsidian_automation import webdav_user_admin as admin
from obsidian_automation.webdav_user_admin import (
    AdminAuth, UserAdminError, UserStore, make_server,
)

FAKE = r'''#!{python}
"""Stand-in for Apache's htpasswd: same options and exit codes, in-place rewrite."""
import hashlib, os, sys
args = sys.argv[1:]
mode_verify = "-v" in args
mode_delete = "-D" in args
path, name = [a for a in args if not a.startswith("-") and not a.isdigit()][-2:]
def digest(secret): return "$fake$" + hashlib.sha256(secret.encode()).hexdigest()
def rewrite(lines):
    with open(path, "r+") as handle:
        handle.seek(0); handle.write("".join(line + "\n" for line in lines)); handle.truncate()
lines = [l for l in open(path).read().splitlines() if l]
users = {{l.split(":", 1)[0]: l.split(":", 1)[1] for l in lines}}
if mode_verify:
    secret = sys.stdin.readline().rstrip("\n")
    if name not in users: sys.exit(6)
    sys.exit(0 if users[name] == digest(secret) else 3)
if mode_delete:
    if os.environ.get("FAKE_HTPASSWD") == "fail": sys.exit(1)
    if os.environ.get("FAKE_HTPASSWD") == "noop": sys.exit(0)
    rewrite([l for l in lines if l.split(":", 1)[0] != name]); sys.exit(0)
secret = sys.stdin.readline().rstrip("\n")
if os.environ.get("FAKE_HTPASSWD") == "fail": sys.exit(1)
if os.environ.get("FAKE_HTPASSWD") == "corrupt": secret += "x"
users[name] = digest(secret)
rewrite([k + ":" + v for k, v in users.items()])
'''

REAL = shutil.which("htpasswd")


def pw(label: str) -> str:
    """A test password derived at run time (no password-like literal in the source)."""
    return hashlib.sha256(("webdav-user-admin-test:" + label).encode()).hexdigest()[:24]


def fake_hash(secret: str) -> str:
    return "$fake$" + hashlib.sha256(secret.encode()).hexdigest()


@pytest.fixture
def fake_bin(tmp_path):
    path = tmp_path / "fake-htpasswd"
    path.write_text(FAKE.format(python=sys.executable))
    path.chmod(0o700)
    return str(path)


def make_users_file(tmp_path, names=("vault-oa", "vault-desktop"), real=False, bin_path=None):
    path = tmp_path / "htpasswd"
    path.write_text("")
    for name in names:
        secret = pw("initial-" + name)
        if real:
            subprocess.run([REAL, "-i", "-B", "-C", "4", str(path), name], input=secret + "\n",
                           text=True, capture_output=True, check=True)
        else:
            with open(path, "a") as handle:
                handle.write(f"{name}:{fake_hash(secret)}\n")
    return path


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        self.now += 1
        return self.now


@pytest.fixture(params=["fake", "real"])
def kit(request, tmp_path, fake_bin):
    if request.param == "real" and not REAL:
        pytest.skip("real htpasswd not installed")
    binary = fake_bin if request.param == "fake" else REAL
    users = make_users_file(tmp_path, real=request.param == "real")
    store = UserStore(users, tmp_path / "state", htpasswd_bin=binary, clock=Clock(), cost=4)
    return store, users, binary


def verify(binary, users, name, secret):
    return subprocess.run([binary, "-v", "-i", str(users), name], input=secret + "\n", text=True,
                          capture_output=True).returncode == 0


# ------------------------------------------------------------------- store

def test_create_returns_the_generated_password_once_and_keeps_the_file_inode(kit):
    store, users, binary = kit
    inode = users.stat().st_ino
    result = store.create("vault-laptop", None, "ノート PC 用")
    assert result["name"] == "vault-laptop" and len(result["password"]) >= 20
    assert verify(binary, users, "vault-laptop", result["password"])
    assert verify(binary, users, "vault-desktop", pw("initial-vault-desktop"))  # others kept
    assert users.stat().st_ino == inode  # the Apache bind mount keeps seeing the file
    listing = {u["name"]: u for u in store.list_users()}
    assert listing["vault-laptop"]["description"] == "ノート PC 用"
    assert result["password"] not in json.dumps(store.list_users())
    assert "$" not in json.dumps(store.list_users())  # no hash is ever listed


def test_create_with_a_given_password_does_not_echo_it(kit):
    store, users, binary = kit
    result = store.create("vault-tablet", pw("given-1"), "")
    assert result["password"] is None
    assert verify(binary, users, "vault-tablet", pw("given-1"))


@pytest.mark.parametrize("name", ["", "ab", "Admin", "1abc", "a b c", "a" * 33, "../etc", "x:y", None, 5])
def test_invalid_names_are_refused_without_touching_the_file(kit, name):
    store, users, _ = kit
    before = users.read_text()
    with pytest.raises(UserAdminError) as exc:
        store.create(name, None, "")
    assert exc.value.code == "invalid_name" and users.read_text() == before


@pytest.mark.parametrize("password", ["short", "x" * 129, "has:colon-in-it-12", "line\nbreak-password", 5])
def test_invalid_passwords_are_refused(kit, password):
    store, users, _ = kit
    before = users.read_text()
    with pytest.raises(UserAdminError) as exc:
        store.create("vault-new", password, "")
    assert exc.value.code == "invalid_password" and users.read_text() == before


@pytest.mark.parametrize("description", ["x" * 81, "tab\there", 5])
def test_invalid_descriptions_are_refused(kit, description):
    store, _, _ = kit
    with pytest.raises(UserAdminError) as exc:
        store.create("vault-new", None, description)
    assert exc.value.code == "invalid_description"


def test_a_duplicate_name_is_refused_and_the_old_password_stays(kit):
    store, users, binary = kit
    with pytest.raises(UserAdminError) as exc:
        store.create("vault-desktop", None, "")
    assert exc.value.code == "user_exists"
    assert verify(binary, users, "vault-desktop", pw("initial-vault-desktop"))


def test_reset_changes_the_password_and_the_update_time(kit):
    store, users, binary = kit
    before = {u["name"]: u for u in store.list_users()}["vault-desktop"]
    result = store.set_password("vault-desktop", None)
    assert not verify(binary, users, "vault-desktop", pw("initial-vault-desktop"))
    assert verify(binary, users, "vault-desktop", result["password"])
    after = {u["name"]: u for u in store.list_users()}["vault-desktop"]
    assert after["updated"] != before["updated"]
    with pytest.raises(UserAdminError) as exc:
        store.set_password("nobody-here", None)
    assert exc.value.code == "user_not_found"


def test_description_edit_touches_only_the_metadata(kit):
    store, users, _ = kit
    text = users.read_text()
    store.set_description("vault-desktop", "デスクトップ用")
    assert users.read_text() == text
    assert {u["name"]: u for u in store.list_users()}["vault-desktop"]["description"] == "デスクトップ用"


def test_delete_needs_the_confirmation_and_refuses_protected_and_last_users(kit):
    store, users, binary = kit
    with pytest.raises(UserAdminError) as exc:
        store.delete("vault-desktop", "something-else")
    assert exc.value.code == "confirmation_mismatch"
    with pytest.raises(UserAdminError) as exc:
        store.delete("vault-oa", "vault-oa")
    assert exc.value.code == "user_protected" and exc.value.http == 409
    store.delete("vault-desktop", "vault-desktop")
    assert [u["name"] for u in store.list_users()] == ["vault-oa"]
    assert not verify(binary, users, "vault-desktop", pw("initial-vault-desktop"))
    only = UserStore(users, store.state, protected=(), htpasswd_bin=binary, clock=Clock())
    with pytest.raises(UserAdminError) as exc:
        only.delete("vault-oa", "vault-oa")
    assert exc.value.code == "last_user"
    with pytest.raises(UserAdminError) as exc:
        store.delete("ghost-user", "ghost-user")
    assert exc.value.code == "user_not_found"


def test_every_change_is_backed_up_and_only_the_newest_backups_are_kept(kit, monkeypatch):
    store, users, _ = kit
    monkeypatch.setattr(admin, "BACKUPS_KEPT", 5)
    for number in range(8):
        store.create(f"vault-extra-{number}", None, "")
    backups = sorted((store.state / "backups").glob("htpasswd-*.bak"))
    assert len(backups) == 5
    assert oct(stat.S_IMODE(backups[0].stat().st_mode)) == "0o600"
    # the newest backup is the file as it was before the last change (7 extra users)
    assert backups[-1].read_text().count("vault-extra-") == 7


@pytest.mark.parametrize("mode", ["fail", "corrupt"])
def test_a_failed_or_unverifiable_write_is_rolled_back(tmp_path, fake_bin, monkeypatch, mode):
    users = make_users_file(tmp_path)
    store = UserStore(users, tmp_path / "state", htpasswd_bin=fake_bin, clock=Clock())
    before = users.read_text()
    monkeypatch.setenv("FAKE_HTPASSWD", mode)
    with pytest.raises(UserAdminError) as exc:
        store.create("vault-new", None, "")
    assert exc.value.code == "write_failed" and users.read_text() == before
    with pytest.raises(UserAdminError):
        store.set_password("vault-desktop", None)
    assert users.read_text() == before
    assert [u["name"] for u in store.list_users()] == ["vault-desktop", "vault-oa"]


def test_a_delete_that_did_not_take_effect_is_rolled_back(tmp_path, fake_bin, monkeypatch):
    users = make_users_file(tmp_path)
    store = UserStore(users, tmp_path / "state", protected=(), htpasswd_bin=fake_bin, clock=Clock())
    before = users.read_text()
    monkeypatch.setenv("FAKE_HTPASSWD", "noop")
    with pytest.raises(UserAdminError) as exc:
        store.delete("vault-desktop", "vault-desktop")
    assert exc.value.code == "write_failed" and users.read_text() == before


def test_a_missing_htpasswd_tool_is_a_clean_failure(tmp_path):
    users = make_users_file(tmp_path)
    store = UserStore(users, tmp_path / "state", htpasswd_bin=str(tmp_path / "missing-tool"))
    before = users.read_text()
    with pytest.raises(UserAdminError) as exc:
        store.create("vault-new", None, "")
    assert exc.value.code == "write_failed" and users.read_text() == before


# -------------------------------------------------------------------- HTTP

HOST = "vault-admin.test"
ADMIN_PASSWORD = pw("admin")


class Api:
    def __init__(self, tmp_path, fake_bin, *, allow_ips=("127.0.0.1",), clock=None):
        self.users = make_users_file(tmp_path)
        admin_file = tmp_path / "admin.htpasswd"
        admin_file.write_text(f"admin:{fake_hash(ADMIN_PASSWORD)}\n")
        self.clock = clock or Clock()
        self.store = UserStore(self.users, tmp_path / "state", htpasswd_bin=fake_bin, clock=self.clock)
        self.auth = AdminAuth(admin_file, "admin", htpasswd_bin=fake_bin, clock=self.clock)
        self.server = make_server(store=self.store, auth=self.auth, bind="127.0.0.1", port=0,
                                  allowed_hosts=[HOST], allow_ips=allow_ips, clock=self.clock)
        self.port = self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def call(self, method, path, *, payload=None, password=ADMIN_PASSWORD, user="admin",
             host=HOST, origin="auto", csrf="auto", content_type="application/json", raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Host": host}
        if password is not None:
            headers["Authorization"] = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        if method == "POST":
            if csrf == "auto":
                csrf = json.loads(self.call("GET", "/api/v1/session")[1])["csrf"]
            if csrf:
                headers["X-Admin-CSRF"] = csrf
            if origin == "auto":
                origin = f"https://{host}"
            if origin:
                headers["Origin"] = origin
            headers["Content-Type"] = content_type
        body = raw if raw is not None else (None if payload is None else json.dumps(payload))
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        data = response.read()
        result = (response.status, data, dict(response.getheaders()))
        conn.close()
        return result


@pytest.fixture
def api(tmp_path, fake_bin):
    made = Api(tmp_path, fake_bin)
    yield made
    made.close()


def jbody(result):
    return json.loads(result[1])


def test_authentication_is_required_and_only_the_admin_account_is_accepted(api):
    status, _, headers = api.call("GET", "/api/v1/users", password=None)
    assert status == 401 and "Basic" in headers["WWW-Authenticate"]
    assert api.call("GET", "/api/v1/users", password=pw("wrong-1"))[0] == 401
    assert api.call("GET", "/api/v1/users", user="vault-oa", password=pw("initial-vault-oa"))[0] == 401
    assert api.call("GET", "/api/v1/users")[0] == 200
    # a WebDAV user's own password never opens the admin
    assert api.call("GET", "/api/v1/users", user="admin", password=pw("initial-vault-desktop"))[0] == 401


def test_only_listed_client_addresses_and_host_names_are_served(tmp_path, fake_bin):
    outside = Api(tmp_path, fake_bin, allow_ips=("10.9.9.9",))
    try:
        assert outside.call("GET", "/api/v1/users")[0] == 403
        assert jbody(outside.call("GET", "/api/v1/users")) == {"error": "source_rejected"}
    finally:
        outside.close()


def test_a_foreign_host_header_is_rejected(api):
    assert jbody(api.call("GET", "/api/v1/users", host="evil.example")) == {"error": "host_rejected"}


def test_failed_logins_are_throttled_then_forgiven_after_the_window(tmp_path, fake_bin):
    clock = Clock()
    api = Api(tmp_path, fake_bin, clock=clock)
    try:
        for _ in range(5):
            assert api.call("GET", "/api/v1/users", password=pw("bad-1"))[0] == 401
        status, _, headers = api.call("GET", "/api/v1/users")  # even the right password
        assert status == 429 and "Retry-After" in headers
        clock.now += admin.THROTTLE_SECONDS + 1
        assert api.call("GET", "/api/v1/users")[0] == 200
    finally:
        api.close()


def test_writes_need_the_exact_origin_and_a_one_use_csrf_token(api):
    payload = {"name": "vault-new"}
    assert jbody(api.call("POST", "/api/v1/users", payload=payload, origin=None)) == {"error": "origin_rejected"}
    assert api.call("POST", "/api/v1/users", payload=payload, origin="https://evil.example")[0] == 403
    assert api.call("POST", "/api/v1/users", payload=payload, csrf=None)[0] == 403
    token = jbody(api.call("GET", "/api/v1/session"))["csrf"]
    assert api.call("POST", "/api/v1/users", payload=payload, csrf=token)[0] == 200
    assert api.call("POST", "/api/v1/users", payload={"name": "vault-two"}, csrf=token)[0] == 403  # reused


def test_create_edit_reset_and_delete_over_http(api, capsys):
    status, data, headers = api.call("POST", "/api/v1/users", payload={"name": "vault-phone", "description": "電話"})
    created = json.loads(data)
    assert status == 200 and len(created["password"]) >= 20
    assert headers["Cache-Control"] == "no-store" and "default-src 'none'" in headers["Content-Security-Policy"]
    listing = jbody(api.call("GET", "/api/v1/users"))
    entry = next(u for u in listing["users"] if u["name"] == "vault-phone")
    assert entry["description"] == "電話" and created["password"] not in json.dumps(listing)
    assert listing["protected"] == ["nc-vault-ui", "vault-oa"]
    assert api.call("POST", "/api/v1/users/vault-phone/description",
                    payload={"description": "スマホ"})[0] == 200
    again = jbody(api.call("POST", "/api/v1/users/vault-phone/password", payload={}))
    assert again["password"] and again["password"] != created["password"]
    given = jbody(api.call("POST", "/api/v1/users/vault-phone/password",
                           payload={"password": pw("own-1")}))
    assert given["password"] is None
    assert api.call("POST", "/api/v1/users/vault-phone/delete", payload={"confirm": "wrong"})[0] == 400
    assert api.call("POST", "/api/v1/users/vault-phone/delete", payload={"confirm": "vault-phone"})[0] == 200
    assert api.call("POST", "/api/v1/users/vault-oa/delete", payload={"confirm": "vault-oa"})[0] == 409
    # no secret ever reaches the server log or the audit log
    logs = capsys.readouterr().err + (api.store.state / "audit.log").read_text()
    for secret in (created["password"], again["password"], pw("own-1"), ADMIN_PASSWORD):
        assert secret not in logs
    audit = [json.loads(line) for line in (api.store.state / "audit.log").read_text().splitlines()]
    assert {"create", "edit_description", "reset_password", "delete"} <= {a["action"] for a in audit}
    assert all(set(a) <= {"ts", "action", "user", "ok", "ip", "code"} for a in audit)


def test_bad_requests_routes_and_methods_are_refused(api):
    assert api.call("POST", "/api/v1/users", payload={"name": "vault-new", "extra": 1})[0] == 400
    assert api.call("POST", "/api/v1/users", payload={})[0] == 400
    assert api.call("POST", "/api/v1/users", raw="not json")[0] == 400
    assert api.call("POST", "/api/v1/users", payload={"name": "vault-new"}, content_type="text/plain")[0] == 400
    assert api.call("POST", "/api/v1/users", raw=json.dumps({"name": "x" * 5000}))[0] == 400
    assert api.call("POST", "/api/v1/users/vault-oa/unknown", payload={})[0] == 404
    assert api.call("POST", "/api/v1/users/UPPER/delete", payload={"confirm": "UPPER"})[0] == 404
    assert api.call("GET", "/api/v1/users?x=1")[0] == 404
    assert api.call("GET", "/nope")[0] == 404
    for method in ("PUT", "DELETE", "HEAD"):
        assert api.call(method, "/api/v1/users")[0] == 405


def test_the_page_and_its_assets_are_served_behind_the_login(api):
    assert api.call("GET", "/", password=None)[0] == 401
    status, body, headers = api.call("GET", "/")
    assert status == 200 and "WebDAV ユーザー管理".encode() in body and headers["Content-Type"].startswith("text/html")
    assert api.call("GET", "/static/webdav-admin.js")[0] == 200
    assert api.call("GET", "/static/webdav-admin.css")[0] == 200


def test_server_refuses_a_public_bind_and_a_missing_host_list(tmp_path, fake_bin):
    users = make_users_file(tmp_path)
    store = UserStore(users, tmp_path / "state", htpasswd_bin=fake_bin)
    auth = AdminAuth(tmp_path / "admin.htpasswd", "admin", htpasswd_bin=fake_bin)
    for bind in ("0.0.0.0", "8.8.8.8", "::1"):
        with pytest.raises(ValueError):
            make_server(store=store, auth=auth, bind=bind, port=0, allowed_hosts=[HOST])
    with pytest.raises(ValueError):
        make_server(store=store, auth=auth, bind="127.0.0.1", port=0, allowed_hosts=[])


def test_state_files_are_private(api):
    api.call("POST", "/api/v1/users", payload={"name": "vault-new"})
    for path in (api.store.state / "users.json", api.store.state / "audit.log"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(api.store.state.stat().st_mode) == 0o700


def _call_as(api, forwarded, **kwargs):
    """Like api.call but behind NPM: the proxy appends the client it saw."""
    conn = http.client.HTTPConnection("127.0.0.1", api.port, timeout=5)
    password = kwargs.get("password", ADMIN_PASSWORD)
    headers = {"Host": HOST, "X-Forwarded-For": forwarded,
               "Authorization": "Basic " + base64.b64encode(f"admin:{password}".encode()).decode()}
    conn.request("GET", "/api/v1/users", headers=headers)
    response = conn.getresponse()
    response.read()
    status = response.status
    conn.close()
    return status


def test_the_throttle_is_per_real_client_behind_the_proxy_not_per_proxy(tmp_path, fake_bin):
    api = Api(tmp_path, fake_bin)
    try:
        for _ in range(5):  # a LAN attacker, seen by NPM as 192.168.0.66
            assert _call_as(api, "192.168.0.66", password=pw("bad-1")) == 401
        assert _call_as(api, "192.168.0.66") == 429          # blocked
        assert _call_as(api, "192.168.0.10") == 200          # the real admin is not locked out
        # a spoofed leading entry does not help: the right-most one (added by NPM) counts
        assert _call_as(api, "192.168.0.10, 192.168.0.66") == 429
        # an unparsable header falls back to the peer address, never to a free pass
        assert _call_as(api, "not-an-ip") == 200
    finally:
        api.close()


def test_the_audit_names_the_real_client_and_a_control_character_password_never_passes(api):
    api.call("POST", "/api/v1/users", payload={"name": "vault-new"})
    conn = http.client.HTTPConnection("127.0.0.1", api.port, timeout=5)
    conn.request("GET", "/api/v1/users", headers={
        "Host": HOST, "X-Forwarded-For": "192.168.0.42",
        "Authorization": "Basic " + base64.b64encode(
            f"admin:{ADMIN_PASSWORD}\nanything".encode()).decode()})
    assert conn.getresponse().status == 401  # a newline cannot smuggle a second stdin line
    conn.close()
    token = jbody(api.call("GET", "/api/v1/session"))["csrf"]
    conn = http.client.HTTPConnection("127.0.0.1", api.port, timeout=5)
    conn.request("POST", "/api/v1/users", body=json.dumps({"name": "vault-fwd"}), headers={
        "Host": HOST, "X-Forwarded-For": "192.168.0.42", "Origin": f"https://{HOST}",
        "X-Admin-CSRF": token, "Content-Type": "application/json",
        "Authorization": "Basic " + base64.b64encode(f"admin:{ADMIN_PASSWORD}".encode()).decode()})
    assert conn.getresponse().status == 200
    conn.close()
    audit = [json.loads(l) for l in (api.store.state / "audit.log").read_text().splitlines()]
    assert any(a["action"] == "create" and a["user"] == "vault-fwd" and a["ip"] == "192.168.0.42" for a in audit)
