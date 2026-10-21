"""Opt-in Daily material-milestone evaluator is conservative and offline."""
from __future__ import annotations

from copy import deepcopy
import json

import pytest

from obsidian_automation import github_daily_milestone_eval as evaluation

SOURCE="a"*64
A={"repository":"sample/ALTAIR","entity_type":"pull_request","number":12}
B={"repository":"example/NEBULA","entity_type":"issue","number":23}


def lab(key,entity,refs,materiality="material",source_support="supported",
        state="adjudicated"):
    return {
        "milestone_id":key,"entity":entity,"source_refs":refs,
        "source_support":source_support,"materiality":materiality,
        "review_status":state,"reason":f"Manual original-source scope review for {key}",
    }


def sentence(n,refs,*,grounding="supported",materiality="material",
             covers=(),state="adjudicated"):
    return {
        "sentence_sha256":f"{n:064x}","source_refs":refs,
        "grounding":grounding,"review_status":state,
        "semantic_verdict":materiality,"covered_milestone_ids":list(covers),
    }


def result(route,entity,number,*sentences):
    return {
        "route":route,"entity":entity,
        "preview_sha256":f"{number:064x}","sentences":list(sentences),
    }


def valid():
    return {
        "record_version":evaluation.VERSION,
        "source_capture_sha256":SOURCE,
        "source_date":"2026-10-18",
        "review":{"reviewer_type":"human","reviewer_id":"independent-human-expert",
                  "reviewed_at":"2026-10-21T14:00:00+09:00",
                  "inventory_complete":True},
        "entity_inventory":[
            {"entity":A,"source_ref_count":3,
             "applicable_routes":["baseline","feature_first"]},
            {"entity":B,"source_ref_count":2,
             "applicable_routes":["baseline","feature_first"]},
        ],
        "milestones":[
            lab("a-feature",A,[0]),
            lab("a-routine",A,[1],materiality="routine"),
            lab("b-issue-progress",B,[1]),
        ],
        "route_results":[
            result("baseline",A,1,
                   sentence(1,[0],covers=["a-feature"]),
                   sentence(2,[1],materiality="routine")),
            result("baseline",B,2,
                   sentence(3,[1],covers=["b-issue-progress"])),
            result("feature_first",A,3,sentence(4,[0],covers=["a-feature"])),
            result("feature_first",B,4),
        ],
    }


def test_complete_self_reported_review_scored_but_never_publishes():
    obj=evaluation.evaluate_annotations(valid())
    assert obj["status"]=="ADJUDICATED_INPUT_SCORED_NOT_PRODUCTION_CERTIFIED"
    assert obj["source_milestone_label_count"]==3
    assert obj["candidate_material_label_count"]==2
    assert obj["routine_negative_label_count"]==1
    base=obj["scores_per_route"]["baseline"]
    assert base["post_grounding_material_precision"]==pytest.approx(2/3)
    assert base["source_milestone_recall"]==1.0
    assert base["supported_routine_overinclusion_count"]==1
    assert base["missed_material_milestone_ids"]==[]
    assert base["material_f1"]==pytest.approx(0.8)
    other=obj["scores_per_route"]["feature_first"]
    assert other["post_grounding_material_precision"]==1.0
    assert other["source_milestone_recall"]==0.5
    assert other["missed_material_milestone_ids"]==["b-issue-progress"]
    assert obj["human_reviewer_independence_verified_by_tool"] is False
    assert obj["semantic_acceptance_automatically_granted"] is False
    assert obj["publication_authorized"] is False
    assert obj["source_deleted"] is False
    assert obj["writer_timer_integration_authorized"] is False


def test_model_provisional_reviewer_is_not_human_acceptance():
    data=valid()
    data["review"]["reviewer_type"]="model_provisional"
    doc=evaluation.evaluate_annotations(data)
    assert doc["status"]=="NOT_HUMAN_ADJUDICATED_OR_INCOMPLETE"
    assert doc["annotation_complete"] is True
    assert doc["semantic_acceptance_automatically_granted"] is False


def test_pending_human_milestone_or_sentence_cannot_qualify():
    data=valid()
    data["milestones"][0]["review_status"]="pending"
    data["route_results"][0]["sentences"][1]["review_status"]="pending"
    doc=evaluation.evaluate_annotations(data)
    assert doc["status"]=="NOT_HUMAN_ADJUDICATED_OR_INCOMPLETE"
    assert "a-feature" in doc["missing_or_uncertain_annotations"]
    assert doc["scores_per_route"]["baseline"]["numerators_describe_only_adjudicated_items"] is False


def test_unreviewed_inventory_never_qualified_even_if_every_sample_labeled():
    data=valid()
    data["review"]["inventory_complete"]=False
    result=evaluation.evaluate_annotations(data)
    assert result["status"]=="NOT_HUMAN_ADJUDICATED_OR_INCOMPLETE"
    assert result["annotation_complete"] is False


def test_missing_optional_feature_route_blocks_full_paired_comparison():
    data=valid()
    data["route_results"]=[
        row for row in data["route_results"] if not (
            row["route"]=="feature_first" and row["entity"]==B)]
    out=evaluation.evaluate_annotations(data)
    assert out["status"]=="NOT_HUMAN_ADJUDICATED_OR_INCOMPLETE"
    assert "feature_first:incomplete_route_entity_inventory" in out[
        "missing_or_uncertain_annotations"]


@pytest.mark.parametrize("mutate,reason",[
    (lambda d: d["milestones"].append(deepcopy(d["milestones"][0])), "duplicate milestone"),
    (lambda d: d["milestones"][0].update(source_refs=[3]), "outside original C4"),
    (lambda d: d["milestones"][0].update(source_refs=[0,0]), "duplicate refs"),
    (lambda d: d["route_results"][0]["sentences"][0].update(
        covered_milestone_ids=["b-issue-progress"]), "cross-Issue/PR"),
    (lambda d: d["route_results"][0]["sentences"][0].update(
        source_refs=[1]), "coverage not bound"),
    (lambda d: d["route_results"][0]["sentences"][0].update(
        source_refs=[1], covered_milestone_ids=["a-routine"]), "routine/unsupported milestone"),
    (lambda d: d["route_results"][0]["sentences"][0].update(
        grounding="unsupported"), "rejected/routine"),
    (lambda d: d["route_results"][0]["sentences"][0].update(
        semantic_verdict="material", covered_milestone_ids=[]), "material sentence has no"),
    (lambda d: d["route_results"][0]["sentences"][1].update(
        semantic_verdict="material"), "material sentence has no"),
    (lambda d: d["entity_inventory"][0].update(source_ref_count=0), "bad source ref count"),
    (lambda d: d["route_results"][0].update(route="production"), "unexpected C5 route"),
    (lambda d: d["milestones"][0].update(review_status="automatically_approved"),
     "invalid milestone verdict"),
])
def test_invalid_or_fabricated_quality_claims_fail_closed(mutate,reason):
    obj=valid()
    mutate(obj)
    with pytest.raises(evaluation.MilestoneEvaluationError,match=reason):
        evaluation.evaluate_annotations(obj)


def test_cross_entity_review_label_cannot_be_shared_via_same_source_number():
    data=valid()
    data["route_results"][1]["sentences"][0]["covered_milestone_ids"]=["a-feature"]
    with pytest.raises(evaluation.MilestoneEvaluationError,match="cross-Issue/PR"):
        evaluation.evaluate_annotations(data)


def test_supported_grounding_alone_cannot_supply_material_quality():
    obj=valid()
    obj["route_results"][0]["sentences"][1]["grounding"]="supported"
    obj["route_results"][0]["sentences"][1]["semantic_verdict"]="routine"
    score=evaluation.evaluate_annotations(obj)["scores_per_route"]["baseline"]
    assert score["post_grounding_supported_sentences"]==3
    assert score["post_grounding_material_precision"]==pytest.approx(2/3)


def test_grounding_rejected_text_does_not_inflate_recalled_milestones():
    obj=valid()
    obj["route_results"][0]["sentences"][0]["grounding"]="unsupported"
    obj["route_results"][0]["sentences"][0]["semantic_verdict"]="unsupported"
    obj["route_results"][0]["sentences"][0]["covered_milestone_ids"]=[]
    score=evaluation.evaluate_annotations(obj)["scores_per_route"]["baseline"]
    assert score["source_milestone_recall"]==0.5
    assert score["missed_material_milestone_ids"]==["a-feature"]
    assert score["post_grounding_unsupported_sentences"]==1


def test_deterministic_bounded_wire_no_quality_authority():
    raw=json.dumps(valid(),sort_keys=True).encode()
    result=evaluation.evaluate_bytes(raw)
    assert result == evaluation.evaluate_bytes(raw)
    decoded=json.loads(result)
    assert decoded["semantic_acceptance_automatically_granted"] is False
    assert decoded["publication_authorized"] is False
    with pytest.raises(evaluation.MilestoneEvaluationError,match="bounded"):
        evaluation.evaluate_bytes(b"x"*(4*1024*1024+1))
    with pytest.raises(evaluation.MilestoneEvaluationError,match="JSON"):
        evaluation.evaluate_bytes(b"not json")


def test_cannot_use_a_nonhuman_or_incomplete_review_as_quality_pass():
    data=valid()
    for x in data["milestones"]:
        x["review_status"]="pending"
    data["review"]["reviewer_type"]="model_provisional"
    data["review"]["inventory_complete"]=False
    out=evaluation.evaluate_annotations(data)
    assert out["status"]=="NOT_HUMAN_ADJUDICATED_OR_INCOMPLETE"
    assert out["human_reviewer_independence_verified_by_tool"] is False


def test_strict_invalid_unknown_field_and_shuffled_route_identity():
    data=valid()
    data["review"]["signed_off_by_arca"]=True
    with pytest.raises(evaluation.MilestoneEvaluationError,match="reviewer provenance"):
        evaluation.evaluate_annotations(data)
    data=valid()
    data["route_results"][0]["entity"]={"repository":"sample/ALTAIR",
                                        "entity_type":"issue","number":12}
    with pytest.raises(evaluation.MilestoneEvaluationError,match="out-of-inventory"):
        evaluation.evaluate_annotations(data)



def test_optional_route_not_applicable_does_not_count_as_missed_milestone():
    data=valid()
    data["entity_inventory"][1]["applicable_routes"]=["baseline"]
    data["route_results"]=[
        row for row in data["route_results"]
        if row["route"]!="feature_first" or row["entity"]!=B
    ]
    result=evaluation.evaluate_annotations(data)
    assert result["status"]=="ADJUDICATED_INPUT_SCORED_NOT_PRODUCTION_CERTIFIED"
    assert result["scores_per_route"]["feature_first"]["reviewed_source_entities"]==1
    assert result["scores_per_route"]["feature_first"]["reviewed_material_milestones"]==1
    assert result["scores_per_route"]["feature_first"]["source_milestone_recall"]==1.0
    assert result["scores_per_route"]["baseline"]["reviewed_material_milestones"]==2


def test_inapplicable_feature_first_result_cannot_fabricate_comparison_denominator():
    data=valid()
    data["entity_inventory"][1]["applicable_routes"]=["baseline"]
    with pytest.raises(evaluation.MilestoneEvaluationError,
                       match="duplicated, inapplicable or out-of-inventory"):
        evaluation.evaluate_annotations(data)


def test_invalid_route_applicability_requires_unconditional_baseline():
    data=valid()
    data["entity_inventory"][0]["applicable_routes"]=["feature_first"]
    with pytest.raises(evaluation.MilestoneEvaluationError,
                       match="baseline is mandatory"):
        evaluation.evaluate_annotations(data)


def test_materiality_without_source_support_is_not_positive_ground_truth():
    data=valid()
    data["milestones"][0]["source_support"]="unsupported"
    data["route_results"][0]["sentences"][0]["covered_milestone_ids"]=[]
    data["route_results"][0]["sentences"][0]["semantic_verdict"]="unsupported"
    data["route_results"][0]["sentences"][0]["grounding"]="unsupported"
    with pytest.raises(evaluation.MilestoneEvaluationError,
                       match="routine/unsupported milestone"):
        # Feature-first still points to the same now-unfounded milestone.
        evaluation.evaluate_annotations(data)



def test_incomplete_annotations_redact_every_numeric_quality_metric():
    data=valid()
    data["milestones"][0]["review_status"]="pending"
    data["route_results"][0]["sentences"][1]["review_status"]="pending"
    out=evaluation.evaluate_annotations(data)
    assert out["status"]=="NOT_HUMAN_ADJUDICATED_OR_INCOMPLETE"
    assert out["candidate_material_label_count"] is None
    assert out["routine_negative_label_count"] is None
    for item in list(out["scores_per_route"].values()) + list(
            out["paired_scope_scores"].values()):
        for key in ("post_grounding_material_precision","source_milestone_recall",
                    "material_f1","matched_material_milestones",
                    "reviewed_material_milestones","missed_material_milestone_ids",
                    "supported_routine_overinclusion_count"):
            assert item[key] is None, (key,item)
        assert item["score_eligible_from_complete_human_self_report"] is False


def test_model_provisional_annotations_redact_all_quality_values():
    data=valid()
    data["review"]["reviewer_type"]="model_provisional"
    out=evaluation.evaluate_annotations(data)
    assert out["status"]=="NOT_HUMAN_ADJUDICATED_OR_INCOMPLETE"
    assert out["scores_per_route"]["baseline"]["source_milestone_recall"] is None
    assert out["paired_scope_scores"]["baseline"]["post_grounding_material_precision"] is None
    assert out["semantic_acceptance_automatically_granted"] is False


def test_explicit_paired_baseline_prevents_spurious_optional_route_win():
    data=valid()
    data["entity_inventory"][1]["applicable_routes"]=["baseline"]
    data["route_results"]=[
        row for row in data["route_results"]
        if not (row["route"]=="feature_first" and row["entity"]==B)
    ]
    data["route_results"][1]["sentences"]=[]
    # Whole baseline misses Issue B; optional feature never ran for Issue B.
    out=evaluation.evaluate_annotations(data)
    assert out["scores_per_route"]["baseline"]["source_milestone_recall"]==0.5
    assert out["scores_per_route"]["feature_first"]["source_milestone_recall"]==1.0
    assert out["unpaired_route_scores_not_directly_comparable"] is True
    assert out["paired_scope_entity_count"]==1
    assert out["paired_scope_scores"]["baseline"]["source_milestone_recall"]==1.0
    assert out["paired_scope_scores"]["feature_first"]["source_milestone_recall"]==1.0


def test_offline_cli_rejects_oversized_file_before_allocating_unbounded_bytes(tmp_path):
    import subprocess
    import sys
    target=tmp_path/"too-big.json"
    target.write_bytes(b"x"*(4*1024*1024+20))
    result=subprocess.run(
        [sys.executable,"-m","obsidian_automation.github_daily_milestone_eval",str(target)],
        text=True,capture_output=True,check=False)
    assert result.returncode!=0
    assert result.stdout==""
    assert "bounded evaluation bytes" in result.stderr


def test_offline_cli_rejects_symlink_to_private_data(tmp_path):
    import os
    import subprocess
    import sys
    target=tmp_path/"source.json"
    target.write_text(json.dumps(valid()))
    link=tmp_path/"alias.json"
    link.symlink_to(target)
    result=subprocess.run(
        [sys.executable,"-m","obsidian_automation.github_daily_milestone_eval",str(link)],
        text=True,capture_output=True,check=False)
    if hasattr(os,"O_NOFOLLOW"):
        assert result.returncode != 0
        assert result.stdout==""
