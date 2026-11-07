"""Issue #530: one global Settings destination, all original forms retained."""
from __future__ import annotations

import ast
from pathlib import Path
import re
import shutil
import subprocess

import pytest
from obsidian_automation.admin_dashboard import HTML, WORKFLOW_UI_ASSETS

ROOT = Path(__file__).resolve().parents[1]
MANAGER = ROOT / "src/obsidian_automation/admin_dashboard.py"
JS = ROOT / "src/obsidian_automation/webui_design_v1/status_manager.js"


def menu_html() -> str:
    match = re.search(
        r'<nav id="manager-menu"[^>]*>(.*?)</nav>', HTML, re.DOTALL
    )
    assert match is not None
    return match.group(1)


def test_settings_menu_has_only_one_global_settings_destination():
    nav = menu_html()
    buttons = re.findall(
        r'<button type="button" data-manager-route="([^"]+)">([^<]+)</button>',
        nav,
    )
    assert buttons == [
        ("home", "Service overview"),
        ("global", "Global System Settings"),
    ]
    assert "data-manager-target" not in nav
    for removed in ("Nextcloud Settings", "Gitea Settings", "GitHub Settings"):
        assert removed not in nav
    assert 'id="manager-menu-toggle"' in HTML


def test_connection_forms_remain_inside_global_settings_without_duplication():
    for marker in (
        'id="manager-global-nextcloud"',
        'id="manager-global-gitea"',
        'id="manager-global-github"',
        'id="nextcloud-form"',
        'id="connections-form"',
        'id="daily-connections-form"',
        'id="manager-setup-readiness"',
        'id="manager-setup-check"',
    ):
        assert marker in HTML
        assert HTML.count(marker) == 1, marker
    for marker in ("snapshot-run", "daily-run", "formatter-run"):
        assert f'id="{marker}"' in HTML


def test_javascript_preserves_history_and_secret_boundary():
    js = JS.read_text()
    assert 'button.addEventListener("click",()=>navigate(button.dataset.managerRoute));' in js
    assert "dataset.managerTarget" not in js
    assert '"popstate"' in js
    assert 'window.history.pushState' in js
    assert 'window.history.go(-historyDepth)' in js
    assert 'querySelectorAll(\'input[type="password"]\')' in js
    assert 'method:"POST"' not in js
    assert 'method:"PUT"' not in js
    for endpoint in (
        "/api/v1/workflow-control",
        "/api/v1/snapshot",
        "/api/v1/daily",
        "/api/v1/formatter",
    ):
        assert endpoint in js


def test_source_module_modification_is_only_static_html():
    """Verify the historical #531 UI-only change against its frozen base.

    Compare the actual reviewed #531 head to the previous #527 merge,
    never a *moving* current main. Later approved B3 backend additions,
    connection TOCTOU guards and other feature work are independently
    tested and cannot invalidate this historical UI-only proof.
    """
    git = shutil.which("git")
    if not git:
        pytest.skip("git unavailable")

    def frozen(commit: str) -> ast.Module:
        result = subprocess.run(
            [git, "show", commit + ":src/obsidian_automation/admin_dashboard.py"],
            cwd=ROOT, check=False, capture_output=True, text=True, timeout=8,
        )
        if result.returncode != 0:
            pytest.skip("historical UI-only commit unavailable")
        return ast.parse(result.stdout)

    before = frozen("6b592d527ab1e6500de931e38a42b27ca9c971f4")
    after = frozen("c914264aa5d2c5f47be577c390c83ea9f2b49300")

    def backend_ast(tree: ast.Module) -> list[str]:
        result = []
        for node in tree.body:
            # #531 is permitted to change only its inline presentation
            # constants. Current live source is intentionally NOT an input.
            if (isinstance(node, ast.Assign)
                    and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id in ("HTML", "JS")):
                continue
            result.append(ast.dump(node, include_attributes=False))
        return result

    assert backend_ast(before) == backend_ast(after)


def test_existing_browser_history_regression_remains_green():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node unavailable")
    result = subprocess.run(
        [node, str(ROOT / "tests/status_manager_history.test.cjs")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "status_manager_history: 14 checks passed" in result.stdout
