"""B4 GitHub Connections with the existing B2 Nextcloud account.

Only a GitHub token is new. Project notes are pulled into disposable local
staging before each no-LLM Preview run; the remote Vault is never written.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
import hashlib
from dataclasses import replace
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading

from . import github_daily_runonce as daily
from . import github_project_watcher as watcher
from . import github_daily_deterministic_preview as preview
from .snapshot_setup import NextcloudSource


class DailySetupError(ValueError):
    pass


class DailySetup:
    """Store one GitHub token and reuse a read-only shared Nextcloud connection."""

    def __init__(self, state: Path, shared, *, probe=None, copier=None):
        self.state = state
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.shared = shared
        self._lock = threading.RLock()
        self.probe = probe or probe_github
        self.copier = copier or pull_project_notes

    def _shared(self):
        # Prefer the shared provider-only API, which is valid without
        # Gitea. Retain support for the existing injected B2 test fixtures.
        # SetupStore.save() holds this SAME RLock while replacing both
        # rclone.conf and the connection JSON. Capture source and semantic
        # generation together, not across two independent rotations.
        shared_lock = getattr(self.shared, "_lock", None)
        with shared_lock if shared_lock is not None else nullcontext():
            source = getattr(
                self.shared, "source_connections", self.shared.connections
            )
            connections = source()
            if connections is None:
                raise DailySetupError("nextcloud_not_configured")
            connections.validate()
            # Bind semantic WebDAV credentials and the selected Vault, not
            # rclone's obscured password bytes (which may use fresh salt).
            loader = getattr(self.shared, "load_nextcloud", self.shared.load)
            shared_values = loader()
            fields = (
                "webdav_url", "vault_folder",
                "nextcloud_user", "nextcloud_password",
            )
            if (type(shared_values) is not dict
                    or any(type(shared_values.get(key)) is not str
                           for key in fields)):
                raise DailySetupError("nextcloud_config_unavailable")
            raw = json.dumps(
                {key: shared_values[key] for key in fields},
                ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")
            generation = hashlib.sha256(raw).hexdigest()
            return connections, generation

    def source_fingerprint(self) -> str | None:
        try:
            return self._shared()[1]
        except (OSError, ValueError):
            return None

    @contextmanager
    def phase_receipt_guard(self):
        """Short phase-only source/receipt fence; never hold during I/O.

        Always acquire DailySetup._lock before SetupStore._lock, matching
        project_source() and configured(). In particular, taking the shared
        lock first and then querying configured() can deadlock against B4.
        A missing shared rotation lock must fail closed for phase receipts.
        """
        with self._lock:
            rotation_lock = getattr(self.shared, "_lock", None)
            if rotation_lock is None:
                raise DailySetupError("phase_source_lock_unavailable")
            with rotation_lock:
                yield

    def configured(self) -> bool:
        try:
            with self._lock:
                _, generation = self._shared()
                if daily._private_file(self.state / "source-generation",
                                       maximum=65).decode("ascii") != generation:
                    return False
                daily.load_run_config(self.state)
                return True
        except (OSError, UnicodeError, ValueError, daily.DailyPreviewRunError):
            return False

    def status(self) -> dict[str, object]:
        with self._lock:
            ready = self.configured()
            source = getattr(self.shared, "source_connections", self.shared.connections)
            available = source() is not None
            return {
                "version": 1, "configured": ready,
                "github": "configured" if ready else "missing",
                "nextcloud_source": "configured" if available else "missing",
                "project_mirror": "pulled_per_run",
                "vault_write": False,
            }

    @staticmethod
    def _atomic(path: Path, data: bytes) -> None:
        fd, temporary = tempfile.mkstemp(prefix=".github-daily-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def validate_payload(payload: object) -> str:
        if type(payload) is not dict or set(payload) != {"github_token"}:
            raise DailySetupError("invalid_github_connection")
        token = payload["github_token"]
        if type(token) is not str or daily._TOKEN.fullmatch(token) is None:
            raise DailySetupError("invalid_github_connection")
        return token

    def save(self, payload: object) -> bool:
        """Validate the GitHub credential before changing the private store."""
        token = self.validate_payload(payload)
        with self._lock:
            _, generation = self._shared()
            if not self.probe(token):
                return False
            preview._private_root(self.state)
            # Same-user 0700 app state, with no legacy Collector/Reader users.
            for name in ("mirror", "cas", "cas/evidence", "cas/receipt",
                         "cas/capture", "preview"):
                path = self.state / name
                path.mkdir(mode=0o700, parents=True, exist_ok=True)
                preview._private_root(path)
            watcher_config = (
                "[watcher]\n"
                "vault_root = " + json.dumps(str(self.state / "mirror")) + "\n"
                "state_db = " + json.dumps(str(self.state / "watcher.sqlite3")) + "\n"
                'project_folder = "10-Project"\n'
                'github_api_base = "https://api.github.com"\n'
                "request_timeout_seconds = 15\n"
            ).encode("utf-8")
            self._atomic(self.state / "github.token", (token + "\n").encode("ascii"))
            self._atomic(self.state / "watcher.toml", watcher_config)
            # Commit source-generation last. Changed B2 Connections invalidate
            # old B4 config without a second Nextcloud credential.
            self._atomic(self.state / "source-generation", generation.encode("ascii"))
            return self.configured()

    @contextmanager
    def project_source(self, *, expected_generation: str | None = None):
        """Fetch only local Project notes into a fresh, disposable mirror.

        The shared B2 Nextcloud/WebDAV account is read-only here. Caller uses
        the validated mirror for run_once and the temporary tree is removed
        even after an API failure. No remote push, upload, DELETE, or MOVE.
        """
        # The disposable directory also owns any private, source-pinned
        # rclone.conf copy. No secret is transferred to another trust domain.
        with tempfile.TemporaryDirectory(
                prefix=".project-mirror-", dir=self.state) as directory:
            root = Path(directory)
            with self._lock:
                if not self.configured():
                    raise DailySetupError("github_daily_not_configured")
                if expected_generation is None:
                    # Existing B4/unpinned source path is unchanged.
                    connections, generation = self._shared()
                    config, token = daily.load_run_config(self.state)
                else:
                    # SetupStore.save() holds this same shared RLock across
                    # rclone.conf and JSON rotation. Freeze credentials and
                    # their semantic generation together, then copy the
                    # rclone config into this 0700 disposable source before
                    # allowing connection saves to proceed concurrently.
                    lock = getattr(self.shared, "_lock", None)
                    with lock if lock is not None else nullcontext():
                        connections, generation = self._shared()
                        config, token = daily.load_run_config(self.state)
                        if (type(expected_generation) is not str
                                or generation != expected_generation):
                            raise DailySetupError("source_connections_changed")
                        try:
                            original = Path(connections.rclone_config)
                            if original.is_symlink():
                                raise DailySetupError("source_connections_changed")
                            content = original.read_bytes()
                            if not 0 < len(content) <= 65536:
                                raise DailySetupError("source_connections_changed")
                            pinned = root / ".pinned-rclone.conf"
                            fd = os.open(
                                pinned, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                            )
                            with os.fdopen(fd, "wb") as writer:
                                writer.write(content)
                                writer.flush()
                                os.fsync(writer.fileno())
                            connections = NextcloudSource(
                                connections.rclone_remote, pinned,
                            )
                        except OSError:
                            raise DailySetupError(
                                "source_connections_changed"
                            ) from None
            # The remote copier uses a private immutable config snapshot for
            # phase-admitted runs. No Connections/UI lock is held across
            # remote rclone or GitHub GETs.
            project_dir = root / "10-Project"
            project_dir.mkdir(mode=0o700)
            if not self.copier(connections, project_dir):
                raise DailySetupError("project_mirror_pull_failed")
            projects, _warnings = watcher.scan_projects(root)
            if not projects:
                raise DailySetupError("no_watched_github_projects")
            if generation != self.source_fingerprint():
                raise DailySetupError("source_connections_changed")
            yield replace(config, vault_root=root), token
            # A concurrently replaced B2 connection cannot make old-source
            # preview_ready look like a successful run under new settings.
            if generation != self.source_fingerprint():
                raise DailySetupError("source_connections_changed")


def probe_github(token: str) -> bool:
    """A bounded authenticated GitHub.com GET validates the configured token."""
    try:
        value = watcher.GitHubClient(
            api_base="https://api.github.com", token=token, timeout_seconds=10
        )._request_json("/user")
        return type(value) is dict and isinstance(value.get("login"), str) and bool(
            value["login"])
    except (OSError, ValueError, watcher.GitHubProjectWatcherError):
        return False


def pull_project_notes(connections, target: Path) -> bool:
    """Use existing B2 rclone config for a GET-only remote-to-local copy."""
    connections.validate()
    if not target.is_dir() or target.is_symlink():
        return False
    source = connections.rclone_remote + "/10-Project"
    argv = ["rclone", "--config", str(connections.rclone_config), "copy",
            source, str(target), "--include", "*.md",
            "--retries", "1", "--low-level-retries", "1"]
    try:
        result = subprocess.run(argv, cwd=target.parent, capture_output=True,
                                timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    # Never expose rclone logs: they can contain private URLs/paths.
    return result.returncode == 0
