from __future__ import annotations

import json
from pathlib import Path

import pytest

from obsidian_automation.artifact_lifecycle import (
    ArtifactLifecycleError,
    parse_validation_record,
    store_untrusted_proposal,
)
from obsidian_automation.knowledge_validator import validate_proposal


def _proposal(target: str, *, status: str = "active") -> bytes:
    content = (
        "---\n"
        "type: knowledge-note\n"
        f"status: {status}\n"
        "category: summary\n"
        "maturity: draft\n"
        "source_type: self\n"
        "---\n"
        "# About\n\n"
        "Knowledge validator test.\n"
    )
    payload = {
        "contract_version": 1,
        "operation": "create_note",
        "mutation_id": f"validator-{status}",
        "target": {"path": target},
        "content": content,
    }
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode()


def _roots(tmp_path: Path) -> tuple[Path, Path]:
    vault = tmp_path / "vault"
    (vault / "11-Knowledge").mkdir(parents=True)
    state = tmp_path / "state"
    state.mkdir()
    return vault, state


def test_accepts_and_persists_exact_validated_artifact(tmp_path: Path) -> None:
    vault, state = _roots(tmp_path)
    proposal_sha, _ = store_untrusted_proposal(
        state,
        _proposal("11-Knowledge/accepted.md"),
    )

    first = validate_proposal(state, vault, proposal_sha)
    second = validate_proposal(state, vault, proposal_sha)

    assert first["result"] == "accepted"
    mutation_sha = first["mutation_sha256"]
    assert isinstance(mutation_sha, str)
    assert (state / "10-Validation" / f"{mutation_sha}.mutation.json").is_file()
    assert second["result"] == "accepted"
    assert second["mutation_sha256"] == mutation_sha
    assert second["reused"] is True

    record_path = state / "10-Validation" / f"{proposal_sha}.validation.json"
    record = parse_validation_record(record_path.read_bytes())
    assert record.result == "accepted"
    assert record.mutation_sha256 == mutation_sha


def test_rejection_is_a_durable_normal_outcome_and_is_idempotent(tmp_path: Path) -> None:
    vault, state = _roots(tmp_path)
    proposal_sha, _ = store_untrusted_proposal(
        state,
        _proposal("11-Knowledge/rejected.md", status="archived"),
    )

    first = validate_proposal(state, vault, proposal_sha)
    second = validate_proposal(state, vault, proposal_sha)

    assert first["result"] == "rejected"
    assert first["mutation_sha256"] is None
    assert first["reused"] is False
    assert "status must be active" in str(first["reason"])

    assert second["result"] == "rejected"
    assert second["reused"] is True
    assert second["reason"] == first["reason"]


def test_reuse_fails_closed_if_source_proposal_disappears(tmp_path: Path) -> None:
    vault, state = _roots(tmp_path)
    proposal_sha, proposal_path = store_untrusted_proposal(
        state,
        _proposal("11-Knowledge/missing-source.md"),
    )
    validate_proposal(state, vault, proposal_sha)
    proposal_path.unlink()

    with pytest.raises(ArtifactLifecycleError):
        validate_proposal(state, vault, proposal_sha)


def test_rejects_targets_outside_knowledge_root(tmp_path: Path) -> None:
    vault, state = _roots(tmp_path)
    (vault / "98-System").mkdir()
    proposal_sha, _ = store_untrusted_proposal(
        state,
        _proposal("98-System/not-knowledge.md"),
    )

    result = validate_proposal(state, vault, proposal_sha)

    assert result["result"] == "rejected"
    assert "outside deployment-policy-approved roots" in str(result["reason"])
