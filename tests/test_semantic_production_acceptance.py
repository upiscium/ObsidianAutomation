from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import obsidian_automation.semantic_production_acceptance as acceptance
from obsidian_automation.semantic_production_acceptance import (
    AcceptanceReceipt,
    CommandResult,
    SemanticProductionAcceptanceError,
    benchmark_acceptance,
    load_receipt,
    observe_selection_acceptance,
    plan_canary_acceptance,
    preflight_acceptance,
    store_receipt,
    verify_index_acceptance,
)


REVISION = "a" * 40
INDEX_SHA = "b" * 64
OTHER_INDEX_SHA = "c" * 64


def _result(
    stdout: str = "",
    *,
    returncode: int = 0,
    stderr: str = "",
) -> CommandResult:
    return CommandResult(
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _preflight_fixture(tmp_path: Path):
    app = tmp_path / "app"
    app.mkdir()
    etc = tmp_path / "etc"
    etc.mkdir()
    revision = etc / "pre-review-revision.env"
    revision.write_text(
        f"OBSIDIAN_AUTOMATION_REVISION={REVISION}\n",
        encoding="utf-8",
    )
    input_env = etc / "pre-review-input.env"
    input_env.write_text(
        "AI_INPUT_MODE=legacy\n"
        "AI_INPUT_GENERATOR_PROVIDER=ollama\n",
        encoding="utf-8",
    )
    planner_unit = etc / "obsidian-ai-input-planner.service"
    planner_unit.write_text(
        "[Service]\n"
        "Environment=AI_INPUT_MODE=legacy\n"
        "ExecStart=/bin/true\n",
        encoding="utf-8",
    )
    ai_root = tmp_path / "state"
    selection_dir = (
        ai_root / "02-Orchestration" / "semantic-selections"
    )
    selection_dir.mkdir(parents=True)
    return app, revision, input_env, planner_unit, ai_root


def _preflight_runner(argv):
    command = tuple(argv)
    if command[-2:] == ("rev-parse", "HEAD"):
        return _result(REVISION + "\n")
    if command[-2:] == ("branch", "--show-current"):
        return _result("main\n")
    if command[-2:] == ("status", "--porcelain"):
        return _result("")
    if command[:2] == ("systemctl", "is-enabled"):
        return _result("enabled\n")
    if command[:2] == ("systemctl", "is-active"):
        return _result("active\n")
    if command and command[0] == "runuser":
        user = command[2]
        flag = command[-2]
        allowed = (
            user == "obsidian-ai-reader"
            and flag in {"-r", "-w"}
        )
        return _result(returncode=0 if allowed else 1)
    raise AssertionError(f"unexpected command: {command}")


def test_preflight_acceptance_proves_legacy_and_reader_only_boundary(
    tmp_path: Path,
) -> None:
    app, revision, input_env, unit, ai_root = _preflight_fixture(
        tmp_path
    )
    receipt = preflight_acceptance(
        expected_revision=REVISION,
        app_root=app,
        revision_env=revision,
        input_env=input_env,
        planner_unit=unit,
        ai_root=ai_root,
        runner=_preflight_runner,
    )

    assert receipt.stage == "preflight"
    assert receipt.result == "passed"
    assert receipt.payload["expected_revision"] == REVISION
    assert receipt.payload["input_mode"] == "legacy"
    assert receipt.payload[
        "semantic_selection_store_reader_only"
    ] is True
    assert receipt.payload["pre_review_timer_enabled"] is True
    assert receipt.payload["pre_review_timer_active"] is True


def test_preflight_rejects_already_enabled_semantic_mode(
    tmp_path: Path,
) -> None:
    app, revision, input_env, unit, ai_root = _preflight_fixture(
        tmp_path
    )
    input_env.write_text(
        "AI_INPUT_MODE=semantic-deep-knowledge\n"
        f"AI_INPUT_SEMANTIC_INDEX_SHA={INDEX_SHA}\n",
        encoding="utf-8",
    )

    with pytest.raises(
        SemanticProductionAcceptanceError,
        match="already enabled",
    ):
        preflight_acceptance(
            expected_revision=REVISION,
            app_root=app,
            revision_env=revision,
            input_env=input_env,
            planner_unit=unit,
            ai_root=ai_root,
            runner=_preflight_runner,
        )


def test_verify_index_receipt_is_exact_and_content_free(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    index = SimpleNamespace(
        corpus_manifest_sha256="1" * 64,
        embedding_plan_sha256="2" * 64,
        embedding_result_set_sha256="3" * 64,
        chunk_policy="heading-section-lf-v0",
        provider="ollama",
        adapter_version="ollama-embed-v0",
        model_identifier="embed-model:latest",
        model_revision="4" * 64,
        vector_dimension=768,
        vector_encoding="json-number-finite-v0",
        source_kind_counts={
            "daily": 2,
            "idea": 1,
            "project": 1,
            "project-note": 2,
            "knowledge": 3,
        },
        vectors=(object(), object(), object()),
    )
    corpus = object()
    verified = []

    monkeypatch.setattr(
        acceptance,
        "load_semantic_index_manifest",
        lambda root, sha: index,
    )
    monkeypatch.setattr(
        acceptance,
        "load_semantic_corpus_manifest",
        lambda root, sha: corpus,
    )
    monkeypatch.setattr(
        acceptance,
        "verify_semantic_corpus_current",
        lambda root, value: verified.append((root, value)),
    )

    receipt = verify_index_acceptance(
        tmp_path / "state",
        tmp_path / "vault",
        semantic_index_sha256=INDEX_SHA,
    )
    assert receipt.result == "passed"
    assert receipt.payload["semantic_index_sha256"] == INDEX_SHA
    assert receipt.payload["vector_dimension"] == 768
    assert receipt.payload["vector_count"] == 3
    assert receipt.payload["current_vault_binding"] is True
    assert verified == [(tmp_path / "vault", corpus)]
    assert "content" not in str(receipt.payload).casefold()


def test_benchmark_acceptance_persists_failed_gate_as_failed_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        acceptance,
        "load_benchmark_set",
        lambda path: object(),
    )
    monkeypatch.setattr(
        acceptance,
        "evaluate_semantic_benchmark",
        lambda *args, **kwargs: {
            "name": "fixture",
            "benchmark_plan_sha256": "5" * 64,
            "benchmark_result_set_sha256": "6" * 64,
            "top_k": 3,
            "metrics": {
                "bm25": {"semantic": {"recall_at_k_macro": 0.5}},
                "hybrid": {"semantic": {"recall_at_k_macro": 0.5}},
            },
            "acceptance": {
                "hybrid_semantic_recall_improved_over_bm25": False,
                "hybrid_exact_technical_top1_not_regressed": True,
                "passed": False,
            },
        },
    )
    receipt = benchmark_acceptance(
        tmp_path / "state",
        tmp_path / "vault",
        semantic_index_sha256=INDEX_SHA,
        benchmark_path=tmp_path / "benchmark.json",
        benchmark_plan_sha256="5" * 64,
        benchmark_result_set_sha256="6" * 64,
    )
    assert receipt.stage == "benchmark"
    assert receipt.result == "failed"
    assert receipt.payload["acceptance"]["passed"] is False


def test_observe_selection_does_not_move_cadence_clock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cadence = SimpleNamespace(
        last_submission_at="2026-09-29T01:00:00Z",
        last_selection_policy="legacy",
    )
    novelty = SimpleNamespace(
        decision="selected",
        skip_reason=None,
        cluster_coherence=0.73,
        recent_context_max_similarity=0.42,
        knowledge_max_similarity=0.66,
    )
    selection = SimpleNamespace(
        selection_policy="semantic-project-distill-v0",
        selected=(
            SimpleNamespace(source_kind="project-note"),
            SimpleNamespace(source_kind="daily"),
            SimpleNamespace(source_kind="knowledge"),
        ),
        novelty=novelty,
    )
    monkeypatch.setattr(
        acceptance,
        "load_cadence_state",
        lambda root: cadence,
    )
    monkeypatch.setattr(
        acceptance,
        "build_semantic_selection",
        lambda *args, **kwargs: selection,
    )
    monkeypatch.setattr(
        acceptance,
        "store_semantic_selection",
        lambda *args, **kwargs: (
            "7" * 64,
            tmp_path / "selection.json",
        ),
    )

    receipt = observe_selection_acceptance(
        tmp_path / "state",
        tmp_path / "vault",
        semantic_index_sha256=INDEX_SHA,
        selection_policy="semantic-project-distill-v0",
    )
    assert receipt.result == "passed"
    assert receipt.payload["selection_sha256"] == "7" * 64
    assert receipt.payload["cadence_state_unchanged"] is True
    assert receipt.payload["selected_source_kinds"] == {
        "daily": 1,
        "knowledge": 1,
        "project-note": 1,
    }


def _receipt(
    stage: str,
    payload: dict[str, object],
    *,
    result: str = "passed",
) -> AcceptanceReceipt:
    return AcceptanceReceipt(
        stage=stage,
        result=result,
        payload=payload,
    )


def _stored_receipts(
    tmp_path: Path,
    *,
    benchmark_result: str = "passed",
    benchmark_index: str = INDEX_SHA,
):
    receipt_dir = tmp_path / "receipts"
    receipt_dir.mkdir()
    pre_sha, _ = store_receipt(
        receipt_dir,
        _receipt(
            "preflight",
            {"expected_revision": REVISION},
        ),
    )
    index_sha, _ = store_receipt(
        receipt_dir,
        _receipt(
            "verify-index",
            {"semantic_index_sha256": INDEX_SHA},
        ),
    )
    bench_sha, _ = store_receipt(
        receipt_dir,
        _receipt(
            "benchmark",
            {
                "semantic_index_sha256": benchmark_index,
                "acceptance": {"passed": benchmark_result == "passed"},
            },
            result=benchmark_result,
        ),
    )
    selection_sha, _ = store_receipt(
        receipt_dir,
        _receipt(
            "observe-selection",
            {
                "semantic_index_sha256": INDEX_SHA,
                "selection_policy": "semantic-project-distill-v0",
                "decision": "selected",
            },
        ),
    )
    return (
        receipt_dir,
        pre_sha,
        index_sha,
        bench_sha,
        selection_sha,
    )


def test_plan_canary_requires_exact_cross_bound_receipts(
    tmp_path: Path,
) -> None:
    (
        receipt_dir,
        pre_sha,
        index_sha,
        bench_sha,
        selection_sha,
    ) = _stored_receipts(tmp_path)

    receipt = plan_canary_acceptance(
        receipt_dir,
        preflight_receipt_sha256=pre_sha,
        index_receipt_sha256=index_sha,
        benchmark_receipt_sha256=bench_sha,
        selection_receipt_sha256=selection_sha,
    )
    assert receipt.stage == "plan-canary"
    assert receipt.result == "passed"
    assert receipt.payload["expected_revision"] == REVISION
    assert receipt.payload["semantic_index_sha256"] == INDEX_SHA
    assert receipt.payload["env_plan"] == [
        "AI_INPUT_MODE=semantic-deep-knowledge",
        f"AI_INPUT_SEMANTIC_INDEX_SHA={INDEX_SHA}",
        (
            "AI_INPUT_SEMANTIC_SELECTION_POLICY="
            "semantic-project-distill-v0"
        ),
    ]
    assert receipt.payload["mutation_performed"] is False


def test_plan_canary_rejects_failed_benchmark(
    tmp_path: Path,
) -> None:
    (
        receipt_dir,
        pre_sha,
        index_sha,
        bench_sha,
        selection_sha,
    ) = _stored_receipts(
        tmp_path,
        benchmark_result="failed",
    )

    with pytest.raises(
        SemanticProductionAcceptanceError,
        match="benchmark acceptance receipt did not pass",
    ):
        plan_canary_acceptance(
            receipt_dir,
            preflight_receipt_sha256=pre_sha,
            index_receipt_sha256=index_sha,
            benchmark_receipt_sha256=bench_sha,
            selection_receipt_sha256=selection_sha,
        )


def test_plan_canary_rejects_index_binding_mismatch(
    tmp_path: Path,
) -> None:
    (
        receipt_dir,
        pre_sha,
        index_sha,
        bench_sha,
        selection_sha,
    ) = _stored_receipts(
        tmp_path,
        benchmark_index=OTHER_INDEX_SHA,
    )

    with pytest.raises(
        SemanticProductionAcceptanceError,
        match="same Semantic Index",
    ):
        plan_canary_acceptance(
            receipt_dir,
            preflight_receipt_sha256=pre_sha,
            index_receipt_sha256=index_sha,
            benchmark_receipt_sha256=bench_sha,
            selection_receipt_sha256=selection_sha,
        )


def test_receipt_store_is_content_addressed_and_immutable(
    tmp_path: Path,
) -> None:
    receipt_dir = tmp_path / "receipts"
    receipt_dir.mkdir()
    receipt = _receipt(
        "verify-index",
        {"semantic_index_sha256": INDEX_SHA},
    )
    digest, path = store_receipt(receipt_dir, receipt)
    assert path.name == (
        f"{digest}.semantic-production-acceptance.json"
    )
    assert load_receipt(
        receipt_dir,
        digest,
        expected_stage="verify-index",
    ) == receipt
