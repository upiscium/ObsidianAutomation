"""Opt-in, bounded parallel-vs-baseline Daily feature-first evaluation.

Source classifier's YES triggers an *extra* feature-first C5; NO/unknown never
prevents the original C5. Deterministic actual implementation anchors fail
open for low-recall classifiers. Both drafts are Grounded independently
against complete immutable original evidence; neither has publication rights.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Callable

from . import github_daily_witnessed_narrative as c4
from . import github_daily_d1_priority_shadow as d1
from . import github_daily_feature_stage as feature
from . import github_daily_narrative_runtime as rt

VERSION = "daily-feature-two-path-private-v1"


@dataclass(frozen=True)
class PrivateFeaturePair:
    canonical_bytes: bytes
    baseline: rt.NarrativeRun
    feature_first: rt.NarrativeRun | None

    def report(self) -> dict:
        return json.loads(self.canonical_bytes)


def run_private_feature_pair(
        capture_sha256, partial_replies, grounding_replies,
        materiality_decisions=(), *,
        target_entity: tuple[str, str, int],
        source_plan: d1.PriorityPlan,
        baseline_infer: Callable,
        feature_infer: Callable | None,
        gate_vote: feature.GateVote | None = None,
        max_calls_per_path: int = 4,
        max_seconds_per_path: float = 120,
) -> PrivateFeaturePair:
    if (type(source_plan) is not d1.PriorityPlan or not callable(baseline_infer)
            or (feature_infer is not None and not callable(feature_infer))
            or type(target_entity) is not tuple or len(target_entity) != 3):
        raise rt.NarrativeRuntimeError("invalid bounded private C5 pair request")
    verified = c4.build_witnessed_narrative_contexts(
        capture_sha256,partial_replies,grounding_replies,materiality_decisions).report()
    # A caller-provided d1 plan is not a source authority. Reconstruct the
    # complete exact priority plan from the *same verified C4 entities* before
    # accepting feature attribution or any extra-model call.
    expected_plan = d1._original_plan(
        [row["context"] for row in verified["contexts"]],capture_sha256)
    if expected_plan.sha256 != source_plan.sha256:
        raise rt.NarrativeRuntimeError("supplied d1 plan differs from verified original C4")
    source_contexts = [
        row for row in verified["contexts"]
        if (row["context"]["entity"]["repository"],
            row["context"]["entity"]["entity_type"],
            row["context"]["entity"]["number"]) == target_entity
    ]
    if len(source_contexts)!=1:
        raise rt.NarrativeRuntimeError("pair target not one verified selected Issue/PR")
    view = feature.build_feature_first_view(source_contexts[0]["context"], source_plan)
    route = feature.route_feature_pair(view,gate_vote)
    if route["run_feature_first_C5"] and feature_infer is None:
        raise rt.NarrativeRuntimeError("feature-first infer callback missing")
    kwargs = {
        "capture_sha256":capture_sha256,
        "partial_replies":partial_replies,
        "grounding_replies":grounding_replies,
        "materiality_decisions":materiality_decisions,
        "target_entity":target_entity,
        "max_entities":1,
        "max_calls":max_calls_per_path,
        "max_elapsed_seconds":max_seconds_per_path,
    }
    # Baseline is executed first, unconditionally, on unchanged original C4.
    baseline = rt.run_witnessed_narrative_inference(
        **kwargs, infer=baseline_infer)
    alternate = None
    alternate_error = None
    if route["run_feature_first_C5"]:
        try:
            alternate = rt.run_witnessed_narrative_inference(
                **kwargs, infer=feature_infer,
                feature_source_plan=source_plan)
        except rt.NarrativeRuntimeError:
            alternate_error="source_or_inference_preflight_failed"
    doc = {
        "record_version":VERSION,
        "stage":"daily_private_feature_first_paired_comparison",
        "capture_sha256":capture_sha256,
        "entity":{"repository":target_entity[0],
                  "entity_type":target_entity[1],"number":target_entity[2]},
        "source_context_sha256":source_contexts[0]["context_sha256"],
        "source_plan_sha256":source_plan.sha256,
        "feature_view_sha256":view.sha256 if view.structural_feature_anchor else None,
        "feature_source_refs":list(view.feature_source_refs),
        "status_source_refs":list(view.status_source_refs),
        "routing":route,
        "baseline_preview_sha256":baseline.preview.sha256,
        "baseline_complete":baseline.report()["inference_responses_complete"],
        "feature_first_preview_sha256":alternate.preview.sha256 if alternate is not None else None,
        "feature_first_complete":(
            alternate.report()["inference_responses_complete"] if alternate is not None else False),
        "feature_first_failure_class":alternate_error,
        "source_evidence_deleted":False,
        "source_refs_changed":False,
        "publication_authorized":False,
        "semantic_importance_automatically_certified":False,
        "requires_human_relevance_review":True,
    }
    return PrivateFeaturePair(feature._json(doc),baseline,alternate)
