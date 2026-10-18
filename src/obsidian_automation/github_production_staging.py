"""Two-phase, explicitly approved GitHub updater for #294.

The legacy all-in-one updater is kept intact. Stage must not run live smoke.
Activation cannot infer approval from SSH connectivity or from Stage success.
"""
from __future__ import annotations

import argparse
import ast
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from . import github_production_update as legacy

VERSION = 1  # host-compatible control and activation envelope
STAGE_RECEIPT_VERSION = 2  # build-environment manifest is mandatory in v2
PRODUCTION_STAGE_VENV_ROOT = Path("/opt/obsidian-automation/venv")
# The only accepted startup .pth is the root-owned setuptools 78.1.1 wheel's
# reviewed distutils shim (exact byte digest from the audited production wheel).
# Any additional .pth, path insertion, or arbitrary startup import is refused.
REVIEWED_DISTUTILS_PTH_SHA256 = "2638ce9e2500e572a5e0de7faed6661eb569d1b696fcba07b0dd223da5f5d224"
STAGING_ROOT = "staged-github-production-v1"
CONTROL_FILE = "control.json"
SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_RECORD = 32768
DEFAULT_CONFIG = Path("/etc/obsidian-github-summarizer/config.env")
def _trusted_stage_runner(argv: Sequence[str]) -> legacy.CommandResult:
    """Sanitize privileged Git/pip execution without changing other role smokes."""
    if not argv:
        raise legacy.ProductionUpdateError("empty Stage command")
    if argv[0] not in {"git"} and not (
        Path(argv[0]).is_absolute() and Path(argv[0]).name == "pip"
    ):
        return legacy._default_runner(argv)

    env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/root",
           "LC_ALL": "C.UTF-8"}
    if argv[0] == "git":
        env.update({
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
        })
        # Only the explicitly reviewed Adam SSH agent is accepted by Stage.
        if os.environ.get("SSH_AUTH_SOCK") == "/run/user/1000/ssh-agent":
            env["SSH_AUTH_SOCK"] = "/run/user/1000/ssh-agent"
        command = (
            "/usr/bin/git", "-c", "core.fsmonitor=false",
            "-c", "core.hooksPath=/dev/null", *argv[1:],
        )
    else:
        env.update({"PIP_CONFIG_FILE": "/dev/null", "PIP_NO_INPUT": "1",
                    "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PYTHONNOUSERSITE": "1",
                    "PYTHONDONTWRITEBYTECODE": "1"})
        command = tuple(argv)
    result = subprocess.run(
        command, check=False, capture_output=True, text=True, env=env,
    )
    return legacy.CommandResult(result.returncode, result.stdout, result.stderr)


EFFECT_SERVICES = (
    "obsidian-github-writer.service",
    "obsidian-github-compactor.service",
    "obsidian-github-sync.service",
    "obsidian-github-daily-summary.service",
    "obsidian-github-daily-apply.service",
)

def _encode(obj: Mapping[str, object]) -> bytes:
    return (json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode()

def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def _read_file(path: Path, max_bytes: int = MAX_RECORD) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > max_bytes:
            raise legacy.ProductionUpdateError("untrusted staging file type or size")
        payload = bytearray()
        while len(payload) <= max_bytes:
            part = os.read(fd, min(65536, max_bytes + 1 - len(payload)))
            if not part:
                break
            payload.extend(part)
        if len(payload) != info.st_size or len(payload) > max_bytes:
            raise legacy.ProductionUpdateError("staging file changed while reading")
        return bytes(payload)
    finally:
        os.close(fd)

def _decode(data: bytes) -> dict[str, object]:
    try:
        obj = json.loads(data)
    except (ValueError, UnicodeDecodeError) as exc:
        raise legacy.ProductionUpdateError("staging record JSON invalid") from exc
    if not isinstance(obj, dict) or _encode(obj) != data:
        raise legacy.ProductionUpdateError("staging JSON is noncanonical")
    return obj

def _validate_dir(path: Path, *, private: bool) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != os.geteuid():
        raise legacy.ProductionUpdateError("staging path ownership/type invalid")
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o022 or (private and mode & 0o077):
        raise legacy.ProductionUpdateError("staging path grants excess permissions")

def _state_dir(root: Path, *, create: bool) -> Path:
    _validate_dir(root, private=False)
    result = root / STAGING_ROOT
    if create:
        result.mkdir(mode=0o700, exist_ok=True)
    _validate_dir(result, private=True)
    if create:
        # Always sync the parent even when the child already exists. An earlier
        # invocation may have created this entry and failed its parent fsync;
        # treating existence as durability would allow a later unsafe retry.
        # The inspect-only path never creates or syncs deployment state.
        parent_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    return result

@contextmanager
def _lock(root: Path, *, require_root: bool = True,
          host_receipt_dir: Path | None = None):
    host_root = host_receipt_dir
    if host_root is None:
        host_root = (legacy.HOST_RECEIPT_DIR if require_root
                     else root.parent / "host-receipts")
    # Lock order is always host-wide first, Stage second. Both remain held
    # until effects and receipts complete; host lifecycle uses the same inode.
    # Stage/Activate mutate the same live install/unit identities. Root
    # callers must never manufacture a new host-lock inode if another
    # installed updater still owns the previous inode.
    with legacy._host_update_guard(
        host_root, require_root=require_root,
        require_existing_lock=require_root,
    ):
        state = _state_dir(root, create=True)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(state / "transaction.lock", flags, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise legacy.ProductionUpdateError("staging lock identity invalid")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise legacy.ProductionUpdateError("a staged update is already running") from exc
            yield state
        finally:
            os.close(fd)

def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise legacy.ProductionUpdateError("staging record short write")
        view = view[written:]


def _publish(state: Path, name: str, data: bytes, *, immutable: bool) -> None:
    """fsynced private staging, atomic rename for control or no-replace link."""
    if len(data) > MAX_RECORD:
        raise legacy.ProductionUpdateError("staging record exceeds size budget")
    _validate_dir(state, private=True)
    fd, temp_name = tempfile.mkstemp(prefix=".staged-update-", dir=state)
    temp = Path(temp_name)
    try:
        os.fchmod(fd, 0o600)
        _write_all(fd, data)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        target = state / name
        if immutable:
            try:
                os.link(temp, target, follow_symlinks=False)
            except FileExistsError as exc:
                raise legacy.ProductionUpdateError("staging immutable receipt exists") from exc
        else:
            os.replace(temp, target)
        parent_fd = os.open(state, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        temp.unlink(missing_ok=True)

def _control_read(state: Path) -> dict[str, object] | None:
    path = state / CONTROL_FILE
    if not os.path.lexists(path):
        return None
    obj = _decode(_read_file(path))
    if set(obj) != {"record_version", "status", "target_sha", "stage_sha256", "updated_at"}:
        raise legacy.ProductionUpdateError("staging control fields invalid")
    if obj["record_version"] != VERSION or obj["status"] not in {
        "preparing", "staged", "activating", "activated", "failed"
    }:
        raise legacy.ProductionUpdateError("staging control state invalid")
    if not isinstance(obj["target_sha"], str) or not SHA_RE.fullmatch(obj["target_sha"]):
        raise legacy.ProductionUpdateError("staging control target invalid")
    digest = obj["stage_sha256"]
    if digest is not None and (not isinstance(digest, str) or not HASH_RE.fullmatch(digest)):
        raise legacy.ProductionUpdateError("staging control receipt invalid")
    return obj

def _control_write(state: Path, status: str, target: str, receipt: str | None) -> None:
    if status not in {"preparing", "staged", "activating", "activated", "failed"}:
        raise legacy.ProductionUpdateError("staging status invalid")
    _publish(state, CONTROL_FILE, _encode({
        "record_version": VERSION, "status": status, "target_sha": target,
        "stage_sha256": receipt, "updated_at": legacy._utc_now(),
    }), immutable=False)

def _assert_known_timer_inventory(runner: legacy.CommandRunner, systemd_dir: Path) -> None:
    """Only the two separately authorized GitHub timers may exist anywhere.

    Checking timer state for the two named units is insufficient if a third
    already-loaded transient timer can start services during Stage.
    """
    expected = set(legacy.MANAGED_TIMER_UNITS)
    installed = {
        name for name in _all_installed_unit_names(systemd_dir)
        if name.endswith(".timer")
    }
    if installed != expected:
        raise legacy.ProductionUpdateError("unknown or missing installed GitHub timer")
    commands = (
        ("systemctl", "list-unit-files", "--all", "--type=timer",
         "--no-legend", "--no-pager", "obsidian-github-*.timer"),
        ("systemctl", "list-units", "--all", "--type=timer",
         "--no-legend", "--plain", "--no-pager", "obsidian-github-*.timer"),
    )
    for index, argv in enumerate(commands):
        observation = runner(argv)
        if observation.returncode != 0:
            raise legacy.ProductionUpdateError("cannot enumerate GitHub timer authority")
        names: set[str] = set()
        for line in observation.stdout.splitlines():
            fields = line.strip().split()
            if not fields:
                continue
            name = fields[0]
            if (not name.startswith("obsidian-github-") or not name.endswith(".timer")
                    or "/" in name or name in names):
                raise legacy.ProductionUpdateError("GitHub timer inventory is ambiguous")
            names.add(name)
        if (names != expected if index == 0 else not names.issubset(expected)):
            raise legacy.ProductionUpdateError("unexpected loaded or installed GitHub timer")


def _systemd_unit_search_dirs(
    runner: legacy.CommandRunner, systemd_dir: Path
) -> tuple[Path, ...]:
    response = runner(("systemd-analyze", "unit-paths"))
    if response.returncode != 0:
        raise legacy.ProductionUpdateError("cannot inspect systemd unit search paths")
    paths: set[Path] = {systemd_dir}
    count = 0
    for raw in response.stdout.splitlines():
        name = raw.strip()
        candidate = Path(name)
        if not name or not candidate.is_absolute() or ".." in candidate.parts:
            raise legacy.ProductionUpdateError("invalid systemd unit search path")
        count += 1
        paths.add(candidate)
    if count == 0 or len(paths) > 64:
        raise legacy.ProductionUpdateError("systemd unit search path inventory invalid")
    return tuple(sorted(paths))


def _assert_no_systemd_dropins(
    runner: legacy.CommandRunner, systemd_dir: Path, names: Sequence[str]
) -> None:
    """Reject all effective or staged drop-ins, rather than silently omit them.

    A DropInPaths change can replace ExecStart or EnvironmentFile without
    affecting the top-level unit digest bound to the Stage receipt.
    """
    directories = _systemd_unit_search_dirs(runner, systemd_dir)
    installed_names = set(_all_installed_unit_names(systemd_dir))
    for name in names:
        if name not in installed_names:
            raise legacy.ProductionUpdateError("managed unit inventory drift")
        observed = runner((
            "systemctl", "show", "-p", "LoadState", "-p", "FragmentPath",
            "-p", "DropInPaths", "--no-pager", name,
        ))
        if observed.returncode != 0:
            raise legacy.ProductionUpdateError("cannot inspect effective systemd unit")
        fields: dict[str, str] = {}
        for line in observed.stdout.splitlines():
            if "=" not in line:
                raise legacy.ProductionUpdateError("invalid effective systemd unit response")
            key, value = line.split("=", 1)
            if key in fields or key not in {"LoadState", "FragmentPath", "DropInPaths"}:
                raise legacy.ProductionUpdateError("ambiguous effective systemd unit response")
            fields[key] = value
        if (set(fields) != {"LoadState", "FragmentPath", "DropInPaths"}
                or fields["LoadState"] != "loaded"
                or fields["FragmentPath"] != str(systemd_dir / name)
                or fields["DropInPaths"]):
            raise legacy.ProductionUpdateError("unsupported effective systemd drop-in or unit origin")
        # Include dashed-prefix directories: foo-bar.service also inherits
        # foo-.service.d, in addition to exact and type-wide drop-ins.
        stem, _, unit_type = name.rpartition(".")
        segments = stem.split("-")
        dropins = {name + ".d", unit_type + ".d"}
        dropins.update(
            "-".join(segments[:index]) + "-." + unit_type + ".d"
            for index in range(1, len(segments))
        )
        for prefix in directories:
            for dirname in sorted(dropins):
                if os.path.lexists(prefix / dirname):
                    raise legacy.ProductionUpdateError("unreviewed systemd drop-in directory")


def _snapshot_timers(runner: legacy.CommandRunner) -> dict[str, dict[str, bool]]:
    out: dict[str, dict[str, bool]] = {}
    for unit in legacy.MANAGED_TIMER_UNITS:
        enabled, active = legacy._timer_state(runner, unit)
        if enabled is None or active is None:
            raise legacy.ProductionUpdateError("managed timer must already exist")
        out[unit] = {"enabled": enabled, "active": active}
    return out

def _require_inert(runner: legacy.CommandRunner) -> None:
    for unit in legacy.MANAGED_TIMER_UNITS:
        if legacy._timer_state(runner, unit) != (False, False):
            raise legacy.ProductionUpdateError(f"staged timer is not inert: {unit}")

def _contain_failed_timers(runner: legacy.CommandRunner) -> None:
    """Best effort, but never silently claim inert timers on an uncertain stop."""
    uncertain = False
    for unit in legacy.MANAGED_TIMER_UNITS:
        try:
            result = runner(("systemctl", "disable", "--now", unit))
            if result.returncode != 0:
                uncertain = True
        except BaseException:
            uncertain = True
    try:
        _require_inert(runner)
    except BaseException:
        uncertain = True
    if uncertain:
        raise legacy.ProductionUpdateError(
            "TIMER_CONTAINMENT_UNCONFIRMED: manual operator inspection required"
        )


_MONOTONIC_ELAPSE_COMPONENT = re.compile(
    r"(?P<amount>[0-9]{1,12}(?:\.[0-9]{1,6})?)(?:month|min|us|ms|y|w|d|h|s)"
)


def _finite_monotonic_elapse(value: str) -> bool:
    """Validate systemctl's rendered monotonic time span, not arbitrary text.

    systemctl show normally formats this property as a time span such as
    '1month 2w 5d 13h 51min 2.256ms'; zero/unset and unrecognized encodings
    cannot prove that a waiting timer is armed. This check deliberately
    rejects unsupported future formats rather than accepting unknown strings.
    """
    if not isinstance(value, str) or not 0 < len(value) <= 128:
        return False
    parts = value.split(" ")
    if not parts or len(parts) > 16:
        return False
    positive = False
    for part in parts:
        match = _MONOTONIC_ELAPSE_COMPONENT.fullmatch(part)
        if match is None:
            return False
        positive = positive or any(char in "123456789" for char in match["amount"])
    return positive


def _require_restored_sync_timer_armed(runner: legacy.CommandRunner) -> None:
    """An active Sync timer must also have a viable next-elapse state."""
    response = runner((
        "systemctl", "show", legacy.TIMER_UNIT,
        "--property=SubState", "--property=NextElapseUSecMonotonic",
    ))
    if response.returncode != 0:
        raise legacy.ProductionUpdateError("cannot verify restored Sync timer")
    state: dict[str, str] = {}
    for line in response.stdout.splitlines():
        if line.count("=") != 1:
            raise legacy.ProductionUpdateError("ambiguous Sync timer arming state")
        name, value = line.split("=", 1)
        if name in state or name not in {"SubState", "NextElapseUSecMonotonic"}:
            raise legacy.ProductionUpdateError("ambiguous Sync timer arming state")
        state[name] = value
    if set(state) != {"SubState", "NextElapseUSecMonotonic"}:
        raise legacy.ProductionUpdateError("incomplete Sync timer arming state")
    if (state["SubState"] not in {"waiting", "running"} or
            (state["SubState"] == "waiting" and
             not _finite_monotonic_elapse(state["NextElapseUSecMonotonic"]))):
        raise legacy.ProductionUpdateError("restored Sync timer not armed")


def _require_idle_services(runner: legacy.CommandRunner, systemd_dir: Path) -> None:
    """Verify complete installed and loaded GitHub service authority and idle state."""
    expected = {
        name for name in _all_installed_unit_names(systemd_dir)
        if name.endswith(".service")
    }
    if not expected:
        raise legacy.ProductionUpdateError("empty managed GitHub service inventory")
    queries = (
        (("systemctl", "list-unit-files", "--all", "--type=service",
          "--no-legend", "--no-pager", "obsidian-github-*.service"), True),
        (("systemctl", "list-units", "--all", "--type=service",
          "--no-legend", "--plain", "--no-pager", "obsidian-github-*.service"), False),
    )
    for argv, require_all in queries:
        response = runner(argv)
        if response.returncode != 0:
            raise legacy.ProductionUpdateError("cannot enumerate GitHub service authority")
        discovered: set[str] = set()
        for line in response.stdout.splitlines():
            parts = line.strip().split()
            if not parts:
                continue
            unit = parts[0]
            if (not unit.startswith("obsidian-github-") or not unit.endswith(".service")
                    or "/" in unit or unit in discovered):
                raise legacy.ProductionUpdateError("ambiguous GitHub service inventory")
            discovered.add(unit)
        if (discovered != expected if require_all else not discovered.issubset(expected)):
            raise legacy.ProductionUpdateError("unexpected installed or loaded GitHub service")
    units = set(EFFECT_SERVICES)
    units.update(name for name in _all_installed_unit_names(systemd_dir)
                 if name.endswith(".service"))
    for unit in sorted(units):
        observed = runner(("systemctl", "is-active", unit))
        if observed.returncode != 3 or observed.stdout.strip() != "inactive":
            raise legacy.ProductionUpdateError(f"effect-capable service not confirmed idle: {unit}")

def _checkout(app: Path, runner: legacy.CommandRunner, expected: str | None = None) -> str:
    legacy._require_directory(app, label="staged app checkout")
    if legacy._git_output(runner, app, "branch", "--show-current", label="staging branch") != "main":
        raise legacy.ProductionUpdateError("staged deployment requires main branch")
    if legacy._git_output(runner, app, "status", "--porcelain", label="staging status"):
        raise legacy.ProductionUpdateError("staging checkout is dirty")
    current = legacy._git_output(runner, app, "rev-parse", "HEAD", label="staging revision")
    if not SHA_RE.fullmatch(current) or (expected is not None and current != expected):
        raise legacy.ProductionUpdateError("staged revision does not match pinned SHA")
    return current

def _config_manifest(paths: Sequence[Path]) -> dict[str, str]:
    if not paths or len(paths) > 16 or len({str(p) for p in paths}) != len(paths):
        raise legacy.ProductionUpdateError("config bindings missing or invalid")
    hashes = {}
    for path in paths:
        if not path.is_absolute():
            raise legacy.ProductionUpdateError("config binding path must be absolute")
        hashes[str(path)] = _digest(_read_file(path, max_bytes=65536))
    return hashes

def _unit_manifest(directory: Path, names: Sequence[str]) -> dict[str, str]:
    result = {}
    for name in names:
        if "/" in name or not name.startswith("obsidian-github-") or not name.endswith((".timer", ".service")):
            raise legacy.ProductionUpdateError("managed unit name invalid")
        result[name] = _digest(_read_file(directory / name, max_bytes=1024 * 1024))
    return result


def _all_installed_unit_names(directory: Path) -> tuple[str, ...]:
    """Bind the complete installed managed namespace, including older units."""
    found = {
        file.name for pattern in ("obsidian-github-*.service", "obsidian-github-*.timer")
        for file in directory.glob(pattern)
    }
    if not found or len(found) > 128:
        raise legacy.ProductionUpdateError("installed managed unit set is invalid")
    return tuple(sorted(found))


def _environment_manifest(systemd_dir: Path, names: Sequence[str]) -> dict[str, str]:
    """Bind every EnvironmentFile declared by the exact installed service units.

    Optional absent files have a stable explicit sentinel so adding credentials
    after Stage cannot silently alter what Activate will execute.
    """
    paths: dict[str, bool] = {}
    for name in names:
        if not name.endswith(".service"):
            continue
        data = _read_file(systemd_dir / name, max_bytes=1024 * 1024)
        for line in data.decode("utf-8").splitlines():
            stripped = line.lstrip()
            if not stripped or stripped.startswith(("#", ";")):
                continue
            assignment = re.match(r"^EnvironmentFile\s*=\s*(.*)$", stripped)
            if assignment is None:
                if stripped.startswith("EnvironmentFile"):
                    raise legacy.ProductionUpdateError("unsupported EnvironmentFile assignment")
                continue
            raw = assignment.group(1).strip()
            optional = raw.startswith("-")
            value = raw[1:] if optional else raw
            if "%" in value or "$" in value:
                raise legacy.ProductionUpdateError("unsupported EnvironmentFile specifier")
            if not value or any(c.isspace() or c in "'\\\"" for c in value):
                raise legacy.ProductionUpdateError("unrecognized systemd EnvironmentFile expression")
            bound = Path(value)
            if not bound.is_absolute():
                raise legacy.ProductionUpdateError("EnvironmentFile must be an absolute path")
            if value in paths and paths[value] != optional:
                raise legacy.ProductionUpdateError("EnvironmentFile requirement is ambiguous")
            paths[value] = optional
    manifest = {}
    for path, optional in sorted(paths.items()):
        try:
            manifest[path] = _digest(_read_file(Path(path), max_bytes=65536))
        except FileNotFoundError:
            if not optional:
                raise legacy.ProductionUpdateError("required EnvironmentFile missing")
            manifest[path] = "absent"
    return manifest


_DIRECT_FILE_FLAGS = frozenset({
    "--config", "--rclone-config", "--filter-file", "--password-file",
})


def _direct_service_input_manifest(
    systemd_dir: Path, names: Sequence[str]
) -> dict[str, str]:
    """Bind files supplied as direct ExecStart arguments, not EnvironmentFiles.

    Only audited absolute file arguments are accepted. An unrecognized
    *-file/*-config flag fails closed rather than silently skipping security
    inputs (e.g. watcher config.toml, rclone config, WebDAV credentials).
    """
    paths: set[Path] = set()
    directive = re.compile(r"^Exec(?:Condition|Start|StartPre|StartPost|Reload|Stop|StopPost)\s*=(.*)$")
    for name in names:
        if not name.endswith(".service"):
            continue
        raw = _read_file(systemd_dir / name, max_bytes=1024 * 1024).decode("utf-8")
        commands: list[str] = []
        accumulated = ""
        for line in raw.splitlines():
            clean = line.strip()
            if not accumulated:
                if not clean or clean.startswith(("#", ";")):
                    continue
                match = directive.match(clean)
                if match is None:
                    continue
                accumulated = match.group(1).strip()
            else:
                accumulated += " " + clean
            if accumulated.endswith("\\"):
                accumulated = accumulated[:-1].rstrip()
                continue
            commands.append(accumulated)
            accumulated = ""
        if accumulated:
            raise legacy.ProductionUpdateError("unterminated systemd ExecStart file argument")
        for command in commands:
            # systemd decodes C-style escapes, unlike POSIX shlex.
            # Continuation backslashes were already consumed above; any
            # remaining backslash might alter an Exec file path at runtime,
            # so fail closed instead of hashing a guessed pathname.
            if "\\" in command:
                raise legacy.ProductionUpdateError(
                    "unsupported systemd Exec backslash escape"
                )
            try:
                words = shlex.split(command, posix=True)
            except ValueError as exc:
                raise legacy.ProductionUpdateError("invalid systemd ExecStart arguments") from exc
            if "%" in command:
                raise legacy.ProductionUpdateError("unsupported systemd Exec specifier")
            if words:
                prefix = words[0][:len(words[0]) - len(words[0].lstrip("-+!@:|"))]
                executable = Path(words[0].lstrip("-+!@:|")).name
                if "|" in prefix or executable in {"sh", "bash", "dash", "zsh", "env"}:
                    raise legacy.ProductionUpdateError("shell-command Exec inputs cannot be bound")
            for i, word in enumerate(words):
                key, sep, embedded = word.partition("=")
                is_file_flag = key in _DIRECT_FILE_FLAGS
                if (key.startswith("--")
                        and (key.endswith("-file") or key.endswith("-config"))
                        and not is_file_flag):
                    raise legacy.ProductionUpdateError("unreviewed direct file argument")
                if not is_file_flag:
                    continue
                if sep:
                    value = embedded
                elif i + 1 < len(words):
                    value = words[i + 1]
                else:
                    raise legacy.ProductionUpdateError("direct file argument has no value")
                path = Path(value)
                if (not path.is_absolute() or ".." in path.parts
                        or "$" in value or "%" in value):
                    raise legacy.ProductionUpdateError("direct file input must be an absolute stable path")
                paths.add(path)
    if len(paths) > 32:
        raise legacy.ProductionUpdateError("too many direct file inputs")
    return {str(path): _digest(_read_file(path, max_bytes=65536))
            for path in sorted(paths)}




def _assert_trusted_stage_build_venv(
    venv_root: Path, *, trusted_uid: int = 0
) -> dict[str, str]:
    """Pre-effect ownership admission for code run by pip under Stage authority.

    This prevents a less-privileged user from replacing root-executed pip,
    setuptools or Python startup files. It cannot prove the provenance of
    legitimately root-installed code; that remains an operator review gate.
    """
    if not venv_root.is_absolute() or ".." in venv_root.parts:
        raise legacy.ProductionUpdateError("untrusted Stage build venv path")
    # Check complete ancestor chain before traversing the build environment.
    for path in reversed((venv_root, *venv_root.parents)):
        info = path.lstat()
        mode = stat.S_IMODE(info.st_mode)
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise legacy.ProductionUpdateError("untrusted Stage build venv ancestor")
        if path == venv_root:
            if info.st_uid != trusted_uid or mode & 0o022:
                raise legacy.ProductionUpdateError("untrusted Stage build venv owner/mode")
        elif info.st_uid not in {0, trusted_uid} or (
            mode & 0o022 and not (info.st_uid == 0 and mode & stat.S_ISVTX)
        ):
            raise legacy.ProductionUpdateError("writable Stage build venv ancestor")

    def check(path: Path, *, directory: bool) -> None:
        info = path.lstat()
        mode = stat.S_IMODE(info.st_mode)
        if (info.st_uid != trusted_uid or mode & 0o022
                or stat.S_ISLNK(info.st_mode)):
            raise legacy.ProductionUpdateError("untrusted Stage build file authority")
        if directory:
            if not stat.S_ISDIR(info.st_mode):
                raise legacy.ProductionUpdateError("untrusted Stage build directory")
        elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise legacy.ProductionUpdateError("untrusted Stage build file type")

    pip = venv_root / "bin" / "pip"
    check(venv_root / "bin", directory=True)
    check(pip, directory=False)
    if not (pip.stat().st_mode & stat.S_IXUSR):
        raise legacy.ProductionUpdateError("unexecutable Stage build pip")
    check(venv_root / "pyvenv.cfg", directory=False)
    config_bytes = _read_file(venv_root / "pyvenv.cfg", max_bytes=65536)
    site_flags = [
        value.strip().lower()
        for line in config_bytes.splitlines()
        for key, sep, value in (line.partition(b"="),)
        if sep and key.strip().lower() == b"include-system-site-packages"
    ]
    if site_flags != [b"false"]:
        raise legacy.ProductionUpdateError(
            "Stage venv must disable external system site-packages"
        )
    # An ordinary virtualenv points bin/python and bin/python3 to the distro
    # interpreter. These expected symlinks are allowed only when they resolve
    # to a root-owned, non-writable binary under the trusted /usr/bin tree.
    python_targets: list[str] = []
    for name in ("python", "python3"):
        path = venv_root / "bin" / name
        link = path.lstat()
        if link.st_uid != trusted_uid or not (
            stat.S_ISLNK(link.st_mode) or stat.S_ISREG(link.st_mode)
        ):
            raise legacy.ProductionUpdateError("untrusted Stage Python launcher")
        target = path.resolve(strict=True)
        if (target.parent != Path("/usr/bin")
                or re.fullmatch(r"python3\.[0-9]{1,2}", target.name) is None):
            raise legacy.ProductionUpdateError("Stage Python target outside trusted system")
        for parent in (target, *target.parents):
            info = parent.lstat()
            if info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
                raise legacy.ProductionUpdateError("untrusted Stage system Python target")
            if parent == target:
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise legacy.ProductionUpdateError("invalid Stage system Python binary")
            elif not stat.S_ISDIR(info.st_mode):
                raise legacy.ProductionUpdateError("untrusted Stage Python target ancestor")
        python_targets.append(str(target))
    first_line = _read_file(pip, max_bytes=2 * 1024 * 1024).split(b"\n", 1)[0]
    if first_line not in (
        ("#!" + str(venv_root / "bin" / name)).encode("utf-8")
        for name in ("python", "python3")
    ):
        raise legacy.ProductionUpdateError("unexpected Stage pip interpreter shebang")
    check(venv_root / "lib", directory=True)
    sites = tuple(sorted((venv_root / "lib").glob("python*/site-packages")))
    if len(sites) != 1:
        raise legacy.ProductionUpdateError("ambiguous Stage build site-packages")
    site = sites[0]
    check(site.parent, directory=True)
    check(site, directory=True)
    versions: dict[str, str] = {}
    for project in ("pip", "setuptools", "wheel"):
        check(site / project, directory=True)
        matches = tuple(site.glob(project + "-*.dist-info"))
        if len(matches) != 1:
            raise legacy.ProductionUpdateError("missing or ambiguous Stage build backend")
        check(matches[0], directory=True)
        data = _read_file(matches[0] / "METADATA", max_bytes=1024 * 1024)
        names = re.findall(rb"^Name: ([A-Za-z0-9_.-]+)$", data, flags=re.MULTILINE)
        raw_versions = re.findall(
            rb"^Version: ([0-9]+(?:\.[0-9]+){1,3})$", data, flags=re.MULTILINE
        )
        if (names != [project.encode("ascii")] or len(raw_versions) != 1):
            raise legacy.ProductionUpdateError("Stage build distribution identity invalid")
        versions[project] = raw_versions[0].decode("ascii")
        if project == "setuptools" and int(versions[project].split(".")[0]) < 75:
            raise legacy.ProductionUpdateError("unreviewed Stage setuptools version")

    # Check all installed startup/import files: arbitrary .pth imports and
    # unrelated site modules can run before a backend's own first statement.
    # Hash all stable non-application files to bind the preinstalled build
    # environment to the staged receipt; the app's own wheel is being replaced.
    digest = hashlib.sha256()
    digest.update(b"trusted-stage-build-environment-v1\n")
    digest.update(bytes.fromhex(_digest(_read_file(pip, max_bytes=2 * 1024 * 1024))))
    digest.update(bytes.fromhex(_digest(_read_file(
        venv_root / "pyvenv.cfg", max_bytes=65536
    ))))
    for target in python_targets:
        digest.update(target.encode("utf-8") + b"\x00")
    # Bound enumeration *before* sorting; even a root-managed malformed venv
    # should not cause a memory blowup in a pre-effect admission probe.
    candidates: list[Path] = []
    for path in site.rglob("*"):
        if len(candidates) >= 8192:
            raise legacy.ProductionUpdateError("Stage build venv inventory exceeds budget")
        candidates.append(path)
    for path in sorted(candidates):
        info = path.lstat()
        directory = stat.S_ISDIR(info.st_mode)
        check(path, directory=directory)
        if path.suffix == ".pth":
            # Python site initialization executes import statements in .pth,
            # and path-only lines can redirect imports outside this inventory.
            # Ownership of the .pth file alone does not protect its targets.
            # Fail closed to the exact previously audited setuptools shim.
            if (path != site / "distutils-precedence.pth"
                    or _digest(_read_file(path, max_bytes=65536))
                    != REVIEWED_DISTUTILS_PTH_SHA256):
                raise legacy.ProductionUpdateError("unreviewed Stage .pth startup authority")
        relative = path.relative_to(site).as_posix()
        root_entry = relative.split("/", 1)[0]
        if (relative == "obsidian_automation" or
                relative.startswith("obsidian_automation/") or
                (root_entry.startswith("obsidian_automation-")
                 and root_entry.endswith(".dist-info"))):
            # Reviewed app code/entrypoints receive separate exact-byte
            # postinstall attestation; they are expected to change under pip.
            continue
        digest.update(relative.encode("utf-8") + b"\x00")
        digest.update(format(stat.S_IMODE(info.st_mode), "04o").encode("ascii") + b"\x00")
        if directory:
            digest.update(b"dir\n")
        else:
            digest.update(bytes.fromhex(_digest(_read_file(path, max_bytes=2 * 1024 * 1024))))
            digest.update(b"\n")
    return {"digest_sha256": digest.hexdigest(), **versions}


@contextmanager
def _controlled_package_install_umask():
    """Ensure predictable service-readable wheel/install bytes, then restore.

    Stage has a serialized root transaction and does not run any helper
    threads. Keep this process-global change scoped to the pip subprocess.
    """
    original = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(original)


MAX_IGNORED_BUILD_ENTRIES = 4096
MAX_IGNORED_BUILD_BYTES = 128 * 1024 * 1024


def _current_mount_points() -> frozenset[Path]:
    """Include same-device bind mounts, which os.path.ismount may miss.

    Linux mountinfo exposes the mountpoint independently of st_dev. Reject
    unreadable/ambiguous mount metadata rather than traversing untrusted
    mount trees during a privileged cleanup.
    """
    try:
        raw = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise legacy.ProductionUpdateError(
            "cannot inspect build cleanup mount boundaries"
        ) from exc
    points: set[Path] = set()
    for line in raw.splitlines():
        fields = line.split()
        if len(fields) < 10 or " - " not in line:
            raise legacy.ProductionUpdateError(
                "ambiguous build cleanup mount boundary"
            )
        # Linux mountinfo escapes whitespace and backslash as octal sequences.
        value = fields[4]
        if re.search(r"\\(?![0-7]{3})", value):
            raise legacy.ProductionUpdateError(
                "unrecognized build cleanup mount escape"
            )
        name = re.sub(
            r"\\([0-7]{3})",
            lambda match: chr(int(match.group(1), 8)),
            value,
        )
        point = Path(name)
        if not point.is_absolute() or ".." in point.parts:
            raise legacy.ProductionUpdateError(
                "invalid build cleanup mount boundary"
            )
        points.add(point)
    if not points:
        raise legacy.ProductionUpdateError("empty build cleanup mount inventory")
    return frozenset(points)


def _inspect_ignored_package_build_artifacts(app_root: Path) -> tuple[Path, ...]:
    """Preflight every inode beneath fixed setuptools paths, without deletion.

    A top-level directory check alone is insufficient: rmtree can traverse an
    unexpected nested mounted filesystem. Refuse symlinks, special files,
    other owners, insecure directories, and oversize/unbounded artifact trees.
    """
    legacy._require_directory(app_root, label="staged production checkout")
    source = app_root / "src"
    legacy._require_directory(source, label="staged source package root")
    roots = (
        app_root / "build",
        app_root / "dist",
        app_root / "obsidian_automation.egg-info",
        source / "obsidian_automation.egg-info",
    )
    owner_uid = os.geteuid()
    device = app_root.stat().st_dev
    mounted = _current_mount_points()
    present: list[Path] = []
    count = 0
    size_bytes = 0
    for root in roots:
        try:
            info = root.lstat()
        except FileNotFoundError:
            continue
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != owner_uid
                or info.st_dev != device or root in mounted
                or os.path.ismount(root) or info.st_mode & 0o022):
            raise legacy.ProductionUpdateError(
                "unsafe ignored package build path; requires operator reconciliation"
            )
        present.append(root)
        pending = [root]
        while pending:
            parent = pending.pop()
            try:
                with os.scandir(parent) as stream:
                    entries = list(stream)
            except OSError as exc:
                raise legacy.ProductionUpdateError(
                    "cannot inspect ignored package build subtree"
                ) from exc
            for entry in entries:
                child = Path(entry.path)
                try:
                    status = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    raise legacy.ProductionUpdateError(
                        "ignored package build inode changed during inspection"
                    ) from exc
                count += 1
                if (count > MAX_IGNORED_BUILD_ENTRIES
                        or status.st_uid != owner_uid
                        or status.st_dev != device
                        or child in mounted
                        or os.path.ismount(child)):
                    raise legacy.ProductionUpdateError(
                        "unsafe or oversized ignored package build subtree"
                    )
                if stat.S_ISDIR(status.st_mode):
                    if status.st_mode & 0o022:
                        raise legacy.ProductionUpdateError(
                            "insecure ignored package build directory"
                        )
                    pending.append(child)
                elif stat.S_ISREG(status.st_mode):
                    if status.st_nlink != 1:
                        raise legacy.ProductionUpdateError(
                            "hardlinked ignored package build artifact"
                        )
                    size_bytes += status.st_size
                    if size_bytes > MAX_IGNORED_BUILD_BYTES:
                        raise legacy.ProductionUpdateError(
                            "oversized ignored package build artifacts"
                        )
                else:
                    raise legacy.ProductionUpdateError(
                        "unreviewed ignored package build file type"
                    )
    return tuple(present)


def _discard_ignored_package_build_artifacts(app_root: Path) -> tuple[str, ...]:
    """Delete ONLY a completely preflighted, fixed, trusted build namespace.

    Called exclusively inside Stage's already-authorized inert timer window.
    """
    present = _inspect_ignored_package_build_artifacts(app_root)
    for path in present:
        # Python shutil.rmtree uses a descriptor-safe implementation on Linux.
        # Trusted root-owned ancestors must be stable until cleanup completes.
        shutil.rmtree(path)
    return tuple(str(path.relative_to(app_root)) for path in present)


def _reviewed_service_roles(app_root: Path) -> tuple[str, ...]:
    """Read role IDs from the reviewed authority provisioner, without executing it."""
    source = app_root / "tools" / "provision_automation_authority.py"
    text = _read_file(source, max_bytes=256*1024).decode("utf-8")
    try:
        tree = ast.parse(text, filename="authority-manifest.py")
        declarations = [
            node.value for node in tree.body
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "PRIMARY_USERS"
        ]
        if len(declarations) != 1:
            raise ValueError("missing canonical PRIMARY_USERS")
        items = ast.literal_eval(declarations[0])
    except (SyntaxError, ValueError, TypeError) as exc:
        raise legacy.ProductionUpdateError("authority role manifest cannot be validated") from exc
    if not isinstance(items, tuple) or not 10 <= len(items) <= 64:
        raise legacy.ProductionUpdateError("authority role inventory count invalid")
    roles = []
    seen = set()
    for item in items:
        if (not isinstance(item, tuple) or len(item) != 3
                or not all(isinstance(x, str) for x in item)):
            raise legacy.ProductionUpdateError("authority role manifest tuple invalid")
        name, group, home = item
        if (not re.fullmatch(r"[a-z][a-z0-9-]{2,63}", name)
                or not re.fullmatch(r"[a-z][a-z0-9-]{2,63}", group)
                or not home.startswith("/") or name in seen):
            raise legacy.ProductionUpdateError("authority role identity invalid")
        seen.add(name)
        if name.startswith("obsidian-"):
            roles.append(name)
    if len(roles) < 8:
        raise legacy.ProductionUpdateError("insufficient Obsidian service identities")
    return tuple(roles)


_ROLE_PROBE_SCRIPT = (
    "import os,sys,obsidian_automation;"
    "sys.exit(0 if all(os.access(p,os.R_OK|os.X_OK) for p in sys.argv[1:]) else 17)"
)


def _service_role_package_smoke(
    app_root: Path, venv_root: Path, code_manifest: Mapping[str, str],
    runner: legacy.CommandRunner,
) -> tuple[str, ...]:
    """Read/import under every manifest identity, without invoking any Writer."""
    roles = _reviewed_service_roles(app_root)
    python = venv_root / "bin" / "python"
    if not python.is_file():
        raise legacy.ProductionUpdateError("installed venv python entrypoint missing")
    script_paths = tuple(
        str(venv_root / "bin" / key[len("entrypoint:"):])
        for key in sorted(code_manifest)
        if key.startswith("entrypoint:")
    )
    if not 2 <= len(script_paths) <= 128:
        raise legacy.ProductionUpdateError("installed console script count invalid")
    for role in roles:
        legacy._run(
            runner,
            ("runuser", "-u", role, "--", str(python), "-I", "-B", "-c",
             _ROLE_PROBE_SCRIPT, *script_paths),
            label="production service-role import and entrypoint readability smoke",
        )
    return roles


def _deployed_code_manifest(app_root: Path, venv_root: Path) -> dict[str, str]:
    """Verify installed package bytes against pinned source and bind entrypoints.

    A clean Git checkout is not sufficient: pip-installed code may drift after
    the staging safe smoke. Do not execute imported application code to attest
    it; compare bounded package files on disk.
    """
    source = app_root / "src" / "obsidian_automation"
    legacy._require_directory(source, label="pinned Python source directory")
    candidates = tuple(sorted((venv_root / "lib").glob(
        "python*/site-packages/obsidian_automation"
    )))
    if len(candidates) != 1:
        raise legacy.ProductionUpdateError("exactly one installed Python package directory required")
    installed = candidates[0]
    legacy._require_directory(installed, label="installed Python package directory")

    source_files = {
        f.relative_to(source).as_posix(): f for f in source.rglob("*.py")
    }
    installed_files = {
        f.relative_to(installed).as_posix(): f for f in installed.rglob("*.py")
    }
    if (not source_files or len(source_files) > 256
            or set(source_files) != set(installed_files)):
        raise legacy.ProductionUpdateError("installed module inventory differs from pinned source")

    # Python's extension loader may choose a .so/.pyd over a same-named .py.
    # A source-vs-installed comparison of only *.py is therefore insufficient
    # to attest executable import inputs. Reject all unsupported file classes;
    # bind ordinary CPython caches only when backed by the exact reviewed .py.
    cache_digest = hashlib.sha256(b"installed-app-bytecode-v1\n")
    observed_count = 0
    cache_pattern = re.compile(
        r"^(?P<module>[^/]+)\.cpython-[0-9]{2,3}(?:\.opt-[0-2])?\.pyc$"
    )
    for entry in sorted(installed.rglob("*")):
        observed_count += 1
        if observed_count > 1024:
            raise legacy.ProductionUpdateError("installed package inventory exceeds budget")
        entry_info = entry.lstat()
        if stat.S_ISDIR(entry_info.st_mode):
            continue
        if not stat.S_ISREG(entry_info.st_mode):
            raise legacy.ProductionUpdateError("unsupported installed application inode")
        relative = entry.relative_to(installed).as_posix()
        if relative in installed_files:
            continue
        match = cache_pattern.fullmatch(entry.name)
        if entry.parent.name != "__pycache__" or match is None:
            raise legacy.ProductionUpdateError("unreviewed executable app file in installed package")
        backing = entry.parent.parent / (match["module"] + ".py")
        if backing.relative_to(installed).as_posix() not in source_files:
            raise legacy.ProductionUpdateError("orphan installed application bytecode")
        cache_digest.update(relative.encode("utf-8") + b"\x00")
        cache_digest.update(bytes.fromhex(_digest(_read_file(
            entry, max_bytes=2 * 1024 * 1024
        ))))

    # Aggregate hashes rather than storing hundreds of individual .pyc keys:
    # Stage receipt size stays bounded as the number of reviewed .py grows.
    result: dict[str, str] = {"bytecode:manifest": cache_digest.hexdigest()}
    for name in sorted(source_files):
        source_sha = _digest(_read_file(source_files[name], max_bytes=2*1024*1024))
        installed_sha = _digest(_read_file(installed_files[name], max_bytes=2*1024*1024))
        if source_sha != installed_sha:
            raise legacy.ProductionUpdateError("installed package bytes differ from pinned source")
        result["python:" + name] = installed_sha

    scripts_dir = venv_root / "bin"
    legacy._require_directory(scripts_dir, label="installed console script directory")
    scripts = sorted(scripts_dir.glob("obsidian-github-*"))
    expected = {"obsidian-github-production-smoke", "obsidian-github-daily-production-smoke"}
    if len(scripts) < 2 or len(scripts) > 128 or not expected.issubset({p.name for p in scripts}):
        raise legacy.ProductionUpdateError("installed GitHub console script set is invalid")
    for entry in scripts:
        result["entrypoint:" + entry.name] = _digest(_read_file(entry, max_bytes=256*1024))
    return result


def _install_stage_managed_units(app_root: Path, venv_root: Path,
                                 systemd_dir: Path) -> tuple[str, ...]:
    """Install reviewed units with the exact consolidated runtime paths.

    The retained legacy updater copies unrendered example units. Stage must
    instead use the consolidated-host path conversion, independent of a helper
    script that is not loaded by the exact-Git-object bootstrap.
    """
    for root in (app_root, venv_root):
        text = str(root)
        if (not root.is_absolute() or ".." in root.parts
                or any(c.isspace() or c in r"\%$'\";" for c in text)):
            raise legacy.ProductionUpdateError("unsafe systemd runtime path")
    legacy._require_directory(systemd_dir, label="stage systemd directory")
    # Validate the complete rendered set before the first destination change.
    approved: list[tuple[Path, bytes]] = []
    for source in legacy._managed_unit_sources(app_root):
        destination = systemd_dir / source.name
        if os.path.lexists(destination) and destination.is_symlink():
            raise legacy.ProductionUpdateError("symlinked staged systemd unit")
        raw = _read_file(source, max_bytes=1024 * 1024)
        try:
            rendered = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise legacy.ProductionUpdateError("invalid staged unit encoding") from exc
        if "\x00" in rendered or "\r" in rendered:
            raise legacy.ProductionUpdateError("unsupported systemd unit encoding")
        rendered = rendered.replace(
            "/opt/obsidian-github-sync/venv/bin",
            str(venv_root / "bin"),
        ).replace(
            "WorkingDirectory=/opt/obsidian-github-sync/app",
            f"WorkingDirectory={app_root}",
        )
        if "/opt/obsidian-github-sync/" in rendered:
            raise legacy.ProductionUpdateError("unrendered legacy runtime in Stage unit")
        approved.append((destination, rendered.encode("utf-8")))
    for destination, rendered in approved:
        legacy._atomic_install_bytes(rendered, destination)
    return tuple(destination.name for destination, _ in approved)


def _install_revision(path: Path, revision: str) -> None:
    legacy._require_directory(path.parent, label="summarizer revision directory")
    fd, name = tempfile.mkstemp(prefix=".daily-stage-", dir=path.parent)
    tmp = Path(name)
    try:
        os.fchmod(fd, 0o644)
        _write_all(fd, f"OBSIDIAN_AUTOMATION_REVISION={revision}\n".encode())
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(tmp, path)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if fd >= 0:
            os.close(fd)
        tmp.unlink(missing_ok=True)

def _receipt_read(state: Path, sha: str) -> dict[str, object]:
    if not isinstance(sha, str) or not HASH_RE.fullmatch(sha):
        raise legacy.ProductionUpdateError("stage receipt digest invalid")
    data = _read_file(state / f"{sha}.staging-receipt.json")
    if _digest(data) != sha:
        raise legacy.ProductionUpdateError("stage receipt content hash mismatch")
    obj = _decode(data)
    expected = {
        "record_version", "stage", "target_sha", "previous_sha",
        "app_root", "venv_root", "systemd_dir", "revision_env",
        "timer_states", "config_manifest", "unit_manifest", "env_manifest", "code_manifest", "direct_manifest",
        "build_manifest", "service_role_smoke", "safe_smoke", "daily_safe_smoke", "created_at",
    }
    if set(obj) != expected or obj["record_version"] != STAGE_RECEIPT_VERSION or obj["stage"] != "staged":
        raise legacy.ProductionUpdateError("stage receipt schema differs")
    for item in ("target_sha", "previous_sha"):
        if not isinstance(obj[item], str) or not SHA_RE.fullmatch(obj[item]):
            raise legacy.ProductionUpdateError("stage commit binding invalid")
    if obj["safe_smoke"] != "passed" or obj["daily_safe_smoke"] != "passed":
        raise legacy.ProductionUpdateError("stage safe-smoke receipts incomplete")
    if any(not isinstance(obj[item], dict)
           for item in ("config_manifest", "unit_manifest", "env_manifest", "code_manifest", "direct_manifest")):
        raise legacy.ProductionUpdateError("stage manifest types invalid")
    build = obj["build_manifest"]
    if build is not None and (
        not isinstance(build, dict) or set(build) != {
            "digest_sha256", "pip", "setuptools", "wheel"
        } or not isinstance(build["digest_sha256"], str)
        or not HASH_RE.fullmatch(build["digest_sha256"])
        or not all(
            isinstance(build[name], str)
            and re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", build[name])
            for name in ("pip", "setuptools", "wheel")
        )
    ):
        raise legacy.ProductionUpdateError("Stage build provenance receipt invalid")
    role_smoke = obj["service_role_smoke"]
    if (not isinstance(role_smoke, dict) or set(role_smoke) != {"status", "roles"}
            or role_smoke["status"] != "passed"
            or not isinstance(role_smoke["roles"], list)
            or len(role_smoke["roles"]) < 8
            or len(role_smoke["roles"]) > 64
            or len(set(role_smoke["roles"])) != len(role_smoke["roles"])
            or not all(isinstance(role, str) and role.startswith("obsidian-")
                       for role in role_smoke["roles"])):
        raise legacy.ProductionUpdateError("stage service-role smoke receipt invalid")
    timers = obj["timer_states"]
    if not isinstance(timers, dict) or set(timers) != set(legacy.MANAGED_TIMER_UNITS):
        raise legacy.ProductionUpdateError("stage timer keys differ")
    for t in timers.values():
        if not isinstance(t, dict) or set(t) != {"enabled", "active"} or any(type(x) is not bool for x in t.values()):
            raise legacy.ProductionUpdateError("stage timer state invalid")
    return obj

def stage_update(*, target_sha: str, app_root: Path, venv_root: Path,
                 systemd_dir: Path, receipt_dir: Path, daily_revision_env: Path,
                 config_files: Sequence[Path],
                 runner: legacy.CommandRunner = _trusted_stage_runner,
                 require_root: bool = True,
                 host_receipt_dir: Path | None = None) -> str:
    """Stage a pinned commit with only safe smoke; keep managed timers inert."""
    if require_root and os.geteuid() != 0:
        raise legacy.ProductionUpdateError("root required for staging")
    if require_root and receipt_dir != legacy.DEFAULT_RECEIPT_DIR:
        raise legacy.ProductionUpdateError("production Stage receipt root is fixed")
    if not isinstance(target_sha, str) or not SHA_RE.fullmatch(target_sha):
        raise legacy.ProductionUpdateError("staging target SHA invalid")
    with _lock(receipt_dir, require_root=require_root,
               host_receipt_dir=host_receipt_dir) as state:
        previous_control = _control_read(state)
        if previous_control is not None and previous_control["status"] != "activated":
            raise legacy.ProductionUpdateError("unresolved staged transaction exists")
        legacy._require_directory(venv_root, label="stage venv")
        legacy._require_directory(systemd_dir, label="stage systemd directory")
        previous_sha = _checkout(app_root, runner)
        legacy._validate_target(target_sha, app_root=app_root, runner=runner)
        if previous_sha != target_sha:
            ff = runner(("git", "-C", str(app_root), "merge-base",
                         "--is-ancestor", previous_sha, target_sha))
            if ff.returncode != 0:
                raise legacy.ProductionUpdateError(
                    "normal Stage rejects non-fast-forward or rollback targets"
                )
        _assert_known_timer_inventory(runner, systemd_dir)
        _assert_no_systemd_dropins(runner, systemd_dir,
                                  _all_installed_unit_names(systemd_dir))
        _require_idle_services(runner, systemd_dir)
        timers = _snapshot_timers(runner)
        configs = _config_manifest(config_files)
        # A privileged offline pip install would execute local pip/setuptools
        # as root. Refuse untrusted or redirected build environments BEFORE the
        # PREPARING marker/timer effects; test-mode runners use simulated pip.
        build_manifest: dict[str, str] | None = None
        if require_root:
            if venv_root != PRODUCTION_STAGE_VENV_ROOT:
                raise legacy.ProductionUpdateError("unexpected privileged Stage venv root")
            build_manifest = _assert_trusted_stage_build_venv(venv_root)
        # Reject unsafe ignored build trees before disabling any timer or
        # writing the durable PREPARING transition marker.
        _inspect_ignored_package_build_artifacts(app_root)
        _control_write(state, "preparing", target_sha, None)
        try:
            for unit in legacy.MANAGED_TIMER_UNITS:
                legacy._run(runner, ("systemctl", "disable", "--now", unit), label="stage stop managed timer")
            _require_inert(runner)
            _assert_known_timer_inventory(runner, systemd_dir)
            _require_idle_services(runner, systemd_dir)
            legacy._git(runner, app_root, "reset", "--hard", target_sha, label="stage exact checkout")
            _checkout(app_root, runner, expected=target_sha)
            _discard_ignored_package_build_artifacts(app_root)
            pip = venv_root / "bin" / "pip"
            if not pip.is_file():
                raise legacy.ProductionUpdateError("stage pip not installed")
            with _controlled_package_install_umask():
                # Build only with the already provisioned trusted venv backend.
                # PEP 517 build isolation can fetch arbitrary setuptools>=75
                # over the network even when the app Git SHA is fully pinned.
                legacy._run(
                    runner,
                    (str(pip), "install", "--no-deps", "--no-index",
                     "--no-build-isolation", "--no-cache-dir",
                     "--force-reinstall", str(app_root)),
                    label="stage install reviewed package",
                )
            if (require_root and
                    _assert_trusted_stage_build_venv(venv_root) != build_manifest):
                raise legacy.ProductionUpdateError(
                    "trusted Stage build environment changed during package install"
                )
            code_manifest = _deployed_code_manifest(app_root, venv_root)
            service_roles = _service_role_package_smoke(
                app_root, venv_root, code_manifest, runner
            )
            installed = _install_stage_managed_units(app_root, venv_root, systemd_dir)
            managed_names = _all_installed_unit_names(systemd_dir)
            if not set(installed).issubset(managed_names):
                raise legacy.ProductionUpdateError("installed unit inventory incomplete")
            _install_revision(daily_revision_env, target_sha)
            environment_files = _environment_manifest(systemd_dir, managed_names)
            direct_files = _direct_service_input_manifest(systemd_dir, managed_names)
            legacy._run(runner, ("systemctl", "daemon-reload"), label="stage reload units")
            _assert_known_timer_inventory(runner, systemd_dir)
            _assert_no_systemd_dropins(runner, systemd_dir, managed_names)
            smoke = venv_root / "bin" / "obsidian-github-production-smoke"
            daily = venv_root / "bin" / "obsidian-github-daily-production-smoke"
            if not smoke.is_file() or not daily.is_file():
                raise legacy.ProductionUpdateError("safe smoke executable missing")
            legacy._run(runner, (str(smoke), "--profile", "safe"), label="stage safe smoke")
            legacy._run(runner, (str(daily), "--profile", "safe"), label="stage Daily safe smoke")
            _require_inert(runner)
            _assert_known_timer_inventory(runner, systemd_dir)
            _assert_no_systemd_dropins(runner, systemd_dir, managed_names)
            _require_idle_services(runner, systemd_dir)
            if _deployed_code_manifest(app_root, venv_root) != code_manifest:
                raise legacy.ProductionUpdateError("installed package changed during safe Stage")
            if (require_root and
                    _assert_trusted_stage_build_venv(venv_root) != build_manifest):
                raise legacy.ProductionUpdateError("trusted Stage build environment drift")
            if _config_manifest(config_files) != configs:
                raise legacy.ProductionUpdateError("configuration drift during stage")
            if _all_installed_unit_names(systemd_dir) != managed_names:
                raise legacy.ProductionUpdateError("installed unit set changed during stage")
            if _environment_manifest(systemd_dir, managed_names) != environment_files:
                raise legacy.ProductionUpdateError("Unit EnvironmentFile drift during stage")
            if _direct_service_input_manifest(systemd_dir, managed_names) != direct_files:
                raise legacy.ProductionUpdateError("direct service file input drift during stage")
            receipt = {
                "record_version": STAGE_RECEIPT_VERSION, "stage": "staged",
                "previous_sha": previous_sha, "target_sha": target_sha,
                "app_root": str(app_root.absolute()), "venv_root": str(venv_root.absolute()),
                "systemd_dir": str(systemd_dir.absolute()),
                "revision_env": str(daily_revision_env.absolute()),
                "config_manifest": configs, "unit_manifest": _unit_manifest(systemd_dir, managed_names),
                "env_manifest": environment_files,
                "direct_manifest": direct_files,
                "code_manifest": code_manifest,
                "build_manifest": build_manifest,
                "service_role_smoke": {"status": "passed", "roles": list(service_roles)},
                "timer_states": timers, "safe_smoke": "passed",
                "daily_safe_smoke": "passed", "created_at": legacy._utc_now(),
            }
            payload = _encode(receipt)
            digest = _digest(payload)
            _publish(state, f"{digest}.staging-receipt.json", payload, immutable=True)
            _control_write(state, "staged", target_sha, digest)
            return digest
        except BaseException as original:
            # A failed stop must never be reported as safe containment.
            containment_error = None
            try:
                _contain_failed_timers(runner)
            except legacy.ProductionUpdateError as exc:
                containment_error = exc
            try:
                _control_write(state, "failed", target_sha, None)
            except Exception:
                pass
            if containment_error is not None:
                raise containment_error from original
            raise


def activate_update(*, stage_sha256: str, approve_live_github_writer: bool,
                    app_root: Path, venv_root: Path, systemd_dir: Path,
                    receipt_dir: Path, daily_revision_env: Path,
                    config_files: Sequence[Path], restore_sync_timer: bool = False,
                    restore_daily_timer: bool = False,
                    runner: legacy.CommandRunner = _trusted_stage_runner,
                    require_root: bool = True,
                    host_receipt_dir: Path | None = None) -> str:
    """Activate one exact Stage, with explicit live and timer approvals."""
    if require_root and os.geteuid() != 0:
        raise legacy.ProductionUpdateError("root required for activation")
    if require_root and receipt_dir != legacy.DEFAULT_RECEIPT_DIR:
        raise legacy.ProductionUpdateError("production Stage receipt root is fixed")
    if not approve_live_github_writer:
        raise legacy.ProductionUpdateError("live GitHub Writer smoke has no operator approval")
    if restore_daily_timer:
        raise legacy.ProductionUpdateError(
            "Daily timer requires separate date-scoped Writer/CAS publication approval"
        )
    if not isinstance(stage_sha256, str) or not HASH_RE.fullmatch(stage_sha256):
        raise legacy.ProductionUpdateError("full stage receipt SHA-256 required")
    with _lock(receipt_dir, require_root=require_root,
               host_receipt_dir=host_receipt_dir) as state:
        control = _control_read(state)
        if control is None or control["status"] != "staged" or control["stage_sha256"] != stage_sha256:
            raise legacy.ProductionUpdateError("no matching inert stage is eligible for activation")
        receipt = _receipt_read(state, stage_sha256)
        if receipt["target_sha"] != control["target_sha"]:
            raise legacy.ProductionUpdateError("stage target mismatch")
        expected_paths = {
            "app_root": app_root, "venv_root": venv_root,
            "systemd_dir": systemd_dir, "revision_env": daily_revision_env,
        }
        for key, path in expected_paths.items():
            if receipt[key] != str(path.absolute()):
                raise legacy.ProductionUpdateError("staged runtime path drift")
        if require_root:
            if venv_root != PRODUCTION_STAGE_VENV_ROOT:
                raise legacy.ProductionUpdateError("unexpected privileged Stage venv root")
            if not isinstance(receipt["build_manifest"], dict):
                raise legacy.ProductionUpdateError("Stage build provenance receipt missing")
            if _assert_trusted_stage_build_venv(venv_root) != receipt["build_manifest"]:
                raise legacy.ProductionUpdateError("Stage build environment changed before Activate")
        _checkout(app_root, runner, expected=receipt["target_sha"])
        _require_inert(runner)
        _assert_known_timer_inventory(runner, systemd_dir)
        _assert_no_systemd_dropins(runner, systemd_dir,
                                  _all_installed_unit_names(systemd_dir))
        _require_idle_services(runner, systemd_dir)
        if _deployed_code_manifest(app_root, venv_root) != receipt["code_manifest"]:
            raise legacy.ProductionUpdateError("installed package drift after stage")
        actual_roles = _service_role_package_smoke(
            app_root, venv_root, receipt["code_manifest"], runner
        )
        if list(actual_roles) != receipt["service_role_smoke"]["roles"]:
            raise legacy.ProductionUpdateError("service-role inventory drift after Stage")
        if _config_manifest(config_files) != receipt["config_manifest"]:
            raise legacy.ProductionUpdateError("configuration drift after stage")
        if _all_installed_unit_names(systemd_dir) != tuple(sorted(receipt["unit_manifest"])):
            raise legacy.ProductionUpdateError("installed unit set drift after stage")
        if _unit_manifest(systemd_dir, sorted(receipt["unit_manifest"])) != receipt["unit_manifest"]:
            raise legacy.ProductionUpdateError("unit-file drift after stage")
        if _environment_manifest(systemd_dir, sorted(receipt["unit_manifest"])) != receipt["env_manifest"]:
            raise legacy.ProductionUpdateError("Unit EnvironmentFile drift after stage")
        if _direct_service_input_manifest(systemd_dir, sorted(receipt["unit_manifest"])) != receipt["direct_manifest"]:
            raise legacy.ProductionUpdateError("direct service file input drift after stage")
        if _read_file(daily_revision_env) != f"OBSIDIAN_AUTOMATION_REVISION={receipt['target_sha']}\n".encode():
            raise legacy.ProductionUpdateError("Daily revision binding changed since Stage")

        timers = receipt["timer_states"]
        for unit, requested in (
            (legacy.TIMER_UNIT, restore_sync_timer),
            (legacy.DAILY_TIMER_UNIT, restore_daily_timer),
        ):
            if requested and not (timers[unit]["enabled"] or timers[unit]["active"]):
                raise legacy.ProductionUpdateError("timer restore exceeds pre-stage authority")

        # Before an effect-capable call: persist 'activating'. If interrupted,
        # a second call is refused rather than accidentally replaying Writer.
        _control_write(state, "activating", receipt["target_sha"], stage_sha256)
        try:
            smoke = venv_root / "bin" / "obsidian-github-production-smoke"
            if not smoke.is_file():
                raise legacy.ProductionUpdateError("live smoke executable missing")
            legacy._run(runner, (str(smoke), "--profile", "live"),
                        label="explicitly approved GitHub Writer live smoke")
            _assert_known_timer_inventory(runner, systemd_dir)
            _assert_no_systemd_dropins(runner, systemd_dir,
                                      _all_installed_unit_names(systemd_dir))
            _require_idle_services(runner, systemd_dir)
            for unit, restore in (
                (legacy.TIMER_UNIT, restore_sync_timer),
                (legacy.DAILY_TIMER_UNIT, restore_daily_timer),
            ):
                if restore:
                    saved = timers[unit]
                    legacy._restore_timer(runner, unit=unit, was_enabled=saved["enabled"],
                                          was_active=saved["active"])
            expected_timers = {
                unit: (bool(timers[unit]["enabled"]), bool(timers[unit]["active"]))
                if allowed else (False, False)
                for unit, allowed in (
                    (legacy.TIMER_UNIT, restore_sync_timer),
                    (legacy.DAILY_TIMER_UNIT, restore_daily_timer),
                )
            }
            for unit, expected in expected_timers.items():
                if legacy._timer_state(runner, unit) != expected:
                    raise legacy.ProductionUpdateError("timer state failed post-activation verification")
            if restore_sync_timer and timers[legacy.TIMER_UNIT]["active"]:
                _require_restored_sync_timer_armed(runner)

            result = {
                "record_version": VERSION, "stage": "activated",
                "stage_sha256": stage_sha256, "target_sha": receipt["target_sha"],
                "live_smoke": "passed", "restore_sync_timer": restore_sync_timer,
                "restore_daily_timer": restore_daily_timer,
                "completed_at": legacy._utc_now(),
            }
            payload = _encode(result)
            digest = _digest(payload)
            _publish(state, f"{digest}.activation-receipt.json", payload, immutable=True)
            _control_write(state, "activated", receipt["target_sha"], stage_sha256)
            return digest
        except BaseException as original:
            containment_error = None
            try:
                _contain_failed_timers(runner)
            except legacy.ProductionUpdateError as exc:
                containment_error = exc
            try:
                _control_write(state, "failed", receipt["target_sha"], stage_sha256)
            except Exception:
                pass
            if containment_error is not None:
                raise containment_error from original
            raise

def inspect_stage(*, receipt_dir: Path) -> dict[str, object]:
    """Read-only inspection never creates transaction or lock files."""
    state = receipt_dir / STAGING_ROOT
    if not os.path.lexists(state):
        return {"status": "none"}
    _state_dir(receipt_dir, create=False)
    current = _control_read(state)
    return {"status": "none"} if current is None else current

def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="obsidian-github-production-update stage|activate|inspect")
    subs = parser.add_subparsers(dest="phase", required=True)
    for phase in ("stage", "activate"):
        p = subs.add_parser(phase)
        p.add_argument("--app-root", type=Path, required=True)
        p.add_argument("--venv-root", type=Path, required=True)
        p.add_argument("--systemd-dir", type=Path, default=legacy.DEFAULT_SYSTEMD_DIR)
        p.add_argument("--receipt-dir", type=Path, default=legacy.DEFAULT_RECEIPT_DIR)
        p.add_argument("--daily-revision-env", type=Path, default=legacy.DEFAULT_DAILY_REVISION_ENV)
        p.add_argument("--bind-config-file", action="append", type=Path, default=None)
        if phase == "stage":
            p.add_argument("--target-sha", required=True)
        else:
            p.add_argument("--stage-sha256", required=True)
            p.add_argument("--approve-live-github-writer", action="store_true")
            p.add_argument("--restore-sync-timer", action="store_true")
            # Daily is controlled by a separately reviewed Writer/CAS admission.
    inspect = subs.add_parser("inspect")
    inspect.add_argument("--receipt-dir", type=Path, default=legacy.DEFAULT_RECEIPT_DIR)
    args = parser.parse_args(list(argv) if argv is not None else None)
    configs = (args.bind_config_file or [DEFAULT_CONFIG]) if args.phase != "inspect" else ()
    try:
        if args.phase == "inspect":
            result = inspect_stage(receipt_dir=args.receipt_dir)
        elif args.phase == "stage":
            digest = stage_update(
                target_sha=args.target_sha, app_root=args.app_root,
                venv_root=args.venv_root, systemd_dir=args.systemd_dir,
                receipt_dir=args.receipt_dir, daily_revision_env=args.daily_revision_env,
                config_files=configs,
            )
            result = {"status": "staged", "stage_sha256": digest}
        else:
            digest = activate_update(
                stage_sha256=args.stage_sha256,
                approve_live_github_writer=args.approve_live_github_writer,
                app_root=args.app_root, venv_root=args.venv_root,
                systemd_dir=args.systemd_dir, receipt_dir=args.receipt_dir,
                daily_revision_env=args.daily_revision_env,
                config_files=configs,
                restore_sync_timer=args.restore_sync_timer,
                restore_daily_timer=False,
            )
            result = {"status": "activated", "activation_sha256": digest}
    except (legacy.ProductionUpdateError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "failed", "error_class": type(exc).__name__}), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0
