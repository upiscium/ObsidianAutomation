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

def _require_idle_services(runner: legacy.CommandRunner) -> None:
    for unit in EFFECT_SERVICES:
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
            if not line.startswith("EnvironmentFile="):
                continue
            raw = line.partition("=")[2].strip()
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
        "timer_states", "config_manifest", "unit_manifest", "env_manifest",
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
           for item in ("config_manifest", "unit_manifest", "env_manifest")):
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
        _require_idle_services(runner)
        timers = _snapshot_timers(runner)
        configs = _config_manifest(config_files)
        _control_write(state, "preparing", target_sha, None)
        try:
            for unit in legacy.MANAGED_TIMER_UNITS:
                legacy._run(runner, ("systemctl", "disable", "--now", unit), label="stage stop managed timer")
            _require_inert(runner)
            _require_idle_services(runner)
            legacy._git(runner, app_root, "reset", "--hard", target_sha, label="stage exact checkout")
            _checkout(app_root, runner, expected=target_sha)
            pip = venv_root / "bin" / "pip"
            if not pip.is_file():
                raise legacy.ProductionUpdateError("stage pip not installed")
            legacy._run(runner, (str(pip), "install", "--no-deps", "--force-reinstall", str(app_root)),
                        label="stage install reviewed package")
            installed = legacy._install_managed_units(app_root, systemd_dir)
            managed_names = _all_installed_unit_names(systemd_dir)
            if not set(installed).issubset(managed_names):
                raise legacy.ProductionUpdateError("installed unit inventory incomplete")
            _install_revision(daily_revision_env, target_sha)
            environment_files = _environment_manifest(systemd_dir, managed_names)
            legacy._run(runner, ("systemctl", "daemon-reload"), label="stage reload units")
            smoke = venv_root / "bin" / "obsidian-github-production-smoke"
            daily = venv_root / "bin" / "obsidian-github-daily-production-smoke"
            if not smoke.is_file() or not daily.is_file():
                raise legacy.ProductionUpdateError("safe smoke executable missing")
            legacy._run(runner, (str(smoke), "--profile", "safe"), label="stage safe smoke")
            legacy._run(runner, (str(daily), "--profile", "safe"), label="stage Daily safe smoke")
            _require_inert(runner)
            _require_idle_services(runner)
            if _config_manifest(config_files) != configs:
                raise legacy.ProductionUpdateError("configuration drift during stage")
            if _all_installed_unit_names(systemd_dir) != managed_names:
                raise legacy.ProductionUpdateError("installed unit set changed during stage")
            if _environment_manifest(systemd_dir, managed_names) != environment_files:
                raise legacy.ProductionUpdateError("Unit EnvironmentFile drift during stage")
            receipt = {
                "record_version": VERSION, "stage": "staged",
                "previous_sha": previous_sha, "target_sha": target_sha,
                "app_root": str(app_root.absolute()), "venv_root": str(venv_root.absolute()),
                "systemd_dir": str(systemd_dir.absolute()),
                "revision_env": str(daily_revision_env.absolute()),
                "config_manifest": configs, "unit_manifest": _unit_manifest(systemd_dir, managed_names),
                "env_manifest": environment_files,
                "timer_states": timers, "safe_smoke": "passed",
                "daily_safe_smoke": "passed", "created_at": legacy._utc_now(),
            }
            payload = _encode(receipt)
            digest = _digest(payload)
            _publish(state, f"{digest}.staging-receipt.json", payload, immutable=True)
            _control_write(state, "staged", target_sha, digest)
            return digest
        except BaseException:
            # On SIGKILL, 'preparing' remains durable. Both are blocked until
            # an independently authorized state reconciliation.
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
        _require_idle_services(runner)
        if _config_manifest(config_files) != receipt["config_manifest"]:
            raise legacy.ProductionUpdateError("configuration drift after stage")
        if _all_installed_unit_names(systemd_dir) != tuple(sorted(receipt["unit_manifest"])):
            raise legacy.ProductionUpdateError("installed unit set drift after stage")
        if _unit_manifest(systemd_dir, sorted(receipt["unit_manifest"])) != receipt["unit_manifest"]:
            raise legacy.ProductionUpdateError("unit-file drift after stage")
        if _environment_manifest(systemd_dir, sorted(receipt["unit_manifest"])) != receipt["env_manifest"]:
            raise legacy.ProductionUpdateError("Unit EnvironmentFile drift after stage")
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
            _require_idle_services(runner)
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
