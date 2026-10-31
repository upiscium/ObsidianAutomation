"""Offline wheel build in a disposable source copy, never installing dependencies.

No build products are written into the source worktree. Extracted wheel resources
are checked in a fresh process, not through pytest's source-path imports.
"""
from __future__ import annotations

import fnmatch
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "src/obsidian_automation/webui_design_v1"


def test_w4a_explicit_package_data_covers_fixed_assets():
    """Deterministic local check also runs when wheel tooling is unavailable."""
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    patterns = config["tool"]["setuptools"]["package-data"]["obsidian_automation"]
    for name in ("live.html", "live.js", "model.js", "styles.css", "index.html", "app.js"):
        assert any(fnmatch.fnmatch("webui_design_v1/" + name, p) for p in patterns)
        assert (ASSETS / name).is_file()


def test_w4a_offline_wheel_and_extracted_resources_have_exact_source_bytes(tmp_path):
    if importlib.util.find_spec("setuptools") is None:
        pytest.skip("setuptools unavailable; package-data coverage test still runs; no downloads")
    source = tmp_path / "source"
    source.mkdir()
    shutil.copy2(ROOT / "pyproject.toml", source / "pyproject.toml")
    shutil.copytree(ROOT / "src", source / "src", ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
    (source / "dist").mkdir()
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, "-c", "from setuptools.build_meta import build_wheel; build_wheel('dist')"],
        cwd=source, env=env, text=True, capture_output=True, timeout=120, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    wheels = list((source / "dist").glob("*.whl"))
    assert len(wheels) == 1
    installed = tmp_path / "extracted-wheel"
    installed.mkdir()
    expected = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in ASSETS.iterdir()
                if p.suffix in {".html", ".js", ".css"}}
    with zipfile.ZipFile(wheels[0]) as wheel:
        for name in expected:
            assert hashlib.sha256(wheel.read("obsidian_automation/webui_design_v1/" + name)).hexdigest() == expected[name]
        # The archive was built exclusively from the local source copy above.
        wheel.extractall(installed)
    probe = """
import hashlib, json
from importlib.resources import files
from pathlib import Path
from obsidian_automation import admin_dashboard as dashboard
assert Path(dashboard.__file__).is_relative_to(Path.cwd())
root = files('obsidian_automation').joinpath('webui_design_v1')
assets = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.iterdir()
          if p.name.endswith(('.html', '.js', '.css'))}
for name, mime in dashboard.WORKFLOW_UI_ASSETS.values():
    assert name in assets and mime
print(json.dumps(assets, sort_keys=True))
"""
    result = subprocess.run([sys.executable, "-c", probe], cwd=installed,
                            env=env, text=True, capture_output=True, timeout=20, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == expected
