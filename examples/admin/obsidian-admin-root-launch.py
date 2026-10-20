"""Root-owned stdlib-only Bootstrap: VERIFY installed modules BEFORE import.

Installed at /usr/local/libexec/obsidian-admin-root-launch.py, root-owned 0755.
Executed by /usr/bin/python3 -I -S, never by a Python package entrypoint.
Both the stdlib-only first stage and verified root package second stage
disable site initialization: no .pth file or sitecustomize runs before
verified source is loaded. No third-party package imports before validation.
"""
from __future__ import annotations

import fcntl
import importlib.util
import io
import types
import marshal
import re
import struct
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

# An inherited, sealed, root-owned descriptor is intentionally harder to
# construct by accident than merely setting an environment marker. The
# privileged installer itself is trusted; hostile root is out of scope.
PROVENANCE_ENV = "OBSIDIAN_ADMIN_BOOTSTRAP_FD"
PROVENANCE_MAGIC = b"OBSIDIAN_ADMIN_VERIFIED_BOOTSTRAP_V1\x00"
PROVENANCE_SEALS = (
    fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK
    | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE
)


AUTHORITY = Path("/etc/obsidian-admin/authority.json")
VENV = Path("/opt/obsidian-automation/venv")
SYSTEMD = Path("/etc/systemd/system")
MODULES = (
    "admin_operator_runtime.py", "admin_operation_broker.py",
    "admin_operation_queue.py", "admin_control_queue_socket.py",
    "admin_control_bridge.py", "admin_systemd_admission.py",
    "admin_local_maintenance_gate.py", "admin_preview_daemon.py",
    "admin_operation_preflight.py", "admin_workflow_quiescence.py",
    "admin_broker_socket.py", "admin_operation_preview.py",
    "admin_status.py", "__init__.py",
)
UNITS = (
    "obsidian-ai-vault-pull.timer", "obsidian-ai-vault-pull.service",
    "obsidian-pre-review.timer", "obsidian-pre-review-status.service",
    "obsidian-github-sync.timer", "obsidian-github-compactor.service",
    "obsidian-github-daily-progress.timer", "obsidian-github-daily-apply.service",
    "obsidian-core-promotion.timer", "obsidian-core-promotion.service",
)


def rooted_file(path: Path, max_bytes: int) -> bytes:
    if not path.is_absolute() or ".." in path.parts:
        raise RuntimeError("unsafe_root_file")
    fd = os.open("/", os.O_DIRECTORY | os.O_RDONLY | os.O_CLOEXEC)
    try:
        for component in path.parts[1:-1]:
            child = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=fd,
            )
            info = os.fstat(child)
            if (
                not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) & 0o022
            ):
                raise RuntimeError("untrusted_source_parent")
            os.close(fd)
            fd = child
        child = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC |
            os.O_NONBLOCK, dir_fd=fd,
        )
        try:
            info = os.fstat(child)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != 0 or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) & 0o022
                or not 1 <= info.st_size <= max_bytes
            ):
                raise RuntimeError("untrusted_source_file")
            body = os.read(child, max_bytes + 1)
            if len(body) != info.st_size:
                raise RuntimeError("untrusted_source_size")
            return body
        finally:
            os.close(child)
    finally:
        os.close(fd)



def _verify_equivalent_import_cache(package_root: Path, cache_root: Path, read_pinned) -> None:
    """Permit ONLY root-owned caches with executable code equivalent to pinned root source.

    The final -I -S -B interpreter may *read* a pre-existing .pyc: -B
    prevents new cache writes, not cache reads. Therefore a source SHA alone
    cannot authorize an arbitrary cache. Compile the pinned root source (never
    execute it), compare normalized marshal-v2 code content and independently check the
    Python cache header. Reject noncanonical paths and compiled extensions.
    """
    status = cache_root.lstat()
    if (not stat.S_ISDIR(status.st_mode) or status.st_uid != 0
            or stat.S_IMODE(status.st_mode) & 0o022):
        raise RuntimeError("unreviewed_executable_import_artifact")
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
                raise RuntimeError("unreviewed_executable_import_artifact")
            match = filename.fullmatch(cache.name)
            detail = cache.lstat()
            if (
                match is None or not stat.S_ISREG(detail.st_mode)
                or detail.st_uid != 0 or detail.st_nlink != 1
                or stat.S_IMODE(detail.st_mode) & 0o022
                or not 16 < detail.st_size <= 4 * 1024 * 1024
            ):
                raise RuntimeError("unreviewed_executable_import_artifact")
            stem, optimization = match.groups()
            source = package_root / (stem + ".py")
            source_bytes = read_pinned(source, 2 * 1024 * 1024)
            cached_bytes = read_pinned(cache, 4 * 1024 * 1024)
            if cached_bytes[:4] != importlib.util.MAGIC_NUMBER:
                raise RuntimeError("unreviewed_executable_import_artifact")
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
                raise RuntimeError("unreviewed_executable_import_artifact")
            if cached_bytes[8:16] != expected_header:
                raise RuntimeError("unreviewed_executable_import_artifact")
            verified_code = compile(
                source_bytes, str(source), "exec", dont_inherit=True,
                optimize=int(optimization or 0),
            )
            # Python marshal >= 3 can encode identical CodeType values with
            # different reference-table layouts. Compare a deterministic v2
            # serialization of the *decoded code*, not the raw pyc body.
            # v2 omits reference-table encoding and includes the current
            # interpreter's executable code, constants and metadata fields.
            # Parsing must consume the entire bounded payload.
            stream = io.BytesIO(cached_bytes[16:])
            cached_code = marshal.load(stream)  # never executes bytecode
            if (
                type(cached_code) is not types.CodeType
                or stream.tell() != len(cached_bytes) - 16
                or marshal.dumps(cached_code, 2)
                != marshal.dumps(verified_code, 2)
            ):
                raise RuntimeError("unreviewed_executable_import_artifact")
    except (OSError, UnicodeError, SyntaxError, ValueError, OverflowError, struct.error, EOFError, RecursionError, SystemError):
        raise RuntimeError("unreviewed_executable_import_artifact") from None

def reject_unverified_import_artifacts(package_root: Path) -> None:
    """Reject source-shadowing native/direct bytecode and unverified caches."""
    try:
        for artifact in package_root.iterdir():
            if artifact.name == "__pycache__":
                _verify_equivalent_import_cache(
                    package_root, artifact,
                    lambda path, bound: rooted_file(path, bound),
                )
            elif artifact.suffix in (".pyc", ".pyo", ".so", ".pyd"):
                raise RuntimeError("unreviewed_executable_import_artifact")
    except (OSError, UnicodeError):
        raise RuntimeError("unsafe_import_artifact_inventory") from None


def preflight() -> Path:
    if (
        os.geteuid() != 0 or sys.flags.isolated != 1
        or sys.flags.no_site != 1
        or sys.flags.dont_write_bytecode != 1
    ):
        raise RuntimeError("bootstrap_privilege_unavailable")
    def unique(pairs):
        found = {}
        for k,v in pairs:
            if k in found:
                raise ValueError("duplicate")
            found[k] = v
        return found
    config = json.loads(
        rooted_file(AUTHORITY, 6500).decode("utf-8"),
        object_pairs_hook=unique,
    )
    if (
        type(config) is not dict
        or set(config) != {
            "version", "enabled", "revision", "web_user",
            "single_active_lxc", "unit_sha256", "module_sha256",
            "coordinated_updaters_verified", "uncoordinated_launchers_retired",
        }
        or type(config["version"]) is not int or config["version"] != 1
        or config["enabled"] is not True
        or config["single_active_lxc"] is not True
        or config["coordinated_updaters_verified"] is not True
        or config["uncoordinated_launchers_retired"] is not True
        or config["web_user"] != "obsidian-admin-web"
        or type(config["revision"]) is not str or len(config["revision"]) != 40
        or any(c not in "0123456789abcdef" for c in config["revision"])
        or type(config["unit_sha256"]) is not dict
        or set(config["unit_sha256"]) != set(UNITS)
        or type(config["module_sha256"]) is not dict
        or set(config["module_sha256"]) != set(MODULES)
    ):
        raise RuntimeError("not_approved_revision")

    # Find exactly one root-installed interpreter ABI. No path is accepted
    # from browser, environment or CLI.
    libdir = VENV / "lib"
    candidates = sorted(libdir.glob("python3.*/site-packages/obsidian_automation"))
    if len(candidates) != 1:
        raise RuntimeError("ambiguous_root_package")
    code = candidates[0]
    for name, hashes, directory, bound in (
        *((name, config["unit_sha256"], SYSTEMD, 16384) for name in UNITS),
        *((name, config["module_sha256"], code, 2 * 1024 * 1024) for name in MODULES),
    ):
        expected = hashes[name]
        if (
            type(expected) is not str or len(expected) != 64
            or any(c not in "0123456789abcdef" for c in expected)
        ):
            raise RuntimeError("bad_revision_hash")
        if hashlib.sha256(rooted_file(directory/name, bound)).hexdigest() != expected:
            raise RuntimeError("installed_source_mismatch")
    reject_unverified_import_artifacts(code)
    # Python interpreter identity is pinned to a ROOT-MANAGED OS location,
    # not a mutable virtualenv/bin/python symlink. The site-packages import
    # directory is already independently SHA-pinned via strict owned paths.
    # Reject ABI mismatch: Python -S does not resolve a venv's sys.prefix.
    expected_lib = f"python{sys.version_info.major}.{sys.version_info.minor}"
    if code.parent.parent.name != expected_lib:
        raise RuntimeError("python_abi_mismatch")
    system_python = Path("/usr/bin/python3")
    if (
        not system_python.resolve(strict=True).is_relative_to(Path("/usr/bin"))
        or not system_python.parent.is_dir()
    ):
        raise RuntimeError("system_python_not_qualified")
    for component in (
        system_python.parent,
        system_python,
        system_python.resolve(strict=True),
    ):
        info = component.lstat()
        if (
            info.st_uid != 0
            # Linux reports permissions 0777 for ordinary symlinks; those
            # bits do not grant authority to replace a root-owned link in
            # /usr/bin. Verify link ownership and separately pin the fully
            # resolved interpreter to a root-owned, non-writable /usr/bin
            # binary. Keep strict mode checks for directories/regular files.
            or (not stat.S_ISLNK(info.st_mode)
                and stat.S_IMODE(info.st_mode) & 0o022)
            or not (stat.S_ISDIR(info.st_mode)
                    or stat.S_ISREG(info.st_mode)
                    or stat.S_ISLNK(info.st_mode))
        ):
            raise RuntimeError("system_python_not_qualified")
    # The parent /usr/bin is verified root-owned and not writable by Web/peer
    # identities; changing the binary between validation and exec requires a
    # trusted root update actor (excluded by the root trust model).
    # Every pinned package file is resolved through a root-owned, symlink-free
    # directory chain. The second stage imports ONLY from this approved
    # site-packages root, *without* Python's site initialization.
    return code.parent


# The only second-stage Python code executed before the attested package
# import is this frozen stdlib-only snippet. The installed root launcher,
# NOT browser headers, supplies its verified absolute site-packages path.
_RUNTIME_CODE = (
    "import sys;"
    "sys.path.insert(0,sys.argv[1]);"
    "from obsidian_automation import admin_operator_runtime as runtime;"
    "raise SystemExit(runtime.main())"
)


def main() -> int:
    try:
        verified_site_root = preflight()
        # Supply the *second* startup-stage guard with a sealed memfd that
        # binds to the very authority bytes the bootstrap just validated.
        # No privileged Python package has been imported at this point.
        manifest_digest = hashlib.sha256(rooted_file(AUTHORITY,6500)).digest()
        fd = os.memfd_create(
            "obsidian-admin-bootstrap", flags=os.MFD_ALLOW_SEALING,
        )
        try:
            value = PROVENANCE_MAGIC + manifest_digest
            if os.write(fd,value) != len(value):
                raise RuntimeError("incomplete_bootstrap_provenance")
            os.fsync(fd)
            fcntl.fcntl(fd,fcntl.F_ADD_SEALS,PROVENANCE_SEALS)
            if (fcntl.fcntl(fd,fcntl.F_GET_SEALS) & PROVENANCE_SEALS) != PROVENANCE_SEALS:
                raise RuntimeError("bootstrap_provenance_unsealed")
            os.set_inheritable(fd,True)
            executable = "/usr/bin/python3"
            os.execve(
                executable,
                [
                    executable, "-I", "-S", "-B", "-c", _RUNTIME_CODE,
                    str(verified_site_root),
                ],
                {
                    "PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C",
                    PROVENANCE_ENV: str(fd),
                },
            )
        finally:
            os.close(fd)
    except (OSError, ValueError, TypeError, RuntimeError, UnicodeError):
        raise SystemExit("obsidian_operator_bootstrap_rejected") from None
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
