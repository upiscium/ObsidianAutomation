"""Target-owned shared-runtime transaction; stdlib only, no canonical writes.

An intent journal is fsynced before any timer is changed. Same-target retries
retain original enable/active states. Unknown/masked unit states are refused
before mutation, not guessed. Only the four automation timers are owned here.
"""
from __future__ import annotations

import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time
from typing import Callable, Sequence

TIMERS = (
    "obsidian-ai-vault-pull.timer", "obsidian-pre-review.timer",
    "obsidian-github-sync.timer", "obsidian-core-promotion.timer",
)
SERVICES = (
    "obsidian-ai-vault-pull.service", "obsidian-ai-input-planner.service",
    "obsidian-ai-human-projection-sync.service",
    "obsidian-ai-review-intake.service",
    "obsidian-ai-post-review-executor-prepare.service",
    "obsidian-ai-post-review-transport.service",
    "obsidian-ai-post-review-executor-finalize.service",
    "obsidian-ai-post-review-reconcile.service",
    "obsidian-pre-review-generator.service",
    "obsidian-pre-review-validator.service", "obsidian-pre-review-reader.service",
    "obsidian-pre-review-evaluator.service", "obsidian-pre-review-status.service",
    "obsidian-github-sync-vault-pull.service", "obsidian-github-sync.service",
    "obsidian-github-writer.service", "obsidian-github-compactor.service",
    "obsidian-core-promotion.service",
)
ROLE_TIMERS = {
    "ai": TIMERS[:2], "github-sync": (TIMERS[2],), "publisher": (TIMERS[3],),
}
ROLE_CONFIGS = {
    "ai": (
        "/etc/obsidian-ai/rclone.conf",
        "/etc/obsidian-ai/pre-review-generator.env",
        "/etc/obsidian-ai/pre-review-evaluator.env",
        "/etc/obsidian-ai/review-intake.env",
        "/etc/obsidian-ai/review-intake-password",
        "/etc/obsidian-ai/vault-pull.filters",
    ),
    "github-sync": ("/etc/obsidian-github-sync/config.toml", "/etc/obsidian-github-mirror/rclone.conf",
                    "/etc/obsidian-github-writer/config.env", "/etc/obsidian-github-writer/webdav-password"),
    "publisher": ("/etc/obsidian-core-promotion/promotion.env", "/etc/obsidian-core-promotion/public-export.toml",
                  "/etc/obsidian-core-promotion/nextcloud.password"),
}


class LifecycleError(RuntimeError):
    """Fixed-code error; command output and environment are never propagated."""


def _safe_path(path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise LifecycleError("unsafe_runtime_path")
    for part in reversed((path, *path.parents)):
        if os.path.lexists(part) and part.is_symlink():
            raise LifecycleError("symlink_runtime_path")


def _write_json(path: Path, value: dict) -> None:
    _safe_path(path)
    data = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    fd, name = tempfile.mkstemp(prefix=".runtime-", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_directory(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _load_source_module(path: Path, name: str):
    _safe_path(path)
    if not path.is_file():
        raise LifecycleError("target_lifecycle_dependency_missing")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise LifecycleError("cannot_load_target_lifecycle_dependency")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class RuntimeTransaction:
    def __init__(
        self, *, source_root: Path, target_sha: str, venv_root: Path,
        receipt_dir: Path, runner: Callable, require_root: bool = True,
        systemd_dir: Path = Path("/etc/systemd/system"),
        revision_env: Path = Path("/etc/obsidian-ai/pre-review-revision.env"),
        drain_timeout: float = 300.0, clock: Callable = time.monotonic,
        sleep: Callable = time.sleep, config_exists: Callable = os.path.lexists,
    ):
        if not re.fullmatch(r"[0-9a-f]{40,64}", target_sha):
            raise LifecycleError("invalid_target_sha")
        self.source_root, self.target_sha, self.venv_root = source_root, target_sha, venv_root
        self.receipt_dir, self.runner, self.require_root = receipt_dir, runner, require_root
        self.systemd_dir, self.revision_env = systemd_dir, revision_env
        self.drain_timeout, self.clock, self.sleep = drain_timeout, clock, sleep
        self.config_exists = config_exists
        self.intent_path = receipt_dir / "pending-runtime.json"
        self.intent = None
        self.lock_fd = None
        self.finished = False
        self.host_activation = "not_attempted"
        self.mutation_started = False

    def _run(self, args: Sequence[str], code: str) -> str:
        result = self.runner(tuple(str(arg) for arg in args))
        if result.returncode:
            raise LifecycleError(code)
        return result.stdout.strip()

    def _state(self, name: str) -> dict[str, str]:
        result = self.runner((
            "systemctl", "show", name, "--property=LoadState", "--property=ActiveState",
            "--property=SubState", "--property=UnitFileState",
            "--property=NextElapseUSecMonotonic",
            "--property=MainPID", "--property=ControlPID", "--property=Job",
        ))
        state = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        # systemctl may return 1 with a complete not-found unit representation.
        # A failed bus/query with no such representation is never absence.
        if result.returncode and not (result.returncode == 1 and state.get("LoadState") == "not-found"
                                     and state.get("ActiveState") == "inactive"):
            raise LifecycleError("systemd_query_failed")
        if state.get("LoadState") not in {"loaded", "not-found"}:
            raise LifecycleError("unsupported_unit_load_state")
        if state.get("ActiveState") not in {"active", "inactive", "failed", "activating", "deactivating"}:
            raise LifecycleError("unknown_unit_active_state")
        return state

    def _snapshot(self) -> dict:
        saved = {}
        for name in TIMERS:
            state = self._state(name)
            exists = state["LoadState"] == "loaded"
            enabled = state.get("UnitFileState", "")
            active = state["ActiveState"]
            if exists and (enabled not in {"enabled", "disabled"} or active not in {"active", "inactive"}):
                raise LifecycleError("unsupported_timer_state")
            if not exists and (active != "inactive" or enabled not in {"", "not-found"}):
                raise LifecycleError("inconsistent_absent_timer")
            saved[name] = {"exists": exists, "enabled": enabled == "enabled", "active": active == "active"}
        return saved

    def _read_intent(self) -> dict:
        _safe_path(self.intent_path)
        info = self.intent_path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 16384 or info.st_mode & 0o077:
            raise LifecycleError("unsafe_runtime_intent")
        if self.require_root and info.st_uid != 0:
            raise LifecycleError("unsafe_runtime_intent_owner")
        def unique_pairs(pairs):
            value = {}
            for key, field in pairs:
                if key in value:
                    raise ValueError("duplicate key")
                value[key] = field
            return value
        try:
            value = json.loads(self.intent_path.read_bytes(), object_pairs_hook=unique_pairs)
        except (ValueError, UnicodeError):
            raise LifecycleError("invalid_runtime_intent") from None
        if (not isinstance(value, dict) or type(value.get("record_version")) is not int
            or value["record_version"] != 1):
            raise LifecycleError("invalid_runtime_intent")
        if value.get("target_sha") != self.target_sha:
            raise LifecycleError("pending_update_requires_same_target")
        states = value.get("timers")
        if not isinstance(states, dict) or set(states) != set(TIMERS):
            raise LifecycleError("invalid_runtime_intent")
        for state in states.values():
            if not isinstance(state, dict) or set(state) != {"exists", "enabled", "active"}:
                raise LifecycleError("invalid_runtime_intent")
            if any(type(flag) is not bool for flag in state.values()):
                raise LifecycleError("invalid_runtime_intent")
            if not state["exists"] and (state["enabled"] or state["active"]):
                raise LifecycleError("invalid_runtime_intent")
        # Never carry unrecognized/free-form fields forward into receipts.
        return {"record_version": 1, "target_sha": self.target_sha, "timers": states, "phase": "resuming"}

    def __enter__(self):
        if self.require_root and os.geteuid() != 0:
            raise LifecycleError("root_required")
        _safe_path(self.receipt_dir)
        self.receipt_dir.mkdir(parents=True, exist_ok=True, mode=0o750)
        directory = self.receipt_dir.stat()
        if not stat.S_ISDIR(directory.st_mode) or directory.st_mode & 0o022:
            raise LifecycleError("unsafe_runtime_receipt_directory")
        if self.require_root and directory.st_uid != 0:
            raise LifecycleError("unsafe_runtime_receipt_owner")
        lock = self.receipt_dir / "host-update.lock"
        _safe_path(lock)
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077
                or (self.require_root and info.st_uid != 0)):
                raise LifecycleError("unsafe_runtime_lock")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(fd)
            raise
        self.lock_fd = fd
        return self

    def _save(self, phase: str, **fields) -> None:
        assert self.intent is not None
        self.intent.update(phase=phase, **fields)
        _write_json(self.intent_path, self.intent)

    def _disable_all(self) -> bool:
        okay = True
        for name in TIMERS:
            try:
                state = self._state(name)
                if state["LoadState"] == "loaded":
                    self._run(("systemctl", "disable", "--now", name), "timer_disable_failed")
                    state = self._state(name)
                    if state.get("UnitFileState") != "disabled" or state["ActiveState"] != "inactive":
                        raise LifecycleError("timer_not_inert")
            except Exception:
                okay = False
        return okay

    def prepare(self) -> None:
        # Reading every unit before the first mutation distinguishes a broken
        # systemd query from genuinely absent units on first installation.
        current = self._snapshot()
        for name in SERVICES:
            self._state(name)
        if self.intent_path.exists():
            self.intent = self._read_intent()
        else:
            self.intent = {"record_version": 1, "target_sha": self.target_sha, "timers": current, "phase": "prepared"}
        self._save("prepared")
        self.mutation_started = True
        if not self._disable_all():
            raise LifecycleError("timer_quiesce_failed")
        deadline = self.clock() + self.drain_timeout
        while True:
            busy = False
            for name in SERVICES:
                state = self._state(name)
                if (state["ActiveState"] not in {"inactive", "failed"}
                    or any(state.get(k, "0") not in {"0", ""} for k in ("MainPID", "ControlPID", "Job"))):
                    busy = True
            if not busy:
                break
            if self.clock() >= deadline:
                raise LifecycleError("runtime_drain_timeout")
            self.sleep(0.25)
        self._save("quiesced")

    def stage_and_smoke(self) -> None:
        assert self.intent is not None
        # Failed inactive units may be reset only after the old runtime drained.
        for name in SERVICES:
            state = self._state(name)
            if state["LoadState"] == "loaded" and state["ActiveState"] == "failed":
                self._run(("systemctl", "reset-failed", name), "reset_failed_unit_failed")
        stager = _load_source_module(self.source_root / "tools/stage_automation_units.py", "_target_unit_stager")
        stager.stage_units(target_sha=self.target_sha, source_root=self.source_root,
                           systemd_dir=self.systemd_dir, revision_env=self.revision_env,
                           runner=self.runner, require_root=self.require_root)
        self._save("units_staged")
        python = str(self.venv_root / "bin/python")
        self._run((python, "-c", "import obsidian_automation.managed_promotion_deployment; import obsidian_automation.github_project_status_worker; import obsidian_automation.pre_review_worker"), "package_import_smoke_failed")
        self._run((str(self.venv_root / "bin/obsidian-pre-review-production-smoke"), "--profile", "safe",
                   "--expected-revision", self.target_sha, "--revision-env", str(self.revision_env),
                   "--systemd-dir", str(self.systemd_dir)), "pre_review_safe_smoke_failed")
        self._run((str(self.venv_root / "bin/obsidian-github-production-smoke"), "--profile", "safe"), "github_safe_smoke_failed")
        # Reapply file-level ACLs on existing orchestration and immutable stage
        # artifacts after the package-stage directory ACLs. This reads no payload.
        self._run(("sh", str(self.source_root / "examples/ai/bootstrap-pre-review-authority.sh")), "pre_review_authority_failed")
        for role, paths in ROLE_CONFIGS.items():
            configured = any(self.config_exists(path) for path in paths)
            previously_serving = any(self.intent["timers"][name]["enabled"] or self.intent["timers"][name]["active"] for name in ROLE_TIMERS[role])
            if configured:
                self._run((sys.executable, str(self.source_root / "tools/private_config_transfer.py"), "verify", "--role", role), "private_authority_smoke_failed")
            elif previously_serving:
                raise LifecycleError("serving_role_configuration_missing")
        if not self._disable_all():
            raise LifecycleError("post_smoke_timer_not_inert")
        self._save("smoke_passed")

    def restore(self) -> None:
        assert self.intent is not None and self.intent["phase"] == "smoke_passed"
        self._save("restoring")
        for name, before in self.intent["timers"].items():
            if before["enabled"]:
                self._run(("systemctl", "enable", name), "timer_enable_failed")
            if before["active"]:
                self._run(("systemctl", "start", name), "timer_start_failed")
            after = self._state(name)
            if after.get("UnitFileState") != ("enabled" if before["enabled"] else "disabled"):
                raise LifecycleError("timer_enablement_restore_mismatch")
            if after["ActiveState"] != ("active" if before["active"] else "inactive"):
                raise LifecycleError("timer_activation_restore_mismatch")
            if before["active"]:
                substate = after.get("SubState", "")
                if substate not in {"waiting", "running"}:
                    raise LifecycleError("timer_not_armed_after_restore")
                if (
                    substate == "waiting"
                    and after.get("NextElapseUSecMonotonic", "")
                    in {"", "0", "infinity", "n/a"}
                ):
                    raise LifecycleError("timer_not_armed_after_restore")
        self.host_activation = "restored" if any(s["enabled"] or s["active"] for s in self.intent["timers"].values()) else "not_attempted"

    def finish(self) -> None:
        assert self.intent is not None
        self._save("completed")
        # An immutable history entry survives removal of the pending intent.
        history = self.receipt_dir / (f"{time.time_ns()}-{self.target_sha[:12]}.runtime.json")
        _write_json(history, self.intent)
        self.intent_path.unlink()
        _fsync_directory(self.receipt_dir)
        self.finished = True

    def __exit__(self, exc_type, exc, traceback):
        try:
            if not self.finished and self.mutation_started:
                contained = self._disable_all()
                self._save("failed", containment="disabled" if contained else "manual_intervention_required")
        finally:
            if self.lock_fd is not None:
                os.close(self.lock_fd)
                self.lock_fd = None
        return False
