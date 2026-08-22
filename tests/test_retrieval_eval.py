from __future__ import annotations

import json
from pathlib import Path

import pytest

from obsidian_automation.artifact_lifecycle import ArtifactLifecycleError
from obsidian_automation.knowledge_index import build_knowledge_index
from obsidian_automation.retrieval_coverage import (
    coverage_by_path,
    query_idf_weights,
    weighted_query_coverage,
)
from obsidian_automation.retrieval_eval import evaluate_retrieval, parse_eval_set


def _note(body: str) -> str:
    return (
        "---\n"
        "type: knowledge-note\n"
        "status: active\n"
        "category: summary\n"
        "maturity: verified\n"
        "source_type: self\n"
        "---\n"
        f"{body}\n"
    )


def _index(tmp_path: Path):
    vault = tmp_path / "vault"
    knowledge = vault / "11-Knowledge"
    knowledge.mkdir(parents=True)
    (knowledge / "Alpha.md").write_text(
        _note("# Alpha\nalpha apple"), encoding="utf-8"
    )
    (knowledge / "Beta.md").write_text(
        _note("# Beta\nbeta banana"), encoding="utf-8"
    )
    (knowledge / "Mixed.md").write_text(
        _note("# Alpha Beta\nalpha beta bridge"), encoding="utf-8"
    )
    return build_knowledge_index(vault)


def _eval_bytes() -> bytes:
    return json.dumps(
        {
            "eval_version": 1,
            "name": "fixture",
            "cases": [
                {
                    "id": "alpha",
                    "query": "alpha apple",
                    "relevant_paths": ["11-Knowledge/Alpha.md"],
                },
                {
                    "id": "beta",
                    "query": "beta banana",
                    "relevant_paths": ["11-Knowledge/Beta.md"],
                },
                {
                    "id": "mixed",
                    "query": "alpha beta bridge",
                    "relevant_paths": ["11-Knowledge/Mixed.md"],
                },
                {
                    "id": "negative",
                    "query": "zzzz-no-match",
                    "relevant_paths": [],
                },
            ],
        },
        ensure_ascii=False,
    ).encode()


def test_idf_weights_include_terms_absent_from_corpus(tmp_path: Path) -> None:
    index = _index(tmp_path)
    weights = query_idf_weights(index, "alpha sandbox")

    assert set(weights) == {"alpha", "sandbox"}
    assert weights["sandbox"] > weights["alpha"]


def test_weighted_coverage_penalizes_partial_overlap(tmp_path: Path) -> None:
    index = _index(tmp_path)
    alpha = next(doc for doc in index.documents if doc.path.endswith("Alpha.md"))

    exact = weighted_query_coverage(index, "alpha apple", alpha)
    partial = weighted_query_coverage(index, "alpha sandbox security", alpha)

    assert exact.coverage == pytest.approx(1.0)
    assert 0.0 < partial.coverage < exact.coverage
    assert "sandbox" in partial.missing_terms
    assert "security" in partial.missing_terms


def test_coverage_by_path_reuses_one_query_contract(tmp_path: Path) -> None:
    index = _index(tmp_path)
    coverages = coverage_by_path(index, "alpha beta bridge")

    assert set(coverages) == {doc.path for doc in index.documents}
    assert coverages["11-Knowledge/Mixed.md"].coverage > coverages[
        "11-Knowledge/Alpha.md"
    ].coverage


def test_eval_reports_baseline_and_diagnostic_sweeps(tmp_path: Path) -> None:
    report = evaluate_retrieval(
        _index(tmp_path), parse_eval_set(_eval_bytes()), top_k=3
    )

    assert report["case_count"] == 4
    baseline = report["baseline"]
    assert baseline["top1_accuracy"] == 1.0
    assert baseline["hit_at_3"] == 1.0
    assert baseline["recall_at_3_macro"] == 1.0
    assert baseline["mrr"] == 1.0
    assert baseline["negative_accuracy"] == 1.0

    relative = report["relative_cutoff_sweep"]
    assert [row["ratio"] for row in relative] == [
        0.0,
        0.2,
        0.3,
        0.4,
        0.5,
        0.6,
        0.7,
        0.8,
        1.0,
    ]
    assert all(0.0 <= row["micro_precision"] <= 1.0 for row in relative)
    assert all(0.0 <= row["micro_recall"] <= 1.0 for row in relative)

    absolute = report["absolute_top1_score_sweep"]
    assert [row["min_top1_score"] for row in absolute] == [
        0.0,
        1.0,
        2.0,
        3.0,
        5.0,
        8.0,
        12.0,
    ]

    coverage = report["query_coverage_sweep"]
    assert [row["min_query_coverage"] for row in coverage] == [
        0.0,
        0.2,
        0.3,
        0.4,
        0.5,
        0.6,
        0.7,
        0.8,
    ]

    combined = report["combined_sweep"]
    assert len(combined) == 8 * 9 * 7
    assert all("min_query_coverage" in row for row in combined)
    assert all("ratio" in row for row in combined)
    assert all("min_top1_score" in row for row in combined)

    top = report["cases"][0]["top_results"][0]
    assert 0.0 <= top["query_coverage"] <= 1.0
    assert top["matched_query_terms"] <= top["query_terms"]


def test_eval_rejects_relevant_path_absent_from_index(tmp_path: Path) -> None:
    eval_set = parse_eval_set(
        json.dumps(
            {
                "eval_version": 1,
                "name": "bad",
                "cases": [
                    {
                        "id": "missing",
                        "query": "missing",
                        "relevant_paths": ["11-Knowledge/Missing.md"],
                    }
                ],
            }
        ).encode()
    )

    with pytest.raises(ArtifactLifecycleError, match="absent"):
        evaluate_retrieval(_index(tmp_path), eval_set)


def test_eval_rejects_duplicate_case_ids() -> None:
    data = json.dumps(
        {
            "eval_version": 1,
            "name": "duplicate",
            "cases": [
                {"id": "same", "query": "a", "relevant_paths": []},
                {"id": "same", "query": "b", "relevant_paths": []},
            ],
        }
    ).encode()

    with pytest.raises(ArtifactLifecycleError, match="duplicate retrieval eval case id"):
        parse_eval_set(data)


def test_relative_cutoff_selected_count_is_nonincreasing(tmp_path: Path) -> None:
    report = evaluate_retrieval(
        _index(tmp_path), parse_eval_set(_eval_bytes()), top_k=3
    )
    counts = [row["selected_total"] for row in report["relative_cutoff_sweep"]]
    assert counts == sorted(counts, reverse=True)


def test_absolute_threshold_selected_count_is_nonincreasing(tmp_path: Path) -> None:
    report = evaluate_retrieval(
        _index(tmp_path), parse_eval_set(_eval_bytes()), top_k=3
    )
    counts = [
        row["selected_total"] for row in report["absolute_top1_score_sweep"]
    ]
    assert counts == sorted(counts, reverse=True)


def test_coverage_threshold_selected_count_is_nonincreasing(tmp_path: Path) -> None:
    report = evaluate_retrieval(
        _index(tmp_path), parse_eval_set(_eval_bytes()), top_k=3
    )
    counts = [row["selected_total"] for row in report["query_coverage_sweep"]]
    assert counts == sorted(counts, reverse=True)
