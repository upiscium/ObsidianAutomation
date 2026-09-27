from __future__ import annotations

from pathlib import Path

import obsidian_automation.knowledge_production as production

from obsidian_automation.artifact_lifecycle import (
    _canonical_json_bytes,
    ensure_artifact_layout,
    sha256_bytes,
)
from obsidian_automation.canonical_mutation import ExecutionReceipt
from obsidian_automation.human_projection import (
    build_post_review_projection_binding,
    emit_completed_projection,
    emit_execution_projection,
    emit_transport_projection,
    parse_request,
    store_post_review_projection_binding,
)
from obsidian_automation.production_orchestrator import (
    TransportRequest,
    TransportResult,
)


CASE = "a" * 64
MUTATION = "b" * 64
EVALUATION = "c" * 64
CONTENT = "d" * 64
INTENT = "e" * 64


def _state(tmp_path: Path) -> tuple[Path, bytes, bytes, bytes]:
    state = tmp_path / "state"
    state.mkdir()
    ensure_artifact_layout(state)

    for directory in ("25-Execution", "27-Transport", "24-Locks"):
        (state / directory).mkdir()

    requests = state / "16-Human-Projection"
    requests.mkdir()
    for role in (
        "reader",
        "generator",
        "validator",
        "evaluator",
        "reviewer",
        "executor",
        "sync",
    ):
        (requests / role).mkdir()
    (state / "17-Human-Projection-Result").mkdir()

    (state / "20-Review" / f"{MUTATION}.approval.json").write_bytes(
        _canonical_json_bytes(
            {
                "record_version": 2,
                "mutation_sha256": MUTATION,
                "evaluation_sha256": EVALUATION,
                "decision": "approve",
                "decided_at": "2026-09-27T00:00:00Z",
                "approver": "human",
            }
        )
    )
    store_post_review_projection_binding(
        state,
        build_post_review_projection_binding(
            case_id=CASE,
            review_projection_request_sha256="f" * 64,
            evaluation_sha256=EVALUATION,
            mutation_sha256=MUTATION,
            created_at="2026-09-27T00:00:00Z",
        ),
    )

    request = TransportRequest(
        mutation_sha256=MUTATION,
        intent_sha256=INTENT,
        target_path="11-Knowledge/generated.md",
        content_sha256=CONTENT,
        requested_at="2026-09-27T00:01:00Z",
    )
    request_bytes = request.to_json_bytes()
    (state / "25-Execution" / f"{MUTATION}.transport-request.json").write_bytes(
        request_bytes
    )

    result = TransportResult(
        mutation_sha256=MUTATION,
        request_sha256=sha256_bytes(request_bytes),
        result="created_verified",
        target_path=request.target_path,
        expected_content_sha256=CONTENT,
        observed_content_sha256=CONTENT,
        observed_at="2026-09-27T00:02:00Z",
        http_status=201,
        etag='"verified"',
    )
    result_bytes = result.to_json_bytes()
    (state / "27-Transport" / f"{MUTATION}.transport-result.json").write_bytes(
        result_bytes
    )

    receipt = ExecutionReceipt(
        mutation_id="post-review-projection-test",
        mutation_sha256=MUTATION,
        target_path=request.target_path,
        content_sha256=CONTENT,
        executed_at=result.observed_at,
    )
    receipt_bytes = receipt.to_json_bytes()
    (state / "30-Receipts" / f"{MUTATION}.receipt.json").write_bytes(
        receipt_bytes
    )
    return state, request_bytes, result_bytes, receipt_bytes


def _projection_requests(state: Path, role: str):
    rows = []
    for path in sorted(
        (state / "16-Human-Projection" / role).glob("*.projection.json")
    ):
        rows.append(parse_request(path.read_bytes()))
    return rows


def test_post_review_projection_emitters_bind_authoritative_artifacts(
    tmp_path: Path,
) -> None:
    state, request_bytes, result_bytes, receipt_bytes = _state(tmp_path)

    emit_execution_projection(
        state,
        case_id=CASE,
        mutation_sha256=MUTATION,
    )
    emit_transport_projection(
        state,
        case_id=CASE,
        mutation_sha256=MUTATION,
    )
    emit_completed_projection(
        state,
        case_id=CASE,
        mutation_sha256=MUTATION,
    )

    executor = _projection_requests(state, "executor")
    sync = _projection_requests(state, "sync")

    by_stage = {item.stage: item for item in (*executor, *sync)}
    assert set(by_stage) == {"execution", "transport", "completed"}

    assert by_stage["execution"].source_sha256 == sha256_bytes(request_bytes)
    assert by_stage["transport"].source_sha256 == sha256_bytes(result_bytes)
    assert by_stage["completed"].source_sha256 == sha256_bytes(receipt_bytes)

    assert by_stage["execution"].target_path == f"04-AI/60-Execution/{CASE}.md"
    assert by_stage["transport"].target_path == f"04-AI/70-Transport/{CASE}.md"
    assert by_stage["completed"].target_path == f"04-AI/80-Completed/{CASE}.md"


def test_completed_hook_queues_terminal_cleanup(
    tmp_path: Path,
) -> None:
    state, _request_bytes, _result_bytes, _receipt_bytes = _state(tmp_path)

    production._emit_completed_and_queue_cleanup(
        state,
        mutation_sha256=MUTATION,
    )

    executor = state / "16-Human-Projection" / "executor"
    projection_requests = list(executor.glob("*.projection.json"))
    cleanup_requests = list(executor.glob("*.projection-terminal-cleanup.json"))

    assert len(projection_requests) == 1
    assert parse_request(projection_requests[0].read_bytes()).stage == "completed"
    assert len(cleanup_requests) == 1
