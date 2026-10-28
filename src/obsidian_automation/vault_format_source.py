"""Bounded read-only 98-System source capture using shared Nextcloud account.

A bounded remote inventory pins every eligible filename before downloading.
Every transfer is capped, and a byte comparison checks the captured files.
No remote mutation or credential export is performed.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import select
import stat
import subprocess
import tempfile
import time
from typing import Callable

from .vault_formatter import (
    DEFAULT_MAX_BYTES, DEFAULT_MAX_FILES, DEFAULT_MAX_TOTAL_BYTES, create_plan,
)

MAX_LIST_OUTPUT = 8 * 1024 * 1024
MAX_REMOTE_TIMEOUT = 120


class FormatterSourceError(ValueError):
    pass


def _bounded_listing(argv: list[str], cwd: Path) -> bytes | None:
    """Stream JSON inventory without unbounded PIPE memory or runtime."""
    process = None
    try:
        process = subprocess.Popen(
            argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if process.stdout is None:
            return None
        limit = time.monotonic() + MAX_REMOTE_TIMEOUT
        data = bytearray()
        while True:
            remaining = limit - time.monotonic()
            if remaining <= 0:
                return None
            ready, _, _ = select.select([process.stdout], [], [], remaining)
            if not ready:
                return None
            chunk = os.read(
                process.stdout.fileno(), min(65536, MAX_LIST_OUTPUT + 1 - len(data))
            )
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > MAX_LIST_OUTPUT:
                return None
        if process.wait(timeout=max(0, limit - time.monotonic())) != 0:
            return None
        return bytes(data)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    finally:
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            if process.stdout is not None:
                process.stdout.close()


def _manifest(raw: bytes | None) -> tuple[tuple[str, int], ...] | None:
    """Admit exactly the safe, size-known remote Markdown files."""
    if raw is None or len(raw) > MAX_LIST_OUTPUT:
        return None
    try:
        rows = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        return None
    if type(rows) is not list or len(rows) > DEFAULT_MAX_FILES:
        return None
    selected: list[tuple[str, int]] = []
    names = set()
    total = 0
    for row in rows:
        if type(row) is not dict or row.get("IsDir") is not False:
            return None
        path, size = row.get("Path"), row.get("Size")
        if type(path) is not str or type(size) is not int:
            return None
        try:
            raw_path = path.encode("utf-8")
            parsed = PurePosixPath(path)
        except (UnicodeError, ValueError, TypeError):
            return None
        if (
            not raw_path or len(raw_path) > 1024
            or parsed.as_posix() != path or parsed.is_absolute()
            or len(parsed.parts) > 64
            or path.startswith(("/", "\\"))
            or "\\" in path
            or any(part in ("", ".", "..") or part.startswith(".")
                   for part in path.split("/"))
            or any(ord(char) < 32 or ord(char) == 127 for char in path)
            or parsed.suffix.lower() not in {".md", ".markdown"}
            or path in names or size < 0 or size > DEFAULT_MAX_BYTES
        ):
            return None
        total += size
        if total > DEFAULT_MAX_TOTAL_BYTES:
            return None
        names.add(path)
        selected.append((path, size))
    return tuple(sorted(selected))


def _local_capture_matches_inventory(target: Path, initial: tuple[tuple[str, int], ...]) -> bool:
    """Require exact local file names and source-admitted sizes before check.

    --files-from-raw cannot be combined with --max-size on rclone 1.60.1-DEV.
    A global hard --max-transfer cap bounds download bytes; this post-copy
    validation then rejects any file grown or replaced since remote listing.
    Only a disposable owner-private staging tree is inspected.
    """
    expected = dict(initial)
    seen: set[str] = set()
    total = 0
    try:
        for path in target.rglob("*"):
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode):
                return False
            rel = path.relative_to(target).as_posix()
            if (rel not in expected or rel in seen
                    or info.st_size != expected[rel]
                    or info.st_size > DEFAULT_MAX_BYTES):
                return False
            seen.add(rel)
            total += info.st_size
            if len(seen) > DEFAULT_MAX_FILES or total > DEFAULT_MAX_TOTAL_BYTES:
                return False
    except (OSError, ValueError):
        return False
    return seen == set(expected)


def pull_system_markdown(source, target: Path) -> bool:
    """Bound source inventory and transfers; use only remote-to-local effects."""
    source.validate()
    if target.is_symlink() or not target.is_dir():
        return False
    remote = source.rclone_remote + "/98-System"
    filters = ["--include", "*.md", "--include", "*.markdown"]
    args = ["rclone", "--config", str(source.rclone_config)]
    listing_args = (
        args + ["lsjson", remote, "--recursive", "--files-only",
                "--no-modtime", "--no-mimetype", *filters]
    )
    initial = _manifest(_bounded_listing(listing_args, target.parent))
    if initial is None:
        return False
    if not initial:
        # Empty source is a valid no-change plan. No download or upload.
        return True

    # The literal newline-separated allowlist is supported by Debian rclone
    # 1.60.1-DEV. _manifest forbids CR/LF/control-character injection; raw
    # selection preserves spaces, '#', brackets and UTF-8 filenames.
    #
    # rclone rejects --files-from-raw combined with --max-size. Partition the
    # fixed inventory into size-budgeted batches and cap *each* copy before
    # remote growth can consume the full 64-MiB budget. The download-check
    # counts both remote and local reads on this rclone version, so it needs
    # 2*expected_bytes+1. Any byte/filename difference fails closed.
    batches: list[tuple[tuple[tuple[str, int], ...], int]] = []
    pending: list[tuple[str, int]] = []
    expected_bytes = 0
    for item in initial:
        if pending and expected_bytes + item[1] > DEFAULT_MAX_BYTES:
            batches.append((tuple(pending), expected_bytes))
            pending = []
            expected_bytes = 0
        pending.append(item)
        expected_bytes += item[1]
    if pending:
        batches.append((tuple(pending), expected_bytes))

    safe = ["--retries", "1", "--low-level-retries", "1", "--checkers", "2"]
    copied: list[tuple[str, int]] = []
    for selected, batch_bytes in batches:
        listing_bytes = b"".join(
            path.encode("utf-8") + b"\n" for path, _ in selected
        )
        if len(listing_bytes) > MAX_LIST_OUTPUT:
            return False
        list_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=".formatter-allowlist-", dir=target.parent,
                mode="wb", delete=False,
            ) as handle:
                list_path = handle.name
                handle.write(listing_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            files = ["--files-from-raw", list_path]
            operations = [
                args + ["copy", remote, str(target), *files,
                        "--max-transfer", f"{batch_bytes + 1}B",
                        "--cutoff-mode", "HARD", "--transfers", "1", *safe],
                args + ["check", remote, str(target), *files, "--download",
                        "--max-transfer", f"{2 * batch_bytes + 1}B",
                        "--cutoff-mode", "HARD", *safe],
            ]
            for index, command in enumerate(operations):
                try:
                    result = subprocess.run(
                        command, cwd=target.parent,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        timeout=MAX_REMOTE_TIMEOUT, check=False,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    return False
                if result.returncode:
                    return False
                if index == 0:
                    copied.extend(selected)
                    if not _local_capture_matches_inventory(target, tuple(copied)):
                        return False
        except OSError:
            return False
        finally:
            if list_path is not None:
                try:
                    os.unlink(list_path)
                except FileNotFoundError:
                    pass

    # A changed remote manifest during download is not a verified preview.
    final = _manifest(_bounded_listing(listing_args, target.parent))
    return final == initial


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
