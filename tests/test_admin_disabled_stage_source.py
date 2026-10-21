"""Static and pure-data gates for the root-owned #403 disabled-only stager.

These tests do not touch live services or create a privileged production stage.
The actual host --inspect and read-only evidence are separately required.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/"tools/accept_admin_operator_disabled_stage.py"


def load():
    spec=importlib.util.spec_from_file_location("_oa403_stage_test",SCRIPT)
    assert spec and spec.loader
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_source_and_destination_contract_is_narrow_and_disabled():
    x=load()
    assert x.TARGET=="977f29bf7a7f69172d894f6cf98785a8b4258f2c"
    assert x.OLD=="2b860f2fdec954ed363f6b23ed1df5f1597bbaf3"
    assert set(x.BLOBS)=={"launcher","unit","runtime","stager"}
    assert x.BLOBS["stager"][1] is None
    assert {str(v[1]) for v in x.BLOBS.values() if v[1] is not None}=={
        "/usr/local/libexec/obsidian-admin-root-launch.py",
        "/etc/systemd/system/obsidian-admin-operator.service",
        "/opt/obsidian-automation/venv/lib/python3.13/site-packages/"
        "obsidian_automation/admin_operator_runtime.py",
    }
    assert x.AUTHORITY==Path("/etc/obsidian-admin/authority.json")
    assert x.SOURCE_PREFIX=="/dev/null/obsidian-admin-source-only"
    assert len(x.TIMERS)==5


def test_production_stage_has_fixed_root_owned_recovery_and_lock_contract():
    x=load()
    assert x.HOST_LOCK==Path("/var/lib/obsidian-automation/deployments/host-update.lock")
    assert x.REFRESH_LOCK==Path("/run/obsidian-automation/semantic-refresh-inhibited.lock")
    assert x.STAGE_PARENT==Path("/var/lib/obsidian-admin/operator-stages")
    source=SCRIPT.read_text()
    assert "fcntl.LOCK_EX | fcntl.LOCK_NB" in source
    assert "O_NOFOLLOW" in source
    assert "sys.flags.isolated" in source
    assert "pycache_prefix" in source
    assert 'options.add_argument("--inspect"' in source
    assert 'options.add_argument("--apply-disabled"' in source
    assert 'options.add_argument("--recover-dir"' in source
    assert '"coordinated_updaters_verified"' in source
    assert '"uncoordinated_launchers_retired"' in source
    assert 'helper.stage(TARGET)' in source
    assert '"enabled","coordinated_updaters_verified"' in source


def test_stager_never_invokes_global_provisioning_or_timer_effects():
    source=SCRIPT.read_text()
    assert "provision_automation_authority.py" not in source
    for forbidden in (
        '"enable",', '"disable",', '"start",', '"stop",',
        '"restart",', '"unmask",',
    ):
        assert forbidden not in source
    assert '"/usr/bin/systemctl","daemon-reload"' in source


def test_disallowed_git_operation_fails_before_any_git_execution(monkeypatch):
    x=load()
    # Fakes only preflight filesystem checks: command guard must prevent
    # any unsafe subprocess invocation, before inspecting arbitrary Git data.
    monkeypatch.setattr(x,"parent_safe",lambda _:None)
    monkeypatch.setattr(x,"root_file",lambda *a,**kw:b"trusted-config")
    original = Path.lstat

    def root_directory_fixture(path,*args,**kwargs):
        if path in (x.APP,x.APP/".git"):
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o755,st_uid=0)
        return original(path,*args,**kwargs)

    monkeypatch.setattr(Path,"lstat",root_directory_fixture)
    with pytest.raises(x.StageBlocked,match="disallowed_git_operation"):
        x.qualified_git("status")


def test_checksum_helper_is_deterministic():
    x=load()
    assert x.sha(b"")==("e3b0c44298fc1c149afbf4c8996fb924"
                         "27ae41e4649b934ca495991b7852b855")
