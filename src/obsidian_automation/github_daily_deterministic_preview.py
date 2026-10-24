"""Offline read-only Daily section preview from an exact Collector CAS index."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
import os
import secrets
from pathlib import Path
import stat
import sys
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from .github_daily_deterministic_digest import (
    DeterministicDigestError, build_digest_from_verified_capture,
)
from .github_daily_progress import MAX_DAILY_BYTES, DailyProgressError, prepare_daily_update


def _private_root(root: Path) -> None:
    if not root.is_absolute() or '..' in root.parts:
        raise DeterministicDigestError('output directory must be absolute without traversal')
    ancestor = Path('/')
    for part in root.parts[1:]:
        ancestor = ancestor / part
        if stat.S_ISLNK(os.lstat(ancestor).st_mode):
            raise DeterministicDigestError('output path traverses a symlink')
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        meta = os.fstat(fd)
        if (not stat.S_ISDIR(meta.st_mode) or meta.st_uid != os.geteuid()
                or stat.S_IMODE(meta.st_mode) & 0o077):
            raise DeterministicDigestError('output directory must be owned and private')
    finally:
        os.close(fd)


def _read_daily(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        meta = os.fstat(fd)
        if not stat.S_ISREG(meta.st_mode) or meta.st_size > MAX_DAILY_BYTES:
            raise DeterministicDigestError('Daily input is not a bounded regular file')
        with os.fdopen(os.dup(fd), 'rb') as input_file:
            data = input_file.read(MAX_DAILY_BYTES + 1)
        if len(data) != meta.st_size:
            raise DeterministicDigestError('Daily input changed during read')
        return data
    finally:
        os.close(fd)


def _matches_existing(directory: int, name: str, content: bytes) -> bool:
    """Accept a retry only when the already-published artifact is byte-identical."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=directory)
    except FileNotFoundError:
        return False
    try:
        meta = os.fstat(fd)
        if (not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.geteuid()
                or meta.st_size != len(content)):
            raise DeterministicDigestError('preview artifact conflicts with retry')
        with os.fdopen(os.dup(fd), 'rb') as source:
            if source.read(len(content) + 1) != content:
                raise DeterministicDigestError('preview artifact conflicts with retry')
        return True
    finally:
        os.close(fd)


def _preflight(root: Path, files: list[tuple[str, bytes]]) -> None:
    """Reject a conflicting artifact before writing any part of the new set."""
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name, content in files:
            _matches_existing(directory, name, content)
    finally:
        os.close(directory)


def _publish(root: Path, name: str, content: bytes) -> None:
    # Write and fsync a private temporary inode, then link into place without
    # replacement. After an interrupted multi-artifact run, identical files
    # may be reused; a partial or different final file is never overwritten.
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if _matches_existing(directory, name, content):
            os.fsync(directory)
            return
        temporary = f'.{name}.{secrets.token_hex(12)}.pending'
        created = False
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory)
            created = True
            with os.fdopen(fd, 'wb') as target:
                target.write(content)
                target.flush()
                os.fsync(target.fileno())
            try:
                os.link(temporary, name, src_dir_fd=directory,
                        dst_dir_fd=directory, follow_symlinks=False)
            except FileExistsError:
                if not _matches_existing(directory, name, content):
                    raise DeterministicDigestError('preview artifact conflicts with retry')
        finally:
            if created:
                os.unlink(temporary, dir_fd=directory)
        os.fsync(directory)
    finally:
        os.close(directory)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Offline no-LLM Collector CAS Daily preview')
    parser.add_argument('--capture-sha', required=True)
    day = parser.add_mutually_exclusive_group(required=True)
    day.add_argument('--target-date')
    day.add_argument('--yesterday', action='store_true')
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--standard-cas-root', type=Path,
                        help='Explicit private single-user CAS root (Standard profile)')
    parser.add_argument('--preview-daily', type=Path)
    args = parser.parse_args(argv)
    if args.preview_daily and not args.output_dir:
        parser.error('--preview-daily requires --output-dir')
    try:
        if args.target_date:
            expected = date.fromisoformat(args.target_date).isoformat()
            if expected != args.target_date:
                raise DeterministicDigestError('noncanonical target date')
        else:
            expected = (datetime.now(ZoneInfo('Asia/Tokyo')).date() -
                        timedelta(days=1)).isoformat()
        if args.standard_cas_root is None:
            digest = build_digest_from_verified_capture(args.capture_sha)
        else:
            from .github_daily_standard_source import read_standard_digest, StandardSourceError
            try:
                digest = read_standard_digest(args.standard_cas_root, args.capture_sha)
            except (StandardSourceError, OSError) as exc:
                raise DeterministicDigestError('Standard source unavailable') from exc
        if digest.capture_sha256 != args.capture_sha or digest.date != expected:
            raise DeterministicDigestError('capture identity or target JST day differs')
        markdown = digest.render().encode('utf-8')
        if args.output_dir is None:
            sys.stdout.buffer.write(markdown)
            return 0
        root = args.output_dir
        _private_root(root)
        files = [(f'{digest.sha256}.deterministic-daily.md', markdown),
                 (f'{digest.sha256}.deterministic-daily.json', digest.canonical_bytes)]
        if args.preview_daily:
            original = _read_daily(args.preview_daily)
            _, changed = prepare_daily_update(
                SimpleNamespace(section_body=digest.render().rstrip('\n') + '\n\n'),
                original)
            files.append((f'{digest.sha256}.daily-note.preview.md', changed))
        _preflight(root, files)
        for name, content in files:
            _publish(root, name, content)
        print('PREVIEW_ONLY MODEL_USED=false PUBLICATION_AUTHORIZED=false')
        for name, _ in files:
            print(f'ARTIFACT={root / name}')
        return 0
    except (DeterministicDigestError, DailyProgressError, OSError, ValueError,
            UnicodeError) as exc:
        print(f'deterministic-daily-preview blocked: {type(exc).__name__}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
