"""Synthetic local-updater exclusion; no live LXC/systemd effects."""
import fcntl
import os
from pathlib import Path

import pytest

from obsidian_automation.admin_operation_broker import BrokerRejected
from obsidian_automation.admin_local_maintenance_gate import (
    HOST_LOCK, REFRESH_LOCK, HOST_PENDING, REFRESH_PENDING,
    cooperating_updater_gate,
)


@pytest.fixture
def roots(tmp_path):
    host = tmp_path / "host"
    refresh = tmp_path / "refresh"
    host.mkdir(mode=0o750)
    refresh.mkdir(mode=0o755)
    for folder, name in ((host, HOST_LOCK), (refresh, REFRESH_LOCK)):
        file = folder / name
        file.touch(mode=0o600)
        file.chmod(0o600)
    return host, refresh


def gate(roots):
    return cooperating_updater_gate(
        host_dir=roots[0], refresh_dir=roots[1], require_root=False,
    )


def test_acquires_both_existing_locks_and_releases(roots):
    with gate(roots):
        for folder, name in ((roots[0], HOST_LOCK), (roots[1], REFRESH_LOCK)):
            fd = os.open(folder / name, os.O_RDWR)
            try:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)
    with gate(roots):
        pass


@pytest.mark.parametrize("which", [0, 1])
def test_busy_updater_blocks_gate(roots, which):
    folder, name = ((roots[0], HOST_LOCK), (roots[1], REFRESH_LOCK))[which]
    fd = os.open(folder / name, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BrokerRejected, match="local_update_busy"):
            with gate(roots):
                pass
    finally:
        os.close(fd)


@pytest.mark.parametrize("which", [0, 1])
def test_missing_lock_fails_closed_without_creating_file(roots, which):
    folder, name = ((roots[0], HOST_LOCK), (roots[1], REFRESH_LOCK))[which]
    (folder / name).unlink()
    with pytest.raises(BrokerRejected, match="local_lock_unavailable"):
        with gate(roots):
            pass
    assert not (folder / name).exists()


@pytest.mark.parametrize("which", [0, 1])
def test_pending_intent_fails_closed(roots, which):
    folder, name = ((roots[0], HOST_PENDING), (roots[1], REFRESH_PENDING))[which]
    (folder / name).write_text("unverified")
    with pytest.raises(BrokerRejected, match="local_update_pending"):
        with gate(roots):
            pass


def test_symlinked_pending_marker_is_not_treated_absent(roots):
    (roots[0] / HOST_PENDING).symlink_to("missing")
    with pytest.raises(BrokerRejected, match="local_update_pending"):
        with gate(roots):
            pass


def test_unsafe_lock_mode_and_hardlink_are_rejected(roots):
    lock = roots[0] / HOST_LOCK
    lock.chmod(0o644)
    with pytest.raises(BrokerRejected, match="unsafe_local_lock"):
        with gate(roots):
            pass
    lock.chmod(0o600)
    os.link(lock, roots[0] / "another")
    with pytest.raises(BrokerRejected, match="unsafe_local_lock"):
        with gate(roots):
            pass


def test_symlink_lock_rejected(roots):
    lock = roots[0] / HOST_LOCK
    lock.unlink()
    lock.symlink_to(roots[1] / REFRESH_LOCK)
    with pytest.raises(BrokerRejected, match="local_lock_unavailable"):
        with gate(roots):
            pass


def test_unsafe_directory_permissions_rejected(roots):
    roots[0].chmod(0o770)
    with pytest.raises(BrokerRejected, match="unsafe_local_lock_directory"):
        with gate(roots):
            pass


def test_root_required_by_default(monkeypatch, roots):
    monkeypatch.setattr(os, "geteuid", lambda: 1001)
    with pytest.raises(BrokerRejected, match="privileged_broker_required"):
        with cooperating_updater_gate(
            host_dir=roots[0], refresh_dir=roots[1],
        ):
            pass


def test_nested_gate_is_nonblocking(roots):
    with gate(roots):
        with pytest.raises(BrokerRejected, match="local_update_busy"):
            with gate(roots):
                pass


def test_gate_does_not_grant_effect_authority(roots):
    from obsidian_automation.admin_operation_broker import OperationBroker
    broker = OperationBroker(
        audit_root=roots[0], adapter=object(),
        maintenance_gate=lambda: gate(roots),
        clock=lambda: None, require_root=False,
    )
    assert broker.effects_enabled is False
