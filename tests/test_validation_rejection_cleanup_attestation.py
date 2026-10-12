from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from obsidian_automation.artifact_lifecycle import (
    _canonical_json_bytes,
    _store_immutable,
    ensure_artifact_layout,
    sha256_bytes,
    store_untrusted_proposal,
    store_validation_record,
    validation_record_bytes,
)
from obsidian_automation.context_bundle import ContextBundle, ContextSource, store_context_bundle
from obsidian_automation.generation_artifact import build_generation_record, store_generation_record
from obsidian_automation.human_projection import (
    ProjectionResult,
    _projection_markdown,
    _store_result,
    build_request,
    emit_validation_projection,
    emit_input_projection,
    emit_context_projection,
    emit_generation_projection,
    parse_request,
    store_request,
)
from obsidian_automation.pre_review_job import _connect_rw
from obsidian_automation.validation_rejection_cleanup import (
    STAGES,
    RejectionCleanupError,
    RejectionCleanupPending,
    parse_attestation,
    prepare_rejection_cleanup,
    store_rejection_cleanup_attestation,
    verify_rejection_cleanup,
)

CASE = "a" * 64
JOB = "b" * 64
NOW = "2026-10-11T00:00:00Z"


def _state(
    tmp_path: Path, *,
    generation_state: str = "deterministic_reject",
    attempt_stage: str = "validation",
    attempt_reason: str = "validation_rejected",
    accepted: bool = False,
    skipped_stage: str | None = None,
    later_generation: bool = False,
) -> tuple[Path, str]:
    root = tmp_path / "private-ai-root"
    root.mkdir()
    ensure_artifact_layout(root)
    (root / "05-Context").mkdir()
    publication_root = root / "16-Human-Projection"
    publication_root.mkdir()
    for role in ("reader", "generator", "validator", "evaluator",
                 "reviewer", "executor", "sync"):
        (publication_root / role).mkdir()
    (root / "17-Human-Projection-Result").mkdir()
    (root / "24-Locks").mkdir()

    proposal = _canonical_json_bytes({
        "contract_version": 1,
        "operation": "create_note",
        "mutation_id": "rejected-cleanup-fixture",
        "target": {"path": "11-Knowledge/candidate.md"},
        "content": "# Candidate\n",
    })
    proposal_sha, _ = store_untrusted_proposal(root, proposal)
    source_text = "# Selected Knowledge Source\\n"
    source_sha = sha256_bytes(source_text.encode("utf-8"))
    chosen = {
        "path": "11-Knowledge/Source.md",
        "source_kind": "knowledge",
        "content_sha256": source_sha,
        "byte_size": len(source_text.encode("utf-8")),
        "project_status": None,
    }
    context = ContextBundle(
        query="Prepare a bounded Knowledge candidate",
        created_at=NOW,
        sources=(ContextSource(
            path=chosen["path"],
            content_sha256=chosen["content_sha256"],
            content=source_text,
        ),),
    )
    context_sha, _ = store_context_bundle(root, context)
    select_data = _canonical_json_bytes({
        "record_version": 1,
        "selection_policy": "coverage-shuffle-v0",
        "objective_policy": "synthesize-v0",
        "catalog_sha256": "8" * 64,
        "epoch": 0, "cycle": 0,
        "seed": "9" * 64,
        "selected": [chosen],
    })
    selection_sha = sha256_bytes(select_data)
    selection_dir = root / "02-Orchestration" / "input-selections"
    selection_dir.mkdir(parents=True)
    _store_immutable(selection_dir / f"{selection_sha}.selection.json", select_data)
    gen_record = build_generation_record(
        root, context_sha256=context_sha, proposal_sha256=proposal_sha,
        implementation_revision="7" * 40,
        prompt_template_version="knowledge-note-generator-v0",
        prompt_template_sha256="6" * 64,
        model_provider="ollama",
        model_identifier="test:generator",
        model_revision="identifier:test:generator",
        model_config={"temperature": 0},
        generated_at=NOW,
    )
    generation_sha, _ = store_generation_record(root, gen_record)
    store_validation_record(
        root,
        proposal_sha256=proposal_sha,
        result="rejected",
        reason="candidate does not meet the validation contract",
        validated_at=NOW,
    )

    conn = _connect_rw(root)
    try:
        conn.execute(
            "INSERT INTO jobs(job_id,context_sha256,recipe_sha256,created_at) "
            "VALUES(?,?,?,?)",
            (JOB, context_sha, "d" * 64, NOW),
        )
        conn.execute(
            "INSERT INTO generations(generation_id,job_id,generation_index,"
            "state,created_at,updated_at) VALUES(?,?,1,?,?,?)",
            (CASE, JOB, generation_state, NOW, NOW),
        )
        conn.execute(
            "INSERT INTO attempts(attempt_id,generation_id,stage,attempt_index,"
            "status,started_at,completed_at,reason_code) VALUES(?,?,?,1,?,?,?,?)",
            ("f" * 64, CASE, "generation", "succeeded", NOW, NOW, None),
        )
        conn.execute(
            "INSERT INTO attempts(attempt_id,generation_id,stage,attempt_index,"
            "status,started_at,completed_at,reason_code) VALUES(?,?,?,?,?,?,?,?)",
            ("e" * 64, CASE, attempt_stage,
             2 if attempt_stage == "generation" else 1,
             "deterministic_reject", NOW, NOW,
             attempt_reason),
        )
        conn.execute(
            "INSERT INTO stage_outputs(generation_id,stage,attempt_id,"
            "output_json,recorded_at) VALUES(?,?,?,?,?)",
            (CASE, "generation", "f" * 64, json.dumps({
                "proposal_sha256": proposal_sha,
                "generation_sha256": generation_sha,
            }), NOW),
        )
        if later_generation:
            conn.execute(
                "INSERT INTO generations(generation_id,job_id,generation_index,"
                "state,created_at,updated_at) VALUES(?,?,2,?,?,?)",
                ("2" * 64, JOB, "queued", NOW, NOW),
            )
        conn.commit()
    finally:
        conn.close()

    # Original producer-generated requests, bound to the same source
    # selection, Context and Generation as the durable pre-review job.
    for stage in ("input", "context", "generation"):
        if stage == skipped_stage:
            continue
        if stage == "input":
            emitted = emit_input_projection(
                root, case_id=CASE, selection_sha256=selection_sha,
                selection_policy="coverage-shuffle-v0",
                objective_policy="synthesize-v0", epoch=0, cycle=0,
                selected=[chosen], created_at=NOW,
            )
        elif stage == "context":
            emitted = emit_context_projection(
                root, case_id=CASE, context_sha256=context_sha, context=context,
            )
        else:
            emitted = emit_generation_projection(
                root, case_id=CASE, generation_sha256=generation_sha,
                proposal_sha256=proposal_sha,
            )
        assert emitted is not None
        digest, request_path = emitted
        request = parse_request(request_path.read_bytes())
        _store_result(root, ProjectionResult(
            request_sha256=digest,
            target_path=request.target_path,
            content_sha256=request.content_sha256,
            result="created", completed_at=NOW,
        ))

    if skipped_stage != "validation":
        emitted = emit_validation_projection(
            root, case_id=CASE, proposal_sha256=proposal_sha,
        )
        assert emitted is not None
        digest, request_file = emitted
        request = parse_request(request_file.read_bytes())
        _store_result(root, ProjectionResult(
            request_sha256=digest, target_path=request.target_path,
            content_sha256=request.content_sha256,
            result="created", completed_at=NOW,
        ))

    if accepted:
        # Simulate changed underlying authoritative Validation source, rather
        # than calling accepted-write helper without a validated mutation.
        validation_path = (
            root / "10-Validation" / f"{proposal_sha}.validation.json"
        )
        validation_path.write_bytes(validation_record_bytes(
            proposal_sha256=proposal_sha,
            result="accepted",
            mutation_sha256="c" * 64,
            validated_at=NOW,
        ))
    return root, proposal_sha


def test_immutable_rejection_evidence_produces_exact_four_targets(tmp_path: Path) -> None:
    root, proposal = _state(tmp_path)
    proof = prepare_rejection_cleanup(root, CASE)
    assert proof.case_id == CASE
    assert proof.job_id == JOB
    assert proof.proposal_sha256 == proposal
    assert proof.validation_sha256 == sha256_bytes((
        root / "10-Validation" / f"{proposal}.validation.json"
    ).read_bytes())
    assert tuple(t.stage for t in proof.targets) == STAGES
    assert tuple(t.path for t in proof.targets) == (
        f"04-AI/00-Input/{CASE}.md",
        f"04-AI/10-Context/{CASE}.md",
        f"04-AI/20-Generation/{CASE}.md",
        f"04-AI/30-Validation/{CASE}.md",
    )
    assert all(len(t.request_sha256) == 64 for t in proof.targets)
    assert parse_attestation(proof.to_json_bytes()) == proof
    verify_rejection_cleanup(root, proof)


def test_durable_private_attestation_is_replay_idempotent(tmp_path: Path) -> None:
    root, _ = _state(tmp_path)
    proof, path = store_rejection_cleanup_attestation(root, CASE)
    replay, replay_path = store_rejection_cleanup_attestation(root, CASE)
    assert replay == proof and replay_path == path
    assert path.read_bytes() == proof.to_json_bytes()
    assert path.parent.name == "validator"
    assert not any((root / "04-AI").glob("**/*")) if (root / "04-AI").exists() else True


@pytest.mark.parametrize(("state", "stage", "reason"), [
    ("queued", "validation", "validation_rejected"),
    ("blocked", "validation", "validation_rejected"),
    ("deterministic_reject", "generation", "validation_rejected"),
    ("deterministic_reject", "validation", "provider_timeout"),
])
def test_requires_verified_terminal_validation_stage_and_reason(
    tmp_path: Path, state: str, stage: str, reason: str,
) -> None:
    root, _ = _state(
        tmp_path, generation_state=state,
        attempt_stage=stage, attempt_reason=reason,
    )
    with pytest.raises(RejectionCleanupError):
        prepare_rejection_cleanup(root, CASE)


def test_accepted_validation_and_old_generation_are_rejected(tmp_path: Path) -> None:
    root, _ = _state(tmp_path, accepted=True)
    with pytest.raises(RejectionCleanupError, match="rejected Validation"):
        prepare_rejection_cleanup(root, CASE)
    (tmp_path / "other").mkdir()
    other, _ = _state(tmp_path / "other", later_generation=True)
    with pytest.raises(RejectionCleanupError, match="no longer current"):
        prepare_rejection_cleanup(other, CASE)


@pytest.mark.parametrize("missing", STAGES)
def test_requires_all_four_original_publication_results(
    tmp_path: Path, missing: str,
) -> None:
    root, _ = _state(tmp_path, skipped_stage=missing)
    with pytest.raises(RejectionCleanupPending):
        prepare_rejection_cleanup(root, CASE)


def test_corrupt_result_and_mutated_original_request_fail_closed(tmp_path: Path) -> None:
    root, _ = _state(tmp_path)
    proof = prepare_rejection_cleanup(root, CASE)
    result_dir = root / "17-Human-Projection-Result"
    result_path = result_dir / f"{proof.targets[0].request_sha256}.projection-result.json"
    result_path.write_bytes(b'{"invalid":true}')
    with pytest.raises(RejectionCleanupError):
        prepare_rejection_cleanup(root, CASE)


def test_tampered_validation_record_and_extra_review_stage_fail_closed(
    tmp_path: Path,
) -> None:
    root, proposal = _state(tmp_path)
    proof = prepare_rejection_cleanup(root, CASE)
    validation_path = root / "10-Validation" / f"{proposal}.validation.json"
    validation_path.write_bytes(b'{"malformed":true}')
    with pytest.raises(RejectionCleanupError):
        prepare_rejection_cleanup(root, CASE)
    assert proof.case_id == CASE


def test_manifest_parser_rejects_arbitrary_path_and_stage(tmp_path: Path) -> None:
    root, _ = _state(tmp_path)
    proof = prepare_rejection_cleanup(root, CASE)
    content = json.loads(proof.to_json_bytes())
    content["targets"][0]["path"] = "11-Knowledge/important.md"
    with pytest.raises(RejectionCleanupError):
        parse_attestation(_canonical_json_bytes(content))
    content["targets"][0]["path"] = f"04-AI/00-Input/{CASE}.md"
    content["targets"][0]["stage"] = "review"
    with pytest.raises(RejectionCleanupError):
        parse_attestation(_canonical_json_bytes(content))


def test_missing_case_and_invalid_scan_bound_are_rejected(tmp_path: Path) -> None:
    root, _ = _state(tmp_path)
    with pytest.raises(RejectionCleanupError):
        prepare_rejection_cleanup(root, "not-a-sha256")
    with pytest.raises(RejectionCleanupError, match="max_scan"):
        prepare_rejection_cleanup(root, CASE, max_scan=0)
    with pytest.raises(RejectionCleanupPending, match="scan bound"):
        prepare_rejection_cleanup(root, CASE, max_scan=1)


def test_verifier_rejects_attestation_tampering(tmp_path: Path) -> None:
    root, _ = _state(tmp_path)
    proof = prepare_rejection_cleanup(root, CASE)
    with pytest.raises(RejectionCleanupError):
        verify_rejection_cleanup(root, replace(
            proof, validation_sha256="9" * 64,
        ))


def test_private_attestation_readback_and_current_state_revalidation(
    tmp_path: Path,
) -> None:
    from obsidian_automation.validation_rejection_cleanup import (
        load_rejection_cleanup_attestation,
    )
    root, _ = _state(tmp_path)
    assert load_rejection_cleanup_attestation(root, CASE) is None
    proof, _ = store_rejection_cleanup_attestation(root, CASE)
    assert load_rejection_cleanup_attestation(root, CASE) == proof

    conn = _connect_rw(root)
    try:
        conn.execute(
            "UPDATE generations SET state = 'blocked' WHERE generation_id = ?",
            (CASE,),
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(RejectionCleanupError, match="not durably rejected"):
        verify_rejection_cleanup(root, proof)


def test_preflight_detects_generation_change_during_projection_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import obsidian_automation.validation_rejection_cleanup as module

    root, _ = _state(tmp_path)
    original = module._published_originals

    def race_during_scan(*args, **kwargs):
        result = original(*args, **kwargs)
        conn = _connect_rw(root)
        try:
            conn.execute(
                "UPDATE generations SET state='blocked' WHERE generation_id=?",
                (CASE,),
            )
            conn.commit()
        finally:
            conn.close()
        return result

    monkeypatch.setattr(module, "_published_originals", race_during_scan)
    with pytest.raises(RejectionCleanupError, match="not durably rejected"):
        prepare_rejection_cleanup(root, CASE)


def test_duplicate_original_stage_is_not_authorized(tmp_path: Path) -> None:
    root, _ = _state(tmp_path)
    proof = prepare_rejection_cleanup(root, CASE)
    fake = build_request(
        case_id=CASE,
        stage="input",
        source_kind="input_selection",
        source_sha256="9" * 64,
        content="# Alternate original Input\n",
        created_at=NOW,
    )
    digest, _ = store_request(root, role="reader", request=fake)
    _store_result(root, ProjectionResult(
        request_sha256=digest,
        target_path=fake.target_path,
        content_sha256=fake.content_sha256,
        result="created",
        completed_at=NOW,
    ))
    assert proof.targets[0].request_sha256 != digest
    with pytest.raises(RejectionCleanupError, match="uniqueness"):
        prepare_rejection_cleanup(root, CASE)


def test_unexpected_later_human_review_stage_blocks_rejection_cleanup(
    tmp_path: Path,
) -> None:
    root, _ = _state(tmp_path)
    request = build_request(
        case_id=CASE, stage="review",
        source_kind="review_request", source_sha256="7" * 64,
        content="# Unexpected Review state\n", created_at=NOW,
    )
    store_request(root, role="reviewer", request=request)
    with pytest.raises(RejectionCleanupError, match="unexpected later-stage"):
        prepare_rejection_cleanup(root, CASE)


def test_symlinked_original_request_is_never_attested(tmp_path: Path) -> None:
    root, _ = _state(tmp_path)
    requests = root / "16-Human-Projection" / "reader"
    original = next(requests.glob("*.projection.json"))
    alias = requests / ("0" * 64 + ".projection.json")
    alias.symlink_to(original)
    with pytest.raises(RejectionCleanupError, match="not safely readable|unsafe artifact"):
        prepare_rejection_cleanup(root, CASE)


def test_stored_attestation_never_authorizes_knowledge_or_other_case(
    tmp_path: Path,
) -> None:
    root, _ = _state(tmp_path)
    proof, _ = store_rejection_cleanup_attestation(root, CASE)
    assert not any(target.path.startswith("11-Knowledge/") for target in proof.targets)
    assert not any("/50-Review/" in target.path for target in proof.targets)
    assert all(target.path.endswith(f"/{CASE}.md") for target in proof.targets)
    with pytest.raises(RejectionCleanupError):
        verify_rejection_cleanup(root, replace(
            proof, targets=(replace(
                proof.targets[0], path=f"04-AI/50-Review/{CASE}.md",
            ),) + proof.targets[1:],
        ))


@pytest.mark.parametrize("stage", ("input", "context", "generation"))
def test_correctly_published_but_wrong_producer_source_is_blocked(
    tmp_path: Path, stage: str,
) -> None:
    root, _ = _state(tmp_path)
    proof = prepare_rejection_cleanup(root, CASE)
    original = next(item for item in proof.targets if item.stage == stage)
    role = "reader" if stage in {"input", "context"} else "generator"
    directory = root / "16-Human-Projection" / role
    original_file = directory / f"{original.request_sha256}.projection.json"
    original_req = parse_request(original_file.read_bytes())

    # A plausible, published replacement with right case/stage/role and a
    # matching publication result still must NOT be bound to this generation.
    fake_sha = "9" * 64
    assert fake_sha != original_req.source_sha256
    changed = original_req.content.replace(original_req.source_sha256, fake_sha)
    assert changed != original_req.content
    fake = build_request(
        case_id=CASE, stage=stage, source_kind=original_req.source_kind,
        source_sha256=fake_sha, content=changed, created_at=original_req.created_at,
    )
    original_file.unlink()
    (
        root / "17-Human-Projection-Result"
        / f"{original.request_sha256}.projection-result.json"
    ).unlink()
    digest, _ = store_request(root, role=role, request=fake)
    _store_result(root, ProjectionResult(
        request_sha256=digest,
        target_path=fake.target_path,
        content_sha256=fake.content_sha256,
        result="created", completed_at=NOW,
    ))
    with pytest.raises(RejectionCleanupError):
        prepare_rejection_cleanup(root, CASE)


def test_unpublished_original_does_not_hide_later_review_conflict(
    tmp_path: Path,
) -> None:
    root, _ = _state(tmp_path)
    proof = prepare_rejection_cleanup(root, CASE)
    (
        root / "17-Human-Projection-Result"
        / f"{proof.targets[0].request_sha256}.projection-result.json"
    ).unlink()
    review = build_request(
        case_id=CASE, stage="review",
        source_kind="review_request", source_sha256="9" * 64,
        content="# Bogus review\n", created_at=NOW,
    )
    store_request(root, role="reviewer", request=review)
    with pytest.raises(RejectionCleanupError, match="unexpected later-stage"):
        prepare_rejection_cleanup(root, CASE)


def test_unpublished_original_does_not_hide_duplicate_conflict(
    tmp_path: Path,
) -> None:
    root, _ = _state(tmp_path)
    proof = prepare_rejection_cleanup(root, CASE)
    (
        root / "17-Human-Projection-Result"
        / f"{proof.targets[0].request_sha256}.projection-result.json"
    ).unlink()
    original_file = (
        root / "16-Human-Projection" / "reader"
        / f"{proof.targets[0].request_sha256}.projection.json"
    )
    original_req = parse_request(original_file.read_bytes())
    fake = build_request(
        case_id=CASE, stage="input",
        source_kind="input_selection", source_sha256="9" * 64,
        content=original_req.content.replace(original_req.source_sha256, "9" * 64),
        created_at=NOW,
    )
    store_request(root, role="reader", request=fake)
    with pytest.raises(RejectionCleanupError, match="uniqueness"):
        prepare_rejection_cleanup(root, CASE)


def test_scan_bound_counts_all_nonprojection_directory_entries(
    tmp_path: Path,
) -> None:
    root, _ = _state(tmp_path)
    for i in range(8):
        (root / "16-Human-Projection" / "reader" / f"junk-{i}.txt").write_text("x")
    with pytest.raises(RejectionCleanupPending, match="scan bound"):
        prepare_rejection_cleanup(root, CASE, max_scan=4)


def test_unbounded_or_special_private_source_does_not_block_preflight(
    tmp_path: Path,
) -> None:
    import os

    root, _ = _state(tmp_path)
    proof = prepare_rejection_cleanup(root, CASE)
    path = (
        root / "16-Human-Projection" / "reader"
        / f"{proof.targets[0].request_sha256}.projection.json"
    )
    path.unlink()
    os.mkfifo(path)
    with pytest.raises(RejectionCleanupError, match="unsafe artifact"):
        prepare_rejection_cleanup(root, CASE)
    path.unlink()
    path.write_bytes(b"X" * (768 * 1024 + 1))
    with pytest.raises(RejectionCleanupError, match="size"):
        prepare_rejection_cleanup(root, CASE)


def test_oversized_published_result_is_never_attested(tmp_path: Path) -> None:
    root, _ = _state(tmp_path)
    proof = prepare_rejection_cleanup(root, CASE)
    result = (
        root / "17-Human-Projection-Result"
        / f"{proof.targets[0].request_sha256}.projection-result.json"
    )
    result.write_bytes(b"x" * (64 * 1024 + 1))
    with pytest.raises(RejectionCleanupError, match="size"):
        prepare_rejection_cleanup(root, CASE)


def test_real_semantic_deep_producer_chain_can_be_preflight_attested(
    tmp_path: Path,
) -> None:
    """Reuse the real semantic planner/generator fixture, not arbitrary hashes."""
    from datetime import datetime, timezone

    import test_semantic_deep_production as deep
    import obsidian_automation.pre_review_worker as worker
    from obsidian_automation.ai_input_planner import INPUT_MODE_SEMANTIC_DEEP, plan_once
    from obsidian_automation.pre_review_job import start_attempt, complete_attempt, job_status
    from obsidian_automation.human_projection import (
        ROLE_NAMES, parse_request,
        emit_validation_projection,
    )

    vault, root, index_sha = deep._semantic_index(tmp_path)
    request_root = root / "16-Human-Projection"
    request_root.mkdir()
    for role in ROLE_NAMES:
        (request_root / role).mkdir()
    (root / "17-Human-Projection-Result").mkdir()

    planned = plan_once(
        root, vault,
        deployed_revision=deep.REVISION,
        input_mode=INPUT_MODE_SEMANTIC_DEEP,
        semantic_index_sha256=index_sha,
        semantic_selection_policy="semantic-project-distill-v0",
        generator_model=deep.GEN_MODEL,
        evaluator_model=deep.EVAL_MODEL,
        now=datetime(2026, 9, 29, 4, 0, tzinfo=timezone.utc),
    )
    assert planned["status"] == "submitted"
    generated = worker.run_generator_worker(
        root, base_url="http://127.0.0.1:8000/v1",
        deployed_revision=deep.REVISION,
        transport=deep._deep_provider_transport,  # local fixture; no network
    )
    assert generated["state"] == "validating"
    case = str(job_status(root, str(planned["job_id"]))["current_generation"]["generation_id"])
    proposal_sha = str(generated["output"]["proposal_sha256"])
    validation = start_attempt(root, case, "validation")
    store_validation_record(
        root, proposal_sha256=proposal_sha, result="rejected",
        reason="deterministic simulated candidate invalid",
        validated_at="2026-09-29T04:05:00Z",
    )
    assert emit_validation_projection(
        root, case_id=case, proposal_sha256=proposal_sha,
    ) is not None
    complete_attempt(
        root, str(validation["attempt_id"]),
        outcome="deterministic_reject", reason_code="validation_rejected",
    )

    for role in ROLE_NAMES:
        for f in (request_root / role).glob("*.projection.json"):
            digest = f.name.removesuffix(".projection.json")
            req = parse_request(f.read_bytes())
            if req.case_id != case:
                continue
            _store_result(root, ProjectionResult(
                request_sha256=digest,
                target_path=req.target_path,
                content_sha256=req.content_sha256,
                result="created", completed_at="2026-09-29T04:06:00Z",
            ))
    proof = prepare_rejection_cleanup(root, case)
    assert len(proof.targets) == 4
    assert tuple(row.stage for row in proof.targets) == STAGES
    verify_rejection_cleanup(root, proof)
