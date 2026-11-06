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
    # Compare AST instead of source string: ensure this UI slice did not
    # silently edit service implementation functions in admin_dashboard.py.
    # A frozen base copy is the exact previous main revision used by this
    # isolated implementation, materialized only when running in a Git repo.
    git = shutil.which("git")
    if not git:
        pytest.skip("git unavailable")
    baseline = subprocess.run(
        [
            git, "show",
            "6b592d527ab1e6500de931e38a42b27ca9c971f4:"
            "src/obsidian_automation/admin_dashboard.py",
        ],
        cwd=ROOT, check=False, capture_output=True, text=True, timeout=8,
    )
    if baseline.returncode != 0:
        pytest.skip("prior merge baseline unavailable")
    current = MANAGER.read_text()

    # #301: the independently tested Gitea-only TOCTOU correction moves
    # ONLY source resolution under the existing Writer/Job gates. Reconstruct
    # the pre-correction source using exact, one-time replacements before the
    # historical #530 AST check; any other backend/inline-JS change fails.
    def undo_approved_gitea_lock_move(source: str) -> str:
        new_prefix = (
            '                payload = json.loads(self._read_bounded_json())\n'
            '                gitea_only = type(payload) is dict and set(payload) == GITEA_FIELDS\n'
            '                daily = self.server.daily_job'
        )
        old_prefix = (
            '                payload = json.loads(self._read_bounded_json())\n'
            '                if type(payload) is dict and set(payload) == GITEA_FIELDS:\n'
            '                    nextcloud = store.load_nextcloud()\n'
            '                    if nextcloud is None:\n'
            '                        raise ValueError("nextcloud_missing")\n'
            '                    payload = {**nextcloud, **payload}\n'
            '                daily = self.server.daily_job'
        )
        new_suffix = (
            '                    # Resolve the ONE canonical shared source only after all\n'
            '                    # writer/Job gates are held. A Nextcloud rotation that\n'
            '                    # wins those gates must not be rolled back by a Gitea-only\n'
            '                    # request that was already in flight.\n'
            '                    if gitea_only:\n'
            '                        nextcloud = store.load_nextcloud()\n'
            '                        if nextcloud is None:\n'
            '                            raise ValueError("nextcloud_missing")\n'
            '                        payload = {**nextcloud, **payload}\n'
            '                    github_setup = self.server.daily_setup'
        )
        old_suffix = '                    github_setup = self.server.daily_setup'
        assert source.count(new_prefix) == 1
        assert source.count(new_suffix) == 1
        assert source.count(old_suffix) == 1
        return source.replace(new_prefix, old_prefix, 1).replace(
            new_suffix, old_suffix, 1
        )

    previous_ast = ast.parse(baseline.stdout)
    current_ast = ast.parse(undo_approved_gitea_lock_move(current))

    def without_html(tree: ast.Module):
        return [
            ast.dump(node, include_attributes=False)
            for node in tree.body
            if not (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "HTML"
            )
        ]

    assert without_html(current_ast) == without_html(previous_ast)


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
