from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from obsidian_automation.canonical_mutation import (
    ApprovalRecord,
    MutationValidationError,
    execute_create_note,
    validate_create_note,
)


def _proposal(path: str = "11-Knowledge/example.md", content: str = "# Example\n") -> bytes:
    return json.dumps(
        {
            "contract_version": 1,
            "operation": "create_note",
            "mutation_id": "mutation-1",
            "target": {"path": path},
            "content": content,
        },
        ensure_ascii=False,
    ).encode("utf-8")


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / "11-Knowledge").mkdir(parents=True)
    (vault / "20-AI").mkdir()
    return vault


def test_valid_create_note_executes_and_returns_receipt(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    validated = validate_create_note(
        _proposal(content="# Exact\n"),
        vault_root=vault,
        allowed_roots=["11-Knowledge"],
    )
    approval = ApprovalRecord(approved=True, mutation_sha256=validated.mutation_sha256)

    receipt = execute_create_note(
        validated.artifact_bytes,
        approval=approval,
        vault_root=vault,
        allowed_roots=["11-Knowledge"],
    )

    target = vault / "11-Knowledge/example.md"
    assert target.read_text() == "# Exact\n"
    assert receipt.result == "success"
    assert receipt.mutation_id == "mutation-1"
    assert receipt.mutation_sha256 == validated.mutation_sha256
    assert receipt.target_path == "11-Knowledge/example.md"
    assert receipt.content_sha256 == hashlib.sha256(b"# Exact\n").hexdigest()
    assert json.loads(receipt.to_json_bytes())["result"] == "success"


def test_existing_target_is_rejected(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    target = vault / "11-Knowledge/example.md"
    target.write_text("existing\n")

    with pytest.raises(MutationValidationError, match="already exists|case-fold collision"):
        validate_create_note(
            _proposal(),
            vault_root=vault,
            allowed_roots=["11-Knowledge"],
        )

    assert target.read_text() == "existing\n"


@pytest.mark.parametrize(
    "path",
    [
        "../escape.md",
        "11-Knowledge/../escape.md",
        "11-Knowledge//escape.md",
        "11-Knowledge/./escape.md",
        r"11-Knowledge\escape.md",
        "/11-Knowledge/escape.md",
    ],
)
def test_unsafe_paths_are_rejected(tmp_path: Path, path: str) -> None:
    vault = _vault(tmp_path)

    with pytest.raises(MutationValidationError):
        validate_create_note(
            _proposal(path=path),
            vault_root=vault,
            allowed_roots=["11-Knowledge"],
        )


def test_unauthorized_root_is_rejected(tmp_path: Path) -> None:
    vault = _vault(tmp_path)

    with pytest.raises(MutationValidationError, match="outside"):
        validate_create_note(
            _proposal(path="20-AI/example.md"),
            vault_root=vault,
            allowed_roots=["11-Knowledge"],
        )


def test_symlink_parent_is_rejected(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    real = tmp_path / "real"
    real.mkdir()
    (vault / "11-Knowledge/link").symlink_to(real, target_is_directory=True)

    with pytest.raises(MutationValidationError, match="safe directory"):
        validate_create_note(
            _proposal(path="11-Knowledge/link/example.md"),
            vault_root=vault,
            allowed_roots=["11-Knowledge"],
        )


def test_approval_hash_mismatch_is_rejected_without_write(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    validated = validate_create_note(
        _proposal(),
        vault_root=vault,
        allowed_roots=["11-Knowledge"],
    )

    with pytest.raises(MutationValidationError, match="approval hash"):
        execute_create_note(
            validated.artifact_bytes,
            approval=ApprovalRecord(approved=True, mutation_sha256="0" * 64),
            vault_root=vault,
            allowed_roots=["11-Knowledge"],
        )

    assert not (vault / "11-Knowledge/example.md").exists()


def test_negative_or_missing_approval_is_rejected(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    validated = validate_create_note(
        _proposal(),
        vault_root=vault,
        allowed_roots=["11-Knowledge"],
    )

    for approval in [
        None,
        ApprovalRecord(approved=False, mutation_sha256=validated.mutation_sha256),
    ]:
        with pytest.raises(MutationValidationError, match="approval"):
            execute_create_note(
                validated.artifact_bytes,
                approval=approval,
                vault_root=vault,
                allowed_roots=["11-Knowledge"],
            )

    assert not (vault / "11-Knowledge/example.md").exists()


def test_casefold_collision_is_rejected(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    (vault / "11-Knowledge/Example.md").write_text("existing\n")

    with pytest.raises(MutationValidationError, match="case-fold"):
        validate_create_note(
            _proposal(path="11-Knowledge/example.md"),
            vault_root=vault,
            allowed_roots=["11-Knowledge"],
        )


def test_target_created_after_validation_is_rejected_at_effect_boundary(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    validated = validate_create_note(
        _proposal(),
        vault_root=vault,
        allowed_roots=["11-Knowledge"],
    )
    target = vault / "11-Knowledge/example.md"
    target.write_text("raced\n")

    with pytest.raises(MutationValidationError, match="already exists|case-fold collision"):
        execute_create_note(
            validated.artifact_bytes,
            approval=ApprovalRecord(approved=True, mutation_sha256=validated.mutation_sha256),
            vault_root=vault,
            allowed_roots=["11-Knowledge"],
        )

    assert target.read_text() == "raced\n"


def test_unknown_property_and_duplicate_property_are_rejected(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    unknown = json.loads(_proposal())
    unknown["overwrite"] = True

    with pytest.raises(MutationValidationError, match="unknown"):
        validate_create_note(
            json.dumps(unknown).encode(),
            vault_root=vault,
            allowed_roots=["11-Knowledge"],
        )

    duplicate = (
        '{"contract_version":1,"operation":"create_note","mutation_id":"a",'
        '"mutation_id":"b","target":{"path":"11-Knowledge/example.md"},'
        '"content":"x"}'
    ).encode()
    with pytest.raises(MutationValidationError, match="duplicate"):
        validate_create_note(
            duplicate,
            vault_root=vault,
            allowed_roots=["11-Knowledge"],
        )
