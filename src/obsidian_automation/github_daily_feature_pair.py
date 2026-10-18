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
from . import github_daily_scope_quality as scope_quality
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
    if (not callable(baseline_infer)
            or type(target_entity) is not tuple or len(target_entity) != 3):
        raise rt.NarrativeRuntimeError("invalid bounded private C5 pair request")
    verified = c4.build_witnessed_narrative_contexts(
        capture_sha256,partial_replies,grounding_replies,materiality_decisions).report()
    source_contexts = [
        row for row in verified["contexts"]
        if (row["context"]["entity"]["repository"],
            row["context"]["entity"]["entity_type"],
            row["context"]["entity"]["number"]) == target_entity
    ]
    if len(source_contexts)!=1:
        raise rt.NarrativeRuntimeError("pair target not one verified selected Issue/PR")
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
    # Baseline is unconditional on the feature classifier, its model
    # profile, optional callback, and optional C4 view preflight.
    baseline = rt.run_witnessed_narrative_inference(
        **kwargs, infer=baseline_infer)
    alternate = None
    alternate_error = None
    view = None
    route = {
        "run_baseline_C5": True, "run_feature_first_C5": False,
        "trigger": "optional_unavailable",
        "classifier_label": None,
        "source_ref_inventory_preserved": True,
        "publication_authorized": False,
        "feature_gate_not_deletion_authority": True,
    }
    try:
        if type(source_plan) is not d1.PriorityPlan:
            raise rt.NarrativeRuntimeError("optional d1 source plan type invalid")
        expected = d1._original_plan(
            [row["context"] for row in verified["contexts"]], capture_sha256)
        if expected.sha256 != source_plan.sha256:
            raise rt.NarrativeRuntimeError("optional d1 plan differs from C4")
        view = feature.build_feature_first_view(
            source_contexts[0]["context"], source_plan)
        route = feature.route_feature_pair(view,gate_vote)
        if route["run_feature_first_C5"]:
            if feature_infer is None or not callable(feature_infer):
                raise rt.NarrativeRuntimeError("optional feature callback missing or invalid")
            alternate = rt.run_witnessed_narrative_inference(
                **kwargs, infer=feature_infer,
                feature_source_plan=source_plan)
    except Exception as exc:
        # A feature-stage fault never changes the completed baseline.
        # Do not write provider exception detail to the digest.
        alternate_error = type(exc).__name__
        route = {**route, "run_baseline_C5": True}
    protected_quality=None
    if alternate is not None and view is not None:
        try:
            grounded=[
                sentence
                for entity in alternate.preview.report()["entities"]
                for sentence in entity.get("sentences",())
                if sentence.get("verdict")=="supported"
            ]
            protected_quality=feature.inspect_operational_translation(view,grounded)
        except (feature.FeatureStageError,KeyError,TypeError,ValueError,AttributeError):
            # Even a quality-check failure does not invalidate either draft.
            # It does prevent describing the feature draft as quality-eligible.
            protected_quality={
                "status":"needs_human_review",
                "known_rule_violations":["invalid_quality_guard_input"],
                "model_or_grounding_is_semantic_authority":False,
                "publication_authorized":False,
            }
    # Independent source-scope acceptance audit, even when the classifier
    # chooses baseline-only (e.g. a test-only PR). Advisory flags never
    # select a winner, hide source, or affect existing C5 admission.
    def scope_audit(run):
        if run is None:
            return None
        try:
            grounded = [sentence
                for entity in run.preview.report()["entities"]
                for sentence in entity.get("sentences", ())
                if sentence.get("verdict") == "supported"]
            return scope_quality.inspect_scope_quality(
                source_contexts[0]["context"], grounded)
        except (ValueError, KeyError, TypeError, AttributeError):
            return {
                "status": "needs_human_review",
                "known_rule_violations": ["invalid_scope_quality_input"],
                "publication_authorized": False,
                "semantic_completeness_certified": False,
            }

    baseline_scope = scope_audit(baseline)
    feature_scope = scope_audit(alternate)
    doc = {
        "record_version":VERSION,
        "stage":"daily_private_feature_first_paired_comparison",
        "capture_sha256":capture_sha256,
        "entity":{"repository":target_entity[0],
                  "entity_type":target_entity[1],"number":target_entity[2]},
        "source_context_sha256":source_contexts[0]["context_sha256"],
        "source_plan_sha256":source_plan.sha256 if type(source_plan) is d1.PriorityPlan else None,
        "feature_view_sha256":view.sha256 if view is not None and view.structural_feature_anchor else None,
        "feature_source_refs":list(view.feature_source_refs) if view is not None else [],
        "status_source_refs":list(view.status_source_refs) if view is not None else [],
        "routing":route,
        "baseline_preview_sha256":baseline.preview.sha256,
        "baseline_complete":baseline.report()["inference_responses_complete"],
        "feature_first_preview_sha256":alternate.preview.sha256 if alternate is not None else None,
        "feature_first_complete":(
            alternate.report()["inference_responses_complete"] if alternate is not None else False),
        "feature_first_failure_class":alternate_error,
        "protected_operational_translation_check":protected_quality,
        "baseline_development_scope_check":baseline_scope,
        "feature_first_development_scope_check":feature_scope,
        "feature_first_has_no_known_operational_inversion":bool(
            protected_quality is not None and
            protected_quality.get("status")=="no_known_inversion_detected"),
        "source_evidence_deleted":False,
        "source_refs_changed":False,
        "publication_authorized":False,
        "semantic_importance_automatically_certified":False,
        "requires_human_relevance_review":True,
    }
    return PrivateFeaturePair(feature._json(doc),baseline,alternate)
