"""One-account snapshot setup. No secret-bearing values are returned to HTTP."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlsplit
from dataclasses import dataclass

from .snapshot_adapter import Connections, _command, git_environment

NEXTCLOUD_FIELDS = {"webdav_url", "vault_folder", "nextcloud_user", "nextcloud_password"}
GITEA_FIELDS = {"git_url", "git_user", "git_token", "branch"}
FIELDS = NEXTCLOUD_FIELDS | GITEA_FIELDS
MARKER = "_rclone_sha256"
# rclone obscure uses unpadded URL-safe Base64, which may contain "-" and "_".
# Nextcloud accepts up to 512 Unicode characters: 4 UTF-8 bytes each plus
# rclone's random 16-byte prefix still encode to fewer than 4096 characters.
_RCLONE_OBSCURED_RE = re.compile(r"[A-Za-z0-9_-]{1,4096}\Z")


@dataclass(frozen=True)
class NextcloudSource:
    rclone_remote: str
    rclone_config: Path

    def validate(self) -> None:
        value = self.rclone_remote
        if (not value.startswith("nextcloud:") or not value[10:]
                or not re.fullmatch(r"[A-Za-z0-9_./ -]{1,100}", value[10:])
                or any(part in ("", ".", "..") for part in value[10:].split("/"))
                or not self.rclone_config.is_file()):
            raise ValueError("invalid_nextcloud_source")


def validate_nextcloud(value: object) -> dict:
    if type(value) is not dict or set(value) != NEXTCLOUD_FIELDS:
        raise ValueError("invalid_nextcloud_connection")
    if any(type(v) is not str or not v or len(v) > 512 or any(ord(c) < 32 for c in v)
           for v in value.values()):
        raise ValueError("invalid_nextcloud_connection")
    web = urlsplit(value["webdav_url"])
    if (web.scheme != "https" or not web.hostname
            or not web.path.startswith("/remote.php/dav/files/")
            or web.username or web.password or web.query or web.fragment
            or not web.path.endswith("/")):
        raise ValueError("invalid_nextcloud_connection")
    if not re.fullmatch(r"[A-Za-z0-9_./ -]{1,100}", value["vault_folder"]) or any(
            part in ("", ".", "..") for part in value["vault_folder"].split("/")):
        raise ValueError("invalid_nextcloud_connection")
    if "\n" in value["nextcloud_user"] or ":" in value["nextcloud_user"]:
        raise ValueError("invalid_nextcloud_connection")
    return dict(value)


def validate(value: object) -> dict:
    if type(value) is not dict or set(value) != FIELDS:
        raise ValueError("invalid_connection")
    if any(type(v) is not str or not v or len(v) > 512 or any(ord(c) < 32 for c in v)
           for v in value.values()):
        raise ValueError("invalid_connection")
    web = urlsplit(value["webdav_url"])
    git = urlsplit(value["git_url"])
    if (web.scheme != "https" or not web.hostname or not web.path.startswith("/remote.php/dav/files/")
            or web.username or web.password or web.query or web.fragment or not web.path.endswith("/")
            or git.scheme != "https" or not git.hostname or not git.path.endswith(".git")
            or git.username or git.password or git.query or git.fragment):
        raise ValueError("invalid_connection")
    if not re.fullmatch(r"[A-Za-z0-9_./ -]{1,100}", value["vault_folder"]) or any(
        part in ("", ".", "..") for part in value["vault_folder"].split("/")):
        raise ValueError("invalid_connection")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,100}", value["branch"]) or ".." in value["branch"] or value["branch"].endswith(("/", ".lock")):
        raise ValueError("invalid_connection")
    if any("\n" in value[key] or ":" in value[key] for key in ("nextcloud_user", "git_user")):
        raise ValueError("invalid_connection")
    return dict(value)


def _atomic(path: Path, content: str) -> None:
    fd, name = tempfile.mkstemp(prefix=".snapshot-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def git_askpass(state: Path) -> Path:
    script = state / "git-askpass"
    # Constant executable, never interpolates user input into shell or argv.
    _atomic(script, "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "v=json.loads((Path(os.environ['SNAPSHOT_CREDENTIALS'])).read_text())\n"
            "p=sys.argv[1] if len(sys.argv)>1 else ''\n"
            "if p.startswith('Username for '): print(v['git_user'])\n"
            "elif p.startswith('Password for '): print(v['git_token'])\n"
            "else: sys.exit(1)\n")
    script.chmod(0o700)
    return script


class SetupStore:
    def __init__(self, state: Path, *, probe=None, obscure=None):
        self.state = state
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = state / "connections.json"
        self.rclone = state / "rclone.conf"
        self.policy = state / "policy.toml"
        self._lock = threading.RLock()
        self.probe = probe or probe_connections
        self.nextcloud_probe = probe_nextcloud
        self.obscure = obscure or obscure_password

    def _read_verified(self) -> dict | None:
        """The ONE credential registry can hold Nextcloud-only or full B2."""
        try:
            metadata = self.path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
                return None
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if (type(saved) is not dict
                    or set(saved) not in (FIELDS | {MARKER}, NEXTCLOUD_FIELDS | {MARKER})):
                return None
            digest = saved.pop(MARKER)
            if type(digest) is not str or not re.fullmatch(r"[a-f0-9]{64}", digest):
                return None
            config_metadata = self.rclone.lstat()
            if (not stat.S_ISREG(config_metadata.st_mode)
                    or stat.S_IMODE(config_metadata.st_mode) != 0o600
                    or hashlib.sha256(self.rclone.read_bytes()).hexdigest() != digest):
                return None
            return saved
        except (OSError, ValueError, KeyError):
            return None

    def load(self) -> dict | None:
        """Full B2 only; existing snapshot jobs never run on partial setup."""
        with self._lock:
            value = self._read_verified()
            if value is None or set(value) != FIELDS:
                return None
            try:
                return validate(value)
            except ValueError:
                return None

    def load_nextcloud(self) -> dict | None:
        """Nextcloud provider works without any Gitea configuration."""
        with self._lock:
            value = self._read_verified()
            if value is None:
                return None
            if set(value) == FIELDS:
                try:
                    value = validate(value)
                except ValueError:
                    return None
            try:
                return validate_nextcloud({k: value[k] for k in NEXTCLOUD_FIELDS})
            except (ValueError, KeyError):
                return None

    def source_connections(self) -> NextcloudSource | None:
        value = self.load_nextcloud()
        if value is None:
            return None
        source = NextcloudSource("nextcloud:" + value["vault_folder"], self.rclone)
        try:
            source.validate()
        except ValueError:
            return None
        return source

    def status(self) -> dict:
        snapshot = self.connections() is not None
        source = self.source_connections() is not None
        return {"version": 1, "configured": snapshot,
                "nextcloud": "configured" if source else "missing",
                "gitea": "read_verified_at_save" if snapshot else "missing"}

    def nextcloud_status(self) -> dict:
        return {"version": 1, "configured": self.source_connections() is not None,
                "scope": "shared_nextcloud", "writes_vault": False}

    def generation(self) -> str | None:
        with self._lock:
            return hashlib.sha256(self.path.read_bytes()).hexdigest() if self.load() is not None else None

    def connections(self) -> Connections | None:
        value = self.load()
        if value is None or not self.rclone.is_file() or not self.policy.is_file():
            return None
        return Connections(self.state, "nextcloud:" + value["vault_folder"],
                           value["git_url"], value["branch"], self.policy, self.rclone,
                           git_credentials=self.path)

    def save_nextcloud(self, payload: object, *, probed: bool = False) -> bool:
        value = validate_nextcloud(payload)
        with self._lock:
            if self.load() is not None:
                raise ValueError("use_shared_full_setup_for_nextcloud_change")
            if not probed and not self.nextcloud_probe(value):
                return False
            obscured = self.obscure(value["nextcloud_password"])
            if _RCLONE_OBSCURED_RE.fullmatch(obscured) is None:
                return False
            config = _rclone_config(value, obscured)
            old = self.rclone.read_text() if self.rclone.exists() else None
            try:
                _atomic(self.rclone, config)
                _atomic(self.path, json.dumps({
                    **value, MARKER: hashlib.sha256(config.encode()).hexdigest()
                }))
            except OSError:
                if old is not None:
                    _atomic(self.rclone, old)
                else:
                    self.rclone.unlink(missing_ok=True)
                raise
            return True

    def save(self, payload: object, *, probed: bool = False) -> bool:
        value = validate(payload)
        with self._lock:
            if not probed and not self.probe(value):
                return False
            obscured = self.obscure(value["nextcloud_password"])
            if _RCLONE_OBSCURED_RE.fullmatch(obscured) is None:
                return False
            config = _rclone_config(value, obscured)
            # Policy is a fixed local default; no HTTP input reaches it.
            if not self.policy.exists():
                _atomic(self.policy, "version = 1\nsettle_seconds = 0\nstability_attempts = 1\nexclude = []\nrepository_owned = []\n")
            old = self.rclone.read_text() if self.rclone.exists() else None
            try:
                git_askpass(self.state)
                _atomic(self.rclone, config)
                _atomic(self.path, json.dumps({**value, MARKER: hashlib.sha256(config.encode()).hexdigest()}))
            except OSError:
                if old is not None:
                    _atomic(self.rclone, old)
                else:
                    self.rclone.unlink(missing_ok=True)
                raise
            return True


def _rclone_config(value: dict, obscured: str) -> str:
    return ("[nextcloud]\ntype = webdav\nurl = " + value["webdav_url"] +
            "\nvendor = nextcloud\nuser = " + value["nextcloud_user"] +
            "\npass = " + obscured + "\n")


def obscure_password(password: str) -> str:
    # rclone reads stdin; no password in command arguments, logs, or environment.
    result = subprocess.run(["rclone", "obscure", "-"], input=password + "\n",
                            capture_output=True, text=True, timeout=10, check=False)
    if result.returncode != 0:
        raise OSError("obscure_failed")
    return result.stdout.strip()


def probe_nextcloud(value: dict) -> bool:
    """Verify only the selected remote Vault via read-only WebDAV PROPFIND."""
    url = value["webdav_url"] + "/".join(quote(p) for p in value["vault_folder"].split("/")) + "/"
    basic = base64.b64encode((value["nextcloud_user"] + ":" + value["nextcloud_password"]).encode()).decode()
    request = urllib.request.Request(url, method="PROPFIND", headers={
        "Depth": "0", "Authorization": "Basic " + basic
    })
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            return response.status == 207
    except (OSError, urllib.error.URLError, ValueError):
        return False


def probe_connections(value: dict) -> bool:
    """Read-only WebDAV metadata and Git repository advertisement."""
    if not probe_nextcloud(value):
        return False
    if not private_repo(value):
        return False
    # Temporary private credential file to support Git's standard HTTPS askpass.
    with tempfile.TemporaryDirectory(prefix="snapshot-probe-") as directory:
        state = Path(directory)
        os.chmod(state, 0o700)
        path = state / "connections.json"
        _atomic(path, json.dumps(value))
        helper = git_askpass(state)
        env = git_environment(False, path, helper)
        ok, _ = _command(["git", "ls-remote", value["git_url"]], cwd=state, env=env, timeout=10)
        return ok


def private_repo(value: dict) -> bool:
    """Read the exact Gitea repository's privacy; never follow a redirect with a token."""
    try:
        parsed = urlsplit(value["git_url"])
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or not parsed.path.endswith(".git")):
            return False
        parts = parsed.path.strip("/").split("/")
        if len(parts) < 2 or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts):
            return False
        owner, name = parts[-2], parts[-1][:-4]
        if not name or name in (".", "..") or owner in (".", ".."):
            return False
        prefix = "/".join(parts[:-2])
        api = f"{parsed.scheme}://{parsed.netloc}/{prefix + '/' if prefix else ''}api/v1/repos/{owner}/{name}"
        request = urllib.request.Request(api, headers={"Authorization": "token " + value["git_token"],
                                                    "Accept": "application/json"})

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        with urllib.request.build_opener(NoRedirect()).open(request, timeout=8) as response:
            if response.status != 200 or not response.headers.get("Content-Type", "").lower().startswith("application/json"):
                return False
            raw = response.read(65537)
            if len(raw) > 65536:
                return False
        repo = json.loads(raw)
        return (type(repo) is dict and repo.get("private") is True
                and repo.get("full_name") == f"{owner}/{name}")
    except (OSError, ValueError, KeyError, TypeError, UnicodeError):
        return False
