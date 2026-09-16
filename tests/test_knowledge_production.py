from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import obsidian_automation.knowledge_production as production
from obsidian_automation.canonical_mutation import CreateNoteMutation


def _mutation(*, status: str = "active") -> CreateNoteMutation:
    return CreateNoteMutation(
        contract_version=1,
        operation="create_note",
        mutation_id="knowledge-production-test",
        target_path="11-Knowledge/test.md",
        content=(
            "---\n"
            "type: knowledge-note\n"
            f"status: {status}\n"
            "category: explanation\n"
            "maturity: draft\n"
            "source_type: self\n"
            "---\n"
            "# About\n"
        ),
    )


def test_executor_cli_hard_codes_knowledge_root_and_policy(monkeypatch, capsys) -> None:
    captured = {}

    def fake_advance(ai_root, vault_root, mutation_sha256, *, allowed_roots, note_policy):
        captured["allowed_roots"] = allowed_roots
        captured["note_policy"] = note_policy
        return SimpleNamespace(status="transport_pending", reason=None)

    monkeypatch.setattr(production, "advance_production_executor", fake_advance)

    rc = production.executor_main(
        [
            "--ai-root",
            "/state",
            "--vault-root",
            "/vault",
            "--mutation-sha256",
            "a" * 64,
        ]
    )

    assert rc == 0
    assert captured["allowed_roots"] == ["11-Knowledge"]
    assert captured["note_policy"] is production.validate_knowledge_note_v0
    assert '"status": "transport_pending"' in capsys.readouterr().out


def test_worker_rejects_policy_violation_before_lock_credential_or_transport(
    monkeypatch,
    tmp_path: Path,
) -> None:
    mutation = _mutation(status="archived")
    transported = False
    credential_read = False
    lock_entered = False

    monkeypatch.setattr(
        production,
        "_load_context",
        lambda *_args, **_kwargs: (b"mutation", mutation, b"review", object()),
    )

    @contextmanager
    def fake_lock(_ai_root):
        nonlocal lock_entered
        lock_entered = True
        yield

    def fake_read_password(_path):
        nonlocal credential_read
        credential_read = True
        return "secret"

    def fake_process(*_args, **_kwargs):
        nonlocal transported
        transported = True
        raise AssertionError("transport must not run")

    monkeypatch.setattr(production, "canonical_io_lock", fake_lock)
    monkeypatch.setattr(production, "_read_password", fake_read_password)
    monkeypatch.setattr(production, "process_transport_request", fake_process)

    rc = production.worker_main(
        [
            "--ai-root",
            str(tmp_path),
            "--mutation-sha256",
            "b" * 64,
            "--base-url",
            "https://nextcloud.example/remote.php/dav/files/ai/ObsidianVault",
            "--username",
            "obsidian-ai-sync",
            "--password-file",
            str(tmp_path / "password"),
        ]
    )

    assert rc == 2
    assert lock_entered is False
    assert credential_read is False
    assert transported is False


def test_worker_hard_codes_knowledge_root_inside_canonical_io_lock(
    monkeypatch,
    tmp_path: Path,
) -> None:
    mutation = _mutation()
    captured = {}
    lock_held = False

    monkeypatch.setattr(
        production,
        "_load_context",
        lambda *_args, **_kwargs: (b"mutation", mutation, b"review", object()),
    )

    @contextmanager
    def fake_lock(ai_root):
        nonlocal lock_held
        assert ai_root == tmp_path
        assert lock_held is False
        lock_held = True
        try:
            yield
        finally:
            lock_held = False

    def fake_read_password(_path):
        assert lock_held is True
        return "secret"

    def fake_process(ai_root, mutation_sha256, *, allowed_roots, **kwargs):
        assert lock_held is True
        captured["allowed_roots"] = allowed_roots
        captured["mutation_sha256"] = mutation_sha256
        return SimpleNamespace(
            result="created_verified",
            target_path=mutation.target_path,
            expected_content_sha256="c" * 64,
        )

    monkeypatch.setattr(production, "canonical_io_lock", fake_lock)
    monkeypatch.setattr(production, "_read_password", fake_read_password)
    monkeypatch.setattr(production, "process_transport_request", fake_process)

    rc = production.worker_main(
        [
            "--ai-root",
            str(tmp_path),
            "--mutation-sha256",
            "c" * 64,
            "--base-url",
            "https://nextcloud.example/remote.php/dav/files/ai/ObsidianVault",
            "--username",
            "obsidian-ai-sync",
            "--password-file",
            str(tmp_path / "password"),
        ]
    )

    assert rc == 0
    assert lock_held is False
    assert captured["allowed_roots"] == ["11-Knowledge"]
    assert captured["mutation_sha256"] == "c" * 64
