from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# Run under both unittest and pytest, without production runtime dependencies.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from obsidian_automation.vault_formatter import (  # noqa: E402
    POLICY_VERSION,
    create_plan,
    format_markdown,
)


class MarkdownFormatTests(unittest.TestCase):
    def test_repairs_whitespace_only_line_and_preserves_hard_break(self):
        raw = b"# Demo\nHello  \n  \nWorld\n"
        expected = b"# Demo\nHello  \n\nWorld\n"
        desired, lines, reason = format_markdown(raw)
        self.assertEqual(desired, expected)
        self.assertEqual(lines, (3,))
        self.assertEqual(reason, "blank-line-whitespace")
        self.assertEqual(format_markdown(desired)[1], ())

    def test_reproduction_of_nested_two_space_blank_line(self):
        original = b"Header\n  item\n  \n  next\n"
        desired, lines, _ = format_markdown(original)
        self.assertEqual(desired, b"Header\n  item\n\n  next\n")
        self.assertEqual(lines, (3,))

    def test_frontmatter_is_untouched_including_yaml_block_scalars(self):
        raw = b"---\nkind: project\nmultiline: |\n  one\n  \n  two\n---\ntext\n  \nend\n"
        desired, lines, _ = format_markdown(raw)
        self.assertEqual(desired, raw.replace(b"text\n  \n", b"text\n\n"))
        self.assertEqual(lines, (9,))

    def test_unterminated_frontmatter_aborts_whole_file(self):
        desired, lines, reason = format_markdown(b"---\nyaml: 1\n  \n")
        self.assertIsNone(desired)
        self.assertEqual(lines, ())
        self.assertEqual(reason, "unterminated-frontmatter")

    def test_fenced_code_and_dataviewjs_are_untouched(self):
        fence = chr(96) * 3
        raw = (f"outside\n  \n{fence}dataviewjs\n  \nline\n{fence}\ntext\n  \nlast\n").encode()
        desired, lines, reason = format_markdown(raw)
        self.assertEqual(desired, (f"outside\n\n{fence}dataviewjs\n  \nline\n{fence}\ntext\n\nlast\n").encode())
        self.assertEqual(lines, (2, 8))
        self.assertEqual(reason, "blank-line-whitespace")

    def test_terminating_fence_requires_matching_delimiter(self):
        fence = chr(96) * 3
        raw = (f"{fence}code\n  \n~~~\n  \n{fence}\nnormal\n  \nend\n").encode()
        desired, lines, _ = format_markdown(raw)
        self.assertEqual(lines, (7,))
        self.assertIn(b"~~~\n  \n", desired)

    def test_unclosed_code_fence_prevents_any_change(self):
        fence = chr(96) * 3
        desired, lines, reason = format_markdown((f"top\n  \n{fence}js\n  \n").encode())
        self.assertIsNone(desired)
        self.assertEqual(lines, ())
        self.assertEqual(reason, "unterminated-protected-block")

    def test_templater_multiline_expression_is_untouched(self):
        raw = b"top\n  \n<%*\n  \nconsole.log('hi')\n%>\nend\n  \nlast\n"
        desired, lines, _ = format_markdown(raw)
        self.assertEqual(lines, (2, 8))
        self.assertIn(b"<%*\n  \n", desired)

    def test_html_comment_pre_and_script_are_opaque(self):
        raw = (
            b"top\n  \n<!--\n  \n-->\n"
            b"<pre>\n  \n</pre>\n"
            b"<script>\n  \n</script>\n"
            b"bottom\n  \nlast\n"
        )
        desired, lines, _ = format_markdown(raw)
        self.assertEqual(lines, (2, 13))
        self.assertIn(b"<pre>\n  \n</pre>", desired)
        self.assertIn(b"<script>\n  \n</script>", desired)

    def test_adjacent_quote_avoids_ambiguous_blank_line(self):
        source = b"> quoted\n  \n> continuation\n"
        desired, lines, reason = format_markdown(source)
        self.assertEqual(desired, source)
        self.assertEqual(lines, ())
        self.assertEqual(reason, "no-safe-changes")

    def test_indented_code_avoids_ambiguous_blank_line(self):
        source = b"    code\n  \n    code\n"
        self.assertEqual(format_markdown(source)[0], source)

    def test_indented_code_boundary_is_untouched(self):
        source = b"    code\n  \nplain\n"
        desired, lines, _ = format_markdown(source)
        self.assertEqual(desired, source)
        self.assertEqual(lines, ())

    def test_tab_and_four_space_blank_lines_untouched(self):
        source = b"A\n\t\nB\n    \nC\n"
        desired, lines, _ = format_markdown(source)
        self.assertEqual(desired, source)
        self.assertEqual(lines, ())

    def test_bom_crlf_and_unterminated_last_line_preserved(self):
        source = b"\xef\xbb\xbfOne\r\n  \r\nTwo"
        desired, lines, _ = format_markdown(source)
        self.assertEqual(desired, b"\xef\xbb\xbfOne\r\n\r\nTwo")
        self.assertEqual(lines, (2,))
        self.assertFalse(desired.endswith(b"\n"))

    def test_mixed_crlf_lf_and_odd_cr_rejected(self):
        for data in (b"one\r\ntwo\n  \n", b"one\rtwo\n  \n"):
            self.assertIsNone(format_markdown(data)[0])

    def test_invalid_utf8_and_binary_rejected(self):
        self.assertEqual(format_markdown(b"\xff\n")[2], "invalid-utf8")
        self.assertEqual(format_markdown(b"\x00  \n")[2], "binary-content")

    def test_untouched_wikilink_and_meta_bind(self):
        source = b"[[other|label]]\n  \ntext\n"
        desired, _, _ = format_markdown(source)
        self.assertIn(b"[[other|label]]", desired)
        self.assertEqual(source.replace(b"\n  \n", b"\n\n"), desired)


    def test_ambiguous_block_html_and_nested_fence_refused(self):
        raw = b"before\n  \n<div>\n  \n</div>\n"
        desired, changed, reason = format_markdown(raw)
        self.assertIsNone(desired)
        self.assertEqual(changed, ())
        self.assertEqual(reason, "unsupported-html-block")

        quote_fence = chr(96) * 3
        desired, changed, reason = format_markdown(
            ("> " + quote_fence + "\n  \n> " + quote_fence + "\n").encode()
        )
        self.assertIsNone(desired)
        self.assertEqual(changed, ())
        self.assertEqual(reason, "unsupported-nested-fence")

    def test_uncommon_unicode_newline_is_not_rewritten(self):
        desired, changed, reason = format_markdown("first\u2028  \nsecond\n".encode())
        self.assertIsNone(desired)
        self.assertEqual(changed, ())
        self.assertEqual(reason, "unsupported-line-separators")


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="vault-format-470-")
        self.addCleanup(self.temp.cleanup)
        self.vault = Path(self.temp.name)
        (self.vault / "98-System").mkdir()
        (self.vault / "10-Project").mkdir()

    def _add(self, relative: str, content: bytes) -> Path:
        p = self.vault / relative
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        return p

    def test_plan_is_deterministic_read_only_and_scoped(self):
        target = self._add("98-System/startup.md", b"top\n  \nend\n")
        external = self._add("10-Project/private.md", b"secret\n  \nend\n")
        before = target.read_bytes(), external.read_bytes()
        first = create_plan(self.vault, include_diffs=True)
        second = create_plan(self.vault, include_diffs=True)
        self.assertEqual(first, second)
        self.assertEqual(first["scope"], "98-System/**")
        self.assertEqual(first["policy_version"], POLICY_VERSION)
        self.assertEqual(first["counts"]["would_change"], 1)
        self.assertEqual(first["results"][0]["path"], "98-System/startup.md")
        self.assertEqual(first["results"][0]["changed_lines"], [2])
        self.assertEqual(first["results"][0]["before_sha256"], hashlib.sha256(before[0]).hexdigest())
        self.assertIn("-  \n", first["diffs"]["98-System/startup.md"])
        self.assertEqual(target.read_bytes(), before[0])
        self.assertEqual(external.read_bytes(), before[1])
        self.assertEqual(first["mode"], "dry-run")

    def test_skips_extensions_hidden_files_and_symlinks(self):
        good = self._add("98-System/z.md", b"item\n  \nitem\n")
        self._add("98-System/asset.js", b"const s = '  ';")
        self._add("98-System/data.json", b'{"x":"keep"}')
        self._add("98-System/.secret.md", b"key\n  \nvalue\n")
        self._add("98-System/.hidden/sub.md", b"key\n  \nvalue\n")
        (self.vault / "98-System/fake.md").symlink_to(good)
        (self.vault / "98-System/link-dir").symlink_to(self.vault / "10-Project", target_is_directory=True)
        status = {x["path"]: (x["status"], x["reason"]) for x in create_plan(self.vault)["results"]}
        self.assertEqual(status["98-System/z.md"][0], "would_change")
        self.assertEqual(status["98-System/asset.js"], ("skipped", "unsupported-extension"))
        self.assertEqual(status["98-System/data.json"], ("skipped", "unsupported-extension"))
        self.assertEqual(status["98-System/.secret.md"], ("skipped", "hidden-path"))
        self.assertEqual(status["98-System/.hidden"], ("skipped", "hidden-directory"))
        self.assertEqual(status["98-System/fake.md"], ("skipped", "symlink"))
        self.assertEqual(status["98-System/link-dir"], ("skipped", "symlink"))

    def test_outside_scope_and_invalid_system_root(self):
        self._add("10-Project/private.md", b"secret\n  \nend\n")
        self.assertEqual(create_plan(self.vault)["results"], [])
        with tempfile.TemporaryDirectory() as other:
            outside = Path(other)
            (outside / "98-System").symlink_to(self.vault / "98-System", target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlinked"):
                create_plan(outside)

    def test_oversized_invalid_and_binary_are_skipped(self):
        self._add("98-System/large.md", b"data\n" * 20)
        self._add("98-System/bad.md", b"\xff\n")
        self._add("98-System/binary.md", b"\x00  \n")
        results = {x["path"]: x for x in create_plan(self.vault, max_bytes=50)["results"]}
        self.assertEqual(results["98-System/large.md"]["reason"], "file-too-large")
        self.assertEqual(results["98-System/bad.md"]["reason"], "invalid-utf8")
        self.assertEqual(results["98-System/binary.md"]["reason"], "binary-content")

    def test_file_limit_and_total_byte_limit_fail_closed(self):
        self._add("98-System/a.md", b"one\n")
        self._add("98-System/b.md", b"two\n")
        with self.assertRaisesRegex(ValueError, "max-files-limit-exceeded"):
            create_plan(self.vault, max_files=1)
        with self.assertRaisesRegex(ValueError, "max-total-bytes-limit-exceeded"):
            create_plan(self.vault, max_total_bytes=4)

    def test_unicode_and_case_collision_fail_closed(self):
        self._add("98-System/Note.md", b"one\n  \ntwo\n")
        self._add("98-System/note.md", b"one\n  \ntwo\n")
        results = create_plan(self.vault)["results"]
        self.assertEqual([r["reason"] for r in results], ["path-name-collision"] * 2)
        self.assertEqual([r["status"] for r in results], ["skipped", "skipped"])

    def test_cli_dryrun_and_check_mode_only(self):
        target = self._add("98-System/sample.md", b"text\n  \ntext\n")
        cmd = [
            sys.executable, "-m", "obsidian_automation.vault_formatter",
            "--vault", str(self.vault),
        ]
        project_src = str(Path(__file__).resolve().parents[1] / "src")
        env = {**os.environ, "PYTHONPATH": project_src}
        response = subprocess.run(cmd, capture_output=True, text=True, env=env)
        self.assertEqual(response.returncode, 0, response.stderr)
        plan = json.loads(response.stdout)
        self.assertEqual(plan["counts"]["would_change"], 1)
        self.assertNotIn("diffs", plan)
        check = subprocess.run(cmd + ["--check"], capture_output=True, text=True, env=env)
        self.assertEqual(check.returncode, 1, check.stderr)
        self.assertEqual(target.read_bytes(), b"text\n  \ntext\n")

    def test_git_diff_check_before_after_synthetic_reproduction(self):
        root = self.vault / "disposable-git"
        root.mkdir()
        target = root / "98-System/03-template/99-startup/startup-mobile.md"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"alpha\nbeta\n")
        git = lambda *args: subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True,
            env={**os.environ, "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
                 "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid"},
        )
        self.assertEqual(git("init", "-q").returncode, 0)
        self.assertEqual(git("add", ".").returncode, 0)
        self.assertEqual(git("commit", "-qm", "baseline").returncode, 0)
        target.write_bytes(b"alpha\n  \nbeta\n")
        self.assertNotEqual(git("diff", "--check").returncode, 0)
        desired, lines, _ = format_markdown(target.read_bytes())
        self.assertEqual(lines, (2,))
        # A disposable Git fixture only. The formatter API/CLI never writes.
        target.write_bytes(desired)
        self.assertEqual(git("diff", "--check").returncode, 0)

    def test_unsupported_limits(self):
        with self.assertRaisesRegex(ValueError, "positive"):
            create_plan(self.vault, max_files=0)


    def test_empty_nested_directory_tree_still_hits_file_budget(self):
        current = self.vault / "98-System"
        for _ in range(8):
            current = current / "level"
            current.mkdir()
        with self.assertRaisesRegex(ValueError, "max-files-limit-exceeded"):
            create_plan(self.vault, max_files=5)

    def test_cli_outside_scope_refuses_missing_system_tree(self):
        with tempfile.TemporaryDirectory(prefix="vault-format-invalid-") as other:
            root = Path(other)
            cmd = [
                sys.executable, "-m", "obsidian_automation.vault_formatter",
                "--vault", str(root),
            ]
            env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
            result = subprocess.run(cmd, capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn(str(self.vault), result.stdout)
            self.assertIn("98-System", result.stderr)


if __name__ == "__main__":
    unittest.main()
