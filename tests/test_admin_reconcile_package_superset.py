"""#442: production package superset must not defeat pinned root import closure."""
from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "tools/reconcile_admin_operator_queue.py"
PINNED = frozenset((
    "__init__.py",
    "admin_broker_socket.py",
    "admin_control_bridge.py",
    "admin_control_queue_socket.py",
    "admin_local_maintenance_gate.py",
    "admin_operation_broker.py",
    "admin_operation_preflight.py",
    "admin_operation_preview.py",
    "admin_operation_queue.py",
    "admin_operator_runtime.py",
    "admin_preview_daemon.py",
    "admin_status.py",
    "admin_systemd_admission.py",
    "admin_workflow_quiescence.py",
))


def cli():
    spec=importlib.util.spec_from_file_location("_oa442_superset_test",CLI)
    assert spec is not None and spec.loader is not None
    obj=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj


class Entry:
    def __init__(self,name,mode=stat.S_IFREG|0o644,uid=0,nlink=1):
        self.name=name
        self.suffix=Path(name).suffix
        self.value=SimpleNamespace(st_mode=mode,st_uid=uid,st_nlink=nlink)

    def lstat(self):
        return self.value


@pytest.mark.parametrize("entry",[
    Entry("trusted-authority-member.py"),
    Entry("unrelated-regular-application-file.py"),
    Entry("metadata.json"),
    Entry("__pycache__",mode=stat.S_IFDIR|0o755,nlink=2),
])
def test_known_root_owned_package_entries_are_qualified(entry):
    assert cli().qualify_package_entry(entry) is None


@pytest.mark.parametrize("entry,reason",[
    (Entry("linked.py",stat.S_IFLNK|0o777),"unreviewed_import_symlink"),
    (Entry("writable.py",stat.S_IFREG|0o666),"unsafe_package_entry_ownership"),
    (Entry("not-root.py",uid=1000),"unsafe_package_entry_ownership"),
    (Entry("linked-hard.py",nlink=2),"unreviewed_package_hardlink"),
    (Entry("shadow.pyc"),"unreviewed_executable_import_artifact"),
    (Entry("shadow.pyo"),"unreviewed_executable_import_artifact"),
    (Entry("shadow.so"),"unreviewed_executable_import_artifact"),
    (Entry("shadow.pyd"),"unreviewed_executable_import_artifact"),
    (Entry("other_data",mode=stat.S_IFDIR|0o755,nlink=2),
     "unexpected_privileged_import_directory"),
    (Entry("unexpected_pipe",mode=stat.S_IFIFO|0o600),
     "unexpected_privileged_import_inode"),
    (Entry("__pycache__",mode=stat.S_IFDIR|0o777,nlink=2),
     "unsafe_package_entry_ownership"),
])
def test_extra_file_fail_closed_for_unsafe_root_package_entries(entry,reason):
    mod=cli()
    with pytest.raises(mod.ReconcileBlocked,match=reason):
        mod.qualify_package_entry(entry)


def test_missing_unreadable_lstat_is_denied_without_side_effects():
    mod=cli()
    class Broken(Entry):
        def lstat(self):
            raise OSError("untrusted path")
    with pytest.raises(mod.ReconcileBlocked,match="unreviewed_package_entry"):
        mod.qualify_package_entry(Broken("unreadable.py"))


def local_package_imports(tree: ast.AST) -> set[str]:
    """Conservatively resolve imports that may load local package modules."""
    imported=set()
    for node in ast.walk(tree):
        if isinstance(node,ast.ImportFrom):
            if node.level and node.module:
                imported.add(node.module.split(".")[0]+".py")
            elif node.level and node.module is None:
                imported.update(alias.name+".py" for alias in node.names)
            elif node.module=="obsidian_automation":
                # Python can resolve package-root attributes as submodules.
                # Treat "*" as an unreviewed candidate and fail the closure.
                imported.update(alias.name+".py" for alias in node.names)
            elif node.module and node.module.startswith("obsidian_automation."):
                imported.add(node.module.split(".")[1]+".py")
        elif isinstance(node,ast.Import):
            imported.update(alias.name.split(".")[1]+".py"
                for alias in node.names
                if alias.name.startswith("obsidian_automation."))
    return imported


@pytest.mark.parametrize("source,expected",[
    ("from obsidian_automation import admin_status",{"admin_status.py"}),
    ("from obsidian_automation import unrelated_feature",
     {"unrelated_feature.py"}),
    ("from obsidian_automation import unrelated_feature as alias",
     {"unrelated_feature.py"}),
    ("from obsidian_automation import *",{"*.py"}),
    ("from . import unrelated_feature",{"unrelated_feature.py"}),
    ("import obsidian_automation.admin_status",{"admin_status.py"}),
])
def test_local_package_import_closure_recognizes_root_aliases(source,expected):
    assert local_package_imports(ast.parse(source))==expected
    if "unrelated_feature.py" in expected or "*.py" in expected:
        assert not local_package_imports(ast.parse(source))<=PINNED


def test_real_pinned_source_import_graph_has_no_unpinned_local_imports():
    """The 14 source hashes pin the complete privileged Python import graph."""
    root=ROOT/"src/obsidian_automation"
    assert len(PINNED)==14
    for name in sorted(PINNED):
        tree=ast.parse((root/name).read_text(),filename=name)
        imported=local_package_imports(tree)
        assert imported <= PINNED,(name,sorted(imported-PINNED))


def test_exact_disabled_manifest_allows_88_nonimported_root_sources(monkeypatch):
    """Live package has 14 privileged sources plus 88 unrelated modules."""
    mod=cli()
    package=Path("/mock/root/site-packages")
    monkeypatch.setattr(mod,"SOURCE_PACKAGE",package)
    rev_old="0"*40
    rev_new="1"*40
    old_q=b"old-pinned-queue-content"
    new_q=b"new-pinned-queue-content"
    monkeypatch.setattr(mod,"PREVIOUS_AUTHORITY_REVISION",rev_old)
    monkeypatch.setattr(mod,"PREVIOUS_MODULE_SHA",mod.sha(old_q))
    monkeypatch.setattr(mod,"EXPECTED_QUEUE_SHA",mod.sha(new_q))
    root=package/"obsidian_automation"
    module_data={name:(b"root-module:"+name.encode()) for name in PINNED}
    module_data["admin_operation_queue.py"]=new_q
    unit_names=[f"unit_{i}.service" for i in range(10)]
    unit_data={name:(b"unit:"+name.encode()) for name in unit_names}
    doc={
        "revision":rev_new,
        "enabled":False,
        "coordinated_updaters_verified":False,
        "uncoordinated_launchers_retired":False,
        "single_active_lxc":True,
        "module_sha256":{name:mod.sha(data) for name,data in module_data.items()},
        "unit_sha256":{name:mod.sha(data) for name,data in unit_data.items()},
    }
    before={**doc,
            "revision":rev_old,
            "module_sha256":{
                **doc["module_sha256"],
                "admin_operation_queue.py":mod.PREVIOUS_MODULE_SHA,
            }}
    canonical=(json.dumps(before,sort_keys=True,separators=(",",":"))+"\n").encode()
    monkeypatch.setattr(mod,"PREVIOUS_AUTHORITY_CANONICAL_SHA",mod.sha(canonical))
    record={(root/name):data for name,data in module_data.items()}
    record.update({Path("/etc/systemd/system")/name:data
                   for name,data in unit_data.items()})
    record[mod.AUTHORITY]=(json.dumps(
        doc,sort_keys=True,separators=(",",":"))+"\n").encode()
    monkeypatch.setattr(mod,"root_file",lambda p,**_:record[p])
    all_entries=[Entry(name) for name in PINNED]+[
        Entry(f"unrelated_feature_{i}.py") for i in range(88)
    ]+[Entry("__pycache__",mode=stat.S_IFDIR|0o755,nlink=2)]
    original=Path.iterdir
    monkeypatch.setattr(Path,"iterdir",lambda self:iter(all_entries)
                        if self==root else original(self))
    assert mod.strict_unchanged_authority()==doc
