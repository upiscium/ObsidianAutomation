"""#403: Path factory creates concrete PosixPath; root-only file reader regression."""
from __future__ import annotations

import os
from pathlib import Path
import stat
from types import SimpleNamespace
import pytest

from obsidian_automation import admin_operator_runtime as runtime
from obsidian_automation.admin_operation_broker import BrokerRejected


def test_native_path_type_is_not_factory_type():
    target=Path("/etc/obsidian-admin/authority.json")
    assert type(target) is type(Path("/"))
    assert type(target) is not Path


def pinned_fixture_reader(monkeypatch):
    actual=os.fstat

    def root_owned_fd(fd):
        original=actual(fd)
        if stat.S_ISDIR(original.st_mode):
            mode=stat.S_IFDIR | 0o755
        else:
            mode=stat.S_IFREG | 0o600
        return SimpleNamespace(
            st_mode=mode,
            st_uid=0,
            st_nlink=original.st_nlink,
            st_size=original.st_size,
        )

    monkeypatch.setattr(os,"fstat",root_owned_fd)


def test_native_path_opens_exact_regular_root_owned_fixture(tmp_path,monkeypatch):
    p=tmp_path/"authority.json"
    expected=b'{"enabled":false,"revision":"reviewed-test-only"}\n'
    p.write_bytes(expected)
    pinned_fixture_reader(monkeypatch)
    assert runtime._pinned_root_regular(p,max_bytes=6500)==expected


def test_native_path_does_not_follow_symlink_to_unreviewed_data(
    tmp_path,monkeypatch,
):
    source=tmp_path/"source.json"
    source.write_bytes(b'{"sensitive":"unreviewed"}\n')
    link=tmp_path/"authority.json"
    link.symlink_to(source)
    pinned_fixture_reader(monkeypatch)
    with pytest.raises(BrokerRejected,match="operator_authority_unavailable"):
        runtime._pinned_root_regular(link,max_bytes=6500)


@pytest.mark.parametrize("value", [
    "/etc/obsidian-admin/authority.json", None, 0, object(),
])
def test_native_reader_still_rejects_non_path_values(value):
    with pytest.raises(BrokerRejected,match="operator_authority_unavailable"):
        runtime._pinned_root_regular(value,max_bytes=6500)


def test_reader_does_not_accept_path_parent_traversal():
    path=Path("/etc/obsidian-admin/../reviewed/authority.json")
    with pytest.raises(BrokerRejected,match="operator_authority_unavailable"):
        runtime._pinned_root_regular(path,max_bytes=6500)
