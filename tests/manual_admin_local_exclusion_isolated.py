"""Real local flock and updater-marker arbitration in private disposable dirs.

Tests actual kernel advisory locks shared by the #300 operator and cooperating
host/Github/pre-review updater entrypoints. No real /var/lib, /run or systemd
production units are modified. No root access required.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile

from obsidian_automation.admin_local_maintenance_gate import (
    HOST_LOCK, HOST_PENDING, REFRESH_LOCK, REFRESH_PENDING,
    cooperating_updater_gate, single_lxc_host_updater_lock,
)
from obsidian_automation.admin_operation_broker import BrokerRejected
from obsidian_automation.pre_review_production_update import RefreshUpdateInhibit


def expect_refused(call, *allowed_codes: str) -> str:
    try:
        call()
    except BrokerRejected as exc:
        if exc.code not in allowed_codes:
            raise RuntimeError("unexpected_lock_reason") from None
        return exc.code
    raise RuntimeError("unsafe_lock_was_admitted")


def enter_gate(host: Path, refresh: Path) -> None:
    with cooperating_updater_gate(
        host_dir=host, refresh_dir=refresh, require_root=False,
    ):
        pass


def lock_child_status(path: Path) -> str:
    probe = (
        "import os,sys,fcntl;"
        "fd=os.open(sys.argv[1],os.O_RDWR|os.O_NOFOLLOW);"
        "status='locked';"
        "\ntry:\n"
        " fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)\n"
        "except BlockingIOError:\n"
        " status='busy'\n"
        "os.close(fd);print(status)"
    )
    run = subprocess.run(
        [sys.executable, "-c", probe, str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, check=False, timeout=5,
    )
    if run.returncode != 0 or run.stdout.strip() not in {"busy", "locked"}:
        raise RuntimeError("kernel_lock_probe_failed")
    return run.stdout.strip()


def main() -> int:
    if os.geteuid() == 0:
        raise RuntimeError("disposable_test_requires_nonroot")
    with tempfile.TemporaryDirectory(prefix="oa300-local-exclusion-",dir="/tmp") as folder:
        root = Path(folder)
        root.chmod(0o700)
        host = root / "deployments"
        host.mkdir(mode=0o750)
        refresh = root / "runtime"
        refresh.mkdir(mode=0o755)
        host_lock = host / HOST_LOCK
        refresh_lock = refresh / REFRESH_LOCK
        for target in (host_lock, refresh_lock):
            target.touch(mode=0o600)
            target.chmod(0o600)

        # Verified local gate acquisition owns the ACTUAL two lock inodes
        # over the entire critical section, including live systemd observation.
        with cooperating_updater_gate(
            host_dir=host, refresh_dir=refresh, require_root=False,
        ):
            if lock_child_status(host_lock) != "busy":
                raise RuntimeError("host_lock_not_held")
            if lock_child_status(refresh_lock) != "busy":
                raise RuntimeError("refresh_lock_not_held")
            code = expect_refused(
                lambda: enter_gate(host,refresh),"local_update_busy",
            )
        if (lock_child_status(host_lock),lock_child_status(refresh_lock)) != (
            "locked","locked",
        ):
            raise RuntimeError("locks_not_released")

        # Representative host-update path holds the FIRST shared lock.
        with single_lxc_host_updater_lock(host_dir=host,require_root=False):
            expect_refused(lambda: enter_gate(host,refresh),"local_update_busy")
        # A pre-review/semantic updater holds the SECOND lock and publishes
        # its inhibitor: both in-flight and stale recovered marker exclude us.
        inhibit = RefreshUpdateInhibit(
            refresh / REFRESH_PENDING, "a"*40, "pre-review",
            require_root=False,
        )
        inhibit.acquire()
        try:
            expect_refused(lambda: enter_gate(host,refresh),"local_update_busy")
        finally:
            inhibit.close()
        expect_refused(lambda: enter_gate(host,refresh),"local_update_pending")
        # Clearing this fixture marker is safe only in this disposable test:
        # production markers require independently reviewed reconciliation.
        cleanup = RefreshUpdateInhibit(
            refresh / REFRESH_PENDING,"a"*40,"pre-review",require_root=False,
        )
        cleanup.acquire(publish=False)
        try:
            cleanup.release()
        finally:
            cleanup.close()
        if (refresh / REFRESH_PENDING).exists():
            raise RuntimeError("disposable_inhibit_cleanup_incomplete")

        (host/HOST_PENDING).write_text("DISPOSABLE",encoding="ascii")
        expect_refused(lambda: enter_gate(host,refresh),"local_update_pending")
        (host/HOST_PENDING).unlink()
        enter_gate(host,refresh)

        # A symlink to the expected lock path must never pass the identity
        # check and must not follow the external file.
        host_lock.unlink()
        host_lock.symlink_to(refresh_lock)
        expect_refused(lambda: enter_gate(host,refresh),"local_lock_unavailable")
        host_lock.unlink()
        host_lock.touch(mode=0o600)
        host_lock.chmod(0o600)
        enter_gate(host,refresh)

        print("ISOLATED_SAME_LXC_KERNEL_LOCKS_PASS")
        print("TWO_LOCK_INODES_HELD_AND_RELEASED=true")
        print("HOST_UPDATE_CONTENDS_WITH_BROKER=true")
        print("SEMANTIC_UPDATER_CONTENDS_WITH_BROKER=true")
        print("PENDING_MARKERS_FAIL_CLOSED=true")
        print("UNSAFE_SYMLINK_REJECTED=true")
        print("PRODUCTION_LOCK_PATHS_TOUCHED=false")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
