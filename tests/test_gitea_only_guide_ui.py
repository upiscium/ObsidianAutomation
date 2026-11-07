"""#304 R1/C: shared Nextcloud and Gitea-only UI contract; source-only."""
from __future__ import annotations

import ast
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from obsidian_automation.admin_dashboard import HTML, JS


ROOT = Path(__file__).resolve().parents[1]


def test_snapshot_form_contains_four_gitea_fields_only_and_prerequisite():
    match = re.search(
        r'<form id="connections-form"[^>]*>(.*?)</form>',
        HTML, flags=re.DOTALL,
    )
    assert match is not None
    form = match.group(1)
    assert re.findall(r'\bname="([^"]+)"', form) == [
        "git_url", "git_user", "git_token", "branch",
    ]
    for field in ("webdav_url", "vault_folder", "nextcloud_user", "nextcloud_password"):
        assert f'name="{field}"' not in form
    assert '<button type="submit" disabled>' in form
    assert 'aria-describedby="gitea-nextcloud-prerequisite"' in match.group(0)
    assert HTML.count('id="gitea-nextcloud-prerequisite"') == 1
    assert 'id="gitea-nextcloud-prerequisite" role="status" aria-live="polite"' in HTML
    assert HTML.count('id="nextcloud-form"') == 1
    assert HTML.count('id="daily-connections-form"') == 1
    nextcloud = re.search(r'<form id="nextcloud-form"[^>]*>(.*?)</form>', HTML, re.DOTALL)
    assert nextcloud is not None
    assert 'name="nextcloud_password"' in nextcloud.group(1)


def test_existing_backend_gitea_only_contract_and_security_boundaries():
    source = (ROOT / "src/obsidian_automation/admin_dashboard.py").read_text()
    assert 'gitea_only = type(payload) is dict and set(payload) == GITEA_FIELDS' in source
    assert "nextcloud = store.load_nextcloud()" in source
    assert 'payload = {**nextcloud, **payload}' in source
    assert 'with formatter_gate, job._lock, (' in source
    # Structural containment, not just textual ordering: a dedented
    # source-read after the Writer gates would otherwise pass a string check.
    tree = ast.parse(source)
    handlers = [n for n in tree.body
                if isinstance(n, ast.ClassDef) and n.name == "Handler"]
    assert len(handlers) == 1
    posts = [n for n in handlers[0].body
             if isinstance(n, ast.FunctionDef) and n.name == "do_POST"]
    assert len(posts) == 1
    routes = [n for n in ast.walk(posts[0])
              if isinstance(n, ast.If) and isinstance(n.test, ast.Compare)
              and ast.unparse(n.test.left) == "self.path"
              and len(n.test.ops) == 1
              and isinstance(n.test.ops[0], ast.Eq)
              and len(n.test.comparators) == 1
              and isinstance(n.test.comparators[0], ast.Constant)
              and n.test.comparators[0].value == "/api/v1/snapshot/connections"]
    assert len(routes) == 1
    gates = [n for n in ast.walk(routes[0])
             if isinstance(n, ast.With) and len(n.items) == 4
             and ast.unparse(n.items[0].context_expr) == "formatter_gate"]
    assert len(gates) == 1
    gate = gates[0]
    lock_order = [ast.unparse(item.context_expr) for item in gate.items]
    assert lock_order[0:2] == ["formatter_gate", "job._lock"]
    assert "daily._lock" in lock_order[2]
    assert "formatter._lock" in lock_order[3]
    branches = [n for n in gate.body
                if isinstance(n, ast.If) and ast.unparse(n.test) == "gitea_only"]
    assert len(branches) == 1
    branch = branches[0]
    assert any(ast.unparse(n) == "nextcloud = store.load_nextcloud()"
               for n in branch.body)
    assert any(ast.unparse(n) == "payload = {**nextcloud, **payload}"
               for n in branch.body)
    calls = [n for n in ast.walk(routes[0]) if isinstance(n, ast.Call)
             and ast.unparse(n.func) == "store.load_nextcloud"]
    assert len(calls) == 1
    saved = [n for n in ast.walk(gate) if isinstance(n, ast.Call)
             and ast.unparse(n.func) == "store.save"]
    assert len(saved) == 1
    assert 'await snapshotPost("/api/v1/snapshot/connections", body);' in JS
    assert 'new FormData(form)' in JS
    assert 'const mine = ++nextcloudReadGeneration;' in JS
    assert 'if (mine !== nextcloudReadGeneration) return;' in JS
    assert 'renderGiteaPrerequisite(nextcloudReady ? "configured" : "missing")' in JS
    assert "localStorage.setItem" not in JS
    assert "sessionStorage" not in JS


def test_inline_original_js_behavior_fail_closed_and_gitea_only():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node not installed; HTML and backend checks remain active")
    result = subprocess.run(
        [node, str(ROOT / "tests/gitea_only_guide.test.cjs")],
        input=JS,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=35,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "gitea_only_guide: 13 PASS" in result.stdout
    syntax = subprocess.run(
        [node, "--check", "-"], input=JS,
        cwd=ROOT, capture_output=True, text=True, timeout=15,
        check=False,
    )
    assert syntax.returncode == 0, syntax.stdout + syntax.stderr
