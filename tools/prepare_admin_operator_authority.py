#!/usr/bin/env python3
"""Write the exact installed-service/module digest inventory, ENABLED FALSE.

This is a source staging helper, not an authority grant or installer.
No systemd unit is enabled/started/stopped by this tool. It refuses existing
authority documents, symlinks or non-root and never prints credential bytes.
Use ONLY after installing reviewed source and units by a separate approved
installation process.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys

# Stage-only helper is deliberately stdlib-only. Invoking it as root must
# not import a mutable application package before reading/validating any
# installed package files, even while generating an OFF-by-default manifest.
import hashlib
import importlib.util
import marshal
import re
import struct

DEFAULT_AUTHORITY_PATH = Path("/etc/obsidian-admin/authority.json")
SOURCE_ONLY_PYCACHE_PREFIX = "/dev/null/obsidian-admin-source-only"
VENV = Path("/opt/obsidian-automation/venv")
_UNITS = frozenset((
    "obsidian-ai-vault-pull.timer", "obsidian-ai-vault-pull.service",
    "obsidian-pre-review.timer", "obsidian-pre-review-status.service",
    "obsidian-github-sync.timer", "obsidian-github-compactor.service",
    "obsidian-github-daily-progress.timer", "obsidian-github-daily-apply.service",
    "obsidian-core-promotion.timer", "obsidian-core-promotion.service",
))
_AUTHORITY_MODULES = (
    "admin_operator_runtime.py", "admin_operation_broker.py",
    "admin_operation_queue.py", "admin_control_queue_socket.py",
    "admin_control_bridge.py", "admin_systemd_admission.py",
    "admin_local_maintenance_gate.py", "admin_preview_daemon.py",
    "admin_operation_preflight.py", "admin_workflow_quiescence.py",
    "admin_broker_socket.py", "admin_operation_preview.py",
    "admin_status.py", "__init__.py",
)


def _pinned_root_regular(path: Path, *, max_bytes: int) -> bytes:
    if not path.is_absolute() or ".." in path.parts:
        raise RuntimeError("unsafe_installed_file")
    parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for component in path.parts[1:-1]:
            child = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent,
            )
            info = os.fstat(child)
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) & 0o022
            ):
                raise RuntimeError("unsafe_installed_parent")
            os.close(parent)
            parent = child
        fd = os.open(
            path.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=parent,
        )
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != 0 or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) & 0o022
                or not 1 <= info.st_size <= max_bytes
            ):
                raise RuntimeError("unsafe_installed_file")
            value = os.read(fd, max_bytes + 1)
            if len(value) != info.st_size:
                raise RuntimeError("unverifiable_installed_file")
            return value
        finally:
            os.close(fd)
    finally:
        os.close(parent)





def _verify_equivalent_import_cache(package_root: Path, cache_root: Path, read_pinned) -> None:
    """Permit ONLY root-owned caches byte-for-byte equivalent to trusted source.

    The final -I -S -B interpreter may *read* a pre-existing .pyc: -B
    prevents new cache writes, not cache reads. Therefore a source SHA alone
    cannot authorize an arbitrary cache. Compile the pinned root source (never
    execute it), compare exact marshal bytes, and independently check the
    Python cache header. Reject noncanonical paths and compiled extensions.
    """
    status = cache_root.lstat()
    if (not stat.S_ISDIR(status.st_mode) or status.st_uid != 0
            or stat.S_IMODE(status.st_mode) & 0o022):
        raise RuntimeError("unreviewed_installed_import_artifact")
    tag = re.escape(sys.implementation.cache_tag)
    filename = re.compile(
        r"([A-Za-z_][A-Za-z0-9_]*)\." + tag +
        r"(?:\.opt-([12]))?\.pyc\Z"
    )
    try:
        found = 0
        for cache in cache_root.iterdir():
            found += 1
            if found > 256:
                raise RuntimeError("unreviewed_installed_import_artifact")
            match = filename.fullmatch(cache.name)
            detail = cache.lstat()
            if (
                match is None or not stat.S_ISREG(detail.st_mode)
                or detail.st_uid != 0 or detail.st_nlink != 1
                or stat.S_IMODE(detail.st_mode) & 0o022
                or not 16 < detail.st_size <= 4 * 1024 * 1024
            ):
                raise RuntimeError("unreviewed_installed_import_artifact")
            stem, optimization = match.groups()
            source = package_root / (stem + ".py")
            source_bytes = read_pinned(source, 2 * 1024 * 1024)
            cached_bytes = read_pinned(cache, 4 * 1024 * 1024)
            if cached_bytes[:4] != importlib.util.MAGIC_NUMBER:
                raise RuntimeError("unreviewed_installed_import_artifact")
            flags = struct.unpack("<I", cached_bytes[4:8])[0]
            if flags == 0:
                # Default timestamp-based CPython cache (PEP 3147).
                source_stat = source.stat()
                expected_header = struct.pack(
                    "<II", int(source_stat.st_mtime) & 0xffffffff,
                    len(source_bytes) & 0xffffffff,
                )
            elif flags in (1, 3):
                # Hash-based CPython cache (PEP 552); both modes are safe only
                # when the compiled code still exactly equals the source.
                expected_header = importlib.util.source_hash(source_bytes)
            else:
                raise RuntimeError("unreviewed_installed_import_artifact")
            if cached_bytes[8:16] != expected_header:
                raise RuntimeError("unreviewed_installed_import_artifact")
            verified_code = compile(
                source_bytes, str(source), "exec", dont_inherit=True,
                optimize=int(optimization or 0),
            )
            if cached_bytes[16:] != marshal.dumps(verified_code):
                raise RuntimeError("unreviewed_installed_import_artifact")
    except (OSError, UnicodeError, SyntaxError, ValueError, OverflowError, struct.error):
        raise RuntimeError("unreviewed_installed_import_artifact") from None

def _verify_import_inventory(module_dir: Path) -> None:
    try:
        for artifact in module_dir.iterdir():
            if artifact.name == "__pycache__":
                if (
                    getattr(sys, "pycache_prefix", None) != SOURCE_ONLY_PYCACHE_PREFIX
                    or sys.flags.dont_write_bytecode != 1
                ):
                    _verify_equivalent_import_cache(
                        module_dir, artifact,
                        lambda path, bound: _pinned_root_regular(
                            path, max_bytes=bound,
                        ),
                    )
            elif artifact.suffix in (".pyc", ".pyo", ".so", ".pyd"):
                raise RuntimeError("unreviewed_installed_import_artifact")
    except (OSError, UnicodeError):
        raise RuntimeError("unverifiable_installed_import_inventory") from None


_SHA = re.compile(r"[0-9a-f]{40}\Z")


def stage(revision: str) -> None:
    if os.geteuid() != 0:
        raise RuntimeError("stage_requires_root")
    if (
        sys.flags.isolated != 1 or sys.flags.no_site != 1
        or sys.flags.dont_write_bytecode != 1
        or getattr(sys, "pycache_prefix", None) != SOURCE_ONLY_PYCACHE_PREFIX
        or not stat.S_ISCHR(Path("/dev/null").lstat().st_mode)
    ):
        raise RuntimeError("isolated_python_required")
    if type(revision) is not str or _SHA.fullmatch(revision) is None:
        raise RuntimeError("invalid_revision")
    # Existing parent must be root-owned 0700; never mkdir or follow symlinks
    # on behalf of this helper.
    directory = DEFAULT_AUTHORITY_PATH.parent
    info = directory.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != 0
        or stat.S_IMODE(info.st_mode) != 0o710
        or directory.is_symlink()
    ):
        raise RuntimeError("unsafe_authority_directory")
    # Locate only the reviewed installed package. Never import code before
    # all its SHA digests have been staged and enabled by a separate review.
    candidates = sorted((VENV/"lib").glob(
        "python3.*/site-packages/obsidian_automation",
    ))
    if len(candidates) != 1:
        raise RuntimeError("ambiguous_root_package")
    module_dir = candidates[0]
    # Python -B prevents writes, not reads. Accept only legitimate, pinned
    # source-equivalent CPython cache bytes, never unverified bytecode/native.
    _verify_import_inventory(module_dir)
    unit_dir = Path("/etc/systemd/system")

    units = {}
    for unit in sorted(_UNITS):
        units[unit] = hashlib.sha256(
            _pinned_root_regular(unit_dir / unit, max_bytes=16384)
        ).hexdigest()
    code = {}
    for file in _AUTHORITY_MODULES:
        code[file] = hashlib.sha256(
            _pinned_root_regular(module_dir / file, max_bytes=2*1024*1024)
        ).hexdigest()
    document = {
        "version": 1, "enabled": False, "revision": revision,
        "single_active_lxc": True, "web_user": "obsidian-admin-web",
        # These attestations can only be turned on after a separate Human
        # cutover review of actual installed update/launcher paths. Merely
        # staging an approved SHA is never authority for systemd effects.
        "coordinated_updaters_verified": False,
        "uncoordinated_launchers_retired": False,
        "unit_sha256": units, "module_sha256": code,
    }
    payload = (json.dumps(document, sort_keys=True,separators=(",",":"))+"\n").encode()
    if len(payload) > 6500:
        raise RuntimeError("authority_over_bound")
    fd = None
    try:
        fd = os.open(
            DEFAULT_AUTHORITY_PATH,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0 or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise RuntimeError("unsafe_created_authority")
        offset = 0
        while offset < len(payload):
            written = os.write(fd,payload[offset:])
            if written <= 0:
                raise RuntimeError("authority_write_unavailable")
            offset += written
        os.fsync(fd)
        dir_fd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if fd is not None:
            os.close(fd)
    # No secrets or full file paths are printed; only stable status summary.
    print("STAGED_DISABLED_EXACT_AUTHORITY")
    print("REVISION="+revision)
    print("UNIT_COUNT="+str(len(units)))
    print("MODULE_COUNT="+str(len(code)))
    print("ENABLED=false")


def main(argv=None) -> int:
    parser=argparse.ArgumentParser(description="Stage disabled root authority")
    parser.add_argument("--revision",required=True)
    args=parser.parse_args(argv)
    try:
        stage(args.revision)
    except (OSError,ValueError,TypeError,RuntimeError) as exc:
        raise SystemExit(str(exc)) from None
    return 0

if __name__=="__main__":
    raise SystemExit(main())
