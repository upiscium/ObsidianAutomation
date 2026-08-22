from __future__ import annotations

import json
from pathlib import Path

import pytest

from obsidian_automation.artifact_lifecycle import ArtifactLifecycleError
from obsidian_automation.knowledge_index import build_knowledge_index
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
    (knowledge / "Alpha.md").write_text(_note("# Alpha\nalpha apple"), encoding="utf-8")
    (knowledge / "Beta.md").write_text(_note("# Beta\nbeta banana"), encoding="utf-8")
    (knowledge / "Mixed.md").write_text(_note("# Alpha Beta\nalpha beta bridge"), encoding="utf-8")
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


def test_eval_reports_baseline_and_diagnostic_sweeps(tmp_path: Path) -> None:
    report = evaluate_retrieval(_index(tmp_path), parse_eval_set(_eval_bytes()), top_k=3)

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
    report = evaluate_retrieval(_index(tmp_path), parse_eval_set(_eval_bytes()), top_k=3)
    counts = [row["selected_total"] for row in report["relative_cutoff_sweep"]]
    assert counts == sorted(counts, reverse=True)


def test_absolute_threshold_selected_count_is_nonincreasing(tmp_path: Path) -> None:
    report = evaluate_retrieval(_index(tmp_path), parse_eval_set(_eval_bytes()), top_k=3)
    counts = [row["selected_total"] for row in report["absolute_top1_score_sweep"]]
    assert counts == sorted(counts, reverse=True)
