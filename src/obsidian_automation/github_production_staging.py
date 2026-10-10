"""Two-phase, explicitly approved GitHub updater for #294.

The legacy all-in-one updater is kept intact. Stage must not run live smoke.
Activation cannot infer approval from SSH connectivity or from Stage success.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import stat
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from . import github_production_update as legacy

VERSION = 1
STAGING_ROOT = "staged-github-production-v1"
CONTROL_FILE = "control.json"
SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_RECORD = 32768
DEFAULT_CONFIG = Path("/etc/obsidian-github-summarizer/config.env")
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
    return result

@contextmanager
def _lock(root: Path):
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
    for argv in commands:
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
        if names != expected:
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
        # Search all supported pending vendor/root/global overrides.
        for prefix in directories:
            # Name-specific and systemd-supported type-wide overrides.
            # Refuse any directory at these positions (including symlinks).
            for dirname in (name + ".d", name.rpartition(".")[2] + ".d"):
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
    directive = re.compile(r"^Exec(?:Start|StartPre|StartPost|Reload|Stop|StopPost)\s*=(.*)$")
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
            try:
                words = shlex.split(command, posix=True)
            except ValueError as exc:
                raise legacy.ProductionUpdateError("invalid systemd ExecStart arguments") from exc
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

    result: dict[str, str] = {}
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
        "safe_smoke", "daily_safe_smoke", "created_at",
    }
    if set(obj) != expected or obj["record_version"] != VERSION or obj["stage"] != "staged":
        raise legacy.ProductionUpdateError("stage receipt schema differs")
    for item in ("target_sha", "previous_sha"):
        if not isinstance(obj[item], str) or not SHA_RE.fullmatch(obj[item]):
            raise legacy.ProductionUpdateError("stage commit binding invalid")
    if obj["safe_smoke"] != "passed" or obj["daily_safe_smoke"] != "passed":
        raise legacy.ProductionUpdateError("stage safe-smoke receipts incomplete")
    if any(not isinstance(obj[item], dict)
           for item in ("config_manifest", "unit_manifest", "env_manifest", "code_manifest", "direct_manifest")):
        raise legacy.ProductionUpdateError("stage manifest types invalid")
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
                 runner: legacy.CommandRunner = legacy._default_runner,
                 require_root: bool = True) -> str:
    """Stage a pinned commit with only safe smoke; keep managed timers inert."""
    if require_root and os.geteuid() != 0:
        raise legacy.ProductionUpdateError("root required for staging")
    if not isinstance(target_sha, str) or not SHA_RE.fullmatch(target_sha):
        raise legacy.ProductionUpdateError("staging target SHA invalid")
    with _lock(receipt_dir) as state:
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
        _control_write(state, "preparing", target_sha, None)
        try:
            for unit in legacy.MANAGED_TIMER_UNITS:
                legacy._run(runner, ("systemctl", "disable", "--now", unit), label="stage stop managed timer")
            _require_inert(runner)
            _assert_known_timer_inventory(runner, systemd_dir)
            _require_idle_services(runner, systemd_dir)
            legacy._git(runner, app_root, "reset", "--hard", target_sha, label="stage exact checkout")
            _checkout(app_root, runner, expected=target_sha)
            pip = venv_root / "bin" / "pip"
            if not pip.is_file():
                raise legacy.ProductionUpdateError("stage pip not installed")
            legacy._run(runner, (str(pip), "install", "--no-deps", "--force-reinstall", str(app_root)),
                        label="stage install reviewed package")
            code_manifest = _deployed_code_manifest(app_root, venv_root)
            installed = legacy._install_managed_units(app_root, systemd_dir)
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
            if _config_manifest(config_files) != configs:
                raise legacy.ProductionUpdateError("configuration drift during stage")
            if _all_installed_unit_names(systemd_dir) != managed_names:
                raise legacy.ProductionUpdateError("installed unit set changed during stage")
            if _environment_manifest(systemd_dir, managed_names) != environment_files:
                raise legacy.ProductionUpdateError("Unit EnvironmentFile drift during stage")
            if _direct_service_input_manifest(systemd_dir, managed_names) != direct_files:
                raise legacy.ProductionUpdateError("direct service file input drift during stage")
            receipt = {
                "record_version": VERSION, "stage": "staged",
                "previous_sha": previous_sha, "target_sha": target_sha,
                "app_root": str(app_root.absolute()), "venv_root": str(venv_root.absolute()),
                "systemd_dir": str(systemd_dir.absolute()),
                "revision_env": str(daily_revision_env.absolute()),
                "config_manifest": configs, "unit_manifest": _unit_manifest(systemd_dir, managed_names),
                "env_manifest": environment_files,
                "direct_manifest": direct_files,
                "code_manifest": code_manifest,
                "timer_states": timers, "safe_smoke": "passed",
                "daily_safe_smoke": "passed", "created_at": legacy._utc_now(),
            }
            payload = _encode(receipt)
            digest = _digest(payload)
            _publish(state, f"{digest}.staging-receipt.json", payload, immutable=True)
            _control_write(state, "staged", target_sha, digest)
            return digest
        except BaseException:
            # An interrupted/failed Stage has no authority to restore timers.
            # Best-effort stop BOTH, even when an intermediate disable failed;
            # never let a restored timer invoke old/new code accidentally.
            for unit in legacy.MANAGED_TIMER_UNITS:
                try:
                    runner(("systemctl", "disable", "--now", unit))
                except Exception:
                    pass
            # A hard kill leaves the durable preparing marker for manual
            # reconciliation. Do not infer safe recovery from a failed step.
            try:
                _control_write(state, "failed", target_sha, None)
            except Exception:
                pass
            raise


def activate_update(*, stage_sha256: str, approve_live_github_writer: bool,
                    app_root: Path, venv_root: Path, systemd_dir: Path,
                    receipt_dir: Path, daily_revision_env: Path,
                    config_files: Sequence[Path], restore_sync_timer: bool = False,
                    restore_daily_timer: bool = False,
                    runner: legacy.CommandRunner = legacy._default_runner,
                    require_root: bool = True) -> str:
    """Activate one exact Stage, with explicit live and timer approvals."""
    if require_root and os.geteuid() != 0:
        raise legacy.ProductionUpdateError("root required for activation")
    if not approve_live_github_writer:
        raise legacy.ProductionUpdateError("live GitHub Writer smoke has no operator approval")
    if restore_daily_timer:
        raise legacy.ProductionUpdateError(
            "Daily timer requires separate date-scoped Writer/CAS publication approval"
        )
    if not isinstance(stage_sha256, str) or not HASH_RE.fullmatch(stage_sha256):
        raise legacy.ProductionUpdateError("full stage receipt SHA-256 required")
    with _lock(receipt_dir) as state:
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
        _checkout(app_root, runner, expected=receipt["target_sha"])
        _require_inert(runner)
        _assert_known_timer_inventory(runner, systemd_dir)
        _assert_no_systemd_dropins(runner, systemd_dir,
                                  _all_installed_unit_names(systemd_dir))
        _require_idle_services(runner, systemd_dir)
        if _deployed_code_manifest(app_root, venv_root) != receipt["code_manifest"]:
            raise legacy.ProductionUpdateError("installed package drift after stage")
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
        except BaseException:
            for unit in legacy.MANAGED_TIMER_UNITS:
                try:
                    runner(("systemctl", "disable", "--now", unit))
                except Exception:
                    pass
            try:
                _control_write(state, "failed", receipt["target_sha"], stage_sha256)
            except Exception:
                pass
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
