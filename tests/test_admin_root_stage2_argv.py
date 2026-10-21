"""#403: real CPython -c staging argv must not leak import-root into argparse.

No root user, authority file, systemd unit, queue, or real service is involved.
The fixture reproduces the production stage-2 failure with a harmless module.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "examples/admin/obsidian-admin-root-launch.py"
PREFIX = "/dev/null/obsidian-admin-source-only"


def code() -> str:
    spec = importlib.util.spec_from_file_location("_oa403_stage2_args", SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._RUNTIME_CODE


@pytest.fixture
def fake_verified_root(tmp_path):
    pkg=tmp_path/"obsidian_automation"
    pkg.mkdir()
    (pkg/"__init__.py").write_text("",encoding="utf-8")
    (pkg/"admin_operator_runtime.py").write_text(
        "import argparse,sys\n"
        "def main(argv=None):\n"
        "    parser=argparse.ArgumentParser()\n"
        "    parser.parse_args(list(argv) if argv is not None else None)\n"
        "    print('STAGE2_ARGPARSE_OK')\n"
        "    return 0\n",
        encoding="utf-8",
    )
    return tmp_path


def invoke(code_text: str, root: Path, extra: tuple[str, ...] = ()):
    return subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-X",
         f"pycache_prefix={PREFIX}", "-c", code_text, str(root), *extra],
        capture_output=True, text=True, check=False, timeout=12,
    )


def test_actual_cpython_stage2_argparse_reproduces_and_fixes_failure(
    fake_verified_root,
):
    current=code()
    # Real production has an extra argument after -c: the verified site root.
    assert 'sys.path.insert(0,sys.argv[1])' in current
    assert 'runtime.main(())' in current
    legacy=current.replace('runtime.main(())','runtime.main()')
    result=invoke(legacy,fake_verified_root)
    assert result.returncode==2
    assert "unrecognized arguments:" in result.stderr
    assert str(fake_verified_root) in result.stderr

    accepted=invoke(current,fake_verified_root)
    assert accepted.returncode==0,accepted.stderr
    assert accepted.stdout.strip()=="STAGE2_ARGPARSE_OK"


def test_second_stage_refuses_any_unreviewed_extra_argv(fake_verified_root):
    result=invoke(code(),fake_verified_root,("--unexpected",))
    assert result.returncode!=0
    assert "STAGE2_ARGPARSE_OK" not in result.stdout


def test_second_stage_refuses_missing_required_cache_policy(fake_verified_root):
    result=subprocess.run(
        [sys.executable,"-I","-S","-B","-c",code(),str(fake_verified_root)],
        capture_output=True,text=True,timeout=12,check=False,
    )
    assert result.returncode!=0
    assert "STAGE2_ARGPARSE_OK" not in result.stdout
