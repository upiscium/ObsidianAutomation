"""Self-contained frontend assets: stdlib checks + optional installed JS runtime.

Run: python -m unittest discover -s tests -p test_webui_design_v1.py -v
No dashboard import, application server or production fixture is involved.
"""
from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "src/obsidian_automation/webui_design_v1"


class Template(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


class WebUIDesignTests(unittest.TestCase):
    def test_live_template_is_read_only_and_same_origin(self):
        template = Template()
        template.feed((ASSETS / "live.html").read_text())
        ids = [attrs["id"] for _, attrs in template.tags if "id" in attrs]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertFalse(any(tag in {"form", "input", "select", "iframe"}
                             for tag, _ in template.tags))
        buttons = [attrs["id"] for tag, attrs in template.tags if tag == "button"]
        self.assertEqual(buttons, ["refresh-observation"])
        for tag, attrs in template.tags:
            self.assertFalse(any(name.startswith("on") for name in attrs))
            if tag == "script" or (tag == "link" and attrs.get("rel") == "stylesheet"):
                url = attrs.get("src", attrs.get("href"))
                self.assertIn(url, {"/static/workflow-control/" + name
                                    for name in ("live.js", "model.js", "styles.css")})
                self.assertTrue((ASSETS / url.rsplit("/", 1)[1]).is_file())
        csp = next(attrs["content"] for tag, attrs in template.tags
                   if tag == "meta" and attrs.get("http-equiv") == "Content-Security-Policy")
        self.assertIn("connect-src 'self'", csp)
        self.assertIn("form-action 'none'", csp)
        self.assertNotIn("*", csp)

    def test_live_async_renderer_in_installed_runtime(self):
        runtime = shutil.which("node") or shutil.which("bun")
        if runtime is None:
            self.skipTest("Node/Bun not installed; no dependency downloads")
        result = subprocess.run([runtime, str(ROOT / "tests/webui_live_v1.test.cjs")],
                                text=True, capture_output=True, timeout=20, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("webui_live_v1: 12 checks passed", result.stdout)

    def test_template_assets_and_accessible_write_only_inputs(self):
        template = Template()
        template.feed((ASSETS / "index.html").read_text())
        ids = [attrs["id"] for _, attrs in template.tags if "id" in attrs]
        self.assertEqual(len(ids), len(set(ids)))
        labels = {attrs.get("for") for tag, attrs in template.tags if tag == "label"}
        for tag, attrs in template.tags:
            self.assertFalse(any(name.startswith("on") for name in attrs))
            if tag == "input":
                self.assertIn(attrs["id"], labels)
                if attrs.get("type") == "password":
                    self.assertNotIn("value", attrs)
                    self.assertEqual(attrs["autocomplete"], "new-password")
            if tag == "script":
                self.assertIn("src", attrs)
                self.assertTrue((ASSETS / attrs["src"]).is_file())
        self.assertTrue((ASSETS / "styles.css").is_file())
        csp = next(attrs["content"] for tag, attrs in template.tags
                   if tag == "meta" and attrs.get("http-equiv") == "Content-Security-Policy")
        self.assertIn("connect-src 'none'", csp)
        self.assertIn("form-action 'none'", csp)

    def test_authored_frontend_in_installed_runtime(self):
        runtime = shutil.which("node") or shutil.which("bun")
        if runtime is None:
            self.skipTest("Node/Bun not installed; do not install dependencies for this preview")
        result = subprocess.run([runtime, str(ROOT / "tests/webui_design_v1.test.cjs")],
                                text=True, capture_output=True, timeout=20, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        checks = re.search(r"webui_design_v1: (\d+) checks passed", result.stdout)
        self.assertIsNotNone(checks, result.stdout)
        self.assertGreaterEqual(int(checks.group(1)), 26)  # Original 17 + strict v1 checks.


if __name__ == "__main__":
    unittest.main()
