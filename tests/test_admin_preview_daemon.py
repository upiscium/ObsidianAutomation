"""No-effect local preview worker under disposable current-UID Unix socket."""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import socket
import tempfile
import threading
import time

import pytest

from obsidian_automation.admin_broker_socket import BrokerSocketClient
from obsidian_automation.admin_operation_preview import preview_from_socket
from obsidian_automation.admin_preview_daemon import serve_preview, main

RID = "12345678-1234-4234-8234-123456789abc"


@pytest.fixture
def path():
    with tempfile.TemporaryDirectory(prefix="oad-", dir="/tmp") as folder:
        os.chmod(folder, 0o700)
        yield Path(folder) / "preview.sock"


def obs():
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "services": [{
            "id": "github-sync", "schedule": "scheduled",
            "latest_execution": "succeeded", "health": "observed_success",
        }],
    }


def test_disposable_local_socket_server_starts_and_gracefully_closes(path):
    stop = threading.Event()
    errors = []
    def run():
        try:
            serve_preview(
                path=path, allowed_peer_uid=os.geteuid(),
                allowed_peer_gid=os.getegid(),
                stop=stop, observation_provider=obs,
                require_root=False,
            )
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=run)
    thread.start()
    try:
        deadline = time.monotonic()+2
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert path.is_socket(), errors
        cap = BrokerSocketClient(path).query({
            "version": 1, "op": "capabilities",
        })
        assert cap["authority"] == "observation_only"
        assert cap["operations"] == {
            "pause": False, "resume": False, "run_once": False,
        }
        result = preview_from_socket({
            "version": 1, "op": "preview", "request_id": RID,
            "service_id": "github-sync", "action": "pause",
        }, socket_path=path)
        assert result["status"] == "compatible"
        assert result["can_execute"] is False
        assert result["authority"] == "preview_only"
    finally:
        stop.set()
        thread.join(timeout=3)
    assert not thread.is_alive()
    assert not errors
    assert not path.exists()
    assert (path.parent / "preview.sock.lock").is_file()


@pytest.mark.parametrize("bad_uid,bad_gid", [(0, 1234), (1234, 0)])
def test_root_peers_and_root_web_groups_are_rejected(path, bad_uid, bad_gid):
    with pytest.raises(ValueError, match="nonroot_web_service_identity_required"):
        serve_preview(
            path=path, allowed_peer_uid=bad_uid, allowed_peer_gid=bad_gid,
            stop=threading.Event(), require_root=False,
        )
    assert not path.exists()


def test_daemon_main_requires_privileged_os_identity(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1001)
    with pytest.raises(SystemExit, match="privileged_preview_daemon_required"):
        main([])


def test_actual_root_daemon_does_not_offer_an_effect_mode():
    import inspect
    from obsidian_automation import admin_preview_daemon
    source = inspect.getsource(admin_preview_daemon)
    assert "import OperationBroker" not in source
    assert "effects_enabled=True" not in source
    assert "--enable-effects" not in source
    assert "run_once" not in source.replace('"""', "") or "Run once" not in source


def test_preview_daemon_bounded_source_uses_fixed_systemd_property_inventory(monkeypatch):
    from obsidian_automation.admin_status import SERVICES, TIMER_FIELDS, SERVICE_FIELDS
    from obsidian_automation import admin_preview_daemon
    seen = []
    class Adapter:
        def _read_properties(self, unit, fields):
            seen.append((unit, fields))
            assert unit in {name for spec in SERVICES for name in (spec.timer, spec.service)}
            if unit.endswith(".timer"):
                assert fields == TIMER_FIELDS
                return {
                    "LoadState": "loaded", "UnitFileState": "disabled",
                    "ActiveState": "inactive", "SubState": "dead",
                }
            assert fields == SERVICE_FIELDS
            return {
                "LoadState": "loaded", "ActiveState": "inactive", "SubState": "dead",
                "Type": "oneshot", "Result": "success", "ExecMainCode": "0",
                "ExecMainStatus": "0", "ExecMainStartTimestampMonotonic": "0",
                "ExecMainExitTimestampMonotonic": "0",
            }
    monkeypatch.setattr(admin_preview_daemon, "SystemctlAdapter", Adapter)
    value = admin_preview_daemon._production_observation()
    assert len(seen) == 10  # exactly five fixed timer + oneshot pairs
    assert {s["id"] for s in value["services"]} == {s.identifier for s in SERVICES}
    assert all(s["schedule"] == "paused" for s in value["services"])
    assert all(s["health"] == "paused" for s in value["services"])


def test_unavailable_one_systemd_property_returns_explicit_unknown(monkeypatch):
    from obsidian_automation.admin_status import SERVICES, TIMER_FIELDS, SERVICE_FIELDS
    from obsidian_automation import admin_preview_daemon
    from obsidian_automation.admin_operation_broker import BrokerRejected
    class Adapter:
        def _read_properties(self, unit, fields):
            if unit == SERVICES[1].service:
                raise BrokerRejected("synthetic_unavailable")
            if unit.endswith(".timer"):
                return {
                    "LoadState": "loaded", "UnitFileState": "enabled",
                    "ActiveState": "active", "SubState": "waiting",
                }
            return {
                "LoadState": "loaded", "ActiveState": "inactive", "SubState": "dead",
                "Type": "oneshot", "Result": "success", "ExecMainCode": "0",
                "ExecMainStatus": "0", "ExecMainStartTimestampMonotonic": "0",
                "ExecMainExitTimestampMonotonic": "0",
            }
    monkeypatch.setattr(admin_preview_daemon, "SystemctlAdapter", Adapter)
    value = admin_preview_daemon._production_observation()
    match = next(s for s in value["services"] if s["id"] == SERVICES[1].identifier)
    assert match["latest_execution"] == "unknown"
    assert match["health"] == "unknown"
