"""Read-only 98-System source capture via the existing single Nextcloud account.

No provider PUT/DELETE; rclone copies eligible Markdown to a disposable local
mirror that is scanned by vault_formatter. All remote diagnostics stay private.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from typing import Callable

from .vault_formatter import create_plan, DEFAULT_MAX_BYTES


class FormatterSourceError(ValueError):
    pass


def pull_system_markdown(source, target: Path) -> bool:
    """Strictly remote-to-local copy and check; no Vault mutation."""
    source.validate()
    if target.is_symlink() or not target.is_dir():
        return False
    remote = source.rclone_remote + "/98-System"
    filters = ["--include", "*.md", "--include", "*.markdown"]
    args = ["rclone", "--config", str(source.rclone_config)]
    safe = ["--retries", "1", "--low-level-retries", "1", "--checkers", "2"]
    jobs = [
        args + ["copy", remote, str(target), *filters, *safe],
        args + ["check", remote, str(target), *filters, *safe],
    ]
    for command in jobs:
        try:
            result = subprocess.run(
                command, cwd=target.parent,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=150, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        if result.returncode:
            return False
    return True


class FormatterSource:
    """Independent source reader, sharing B2/B4 Nextcloud credentials only."""

    def __init__(self, state: Path, shared, *, copier: Callable = pull_system_markdown):
        self.state = state
        self.shared = shared
        self.copier = copier

    def _shared(self):
        source_loader = getattr(self.shared, "source_connections", None)
        settings_loader = getattr(self.shared, "load_nextcloud", None)
        if not callable(source_loader) or not callable(settings_loader):
            raise FormatterSourceError("formatter_source_unavailable")
        source = source_loader()
        details = settings_loader()
        if source is None or type(details) is not dict:
            raise FormatterSourceError("formatter_source_unavailable")
        source.validate()
        required = ("webdav_url", "vault_folder", "nextcloud_user", "nextcloud_password")
        if any(type(details.get(key)) is not str or not details[key] for key in required):
            raise FormatterSourceError("formatter_source_unavailable")
        encoded = json.dumps(
            {key: details[key] for key in required},
            sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")
        generation = hashlib.sha256(encoded).hexdigest()
        return source, generation

    def generation(self) -> str | None:
        try:
            return self._shared()[1]
        except (FormatterSourceError, OSError, ValueError):
            return None

    def configured(self) -> bool:
        return self.generation() is not None

    def __call__(self) -> dict:
        source, generation = self._shared()
        with tempfile.TemporaryDirectory(prefix=".formatter-mirror-", dir=self.state) as directory:
            root = Path(directory)
            target = root / "98-System"
            target.mkdir(mode=0o700)
            if not self.copier(source, target):
                raise FormatterSourceError("formatter_source_pull_failed")
            # The source identity must not have changed while scanning.
            plan = create_plan(root)
            if generation != self.generation():
                raise FormatterSourceError("formatter_source_changed")
            return plan
