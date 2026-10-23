#!/usr/bin/env python3
"""#403 exact-revision disabled-only overlay. No application imports or unit effects.

The production CLI has no path, digest, revision or unit selectors. Paths may
be injected only by in-process disposable-root tests (never by CLI).
"""
from __future__ import annotations

import argparse
import fcntl
import grp
import hashlib
import json
import os
import posixpath
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys

REVISION = "fb8d5cb8aff3da22a0d93eb48fe2ba35b7fa1c30"
GLOBAL_HEAD = "2b860f2fdec954ed363f6b23ed1df5f1597bbaf3"
GIT_BLOB = "66ecad9a46030516457405157f9c9a3f660899c4"
RELEASE = {
    "version": 1, "release_commit": REVISION,
    "source_path": "src/obsidian_automation/admin_workflow_quiescence.py",
    "git_blob": GIT_BLOB,
    "source_sha256": "7e13c28727a9fbc2cb74082aacba8fca544d092ff6e36aeef2635813996edcf7",
}
OLD_REVISION = "1b38edf4596e696386295ae624e3a09a847c2693"
OLD_AUTH = "aafc54c00d84e8ebd248630c3054c3b082494dd7086270f4158c8ecf2fa5e121"
NEW_AUTH = "662cfb3b030d845f16384b3f413cdd7267291aff89f5d6878bc6a70c1d352359"
OLD_MODULE = "dadf7400ae15d4be1f6ba6949be10716d642f8acbf53ffa4f732526d20358321"
NEW_MODULE = "7e13c28727a9fbc2cb74082aacba8fca544d092ff6e36aeef2635813996edcf7"
CHANGED = "admin_workflow_quiescence.py"
MODULES = frozenset((
    "admin_operator_runtime.py", "admin_operation_broker.py", "admin_operation_queue.py",
    "admin_control_queue_socket.py", "admin_control_bridge.py", "admin_systemd_admission.py",
    "admin_local_maintenance_gate.py", "admin_preview_daemon.py",
    "admin_operation_preflight.py", CHANGED, "admin_broker_socket.py",
    "admin_operation_preview.py", "admin_status.py", "__init__.py",
))
UNITS = frozenset((
    "obsidian-ai-vault-pull.timer", "obsidian-ai-vault-pull.service",
    "obsidian-pre-review.timer", "obsidian-pre-review-status.service",
    "obsidian-github-sync.timer", "obsidian-github-compactor.service",
    "obsidian-github-daily-progress.timer", "obsidian-github-daily-apply.service",
    "obsidian-core-promotion.timer", "obsidian-core-promotion.service",
))
TIMERS = tuple(sorted(x for x in UNITS if x.endswith(".timer")))
SERVICES = tuple("obsidian-" + x + ".service" for x in (
    "ai-vault-pull", "semantic-index-refresh-prepare", "semantic-index-refresh-embed",
    "semantic-index-refresh-finalize", "ai-input-planner", "ai-human-projection-sync",
    "ai-human-projection-cleanup-sync", "ai-review-intake",
    "ai-post-review-executor-prepare", "ai-post-review-transport",
    "ai-post-review-executor-finalize", "ai-post-review-projection-sync",
    "ai-post-review-reconcile", "pre-review-generator", "pre-review-validator",
    "pre-review-reader", "pre-review-evaluator", "pre-review-status",
    "github-sync-vault-pull", "github-sync", "github-writer", "github-compactor",
    "github-daily-schedule", "github-daily-collect", "github-daily-summary",
    "github-daily-render", "github-daily-apply", "core-promotion",
))
OLD_ID = "c3b97d52-c988-47b9-addb-31ea49960b71"
PAUSE_ID = "75e533e0-bdba-439e-80cb-d53754faa6c0"
OLD_FILES = tuple(p + "-" + OLD_ID + ".json" for p in (
    "request", "processing", "result", "manual-evidence", "manual-reconcile",
))
PAUSE_FILES = tuple(p + "-" + PAUSE_ID + ".json" for p in (
    "request", "processing", "result",
))
STAGE_NAME = "disabled-quiescence-" + REVISION[:12]
AUTHORITY_DIRECTORY = Path("/etc/obsidian-admin")
QUEUE_DIRECTORY = Path("/var/lib/obsidian-admin/queue")
RUNTIME_DIRECTORY = Path("/run/obsidian-admin")
SOCKET_NAME = "operation.sock"
MAX_MOUNTINFO_BYTES = 1024 * 1024


def kernel_mountinfo():
    """Read only the current process's kernel mount table, with a hard bound."""
    fd = os.open("/proc/self/mountinfo", os.O_RDONLY | os.O_NOFOLLOW |
                 os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        raw = os.read(fd, MAX_MOUNTINFO_BYTES + 1)
    finally:
        os.close(fd)
    require(0 < len(raw) <= MAX_MOUNTINFO_BYTES, "mountinfo_unavailable")
    return raw


def mount_inventory(raw):
    """Parse mountinfo identity, including same-device bind mounts and escapes.

    Unlike st_dev/ismount, mountinfo records bind mountpoints even when the
    backing filesystem/device is identical. Do not execute or import mounted
    source code; inspect only metadata for the fixed target paths.
    """
    require(type(raw) is bytes and 0 < len(raw) <= MAX_MOUNTINFO_BYTES
            and raw.endswith(b"\n"), "mountinfo_unavailable")
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise Blocked("mountinfo_encoding") from exc
    lines = text.splitlines()
    require(0 < len(lines) <= 8192, "mountinfo_unbounded")
    entries = []
    ids = set()

    def unescape(value):
        require(not re.search(r"\\(?![0-7]{3})", value), "mountinfo_escape")
        decoded = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), value)
        require(decoded.startswith("/") and "\x00" not in decoded
                and posixpath.normpath(decoded) == decoded
                and ".." not in Path(decoded).parts, "mountinfo_path")
        return Path(decoded)

    for line in lines:
        require(0 < len(line) <= 16384 and line.count(" - ") == 1,
                "mountinfo_structure")
        before, after = line.split(" - ", 1)
        fields, tail = before.split(), after.split()
        require(len(fields) >= 6 and len(tail) >= 3
                and fields[0].isdigit() and fields[1].isdigit()
                and re.fullmatch(r"[0-9]+:[0-9]+", fields[2]) is not None
                and fields[0] not in ids, "mountinfo_structure")
        ids.add(fields[0])
        root, point = unescape(fields[3]), unescape(fields[4])
        entries.append((fields[0], fields[1], fields[2], str(root),
                        str(point), tail[0], tail[1]))
    return tuple(entries)


def strict_queue_json(raw):
    """Match the durable broker's recursive duplicate-key rejection."""
    def unique(pairs):
        obj = {}
        for key, value in pairs:
            require(key not in obj, "queue_duplicate_key")
            obj[key] = value
        return obj

    value = json.loads(raw, object_pairs_hook=unique)
    require(type(value) is dict and canonical(value) == raw,
            "queue_noncanonical_record")
    return value


class Blocked(RuntimeError):
    pass


def require(ok, why):
    if not ok:
        raise Blocked(why)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


class Paths:
    """Only the production instance is reachable from main()."""
    def __init__(self, root=Path("/")):
        self.root = root

    def at(self, name):
        require(name.startswith("/") and ".." not in Path(name).parts, "unsafe_path")
        return self.root / name.lstrip("/")


def web_group_gid(uid):
    # Production uses the independently configured, fixed OS role. Only
    # disposable non-root tests inject another known supplementary group.
    return grp.getgrnam("obsidian-admin-web").gr_gid


def require_parent_identity(info, canonical_path, uid, web_gid):
    base_gid = 0 if uid == 0 else os.getgid()
    role_modes = {AUTHORITY_DIRECTORY: 0o710, QUEUE_DIRECTORY: 0o700}
    if canonical_path in role_modes:
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == uid
                and info.st_gid == web_gid
                and stat.S_IMODE(info.st_mode) == role_modes[canonical_path],
                "unsafe_role_parent")
    else:
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == uid
                and info.st_gid == base_gid
                and not stat.S_IMODE(info.st_mode) & 0o022,
                "unsafe_parent")


def parent_fd(path, uid):
    """Walk every component without following links; retain the final dir fd."""
    require(path.is_absolute() and ".." not in path.parts, "unsafe_path")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        current = Path("/")
        web_gid = web_group_gid(uid)
        for component in path.parts[1:-1]:
            nxt = os.open(component, os.O_RDONLY | os.O_DIRECTORY |
                          os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            try:
                current /= component
                info = os.fstat(nxt)
                entry = os.stat(component, dir_fd=fd, follow_symlinks=False)
                require((entry.st_dev, entry.st_ino) == (info.st_dev, info.st_ino),
                        "replaced_parent")
                require_parent_identity(info, current, uid, web_gid)
            except BaseException:
                os.close(nxt)
                raise
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def directory(path, uid, mode=None):
    fd = parent_fd(path / "sentinel", uid)
    try:
        info = os.fstat(fd)
        require(mode is None or stat.S_IMODE(info.st_mode) == mode, "directory_mode")
    finally:
        os.close(fd)


def read(path, uid, mode, bound=2 * 1024 * 1024, *, gid=None):
    fd = parent_fd(path, uid)
    try:
        item = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW |
                       os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
        try:
            info = os.fstat(item)
            check_file(info, uid, mode, bound, gid=gid)
            data = os.read(item, bound + 1)
            after = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
            require(len(data) == info.st_size and
                    (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns) ==
                    (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_ctime_ns),
                    "unstable_file")
            return data
        finally:
            os.close(item)
    finally:
        os.close(fd)


def check_file(info, uid, mode, bound, *, gid=None):
    require(stat.S_ISREG(info.st_mode) and info.st_uid == uid
            and info.st_gid == (gid if gid is not None else
                                (0 if uid == 0 else os.getgid()))
            and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == mode
            and 0 < info.st_size <= bound, "unsafe_file")


def exists(path, uid):
    fd = parent_fd(path, uid)
    try:
        try:
            os.stat(path.name, dir_fd=fd, follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False
    finally:
        os.close(fd)


def worker_socket_absent(paths, uid):
    """Only the fixed Worker socket may have an absent runtime parent.

    Open and pin /run first. ENOENT from opening its single known child is
    evidence of absence; all other errors, aliases and required missing
    parents remain failures. Neither mkdir nor unlink is ever attempted.
    """
    runtime = paths.at(str(RUNTIME_DIRECTORY))
    require(runtime == paths.at("/run/obsidian-admin"), "unsafe_socket_path")
    runfd = parent_fd(runtime, uid)
    try:
        anchor = os.fstat(runfd)
        require(stat.S_ISDIR(anchor.st_mode) and anchor.st_uid == uid
                and anchor.st_gid == (0 if uid == 0 else os.getgid())
                and stat.S_IMODE(anchor.st_mode) == 0o755, "unsafe_run_anchor")
        def require_current_anchor():
            current = os.stat(paths.at("/run"), follow_symlinks=False)
            require((current.st_dev, current.st_ino) == (anchor.st_dev, anchor.st_ino),
                    "run_anchor_replaced")
        try:
            child = os.open(runtime.name, os.O_RDONLY | os.O_DIRECTORY |
                            os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=runfd)
        except FileNotFoundError:
            # A single ENOENT is insufficient if another launcher creates
            # the runtime directory during inspection.
            try:
                os.stat(runtime.name, dir_fd=runfd, follow_symlinks=False)
            except FileNotFoundError:
                require_current_anchor()
                return True
            raise Blocked("runtime_directory_changed")
        try:
            info = os.fstat(child)
            entry = os.stat(runtime.name, dir_fd=runfd, follow_symlinks=False)
            require((info.st_dev, info.st_ino) == (entry.st_dev, entry.st_ino)
                    and info.st_dev == anchor.st_dev, "replaced_runtime_directory")
            require(stat.S_ISDIR(info.st_mode) and info.st_uid == uid
                    and info.st_gid == web_group_gid(uid)
                    and stat.S_IMODE(info.st_mode) == 0o710,
                    "unsafe_runtime_directory")
            try:
                os.stat(SOCKET_NAME, dir_fd=child, follow_symlinks=False)
            except FileNotFoundError:
                require_current_anchor()
                return True
            require_current_anchor()
            return False
        finally:
            os.close(child)
    finally:
        os.close(runfd)


def sync(fd):
    os.fsync(fd)


def create(path, data, uid):
    fd = parent_fd(path, uid)
    try:
        item = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                       os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=fd)
        try:
            os.fchmod(item, 0o600)
            with os.fdopen(item, "wb", closefd=False) as stream:
                stream.write(data)
                stream.flush()
            sync(item)
        finally:
            os.close(item)
        sync(fd)
    finally:
        os.close(fd)


def replace(path, data, previous, mode, uid):
    """Write fsynced sibling, verify old bytes, rename relative to pinned parent."""
    fd = parent_fd(path, uid)
    # A crash can leave an unlinked candidate sibling. Never delete or reuse
    # that forensic artifact; a later guarded rollback needs a fresh name.
    name = ".disabled-overlay-" + secrets.token_hex(12)
    try:
        require(read(path, uid, mode, 6500 if mode == 0o600 else 2*1024*1024)
                == previous, "concurrent_target_mutation")
        item = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                       os.O_NOFOLLOW | os.O_CLOEXEC, mode, dir_fd=fd)
        try:
            os.fchmod(item, mode)
            with os.fdopen(item, "wb", closefd=False) as stream:
                stream.write(data)
                stream.flush()
            sync(item)
        finally:
            os.close(item)
        sync(fd)
        require(read(path, uid, mode, 6500 if mode == 0o600 else 2*1024*1024)
                == previous, "concurrent_target_mutation")
        os.replace(name, path.name, src_dir_fd=fd, dst_dir_fd=fd)
        sync(fd)
    finally:
        os.close(fd)


def lock(path, uid):
    fd = parent_fd(path, uid)
    try:
        item = os.open(path.name, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
                       dir_fd=fd)
        try:
            info = os.fstat(item)
            check_file(info, uid, 0o600, 65536) if info.st_size else require(
                stat.S_ISREG(info.st_mode) and info.st_uid == uid
                and info.st_gid == (0 if uid == 0 else os.getgid())
                and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600,
                "unsafe_lock")
            fcntl.flock(item, fcntl.LOCK_EX | fcntl.LOCK_NB)
            after = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
            require((after.st_dev, after.st_ino) == (info.st_dev, info.st_ino),
                    "lock_replaced")
            return item
        except BaseException:
            os.close(item)
            raise
    finally:
        os.close(fd)


def observe_systemd():
    result = {}
    for name in (*TIMERS, *SERVICES, "obsidian-admin-operator.service"):
        # Debian 13 systemctl show does not expose PID properties on timers.
        # Oneshot services and the root Worker still REQUIRE both PID fields;
        # never replace missing service process evidence with an assumed zero.
        keys = ("LoadState", "ActiveState", "SubState", "Job",
                "FragmentPath", "UnitFileState")
        if name not in TIMERS:
            keys += ("MainPID", "ControlPID")
        response = subprocess.run(("/usr/bin/systemctl", "show", name,
                                   *("-p" + key for key in keys), "--no-pager"),
                                  capture_output=True, timeout=5, check=True)
        raw = response.stdout
        require(type(raw) is bytes and 0 < len(raw) <= 8192 and raw.endswith(b"\n"),
                "systemd_malformed")
        try:
            lines = raw.decode("ascii").splitlines()
        except UnicodeError as exc:
            raise Blocked("systemd_nonascii") from exc
        require(len(lines) == len(keys), "systemd_incomplete")
        require(all(line.count("=") == 1 and len(line) <= 256
                    and all(32 <= ord(character) <= 126 for character in line)
                    for line in lines), "systemd_malformed")
        fields = dict(line.split("=", 1) for line in lines)
        require(set(fields) == set(keys) and len(lines) == len(fields), "systemd_incomplete")
        result[name] = fields
    return result


def validate_systemd(snapshot):
    require(set(snapshot) == set((*TIMERS, *SERVICES, "obsidian-admin-operator.service")),
            "systemd_inventory")
    for name, state in snapshot.items():
        require(type(state) is dict and state.get("LoadState") == "loaded"
                and state.get("FragmentPath") == "/etc/systemd/system/" + name
                and state.get("Job") in ("", "0"), "domain_job_or_identity")
        if name.endswith(".timer"):
            require((state.get("UnitFileState"), state.get("ActiveState"),
                     state.get("SubState")) in (("enabled", "active", "waiting"),
                                                ("enabled", "inactive", "dead"),
                                                ("disabled", "inactive", "dead")),
                    "timer_state")
        elif name == "obsidian-admin-operator.service" or name.startswith(
                ("obsidian-github-", "obsidian-github.")):
            require(state.get("ActiveState") == "inactive" and
                    state.get("SubState") == "dead" and
                    state.get("MainPID") == state.get("ControlPID") == "0",
                    "worker_or_domain_active")
        else:
            # Not a global maintenance drain: these are unrelated AI/Core
            # services. Still reject a queued Job or false unit identity.
            require(state.get("ActiveState") in ("inactive", "failed") and
                    state.get("MainPID") == state.get("ControlPID") == "0" and
                    state.get("SubState") in ("dead", "failed"),
                    "unrelated_domain_unsettled")
    require(snapshot["obsidian-admin-operator.service"].get("UnitFileState") == "disabled",
            "worker_enabled")
    return {name: [snapshot[name]["UnitFileState"], snapshot[name]["ActiveState"],
                   snapshot[name]["SubState"]] for name in TIMERS}


class Stage:
    def __init__(self, paths=None, *, uid=0, observer=observe_systemd,
                 source=None, boundary=None, queue_gid=None, mount_reader=None):
        self.paths = paths or Paths()
        self.uid = uid
        self.observer = observer
        self.source = source  # synthetic-root tests only; production CLI never injects
        self.queue_gid = (queue_gid if queue_gid is not None else
                          grp.getgrnam("obsidian-admin-web").gr_gid if uid == 0 else
                          os.getgid())
        self.boundary = boundary or (lambda name: None)
        self.mount_reader = mount_reader or kernel_mountinfo
        at = self.paths.at
        self.module = at("/opt/obsidian-automation/venv/lib/python3.13/site-packages/obsidian_automation/" + CHANGED)
        self.release_dir = at("/var/lib/obsidian-admin/operator-releases/" +
                              REVISION + "/disabled-quiescence")
        self.source_path = self.release_dir / CHANGED
        self.authority = at("/etc/obsidian-admin/authority.json")
        self.package = self.module.parent
        self.units = at("/etc/systemd/system")
        self.queue = at("/var/lib/obsidian-admin/queue")
        self.stage = at("/var/lib/obsidian-admin/operator-stages/") / STAGE_NAME
        self.host_lock = at("/var/lib/obsidian-automation/deployments/host-update.lock")
        self.refresh_lock = at("/run/obsidian-automation/semantic-refresh-inhibited.lock")

    def mount_guard(self):
        # Fixed production targets only. Paths are mapped back from an injected
        # disposable test root; the production CLI always uses root '/'.
        fixed = (
            self.module, self.authority, self.source_path,
            self.release_dir / "release.json", self.release_dir,
            self.paths.at("/opt/obsidian-automation/app/.git"),
            self.stage, self.stage / "pending.json",
            self.stage / "before-module.bin", self.stage / "before-authority.bin",
            self.stage / "rollback.json", self.stage / "completed.json",
            self.stage / "rolled-back.json", self.host_lock, self.refresh_lock,
            self.host_lock.parent / "pending-runtime.json",
            self.refresh_lock.parent / "semantic-refresh-inhibited.json",
            self.queue / "active.json", self.queue / "admission.lock",
            self.paths.at("/run/obsidian-admin/operation.sock"),
            *(self.package / name for name in MODULES),
            *(self.units / name for name in UNITS),
            *(self.queue / name for name in (*OLD_FILES, *PAUSE_FILES)),
        )
        virtual = tuple(Path("/") / path.relative_to(self.paths.root)
                        for path in fixed)
        relevant = []
        for entry in mount_inventory(self.mount_reader()):
            point = Path(entry[4])
            if any(point == target or point in target.parents for target in virtual):
                require(point in (Path("/"), Path("/run")),
                        "unapproved_target_mount")
                relevant.append(entry)
        points = [entry[4] for entry in relevant]
        require(points.count("/") == 1 and points.count("/run") == 1,
                "ambiguous_target_mounts")
        run_entry = next(entry for entry in relevant if entry[4] == "/run")
        require(run_entry[3] == "/" and run_entry[5] == "tmpfs",
                "unexpected_run_mount")
        identity = tuple(sorted(relevant))
        if getattr(self, "_mount_identity", None) is None:
            self._mount_identity = identity
        else:
            require(identity == self._mount_identity, "target_mount_drift")

    def gate(self):
        locks = []
        try:
            for path in (self.host_lock, self.refresh_lock):
                locks.append(lock(path, self.uid))
            self._lock_ids = tuple((os.fstat(fd).st_dev, os.fstat(fd).st_ino)
                                   for fd in locks)
            self._mount_identity = None
            self.mount_guard()
            for path in (self.host_lock.parent / "pending-runtime.json",
                         self.refresh_lock.parent / "semantic-refresh-inhibited.json",
                         self.queue / "active.json"):
                require(not exists(path, self.uid), "pending_socket_or_active_queue")
            require(worker_socket_absent(self.paths, self.uid),
                    "pending_socket_or_active_queue")
            yield
        finally:
            self._lock_ids = ()
            self._mount_identity = None
            for fd in reversed(locks):
                os.close(fd)

    def queue_snapshot(self):
        directory(self.queue, self.uid, 0o700)
        names = sorted(os.listdir(self.queue))
        known = set((*OLD_FILES, *PAUSE_FILES, "admission.lock"))
        require(len(names) <= 16 and set((*OLD_FILES, *PAUSE_FILES)) <= set(names),
                "queue_proof_missing")
        require(not any(x.startswith(("request-", "processing-", "result-",
                    "manual-evidence-", "manual-reconcile-")) and x not in known
                    for x in names) and set(names) == known, "unsettled_or_unknown_queue")
        records = {name: read(self.queue / name, self.uid, 0o600, 32768,
                              gid=(0 if self.uid == 0 else os.getgid())
                              if name.startswith("manual-") else self.queue_gid)
                   for name in (*OLD_FILES, *PAUSE_FILES)}
        # admission.lock belongs to the intake identity, never read or mutate
        # it here; nevertheless its inode/mode/group is part of the snapshot.
        queue_fd = parent_fd(self.queue / "admission.lock", self.uid)
        try:
            lock_info = os.stat("admission.lock", dir_fd=queue_fd,
                                follow_symlinks=False)
        finally:
            os.close(queue_fd)
        require(stat.S_ISREG(lock_info.st_mode) and lock_info.st_uid == self.uid
                and lock_info.st_gid == self.queue_gid and lock_info.st_nlink == 1
                and stat.S_IMODE(lock_info.st_mode) == 0o600,
                "unsafe_queue_admission_lock")
        pause_request, pause_claim, pause_result = (
            strict_queue_json(records[x]) for x in PAUSE_FILES)
        require(self.valid_identity(pause_request, PAUSE_ID, "pause") and
                pause_claim == {"record_version": 1, "request_id": PAUSE_ID,
                                "service_id": "github-sync", "action": "pause",
                                "state": "processing"} and
                pause_result == {"record_version": 1, "request_id": PAUSE_ID,
                                 "service_id": "github-sync", "action": "pause",
                                 "state": "completed"}, "pause_queue_proof_invalid")
        request, processing, result, evidence, reconcile = (
            strict_queue_json(records[x]) for x in OLD_FILES)
        require(self.valid_identity(request, OLD_ID, "run_once")
                and processing == {"record_version": 1, "request_id": OLD_ID,
                                   "service_id": "github-sync", "action": "run_once",
                                   "state": "processing"}
                and result == {"record_version": 1, "request_id": OLD_ID,
                               "service_id": "github-sync", "action": "run_once",
                               "state": "indeterminate"}
                and set(evidence) == {
                    "record_version", "request_id", "service_id", "action", "state",
                    "decision", "approval_reference", "observed_at", "boot_id",
                    "operator_inactive", "authority_disabled", "socket_absent",
                    "role_services_idle", "timer_intents_restored", "business_outcome",
                    "request_sha256", "processing_sha256", "result_sha256", "active_sha256"}
                and set(reconcile) == {
                    "record_version", "request_id", "service_id", "action", "state",
                    "decision", "approval_reference", "approved_at", "boot_id",
                    "evidence_sha256", "request_sha256", "processing_sha256",
                    "result_sha256", "active_sha256"}
                and all(record.get("record_version") == 1 and
                        record.get("request_id") == OLD_ID and
                        record.get("service_id") == "github-sync" and
                        record.get("action") == "run_once" and
                        record.get("decision") == "acknowledge_unknown_no_replay"
                        for record in (evidence, reconcile))
                and evidence.get("state") == "root_verified_manual_evidence"
                and reconcile.get("state") == "manually_reconciled_unknown"
                and evidence.get("business_outcome") == "unknown_no_success_claim"
                and all(evidence.get(key) is True for key in (
                    "operator_inactive", "authority_disabled", "socket_absent",
                    "role_services_idle", "timer_intents_restored"))
                and evidence.get("active_sha256") == digest(records[OLD_FILES[0]])
                and reconcile.get("active_sha256") == digest(records[OLD_FILES[0]])
                and evidence.get("approval_reference") == reconcile.get("approval_reference")
                and evidence.get("boot_id") == reconcile.get("boot_id")
                and type(evidence.get("approval_reference")) is str
                and re.fullmatch(
                    r"https://github\.com/upiscium/ObsidianAutomation/issues/"
                    r"[1-9][0-9]*#issuecomment-[1-9][0-9]*",
                    evidence["approval_reference"]) is not None
                and type(evidence.get("boot_id")) is str
                and re.fullmatch(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}",
                                 evidence["boot_id"]) is not None
                and all(type(record.get(key)) is str and
                        len(record[key]) <= 35 and record[key].endswith("+00:00")
                        for record, key in ((evidence, "observed_at"),
                                            (reconcile, "approved_at")))
                and all(evidence.get(key) == digest(records[name]) and
                        reconcile.get(key) == digest(records[name])
                        for key, name in (("request_sha256", OLD_FILES[0]),
                                          ("processing_sha256", OLD_FILES[1]),
                                          ("result_sha256", OLD_FILES[2])))
                and reconcile.get("evidence_sha256") == digest(records[OLD_FILES[3]]),
                "old_queue_proof_invalid")
        return {**{name: digest(data) for name, data in records.items()},
                "admission.lock": [lock_info.st_dev, lock_info.st_ino,
                                   lock_info.st_uid, lock_info.st_gid,
                                   stat.S_IMODE(lock_info.st_mode)]}

    @staticmethod
    def valid_identity(record, rid, action):
        return (type(record) is dict and set(record) == {
            "record_version", "request_id", "service_id", "action", "state", "queued_at"}
            and type(record["record_version"]) is int and record["record_version"] == 1
            and record["request_id"] == rid and record["service_id"] == "github-sync"
            and record["action"] == action and record["state"] in ("accepted", "processing")
            and type(record["queued_at"]) is str and 0 < len(record["queued_at"]) <= 40)

    def inventory(self, expected_revision):
        directory(self.authority.parent, self.uid, 0o710)
        directory(self.stage.parent.parent, self.uid, 0o700)
        if exists(self.stage.parent, self.uid):
            directory(self.stage.parent, self.uid, 0o700)
        raw = read(self.authority, self.uid, 0o600, 6500)
        manifest = json.loads(raw)
        require(type(manifest) is dict and set(manifest) == {
            "version", "enabled", "revision", "web_user", "single_active_lxc",
            "unit_sha256", "module_sha256", "coordinated_updaters_verified",
            "uncoordinated_launchers_retired"} and manifest["version"] == 1
            and manifest["revision"] == expected_revision
            and manifest["web_user"] == "obsidian-admin-web"
            and manifest["single_active_lxc"] is True
            and all(manifest[key] is False for key in (
                "enabled", "coordinated_updaters_verified", "uncoordinated_launchers_retired"))
            and set(manifest["module_sha256"]) == MODULES
            and set(manifest["unit_sha256"]) == UNITS, "manifest_schema_or_authority")
        for name in MODULES:
            require(digest(read(self.package / name, self.uid, 0o644)) ==
                    manifest["module_sha256"][name], "module_inventory_drift")
        for name in UNITS:
            require(digest(read(self.units / name, self.uid, 0o644, 16384)) ==
                    manifest["unit_sha256"][name], "unit_inventory_drift")
        return raw, manifest

    def verified_release(self):
        if self.source is None:
            directory(self.release_dir.parent.parent, self.uid, 0o700)
            directory(self.release_dir.parent, self.uid, 0o700)
            directory(self.release_dir, self.uid, 0o700)
            require(set(os.listdir(self.release_dir)) == {CHANGED, "release.json"},
                    "unexpected_release_bundle")
            provenance = read(self.release_dir / "release.json", self.uid, 0o400, 2048)
            require(provenance == canonical(RELEASE) and
                    RELEASE["source_sha256"] == NEW_MODULE, "release_provenance_mismatch")
            directory(self.source_path.parent, self.uid)
            app = self.paths.at("/opt/obsidian-automation/app")
            directory(app, self.uid)
            directory(app / ".git", self.uid)
            # The intentionally stale global checkout is NOT the module source.
            # This is a drift check only, not a demand for a global update.
            proc = subprocess.run(("/usr/bin/git", "--no-optional-locks", "-C", str(app),
                                   "-c", "core.fsmonitor=false", "rev-parse", "HEAD"),
                                  env={"PATH": "/usr/bin:/bin", "HOME": "/root",
                                       "GIT_CONFIG_NOSYSTEM": "1",
                                       "GIT_CONFIG_GLOBAL": "/dev/null",
                                       "GIT_CONFIG_SYSTEM": "/dev/null",
                                       "GIT_OPTIONAL_LOCKS": "0"},
                                  capture_output=True, check=True, timeout=10)
            require(proc.stdout.strip() == GLOBAL_HEAD.encode(), "global_revision_mismatch")
        source = self.source if self.source is not None else read(self.source_path, self.uid, 0o400)
        require(digest(source) == NEW_MODULE and
                hashlib.sha1(b"blob " + str(len(source)).encode() + b"\0" + source).hexdigest()
                == GIT_BLOB, "release_source_pin_mismatch")
        return source

    def preflight(self):
        require(not exists(self.stage.parent, self.uid) or
                not exists(self.stage, self.uid), "stage_receipt_occupied")
        old, manifest = self.inventory(OLD_REVISION)
        require(digest(old) == OLD_AUTH and manifest["module_sha256"][CHANGED] == OLD_MODULE,
                "old_pin_mismatch")
        installed = read(self.module, self.uid, 0o644)
        require(digest(installed) == OLD_MODULE, "old_module_mismatch")
        source = self.verified_release()
        candidate = dict(manifest)
        candidate["module_sha256"] = dict(manifest["module_sha256"])
        candidate["module_sha256"][CHANGED] = NEW_MODULE
        candidate["revision"] = REVISION
        target = canonical(candidate)
        require(digest(target) == NEW_AUTH, "candidate_manifest_pin_mismatch")
        timers = validate_systemd(self.observer())
        queue = self.queue_snapshot()
        return old, installed, source, target, timers, queue

    def unchanged(self, timers, queue):
        self.mount_guard()
        for path, identity in zip((self.host_lock, self.refresh_lock), self._lock_ids):
            fd = parent_fd(path, self.uid)
            try:
                info = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
                require((info.st_dev, info.st_ino) == identity, "updater_lock_replaced")
            finally:
                os.close(fd)
        require(validate_systemd(self.observer()) == timers, "timer_drift")
        require(self.queue_snapshot() == queue, "queue_drift")
        for path in (self.host_lock.parent / "pending-runtime.json",
                     self.refresh_lock.parent / "semantic-refresh-inhibited.json",
                     self.queue / "active.json"):
            require(not exists(path, self.uid), "concurrent_runtime_effect")
        require(worker_socket_absent(self.paths, self.uid), "concurrent_runtime_effect")

    def mkdir_stage(self):
        if not exists(self.stage.parent, self.uid):
            parent = parent_fd(self.stage.parent, self.uid)
            try:
                os.mkdir(self.stage.parent.name, 0o700, dir_fd=parent)
                sync(parent)
            finally:
                os.close(parent)
        directory(self.stage.parent, self.uid, 0o700)
        fd = parent_fd(self.stage, self.uid)
        try:
            os.mkdir(self.stage.name, 0o700, dir_fd=fd)
            sync(fd)
        finally:
            os.close(fd)
        directory(self.stage, self.uid, 0o700)

    def record(self, name, value):
        create(self.stage / name, canonical(value) if isinstance(value, dict) else value,
               self.uid)

    def prepared(self):
        directory(self.stage, self.uid, 0o700)
        pending = json.loads(read(self.stage / "pending.json", self.uid, 0o600, 32768))
        require(pending.get("version") == 1 and pending.get("revision") == REVISION
                and pending.get("old_auth") == OLD_AUTH
                and pending.get("new_auth") == NEW_AUTH
                and pending.get("old_module") == OLD_MODULE
                and pending.get("new_module") == NEW_MODULE, "untrusted_pending")
        old = read(self.stage / "before-authority.bin", self.uid, 0o600, 6500)
        module = read(self.stage / "before-module.bin", self.uid, 0o600)
        require(digest(old) == OLD_AUTH and digest(module) == OLD_MODULE,
                "corrupt_rollback_receipt")
        require(json.loads(read(self.stage / "rollback.json", self.uid, 0o600, 32768)) ==
                {"authority_sha256": OLD_AUTH, "module_sha256": OLD_MODULE,
                 "revision": OLD_REVISION}, "corrupt_rollback_receipt")
        require(set(pending["timers"]) == set(TIMERS) and
                set(pending["queue"]) == set((*OLD_FILES, *PAUSE_FILES, "admission.lock")),
                "untrusted_snapshot")
        return pending, old, module

    def verify(self, pending, revision, auth_digest, module_digest):
        self.unchanged(pending["timers"], pending["queue"])
        raw, _ = self.inventory(revision)
        require(digest(raw) == auth_digest and
                digest(read(self.module, self.uid, 0o644)) == module_digest,
                "readback_mismatch")

    def run(self, action):
        from contextlib import contextmanager
        with contextmanager(self.gate)():
            if action == "inspect":
                state = self.preflight()
                return {"status": "PRECHECK_PASS", "revision": REVISION,
                        "authority_sha256": digest(state[0]),
                        "source_sha256": digest(state[2]), "enabled": False}
            if action == "stage":
                old, module, source, target, timers, queue = self.preflight()
                self.unchanged(timers, queue)
                require(self.inventory(OLD_REVISION)[0] == old and
                        read(self.module, self.uid, 0o644) == module and
                        self.verified_release() == source,
                        "preinstall_drift")
                self.mkdir_stage()
                self.boundary("stage_dir")
                self.record("before-authority.bin", old)
                self.boundary("backup_authority")
                self.record("before-module.bin", module)
                self.boundary("backup_module")
                self.record("rollback.json", {"revision": OLD_REVISION,
                    "authority_sha256": OLD_AUTH, "module_sha256": OLD_MODULE})
                self.boundary("rollback_receipt")
                pending = {"version": 1, "revision": REVISION,
                    "old_auth": OLD_AUTH, "new_auth": NEW_AUTH,
                    "old_module": OLD_MODULE, "new_module": NEW_MODULE,
                    "timers": timers, "queue": queue}
                self.record("pending.json", pending)
                self.boundary("prepared")
                self.unchanged(timers, queue)
                require(self.verified_release() == source, "release_drift")
                replace(self.module, source, module, 0o644, self.uid)
                self.boundary("module")
                self.unchanged(timers, queue)
                require(read(self.authority, self.uid, 0o600, 6500) == old,
                        "authority_drift")
                replace(self.authority, target, old, 0o600, self.uid)
                self.boundary("authority")
                self.verify(pending, REVISION, NEW_AUTH, NEW_MODULE)
                self.boundary("readback")
                self.record("completed.json", {"status": "STAGED_DISABLED", "revision": REVISION,
                                                "authority_sha256": NEW_AUTH,
                                                "module_sha256": NEW_MODULE})
                return {"status": "STAGED_DISABLED", "revision": REVISION}
            pending, old, module = self.prepared()
            require(action in ("finalize", "rollback"), "invalid_action")
            completed = exists(self.stage / "completed.json", self.uid)
            rolled = exists(self.stage / "rolled-back.json", self.uid)
            require(not (completed and rolled), "conflicting_receipts")
            if completed:
                require(read(self.stage / "completed.json", self.uid, 0o600, 32768) ==
                        canonical({"status": "STAGED_DISABLED", "revision": REVISION,
                                   "authority_sha256": NEW_AUTH,
                                   "module_sha256": NEW_MODULE}), "untrusted_completed_receipt")
            if rolled:
                require(read(self.stage / "rolled-back.json", self.uid, 0o600, 32768) ==
                        canonical({"status": "RESTORED_DISABLED",
                                   "revision": OLD_REVISION}), "untrusted_rollback_receipt")
            current_module = read(self.module, self.uid, 0o644)
            current_auth = read(self.authority, self.uid, 0o600, 6500)
            require(digest(current_module) in (OLD_MODULE, NEW_MODULE) and
                    digest(current_auth) in (OLD_AUTH, NEW_AUTH), "unknown_partial_state")
            require(not (completed and action == "rollback"), "completed_requires_separate_review")
            require(not (rolled and action == "finalize"), "rolled_back")
            self.unchanged(pending["timers"], pending["queue"])
            if action == "finalize":
                self.verify(pending, REVISION, NEW_AUTH, NEW_MODULE)
                if not completed:
                    self.record("completed.json", {"status": "STAGED_DISABLED", "revision": REVISION,
                                                    "authority_sha256": NEW_AUTH,
                                                    "module_sha256": NEW_MODULE})
                return {"status": "STAGED_DISABLED", "revision": REVISION}
            # Never restore over unknown bytes; partial recovery is resumable.
            if digest(current_auth) == NEW_AUTH:
                self.mount_guard()
                replace(self.authority, old, current_auth, 0o600, self.uid)
                self.boundary("rollback_authority")
                self.unchanged(pending["timers"], pending["queue"])
            if digest(current_module) == NEW_MODULE:
                self.mount_guard()
                replace(self.module, module, current_module, 0o644, self.uid)
                self.boundary("rollback_module")
            self.verify(pending, OLD_REVISION, OLD_AUTH, OLD_MODULE)
            if not rolled:
                self.record("rolled-back.json", {"status": "RESTORED_DISABLED",
                                                  "revision": OLD_REVISION})
            return {"status": "RESTORED_DISABLED", "revision": OLD_REVISION}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument("--stage-disabled", action="store_true")
    choice.add_argument("--finalize", action="store_true")
    choice.add_argument("--rollback", action="store_true")
    parser.add_argument("--authorization", choices=("OA403-DS1-disabled-overlay",))
    args = parser.parse_args(argv)
    action = ("stage" if args.stage_disabled else "finalize" if args.finalize else
              "rollback" if args.rollback else "inspect")
    try:
        require(os.geteuid() == 0, "root_required")
        require(sys.flags.isolated == sys.flags.no_site == sys.flags.dont_write_bytecode == 1
                and sys.pycache_prefix == "/dev/null/obsidian-admin-source-only",
                "isolated_source_only_python_required")
        require(action == "inspect" or args.authorization == "OA403-DS1-disabled-overlay",
                "separate_human_authorization_required")
        print(json.dumps(Stage().run(action), sort_keys=True))
        return 0
    except (Blocked, OSError, ValueError, KeyError, TypeError,
            subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc)[:120]}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
