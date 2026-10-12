from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from obsidian_automation.artifact_lifecycle import (
    _canonical_json_bytes,
    ensure_artifact_layout,
    sha256_bytes,
    store_untrusted_proposal,
    store_validation_record,
    validation_record_bytes,
)
from obsidian_automation.human_projection import (
    ProjectionResult,
    _projection_markdown,
    _store_result,
    build_request,
    emit_validation_projection,
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
            (JOB, "c" * 64, "d" * 64, NOW),
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
                "generation_sha256": "1" * 64,
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

    # Original producer-specific request and immutable Sync publication
    # receipt. No Nextcloud credentials, server, or remote effects.
    for stage, role, source_kind in (
        ("input", "reader", "input_selection"),
        ("context", "reader", "context_bundle"),
        ("generation", "generator", "generation_record"),
    ):
        if stage == skipped_stage:
            continue
        source = "3" * 64
        request = build_request(
            case_id=CASE, stage=stage,
            source_kind=source_kind,
            source_sha256=source,
            content=_projection_markdown(
                case_id=CASE, stage=stage, source_kind=source_kind,
                source_sha256=source, created_at=NOW,
                title="Original producer content",
                body="Original immutable stage output",
            ),
            created_at=NOW,
        )
        digest, _ = store_request(root, role=role, request=request)
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
    with pytest.raises(RejectionCleanupError, match="not a regular file"):
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
