"""#303 Phase 2 / P2-0: one accessor for role folders, no behaviour change."""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

from obsidian_automation import vault_layout
from obsidian_automation.vault_layout import role_folder, role_folders

SRC = Path(__file__).resolve().parents[1] / "src" / "obsidian_automation"
_ROLE_NAME = re.compile(r"(?<![\w-])(00-DailyNote|04-AI|05-Idea|10-Project|11-Knowledge|98-System)(?![\w-])")

# String literals that keep a role folder name on purpose. See
# docs/oa303-phase2-path-inventory.md for the reasons.
# Exact entries (module, literal) must match the whole literal.
_ALLOWED_EXACT = {
    # The mapping itself.
    ("vault_layout", "00-DailyNote"), ("vault_layout", "04-AI"), ("vault_layout", "05-Idea"),
    ("vault_layout", "10-Project"), ("vault_layout", "11-Knowledge"), ("vault_layout", "98-System"),
    # Approval binding identities: digests over a fixed string.
    ("core_live_reconciliation", "98-System/__binding__"),
    ("vault_format_writer", "98-System/__binding__.md"),
}
# Fragment entries (module, fragment) allow a literal that contains the fragment.
_ALLOWED_LITERALS = {
    # ObsidianCore repository layout, not the Vault.
    ("public_publish", "98-System/99-dev/validate-repo.mjs"),
    ("public_publish", "98-System/99-dev/test"),
    # Synthetic smoke fixture (a stored-history shape).
    ("github_production_smoke", "10-Project/Terreate/Terreate.md"),
    # Text shown to people: a generated note body and the dashboard heading.
    ("human_projection", "[[98-System/02-embed/01-button/ai-review-buttons|ai-review-buttons]]"),
    ("human_projection", "context-bound `05-Idea` note"),
    ("admin_dashboard", "<h2>Vault Formatter / 98-System</h2>"),
}


def _allowed(module: str, literal: str) -> bool:
    return (module, literal) in _ALLOWED_EXACT or any(
        m == module and frag in literal for m, frag in _ALLOWED_LITERALS)


def _message_parts(node: ast.AST):
    """The literal pieces of a message argument, not nested calls inside it."""
    yield node
    if not isinstance(node, ast.Call):
        for child in ast.iter_child_nodes(node):
            yield from _message_parts(child)


def _role_literals(path: Path) -> set[str]:
    """String constants naming a role folder, ignoring docstrings and messages."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    messages = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
        if isinstance(node, ast.Call) and node.args:
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name.endswith(("Error", "Denied", "Unavailable")) or name in {"description"}:
                for arg in _message_parts(node.args[0]):
                    messages.add(id(arg))
        if isinstance(node, ast.keyword) and node.arg in {"description", "help"}:
            messages.add(id(node.value))
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or id(node) in docstrings or id(node) in messages:
            continue
        value = node.value
        if isinstance(value, bytes):
            value = value.decode("utf-8", "replace")
        if isinstance(value, str) and _ROLE_NAME.search(value):
            found.add(value)
    return found


class VaultLayoutTest(unittest.TestCase):
    def test_defaults_equal_todays_names(self) -> None:
        self.assertEqual(dict(role_folders()), {
            "daily": "00-DailyNote", "ai": "04-AI", "idea": "05-Idea",
            "project": "10-Project", "knowledge": "11-Knowledge", "system": "98-System",
        })
        self.assertEqual(tuple(role_folders()), vault_layout.ROLES)
        for role in vault_layout.ROLES:
            self.assertEqual(role_folder(role), role_folders()[role])

    def test_unknown_role_is_rejected(self) -> None:
        for bad in ("", "Daily", "project-note", "../daily"):
            with self.assertRaises(ValueError):
                role_folder(bad)

    def test_mapping_is_read_only(self) -> None:
        with self.assertRaises(TypeError):
            role_folders()["daily"] = "Elsewhere"  # type: ignore[index]

    def test_derived_constants_are_unchanged(self) -> None:
        from obsidian_automation import (
            ai_input_planner, context_bundle, github_daily_progress, github_project_watcher,
            human_projection, knowledge_index, knowledge_note_policy, semantic_corpus,
            vault_formatter,
        )
        self.assertEqual(vault_formatter.SCOPE, "98-System/**")
        self.assertEqual(human_projection.PROJECTION_ROOT, "04-AI")
        self.assertEqual(github_daily_progress.DAILY_ROOT, "00-DailyNote")
        self.assertEqual(knowledge_note_policy.KNOWLEDGE_ROOT, "11-Knowledge")
        self.assertEqual(knowledge_index.KNOWLEDGE_ROOT, "11-Knowledge")
        self.assertEqual((context_bundle.KNOWLEDGE_ROOT, context_bundle.PROJECT_ROOT),
                         ("11-Knowledge", "10-Project"))
        self.assertEqual((ai_input_planner.KNOWLEDGE_ROOT, ai_input_planner.PROJECT_ROOT),
                         ("11-Knowledge", "10-Project"))
        self.assertEqual(semantic_corpus.SOURCE_ROOTS,
                         ("00-DailyNote", "05-Idea", "10-Project", "11-Knowledge"))
        self.assertEqual(github_project_watcher.WatcherConfig.__dataclass_fields__[
            "project_folder"].default, "10-Project")

    def test_no_new_hard_coded_role_folders(self) -> None:
        unexpected = []
        for path in sorted(SRC.rglob("*.py")):
            for literal in _role_literals(path):
                if not _allowed(path.stem, literal):
                    match = _ROLE_NAME.search(literal)
                    excerpt = literal[max(0, match.start() - 40):match.end() + 20]
                    unexpected.append(f"{path.name}: ...{excerpt!r}...")
        self.assertEqual(unexpected, [], "read the folder from vault_layout.role_folder()")

    def test_allow_list_has_no_stale_entries(self) -> None:
        literals = {p.stem: _role_literals(p) for p in SRC.rglob("*.py")}
        stale = sorted((m, frag) for m, frag in _ALLOWED_LITERALS
                       if not any(frag in lit for lit in literals.get(m, ())))
        stale += sorted((m, lit) for m, lit in _ALLOWED_EXACT if lit not in literals.get(m, ()))
        self.assertEqual(stale, [])

    def test_guard_sees_paths_hidden_in_messages_and_bytes(self) -> None:
        import tempfile
        source = (
            'raise ValueError("must be below 10-Project")\n'
            'raise ValueError(join("11-Knowledge/x"))\n'
            'MARK = b"98-System/x"\n'
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.py"
            path.write_text(source, encoding="utf-8")
            self.assertEqual(_role_literals(path), {"11-Knowledge/x", "98-System/x"})

    def test_project_path_regexes_match_as_before(self) -> None:
        from obsidian_automation import github_b3_watcher_bridge, github_project_watch_view
        view = github_project_watch_view._PATH
        self.assertTrue(view.fullmatch("10-Project/A/B.md"))
        for bad in ("10XProject/A.md", "11-Knowledge/A.md", "10-Project/A.txt"):
            self.assertIsNone(view.fullmatch(bad))
        bridge = github_b3_watcher_bridge._METADATA_WARNING
        self.assertTrue(bridge.fullmatch("10-Project/A/B.md: invalid github_repo: x"))
        self.assertIsNone(bridge.fullmatch("10XProject/A/B.md: invalid github_repo: x"))


if __name__ == "__main__":
    unittest.main()
