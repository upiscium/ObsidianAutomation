from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from obsidian_automation.core_promotion import (
    PromotionError,
    build_promotion_plan,
    parse_promotion_plan,
    promotion_plan_sha256,
    reconcile_promotion_plan,
    verify_plan_against_core,
)


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _repo(tmp_path: Path) -> tuple[Path, Path, str]:
    repo = tmp_path / "core"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Promotion Test")
    _git(repo, "config", "user.email", "promotion@example.invalid")

    (repo / "98-System/01-script").mkdir(parents=True)
    (repo / "98-System/01-script/task.js").write_text("const version = 1;\n", encoding="utf-8")
    (repo / "98-System/obsolete.md").write_text("obsolete\n", encoding="utf-8")
    (repo / "README.md").write_text("repo owned\n", encoding="utf-8")
    base = _commit(repo, "base")

    config = tmp_path / "public.toml"
    config.write_text(
        """version = 1
strict_missing = true
include = ["98-System/**", "Dashboard.md", ".obsidian/snippets/**"]
exclude = ["98-System/.rclone-bisync/**"]
repository_owned = [".github/**", ".gitignore", "README.md", "LICENSE"]
""",
        encoding="utf-8",
    )
    return repo, config, base


def _head_with_changes(repo: Path) -> str:
    (repo / "98-System/01-script/task.js").write_text("const version = 2;\n", encoding="utf-8")
    (repo / "98-System/new.css").write_text(".new { display: block; }\n", encoding="utf-8")
    (repo / "98-System/obsolete.md").unlink()
    (repo / "README.md").write_text("repo-owned change must not round-trip\n", encoding="utf-8")
    return _commit(repo, "core PR merge")


def test_build_plan_contains_only_public_projection_managed_changes(tmp_path: Path) -> None:
    repo, config, base = _repo(tmp_path)
    head = _head_with_changes(repo)

    plan = build_promotion_plan(repo, base_ref=base, head_ref=head, config_path=config)

    assert plan.base_commit == base
    assert plan.head_commit == head
    assert [(item.action, item.path) for item in plan.changes] == [
        ("update", "98-System/01-script/task.js"),
        ("create", "98-System/new.css"),
        ("delete", "98-System/obsolete.md"),
    ]
    assert all(item.path != "README.md" for item in plan.changes)
    assert parse_promotion_plan(plan.to_json_bytes()) == plan
    assert len(promotion_plan_sha256(plan)) == 64
    verify_plan_against_core(plan, repo, config_path=config)


def test_reconcile_marks_apply_already_applied_and_conflict(tmp_path: Path) -> None:
    repo, config, base = _repo(tmp_path)
    head = _head_with_changes(repo)
    plan = build_promotion_plan(repo, base_ref=base, head_ref=head, config_path=config)

    vault = tmp_path / "vault"
    (vault / "98-System/01-script").mkdir(parents=True)
    (vault / "98-System/01-script/task.js").write_text("const version = 1;\n", encoding="utf-8")
    # Create is already present with exact head bytes: typical Vault-origin publication replay.
    (vault / "98-System/new.css").write_text(".new { display: block; }\n", encoding="utf-8")
    # Delete target diverged after the Core base snapshot.
    (vault / "98-System/obsolete.md").write_text("locally changed\n", encoding="utf-8")

    result = reconcile_promotion_plan(plan, vault)
    by_path = {item.path: item.disposition for item in result.observations}

    assert by_path["98-System/01-script/task.js"] == "apply"
    assert by_path["98-System/new.css"] == "already_applied"
    assert by_path["98-System/obsolete.md"] == "conflict"
    assert result.pending_count == 1
    assert result.has_conflict is True


def test_vault_originated_publication_is_idempotent(tmp_path: Path) -> None:
    repo, config, base = _repo(tmp_path)
    head = _head_with_changes(repo)
    plan = build_promotion_plan(repo, base_ref=base, head_ref=head, config_path=config)

    vault = tmp_path / "vault"
    (vault / "98-System/01-script").mkdir(parents=True)
    (vault / "98-System/01-script/task.js").write_text("const version = 2;\n", encoding="utf-8")
    (vault / "98-System/new.css").write_text(".new { display: block; }\n", encoding="utf-8")
    # obsolete.md is already absent.

    result = reconcile_promotion_plan(plan, vault)

    assert result.has_conflict is False
    assert result.pending_count == 0
    assert {item.disposition for item in result.observations} == {"already_applied"}


def test_plan_fails_if_managed_path_becomes_symlink(tmp_path: Path) -> None:
    repo, config, base = _repo(tmp_path)
    target = repo / "98-System/01-script/task.js"
    target.unlink()
    os.symlink("../../../README.md", target)
    head = _commit(repo, "unsafe symlink")

    with pytest.raises(PromotionError, match="regular files only"):
        build_promotion_plan(repo, base_ref=base, head_ref=head, config_path=config)


def test_plan_requires_ancestor_checkpoint(tmp_path: Path) -> None:
    repo, config, base = _repo(tmp_path)
    _head_with_changes(repo)
    _git(repo, "checkout", "--detach", base)
    (repo / "98-System/branch.md").write_text("branch\n", encoding="utf-8")
    other = _commit(repo, "other branch")
    _git(repo, "checkout", "main")
    head = _git(repo, "rev-parse", "HEAD")

    with pytest.raises(PromotionError, match="not an ancestor"):
        build_promotion_plan(repo, base_ref=other, head_ref=head, config_path=config)


def test_plan_parser_rejects_unknown_property_and_duplicate_json_key(tmp_path: Path) -> None:
    repo, config, base = _repo(tmp_path)
    head = _head_with_changes(repo)
    plan = build_promotion_plan(repo, base_ref=base, head_ref=head, config_path=config)
    value = json.loads(plan.to_json_bytes())
    value["unexpected"] = True

    with pytest.raises(PromotionError, match="properties"):
        parse_promotion_plan(json.dumps(value).encode())

    duplicate = b'{"record_version":1,"record_version":1}'
    with pytest.raises(PromotionError):
        parse_promotion_plan(duplicate)


def test_reconcile_rejects_symlink_in_vault_snapshot(tmp_path: Path) -> None:
    repo, config, base = _repo(tmp_path)
    head = _head_with_changes(repo)
    plan = build_promotion_plan(repo, base_ref=base, head_ref=head, config_path=config)

    vault = tmp_path / "vault"
    vault.mkdir()
    os.symlink(tmp_path, vault / "98-System")

    with pytest.raises(PromotionError, match="symlink"):
        reconcile_promotion_plan(plan, vault)
