"""#403 MVI: fixed source-only root Python bootstrap instead of pyc analysis.

Existing package caches are never read by an interpreter with the exact
uncreatable /dev/null prefix and -B; privileged source SHA and direct shadow
checks stay in place. This tests ONLY disposable fixture data.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "/dev/null/obsidian-admin-source-only"


@pytest.mark.parametrize("source", [
    "examples/admin/obsidian-admin-root-launch.py",
    "tools/prepare_admin_operator_authority.py",
])
def test_qualified_runtime_ignores_only_unreachable_package_cache(
    source, tmp_path, monkeypatch,
):
    spec = importlib.util.spec_from_file_location(
        "_oa403_verified_source_"+Path(source).stem, ROOT/source,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    p = tmp_path/"obsidian_automation"
    p.mkdir()
    (p/"__pycache__").mkdir()
    (p/"__pycache__"/"untrusted.cpython-313.pyc").write_bytes(b"NO_EXECUTE")

    # Simulate ONLY the pinned interpreter flags, not the filesystem owner.
    monkeypatch.setattr(module, "sys", SimpleNamespace(
        pycache_prefix=PREFIX,
        flags=SimpleNamespace(dont_write_bytecode=1),
    ))
    if hasattr(module, "reject_unverified_import_artifacts"):
        validate = module.reject_unverified_import_artifacts
    else:
        validate = module._verify_import_inventory
    assert validate(p) is None

    # Native/direct source shadowing remains rejected even with the prefix.
    (p/"admin_status.so").write_bytes(b"UNREVIEWED_EXTENSION")
    with pytest.raises(RuntimeError, match="unreviewed_"):
        validate(p)


def test_unqualified_interpreter_does_not_skip_original_cache_verifier(
    tmp_path, monkeypatch,
):
    spec = importlib.util.spec_from_file_location(
        "_oa403_default_import", ROOT/"examples/admin/obsidian-admin-root-launch.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    package = tmp_path/"obsidian_automation"
    cache = package/"__pycache__"
    cache.mkdir(parents=True)
    (cache/"bad.pyc").write_bytes(b"BAD")
    monkeypatch.setattr(module, "sys", SimpleNamespace(
        pycache_prefix=None, flags=SimpleNamespace(dont_write_bytecode=1),
    ))
    with pytest.raises(RuntimeError, match="unreviewed_"):
        module.reject_unverified_import_artifacts(package)


def test_actual_python_cannot_read_local_poisoned_cache(tmp_path):
    p = tmp_path/"obsidian_automation"
    p.mkdir()
    (p/"__init__.py").write_text("", encoding="utf-8")
    (p/"worker.py").write_text("ANSWER = 42\n", encoding="utf-8")
    cache = p/"__pycache__"
    cache.mkdir()
    poison = cache/("worker."+sys.implementation.cache_tag+".pyc")
    poison.write_bytes(b"NOT_A_VALID_PYC")
    snippet = (
        "import importlib.util,sys;"
        "assert sys.flags.isolated==sys.flags.no_site==sys.flags.dont_write_bytecode==1;"
        "assert sys.pycache_prefix=='/dev/null/obsidian-admin-source-only';"
        "sys.path.insert(0,sys.argv[1]);"
        "from obsidian_automation import worker;"
        "assert worker.ANSWER==42;"
        "print('SOURCE_ONLY_IMPORT_PASS')"
    )
    result = subprocess.run(
        [sys.executable,"-I","-S","-B","-X","pycache_prefix="+PREFIX,
         "-c",snippet,str(tmp_path)],
        capture_output=True,text=True,timeout=15,check=False,
    )
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()=="SOURCE_ONLY_IMPORT_PASS"
    assert poison.read_bytes()==b"NOT_A_VALID_PYC"


def test_unit_and_stage_use_identical_pinned_cache_policy():
    unit = (ROOT/"examples/admin/obsidian-admin-operator.service").read_text()
    launcher = (ROOT/"examples/admin/obsidian-admin-root-launch.py").read_text()
    staging = (ROOT/"tools/prepare_admin_operator_authority.py").read_text()
    runtime = (ROOT/"src/obsidian_automation/admin_operator_runtime.py").read_text()
    assert ("ExecStart=/usr/bin/python3 -I -S -B -X "
            "pycache_prefix="+PREFIX+" "
            "/usr/local/libexec/obsidian-admin-root-launch.py") in unit
    assert 'SOURCE_ONLY_PYCACHE_PREFIX = "'+PREFIX+'"' in launcher
    assert 'SOURCE_ONLY_PYCACHE_PREFIX = "'+PREFIX+'"' in staging
    assert 'sys.pycache_prefix == '+repr(PREFIX) in launcher
    assert 'getattr(sys,"pycache_prefix",None)!="'+PREFIX+'"' in runtime
    assert '"-X",' in launcher
    assert '"pycache_prefix='+PREFIX+'",' in launcher
