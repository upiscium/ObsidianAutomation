from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .artifact_lifecycle import (
    ArtifactLifecycleError,
    _decode_json_object,
    _read_exact_file,
    _require_sha256,
)
from .context_bundle import MAX_CONTEXT_BYTES
from .knowledge_index import (
    DEFAULT_TOP_K,
    MAX_TOP_K,
    KnowledgeIndex,
    RankedDocument,
    load_knowledge_index,
    rank_documents,
    verify_index_current,
)
from .retrieval_coverage import (
    COVERAGE_THRESHOLDS,
    QueryCoverage,
    coverage_by_path,
)


EVAL_VERSION = 1
METRIC_K = 3
MAX_CASES = 512
CUTOFF_RATIOS = (0.0, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 1.0)
TOP1_SCORE_THRESHOLDS = (0.0, 1.0, 2.0, 3.0, 5.0, 8.0, 12.0)


@dataclass(frozen=True)
class RetrievalEvalCase:
    case_id: str
    query: str
    relevant_paths: tuple[str, ...]

    @property
    def negative(self) -> bool:
        return not self.relevant_paths


@dataclass(frozen=True)
class RetrievalEvalSet:
    name: str
    cases: tuple[RetrievalEvalCase, ...]


def _round_metric(value: float) -> float:
    return round(value, 6)


def _safe_eval_path(path: object) -> str:
    if (
        not isinstance(path, str)
        or not path.startswith("11-Knowledge/")
        or not path.endswith(".md")
    ):
        raise ArtifactLifecycleError(
            "retrieval eval relevant path must be a 11-Knowledge Markdown path"
        )
    if "\\" in path or "\x00" in path:
        raise ArtifactLifecycleError("retrieval eval relevant path is invalid")
    components = path.split("/")
    if any(component in {"", ".", ".."} for component in components):
        raise ArtifactLifecycleError("retrieval eval relevant path is invalid")
    return path


def parse_eval_set(data: bytes) -> RetrievalEvalSet:
    value = _decode_json_object(data, label="retrieval eval set")
    if set(value) != {"eval_version", "name", "cases"}:
        raise ArtifactLifecycleError(
            "retrieval eval set properties do not match contract"
        )
    if type(value["eval_version"]) is not int or value["eval_version"] != EVAL_VERSION:
        raise ArtifactLifecycleError(f"eval_version must be integer {EVAL_VERSION}")

    name = value["name"]
    raw_cases = value["cases"]
    if not isinstance(name, str) or not name.strip() or len(name) > 256:
        raise ArtifactLifecycleError("retrieval eval set name is invalid")
    if not isinstance(raw_cases, list) or not raw_cases or len(raw_cases) > MAX_CASES:
        raise ArtifactLifecycleError(
            f"retrieval eval set must contain 1..{MAX_CASES} cases"
        )

    seen_ids: set[str] = set()
    cases: list[RetrievalEvalCase] = []
    for raw in raw_cases:
        if (
            not isinstance(raw, dict)
            or set(raw) != {"id", "query", "relevant_paths"}
        ):
            raise ArtifactLifecycleError(
                "retrieval eval case properties do not match contract"
            )
        case_id = raw["id"]
        query = raw["query"]
        raw_paths = raw["relevant_paths"]
        if not isinstance(case_id, str) or not case_id.strip() or len(case_id) > 128:
            raise ArtifactLifecycleError("retrieval eval case id is invalid")
        if case_id in seen_ids:
            raise ArtifactLifecycleError(
                f"duplicate retrieval eval case id: {case_id}"
            )
        seen_ids.add(case_id)
        if not isinstance(query, str) or not query.strip() or len(query) > 4096:
            raise ArtifactLifecycleError(
                f"retrieval eval query is invalid: {case_id}"
            )
        if not isinstance(raw_paths, list) or len(raw_paths) > 64:
            raise ArtifactLifecycleError(
                f"retrieval eval relevant_paths is invalid: {case_id}"
            )

        relevant: list[str] = []
        seen_paths: set[str] = set()
        for raw_path in raw_paths:
            path = _safe_eval_path(raw_path)
            folded = path.casefold()
            if folded in seen_paths:
                raise ArtifactLifecycleError(
                    f"duplicate relevant path in retrieval eval case: {case_id}"
                )
            seen_paths.add(folded)
            relevant.append(path)

        cases.append(
            RetrievalEvalCase(
                case_id=case_id,
                query=query,
                relevant_paths=tuple(relevant),
            )
        )

    return RetrievalEvalSet(name=name, cases=tuple(cases))


def load_eval_set(path: Path) -> RetrievalEvalSet:
    return parse_eval_set(_read_exact_file(path))


def _validate_eval_against_index(
    eval_set: RetrievalEvalSet,
    index: KnowledgeIndex,
) -> None:
    index_paths = {doc.path for doc in index.documents}
    for case in eval_set.cases:
        missing = [path for path in case.relevant_paths if path not in index_paths]
        if missing:
            raise ArtifactLifecycleError(
                f"retrieval eval case {case.case_id} references paths absent "
                f"from the selected active index: {missing}"
            )


def _first_relevant_rank(
    ranked: Sequence[RankedDocument],
    relevant: set[str],
) -> int | None:
    for position, item in enumerate(ranked, 1):
        if item.path in relevant:
            return position
    return None


def _baseline_metrics(
    cases: Sequence[RetrievalEvalCase],
    rankings: dict[str, tuple[RankedDocument, ...]],
) -> dict[str, object]:
    positives = [case for case in cases if not case.negative]
    negatives = [case for case in cases if case.negative]

    top1_hits = 0
    recall_sum = 0.0
    precision_sum = 0.0
    hit3 = 0
    reciprocal_sum = 0.0

    for case in positives:
        ranked = rankings[case.case_id]
        relevant = set(case.relevant_paths)
        if ranked and ranked[0].path in relevant:
            top1_hits += 1

        top3 = ranked[:METRIC_K]
        true_at_3 = sum(item.path in relevant for item in top3)
        recall_sum += true_at_3 / len(relevant)
        precision_sum += true_at_3 / METRIC_K
        if true_at_3:
            hit3 += 1

        first_rank = _first_relevant_rank(ranked, relevant)
        if first_rank is not None:
            reciprocal_sum += 1.0 / first_rank

    negative_clean = sum(not rankings[case.case_id] for case in negatives)

    positive_count = len(positives)
    negative_count = len(negatives)
    return {
        "positive_case_count": positive_count,
        "negative_case_count": negative_count,
        "top1_accuracy": (
            _round_metric(top1_hits / positive_count) if positive_count else None
        ),
        "hit_at_3": _round_metric(hit3 / positive_count) if positive_count else None,
        "recall_at_3_macro": (
            _round_metric(recall_sum / positive_count) if positive_count else None
        ),
        "precision_at_3_macro": (
            _round_metric(precision_sum / positive_count) if positive_count else None
        ),
        "mrr": (
            _round_metric(reciprocal_sum / positive_count) if positive_count else None
        ),
        "negative_accuracy": (
            _round_metric(negative_clean / negative_count) if negative_count else None
        ),
    }


def _select_candidates(
    index: KnowledgeIndex,
    ranked: Sequence[RankedDocument],
    coverages: dict[str, QueryCoverage],
    *,
    min_coverage: float,
    ratio: float,
    min_top1_score: float,
    top_k: int,
) -> tuple[RankedDocument, ...]:
    eligible = [
        item
        for item in ranked
        if coverages[item.path].coverage >= min_coverage
    ]
    if not eligible or eligible[0].score < min_top1_score:
        return ()

    threshold = eligible[0].score * ratio
    size_by_path = {doc.path: doc.byte_size for doc in index.documents}
    selected: list[RankedDocument] = []
    total_bytes = 0

    for item in eligible:
        if len(selected) >= top_k:
            break
        if item.score < threshold:
            break
        size = size_by_path[item.path]
        if total_bytes + size > MAX_CONTEXT_BYTES:
            continue
        selected.append(item)
        total_bytes += size

    return tuple(selected)


def _selection_metrics(
    index: KnowledgeIndex,
    cases: Sequence[RetrievalEvalCase],
    rankings: dict[str, tuple[RankedDocument, ...]],
    coverages: dict[str, dict[str, QueryCoverage]],
    *,
    min_coverage: float,
    ratio: float,
    min_top1_score: float,
    top_k: int,
) -> dict[str, object]:
    selected_total = 0
    relevant_total = 0
    true_positive = 0
    negative_total = 0
    negative_clean = 0
    full_recall_cases = 0
    positive_cases = 0

    for case in cases:
        selected = _select_candidates(
            index,
            rankings[case.case_id],
            coverages[case.case_id],
            min_coverage=min_coverage,
            ratio=ratio,
            min_top1_score=min_top1_score,
            top_k=top_k,
        )
        relevant = set(case.relevant_paths)

        selected_total += len(selected)
        relevant_total += len(relevant)
        true_positive += sum(item.path in relevant for item in selected)

        if case.negative:
            negative_total += 1
            if not selected:
                negative_clean += 1
        else:
            positive_cases += 1
            if relevant.issubset({item.path for item in selected}):
                full_recall_cases += 1

    precision = true_positive / selected_total if selected_total else 0.0
    recall = true_positive / relevant_total if relevant_total else 0.0
    f1 = (
        2.0 * precision * recall / (precision + recall)
        if precision + recall
        else 0.0
    )

    return {
        "micro_precision": _round_metric(precision),
        "micro_recall": _round_metric(recall),
        "micro_f1": _round_metric(f1),
        "positive_full_recall_rate": (
            _round_metric(full_recall_cases / positive_cases)
            if positive_cases
            else None
        ),
        "negative_clean_rate": (
            _round_metric(negative_clean / negative_total)
            if negative_total
            else None
        ),
        "average_selected": _round_metric(selected_total / len(cases)),
        "selected_total": selected_total,
    }


def evaluate_retrieval(
    index: KnowledgeIndex,
    eval_set: RetrievalEvalSet,
    *,
    top_k: int = DEFAULT_TOP_K,
) -> dict[str, object]:
    if type(top_k) is not int or not 1 <= top_k <= MAX_TOP_K:
        raise ArtifactLifecycleError(
            f"evaluation top_k must be an integer in 1..{MAX_TOP_K}"
        )
    _validate_eval_against_index(eval_set, index)

    rankings: dict[str, tuple[RankedDocument, ...]] = {}
    coverages: dict[str, dict[str, QueryCoverage]] = {}
    case_reports: list[dict[str, object]] = []

    for case in eval_set.cases:
        ranked = rank_documents(index, case.query)
        case_coverage = coverage_by_path(index, case.query)
        rankings[case.case_id] = ranked
        coverages[case.case_id] = case_coverage
        relevant = set(case.relevant_paths)

        case_reports.append(
            {
                "id": case.case_id,
                "query": case.query,
                "relevant_paths": list(case.relevant_paths),
                "negative": case.negative,
                "matched_count": len(ranked),
                "top_results": [
                    {
                        "path": item.path,
                        "score": format(item.score, ".12g"),
                        "relevant": item.path in relevant,
                        "query_coverage": _round_metric(
                            case_coverage[item.path].coverage
                        ),
                        "matched_query_terms": len(
                            case_coverage[item.path].matched_terms
                        ),
                        "query_terms": (
                            len(case_coverage[item.path].matched_terms)
                            + len(case_coverage[item.path].missing_terms)
                        ),
                    }
                    for item in ranked[: max(METRIC_K, top_k)]
                ],
            }
        )

    relative_sweep = []
    for ratio in CUTOFF_RATIOS:
        row = _selection_metrics(
            index,
            eval_set.cases,
            rankings,
            coverages,
            min_coverage=0.0,
            ratio=ratio,
            min_top1_score=0.0,
            top_k=top_k,
        )
        row["ratio"] = ratio
        relative_sweep.append(row)

    absolute_sweep = []
    for threshold in TOP1_SCORE_THRESHOLDS:
        row = _selection_metrics(
            index,
            eval_set.cases,
            rankings,
            coverages,
            min_coverage=0.0,
            ratio=0.0,
            min_top1_score=threshold,
            top_k=top_k,
        )
        row["min_top1_score"] = threshold
        absolute_sweep.append(row)

    coverage_sweep = []
    for threshold in COVERAGE_THRESHOLDS:
        row = _selection_metrics(
            index,
            eval_set.cases,
            rankings,
            coverages,
            min_coverage=threshold,
            ratio=0.0,
            min_top1_score=0.0,
            top_k=top_k,
        )
        row["min_query_coverage"] = threshold
        coverage_sweep.append(row)

    combined_sweep = []
    for coverage_threshold in COVERAGE_THRESHOLDS:
        for ratio in CUTOFF_RATIOS:
            for score_threshold in TOP1_SCORE_THRESHOLDS:
                row = _selection_metrics(
                    index,
                    eval_set.cases,
                    rankings,
                    coverages,
                    min_coverage=coverage_threshold,
                    ratio=ratio,
                    min_top1_score=score_threshold,
                    top_k=top_k,
                )
                row["min_query_coverage"] = coverage_threshold
                row["ratio"] = ratio
                row["min_top1_score"] = score_threshold
                combined_sweep.append(row)

    return {
        "eval_version": EVAL_VERSION,
        "name": eval_set.name,
        "case_count": len(eval_set.cases),
        "baseline": _baseline_metrics(eval_set.cases, rankings),
        "relative_cutoff_sweep": relative_sweep,
        "absolute_top1_score_sweep": absolute_sweep,
        "query_coverage_sweep": coverage_sweep,
        "combined_sweep": combined_sweep,
        "cases": case_reports,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="obsidian-knowledge-retrieval-eval")
    parser.add_argument("--ai-root", type=Path, required=True)
    parser.add_argument("--vault-root", type=Path, required=True)
    parser.add_argument("--index-sha256", required=True)
    parser.add_argument("--eval-set", type=Path, required=True)
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    args = parser.parse_args(argv)

    try:
        digest = _require_sha256(args.index_sha256, label="index_sha256")
        index = load_knowledge_index(args.ai_root, digest)
        verify_index_current(args.vault_root, index)
        eval_set = load_eval_set(args.eval_set)
        report = evaluate_retrieval(index, eval_set, top_k=args.top_k)
    except (ArtifactLifecycleError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    report["index_sha256"] = digest
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0
