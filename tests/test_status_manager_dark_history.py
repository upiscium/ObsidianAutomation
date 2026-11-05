"""#526: dark manager styling and native mobile Back history, no Job effects."""
from __future__ import annotations
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from obsidian_automation.admin_dashboard import HTML, WORKFLOW_UI_ASSETS

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "src/obsidian_automation/webui_design_v1"
STYLE = ASSETS / "status_manager.css"
JS = ASSETS / "status_manager.js"


def rgb(color: str) -> tuple[int, int, int]:
    assert re.fullmatch(r"#[a-f0-9]{6}", color, re.IGNORECASE)
    return tuple(int(color[step:step+2], 16) for step in (1, 3, 5))


def luminance(color: str) -> float:
    components = (part / 255 for part in rgb(color))
    linear = (val / 12.92 if val <= .04045 else
              ((val + .055) / 1.055) ** 2.4 for val in components)
    r, g, b = linear
    return .2126 * r + .7152 * g + .0722 * b


def contrast(one: str, two: str) -> float:
    a, b = sorted((luminance(one), luminance(two)), reverse=True)
    return (a + .05) / (b + .05)


def colors() -> dict[str, str]:
    contents = STYLE.read_text()
    return dict(re.findall(r"(--sm-[\w-]+)\s*:\s*(#[0-9a-fA-F]{6})", contents))


def test_explicit_dark_theme_and_text_contrast():
    css = STYLE.read_text()
    palette = colors()
    for name in (
        "--sm-page", "--sm-card", "--sm-border", "--sm-ink", "--sm-soft",
        "--sm-action", "--sm-green", "--sm-red", "--sm-gray", "--sm-blue",
    ):
        assert name in palette, name
    assert "color-scheme:dark;" in css
    assert "color-scheme:light;" not in css
    assert ":root {\n  color-scheme:dark;" in css
    assert ".status-manager" in css
    assert luminance(palette["--sm-page"]) < .02
    assert luminance(palette["--sm-card"]) < .035
    for background in ("--sm-page", "--sm-card"):
        for foreground in ("--sm-ink", "--sm-soft"):
            assert contrast(palette[background], palette[foreground]) >= 4.5, (
                background, foreground, contrast(palette[background],palette[foreground])
            )
    assert contrast(palette["--sm-action"],palette["--sm-ink"]) >= 4.5
    for signal in ("--sm-green", "--sm-red", "--sm-gray", "--sm-blue"):
        assert contrast(palette[signal],palette["--sm-card"]) >= 3
    assert "input::placeholder" in css
    assert "manager-setup-state" in css
    assert "#manager-global #connections-form" in css
    assert "outline:3px solid #aac4ff" in css


def test_exact_existing_layout_not_changed():
    css = STYLE.read_text()
    for expression in (
        "grid-template-columns:repeat(3,minmax(0,1fr))",
        "grid-template-columns:repeat(2,minmax(0,1fr))",
        "grid-template-columns:1fr",
        ".manager-service-card .manager-settings-button",
        "@media(max-width:620px)",
    ):
        assert expression in css
    for id in (
        "manager-home", "manager-detail", "manager-global",
        "manager-menu-toggle", "manager-setup-readiness",
        "snapshot-run", "daily-run", "formatter-run",
        "nextcloud-form", "connections-form", "daily-connections-form",
    ):
        assert f'id="{id}"' in HTML
    assert HTML.count('id="manager-home"') == 1
    assert '<title>Obsidian Status Manager</title>' in HTML


def test_no_new_backend_write_route_or_transport():
    js = JS.read_text()
    assert 'window.history.pushState' in js
    assert 'window.history.replaceState' in js
    assert '"popstate"' in js
    assert 'window.history.go(-historyDepth)' in js
    assert 'touchstart' not in js
    assert 'touchmove' not in js
    assert 'touchend' not in js
    assert 'method:"POST"' not in js and 'method:"PUT"' not in js
    assert 'redirect:"error"' in js
    assert 'credentials:"omit"' in js
    assert "/static/status-manager.js" in WORKFLOW_UI_ASSETS
    assert "/static/status-manager.css" in WORKFLOW_UI_ASSETS


def test_real_history_renderer_under_synthetic_dom():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable, static checks remain active")
    result = subprocess.run(
        [node, str(ROOT / "tests/status_manager_history.test.cjs")],
        capture_output=True, text=True, timeout=25, cwd=ROOT, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "status_manager_history: 14 checks passed" in result.stdout
