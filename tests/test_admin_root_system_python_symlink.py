"""Debian/Linux root-owned /usr/bin/python3 symlink trust regression.

CI executes this on Ubuntu, where /usr/bin/python3 is a root-owned symlink
with lstat mode 0777. A symlink's mode bits do not grant file write rights.
The trusted parent and fully resolved binary must remain protected.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import stat
import sys
from types import SimpleNamespace

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "examples/admin/obsidian-admin-root-launch.py"
SYSTEM_PYTHON = Path("/usr/bin/python3")


@pytest.fixture
def synthetic_approved_root_bootstrap(tmp_path, monkeypatch):
    if not SYSTEM_PYTHON.is_symlink():
        pytest.skip("OS-specific root /usr/bin/python3 symlink acceptance runs in Linux CI")
    spec = importlib.util.spec_from_file_location("_admin_python_link_unit", SOURCE)
    assert spec is not None and spec.loader is not None
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)

    root = tmp_path / "reviewed"
    package = root / "venv" / "lib" / (
        f"python{sys.version_info.major}.{sys.version_info.minor}"
    ) / "site-packages" / "obsidian_automation"
    package.mkdir(parents=True)
    unit_root = root / "units"
    authority_path = root / "authority.json"

    sources = {
        **{unit_root/name: ("unit:"+name).encode("ascii") for name in launcher.UNITS},
        **{package/name: ("module:"+name).encode("ascii") for name in launcher.MODULES},
    }
    document = {
        "version": 1,
        "enabled": True,
        "revision": "a" * 40,
        "web_user": "obsidian-admin-web",
        "single_active_lxc": True,
        "coordinated_updaters_verified": True,
        "uncoordinated_launchers_retired": True,
        "unit_sha256": {
            name: hashlib.sha256(sources[unit_root/name]).hexdigest()
            for name in launcher.UNITS
        },
        "module_sha256": {
            name: hashlib.sha256(sources[package/name]).hexdigest()
            for name in launcher.MODULES
        },
    }
    sources[authority_path] = json.dumps(document).encode("utf-8")
    with monkeypatch.context() as m:
        m.setattr(launcher, "VENV", root/"venv")
        m.setattr(launcher, "SYSTEMD", unit_root)
        m.setattr(launcher, "AUTHORITY", authority_path)
        m.setattr(launcher, "rooted_file", lambda p,bound: sources[p])
        # Do not patch process-global os or sys; only this vetted module.
        m.setattr(launcher, "os", SimpleNamespace(geteuid=lambda:0))
        m.setattr(launcher, "sys", SimpleNamespace(
            flags=SimpleNamespace(isolated=1,no_site=1,dont_write_bytecode=1),
            version_info=sys.version_info,
            pycache_prefix="/dev/null/obsidian-admin-source-only",
        ))
        yield launcher, package


def test_root_owned_debian_symlink_0777_is_valid_with_pinned_regular_binary(
    synthetic_approved_root_bootstrap,
):
    launcher, package = synthetic_approved_root_bootstrap
    assert SYSTEM_PYTHON.lstat().st_uid == 0
    assert stat.S_ISLNK(SYSTEM_PYTHON.lstat().st_mode)
    assert stat.S_IMODE(SYSTEM_PYTHON.lstat().st_mode) == 0o777
    target = SYSTEM_PYTHON.resolve(strict=True)
    assert target.is_relative_to(Path("/usr/bin"))
    assert target.lstat().st_uid == 0
    assert not (stat.S_IMODE(target.lstat().st_mode) & 0o022)
    assert launcher.preflight() == package.parent


def test_symlink_owner_is_still_required(
    synthetic_approved_root_bootstrap,monkeypatch,
):
    launcher,_ = synthetic_approved_root_bootstrap
    original = Path.lstat
    def fake_lstat(path,*args,**kwargs):
        value = original(path,*args,**kwargs)
        if path == SYSTEM_PYTHON:
            return SimpleNamespace(st_uid=1001,st_mode=value.st_mode)
        return value
    with monkeypatch.context() as m:
        m.setattr(Path,"lstat",fake_lstat)
        with pytest.raises(RuntimeError,match="system_python_not_qualified"):
            launcher.preflight()


def test_resolved_system_binary_must_remain_non_writable(
    synthetic_approved_root_bootstrap, monkeypatch,
):
    launcher,_ = synthetic_approved_root_bootstrap
    binary = SYSTEM_PYTHON.resolve(strict=True)
    original = Path.lstat
    def fake_lstat(path,*args,**kwargs):
        value = original(path,*args,**kwargs)
        if path == binary:
            return SimpleNamespace(st_uid=value.st_uid,st_mode=value.st_mode | 0o022)
        return value
    with monkeypatch.context() as m:
        m.setattr(Path,"lstat",fake_lstat)
        with pytest.raises(RuntimeError,match="system_python_not_qualified"):
            launcher.preflight()


def test_resolved_system_binary_outside_usr_bin_is_rejected(
    synthetic_approved_root_bootstrap,monkeypatch,
):
    launcher,_ = synthetic_approved_root_bootstrap
    original = Path.resolve
    def fake_resolve(path,*args,**kwargs):
        if path == SYSTEM_PYTHON:
            return Path("/tmp/untrusted-binary")
        return original(path,*args,**kwargs)
    with monkeypatch.context() as m:
        m.setattr(Path,"resolve",fake_resolve)
        with pytest.raises(RuntimeError,match="system_python_not_qualified"):
            launcher.preflight()


def test_nested_resolved_binary_is_not_a_supported_root_interpreter(
    synthetic_approved_root_bootstrap, monkeypatch,
):
    launcher, _ = synthetic_approved_root_bootstrap
    original = Path.resolve

    def fake_resolve(path, *args, **kwargs):
        if path == SYSTEM_PYTHON:
            return Path("/usr/bin/alternative/python3.13")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(Path, "resolve", fake_resolve)
        with pytest.raises(RuntimeError, match="system_python_not_qualified"):
            launcher.preflight()
