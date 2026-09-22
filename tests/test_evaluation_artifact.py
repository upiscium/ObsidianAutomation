from __future__ import annotations

import json
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

import pytest

import obsidian_automation.evaluation_artifact as evaluation_module

from obsidian_automation.artifact_lifecycle import (
    ArtifactLifecycleError,
    sha256_bytes,
    store_untrusted_proposal,
)
from obsidian_automation.context_bundle import build_context_bundle, store_context_bundle
from obsidian_automation.evaluation_artifact import (
    EVALUATION_CONTEXT_STAGE,
    EVALUATION_REQUEST_STAGE,
    EVALUATION_STAGE,
    EvaluationAssessment,
    EvaluationModelMetadata,
    EvaluationRecord,
    EvaluatorMetadata,
    build_evaluation_context,
    build_evaluation_record,
    create_evaluation_request,
    load_evaluation_context,
    load_evaluation_record,
    load_evaluation_request,
    parse_evaluation_context,
    parse_evaluation_record,
    store_evaluation_context,
    store_evaluation_record,
)
from obsidian_automation.evaluator_conflict import ConsistencyConflict
from obsidian_automation.generation_artifact import build_generation_record, store_generation_record
from obsidian_automation.knowledge_index import build_knowledge_index, store_knowledge_index
from obsidian_automation.knowledge_validator import validate_proposal


def _knowledge_note(body: str, *, category: str = "manual") -> str:
    return (
        "---\n"
        "type: knowledge-note\n"
        "status: active\n"
        f"category: {category}\n"
        "maturity: draft\n"
        "source_type: self\n"
        "---\n\n"
        f"{body}\n"
    )


def _proposal(target: str, body: str) -> bytes:
    payload = {
        "contract_version": 1,
        "operation": "create_note",
        "mutation_id": "evaluation-test",
        "target": {"path": target},
        "content": _knowledge_note(body),
    }
    return (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def _roots(tmp_path: Path) -> tuple[Path, Path]:
    vault = tmp_path / "vault"
    knowledge = vault / "11-Knowledge"
    knowledge.mkdir(parents=True)
    state = tmp_path / "state"
    state.mkdir()
    (state / "24-Locks" / "read-view").mkdir(parents=True)
    for stage in (
        "04-Index",
        "05-Context",
        EVALUATION_REQUEST_STAGE,
        EVALUATION_CONTEXT_STAGE,
        EVALUATION_STAGE,
    ):
        (state / stage).mkdir()
    return vault, state


def _accepted_fixture(tmp_path: Path) -> tuple[Path, Path, str, str]:
    vault, state = _roots(tmp_path)
    existing = vault / "11-Knowledge" / "Nextcloud+RemotelySaveでObsidianVaultを共有する方法.md"
    existing.write_text(
        _knowledge_note(
            "# NextcloudとObsidian\n\nRemotelySave と WebDAV を使って Obsidian Vault を共有する。"
        ),
        encoding="utf-8",
    )
    proposal_sha, _ = store_untrusted_proposal(
        state,
        _proposal(
            "11-Knowledge/Nextcloud_RemotelySaveでObsidianVaultを共有する方法.md",
            "# 概要\n\nNextcloud の WebDAV と RemotelySave で Obsidian Vault を共有する方法。",
        ),
    )
    validation = validate_proposal(state, vault, proposal_sha)
    assert validation["result"] == "accepted"
    mutation_sha = validation["mutation_sha256"]
    assert isinstance(mutation_sha, str)
    return vault, state, proposal_sha, mutation_sha


def test_evaluation_request_is_deterministic_and_bound_to_accepted_validation(tmp_path: Path) -> None:
    _, state, proposal_sha, mutation_sha = _accepted_fixture(tmp_path)

    first_sha, first_path, first = create_evaluation_request(state, proposal_sha)
    second_sha, second_path, second = create_evaluation_request(state, proposal_sha)

    assert (first_sha, first_path, first) == (second_sha, second_path, second)
    assert first.proposal_sha256 == proposal_sha
    assert first.mutation_sha256 == mutation_sha
    assert "Nextcloud_RemotelySave" in first.query
    assert "WebDAV" in first.query
    assert load_evaluation_request(state, first_sha) == first


def test_evaluation_request_rejects_unvalidated_proposal(tmp_path: Path) -> None:
    _, state = _roots(tmp_path)
    proposal_sha, _ = store_untrusted_proposal(
        state,
        _proposal("11-Knowledge/unvalidated.md", "# Test\n\nnot validated"),
    )
    with pytest.raises(ArtifactLifecycleError, match="cannot safely open artifact"):
        create_evaluation_request(state, proposal_sha)


def test_reader_builds_recall_biased_context_with_existing_duplicate_candidate(tmp_path: Path) -> None:
    vault, state, proposal_sha, mutation_sha = _accepted_fixture(tmp_path)
    request_sha, _, request = create_evaluation_request(state, proposal_sha)
    index = build_knowledge_index(vault)
    index_sha, _ = store_knowledge_index(state, index)

    context = build_evaluation_context(
        state,
        vault,
        request_sha256=request_sha,
        index_sha256=index_sha,
        created_at="2026-08-24T00:00:00Z",
    )
    context_sha, context_path = store_evaluation_context(state, context)

    assert context_path == state / EVALUATION_CONTEXT_STAGE / f"{context_sha}.context.json"
    assert context.proposal_sha256 == proposal_sha
    assert context.mutation_sha256 == mutation_sha
    assert context.query == request.query
    assert context.candidates
    assert context.candidates[0].path.endswith("Nextcloud+RemotelySaveでObsidianVaultを共有する方法.md")
    assert load_evaluation_context(state, context_sha) == context


@pytest.mark.parametrize(
    "candidate_path",
    ["11-Knowledge/../secret.md", "11-Knowledge\\secret.md"],
)
def test_evaluation_context_rejects_noncanonical_candidate_paths(candidate_path: str) -> None:
    content = "# Candidate\n"
    value = {
        "record_version": 1,
        "request_sha256": "a" * 64,
        "proposal_sha256": "b" * 64,
        "mutation_sha256": "c" * 64,
        "query": "candidate",
        "created_at": "2026-08-24T00:00:00Z",
        "selection_policy": {"version": "bm25-topk-recall-v0", "top_k": 5},
        "candidates": [
            {
                "path": candidate_path,
                "content_sha256": sha256_bytes(content.encode()),
                "score": "1",
                "content": content,
            }
        ],
    }

    with pytest.raises(ArtifactLifecycleError, match="candidate path"):
        parse_evaluation_context(
            (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        )


def test_evaluation_record_binds_generation_validation_and_evaluation_context(tmp_path: Path) -> None:
    vault, state, proposal_sha, mutation_sha = _accepted_fixture(tmp_path)
    request_sha, _, _ = create_evaluation_request(state, proposal_sha)
    index_sha, _ = store_knowledge_index(state, build_knowledge_index(vault))
    evaluation_context = build_evaluation_context(
        state,
        vault,
        request_sha256=request_sha,
        index_sha256=index_sha,
        created_at="2026-08-24T00:00:00Z",
    )
    evaluation_context_sha, _ = store_evaluation_context(state, evaluation_context)

    original_context = build_context_bundle(
        vault,
        query="Nextcloud Obsidian Vault 共有",
        source_paths=["11-Knowledge/Nextcloud+RemotelySaveでObsidianVaultを共有する方法.md"],
        created_at="2026-08-24T00:00:00Z",
    )
    original_context_sha, _ = store_context_bundle(state, original_context)
    generation = build_generation_record(
        state,
        context_sha256=original_context_sha,
        proposal_sha256=proposal_sha,
        implementation_revision="a" * 40,
        prompt_template_version="knowledge-note-generator-v0",
        prompt_template_sha256="b" * 64,
        model_provider="ollama",
        model_identifier="gemma4:12b",
        model_revision="c" * 64,
        model_config={"temperature": 0},
        generated_at="2026-08-24T00:01:00Z",
    )
    generation_sha, _ = store_generation_record(state, generation)

    record = build_evaluation_record(
        state,
        proposal_sha256=proposal_sha,
        mutation_sha256=mutation_sha,
        generation_sha256=generation_sha,
        evaluation_context_sha256=evaluation_context_sha,
        implementation_revision="d" * 40,
        prompt_template_version="knowledge-note-evaluator-v0",
        prompt_template_sha256="e" * 64,
        model_provider="ollama",
        model_identifier="gemma4:12b",
        model_revision="c" * 64,
        model_config={"temperature": 0},
        groundedness="pass",
        redundancy="likely",
        consistency="concern",
        recommendation="do_not_proceed",
        findings=["既存Knowledge Noteと実質的に重複している。"],
        conflicts=[
            ConsistencyConflict(
                proposal_claim="WebDAV synchronization is required.",
                candidate_claim="The note uses local-only storage.",
                incompatibility="The procedures cannot both be followed in the same setup.",
                candidate_path="11-Knowledge/Nextcloud+RemotelySaveでObsidianVaultを共有する方法.md",
            )
        ],
        evaluated_at="2026-08-24T00:02:00Z",
    )
    evaluation_sha, path = store_evaluation_record(state, record)

    assert path == state / EVALUATION_STAGE / f"{evaluation_sha}.evaluation.json"
    loaded = load_evaluation_record(state, evaluation_sha)
    assert loaded.record_version == 2
    assert loaded.assessment.redundancy == "likely"
    assert loaded.assessment.recommendation == "do_not_proceed"
    assert loaded.proposal_sha256 == proposal_sha
    assert loaded.assessment.conflicts == (
        ConsistencyConflict(
            proposal_claim="WebDAV synchronization is required.",
            candidate_claim="The note uses local-only storage.",
            incompatibility="The procedures cannot both be followed in the same setup.",
            candidate_path="11-Knowledge/Nextcloud+RemotelySaveでObsidianVaultを共有する方法.md",
        ),
    )


def test_evaluation_record_cannot_cross_bind_another_mutation(tmp_path: Path) -> None:
    vault, state, proposal_sha, _ = _accepted_fixture(tmp_path)
    request_sha, _, _ = create_evaluation_request(state, proposal_sha)
    index_sha, _ = store_knowledge_index(state, build_knowledge_index(vault))
    context = build_evaluation_context(
        state,
        vault,
        request_sha256=request_sha,
        index_sha256=index_sha,
        created_at="2026-08-24T00:00:00Z",
    )
    context_sha, _ = store_evaluation_context(state, context)

    with pytest.raises(ArtifactLifecycleError, match="accepted validation"):
        build_evaluation_record(
            state,
            proposal_sha256=proposal_sha,
            mutation_sha256="f" * 64,
            generation_sha256="a" * 64,
            evaluation_context_sha256=context_sha,
            implementation_revision="d" * 40,
            prompt_template_version="knowledge-note-evaluator-v0",
            prompt_template_sha256="e" * 64,
            model_provider="ollama",
            model_identifier="gemma4:12b",
            model_revision="c" * 64,
            model_config={},
            groundedness="unknown",
            redundancy="possible",
            consistency="unknown",
            recommendation="manual_review",
            findings=[],
        )


def test_evaluation_context_holds_read_view_only_while_touching_mirror(
    monkeypatch,
    tmp_path: Path,
) -> None:
    vault, state, proposal_sha, _ = _accepted_fixture(tmp_path)
    request_sha, _, _ = create_evaluation_request(state, proposal_sha)
    index_sha, _ = store_knowledge_index(state, build_knowledge_index(vault))

    held = False
    original_verify = evaluation_module.verify_index_current
    original_build = evaluation_module.build_context_bundle

    @contextmanager
    def fake_lock(observed_root):
        nonlocal held
        assert observed_root == state
        held = True
        try:
            yield state / "24-Locks" / "read-view" / "mirror-read.lock"
        finally:
            held = False

    def checked_verify(*args, **kwargs):
        assert held is True
        return original_verify(*args, **kwargs)

    def checked_build(*args, **kwargs):
        assert held is True
        return original_build(*args, **kwargs)

    monkeypatch.setattr(evaluation_module, "mirror_read_lock", fake_lock)
    monkeypatch.setattr(evaluation_module, "verify_index_current", checked_verify)
    monkeypatch.setattr(evaluation_module, "build_context_bundle", checked_build)

    context = evaluation_module.build_evaluation_context(
        state,
        vault,
        request_sha256=request_sha,
        index_sha256=index_sha,
        created_at="2026-09-19T00:00:00Z",
    )

    assert context.candidates
    assert held is False
    # Persisting the immutable derived artifact is intentionally outside the
    # mirror lock; no mirror bytes are read at this point.
    evaluation_module.store_evaluation_context(state, context)
    assert held is False


def _record_payload(
    *,
    record_version: int = 2,
    groundedness: str = "pass",
    redundancy: str = "none",
    consistency: str = "concern",
    recommendation: str = "do_not_proceed",
    conflicts: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    assessment: dict[str, object] = {
        "groundedness": groundedness,
        "redundancy": redundancy,
        "consistency": consistency,
        "recommendation": recommendation,
        "findings": [" legacy evidence "],
    }
    if record_version == 2:
        assessment["conflicts"] = (
            conflicts
            if conflicts is not None
            else [
                {
                    "candidate_path": "11-Knowledge/existing.md",
                    "proposal_claim": "Proposal claim.",
                    "candidate_claim": "Candidate claim.",
                    "incompatibility": "The claims cannot both hold.",
                }
            ]
        )
    return {
        "record_version": record_version,
        "proposal_sha256": "a" * 64,
        "mutation_sha256": "b" * 64,
        "generation_sha256": "c" * 64,
        "evaluation_context_sha256": "d" * 64,
        "evaluator": {
            "implementation_revision": "e" * 40,
            "prompt_template_version": "evaluation-v1",
            "prompt_template_sha256": "f" * 64,
        },
        "model": {
            "provider": "ollama",
            "identifier": "model",
            "revision": "1" * 64,
        },
        "model_config": {"nested": {"temperature": 0}},
        "assessment": assessment,
        "evaluated_at": "2026-09-20T00:00:00Z",
    }


def _record_bytes(value: dict[str, object]) -> bytes:
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode()


def test_historical_v1_record_round_trips_without_v2_shape() -> None:
    fixture = _record_bytes(
        _record_payload(
            record_version=1,
            consistency="concern",
            recommendation="manual_review",
        )
    )

    parsed = parse_evaluation_record(fixture)

    assert parsed.record_version == 1
    assert parsed.assessment.conflicts == ()
    assert parsed.assessment.findings == (" legacy evidence ",)
    assert json.loads(parsed.to_json_bytes())["assessment"].keys() == {
        "groundedness",
        "redundancy",
        "consistency",
        "recommendation",
        "findings",
    }
    assert parsed.to_json_bytes() == fixture


def test_v2_conflicts_round_trip_with_exact_evidence_shape() -> None:
    fixture = _record_bytes(_record_payload())

    parsed = parse_evaluation_record(fixture)
    value = json.loads(parsed.to_json_bytes())

    assert parsed.record_version == 2
    assert parsed.assessment.conflicts == (
        ConsistencyConflict(
            candidate_path="11-Knowledge/existing.md",
            proposal_claim="Proposal claim.",
            candidate_claim="Candidate claim.",
            incompatibility="The claims cannot both hold.",
        ),
    )
    assert value["assessment"]["conflicts"] == [
        {
            "candidate_path": "11-Knowledge/existing.md",
            "proposal_claim": "Proposal claim.",
            "candidate_claim": "Candidate claim.",
            "incompatibility": "The claims cannot both hold.",
        }
    ]


def test_v2_record_rejects_malformed_conflicts_and_inconsistent_evidence() -> None:
    base = _record_payload()
    malformed = []

    extra = deepcopy(base)
    extra["assessment"]["conflicts"][0]["extra"] = "nope"  # type: ignore[index]
    malformed.append(extra)

    oversized = deepcopy(base)
    oversized["assessment"]["conflicts"][0]["proposal_claim"] = "x" * 1001  # type: ignore[index]
    malformed.append(oversized)

    duplicate = deepcopy(base)
    duplicate["assessment"]["conflicts"] = [  # type: ignore[index]
        duplicate["assessment"]["conflicts"][0],  # type: ignore[index]
        duplicate["assessment"]["conflicts"][0],  # type: ignore[index]
    ]
    malformed.append(duplicate)

    unsafe_path = deepcopy(base)
    unsafe_path["assessment"]["conflicts"][0]["candidate_path"] = "11-Knowledge/../secret.md"  # type: ignore[index]
    malformed.append(unsafe_path)

    invalid_utf8 = deepcopy(base)
    invalid_utf8["assessment"]["conflicts"][0]["candidate_claim"] = "\ud800"  # type: ignore[index]
    malformed.append(invalid_utf8)

    for value in malformed:
        with pytest.raises(ArtifactLifecycleError):
            parse_evaluation_record(_record_bytes(value))

    no_conflict_concern = _record_payload(conflicts=[])
    with pytest.raises(ArtifactLifecycleError, match="concern"):
        parse_evaluation_record(_record_bytes(no_conflict_concern))

    conflict_pass = _record_payload(
        groundedness="pass",
        redundancy="none",
        consistency="pass",
        recommendation="proceed",
    )
    with pytest.raises(ArtifactLifecycleError, match="pass or unknown"):
        parse_evaluation_record(_record_bytes(conflict_pass))

    wrong_recommendation = _record_payload(recommendation="manual_review")
    with pytest.raises(ArtifactLifecycleError, match="conservative triad"):
        parse_evaluation_record(_record_bytes(wrong_recommendation))


def test_evaluation_record_model_config_is_nested_immutable_and_canonical() -> None:
    config = {"z": {"items": [{"value": 1}]}, "a": 0}
    record = EvaluationRecord(
        proposal_sha256="a" * 64,
        mutation_sha256="b" * 64,
        generation_sha256="c" * 64,
        evaluation_context_sha256="d" * 64,
        evaluator=EvaluatorMetadata("e" * 40, "evaluation-v1", "f" * 64),
        model=EvaluationModelMetadata("ollama", "model", "1" * 64),
        model_config=config,
        assessment=EvaluationAssessment("pass", "none", "pass", "proceed", ()),
        evaluated_at="2026-09-20T00:00:00Z",
    )
    before = record.to_json_bytes()

    config["z"]["items"][0]["value"] = 2

    assert record.to_json_bytes() == before
    with pytest.raises(TypeError):
        record.model_config["z"]["items"][0]["value"] = 3  # type: ignore[index]
    assert before == _record_bytes(json.loads(before))
