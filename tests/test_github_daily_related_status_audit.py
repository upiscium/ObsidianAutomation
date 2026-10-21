"""Opt-in cross-entity source candidates: flags only, never status proof."""
from __future__ import annotations

from copy import deepcopy
import pytest

from obsidian_automation import github_daily_related_status_audit as rel
from obsidian_automation import github_daily_scope_quality as scope
from test_github_daily_c5_source_view import event
from test_github_daily_d1_priority_shadow import CAP


def ctx(repo,kind,number,day="2026-10-18"):
    return {"stage":"github_daily_narrative_context_v4",
            "entity":{"repository":repo,"entity_type":kind,"number":number},
            "capture_sha256":CAP,"date":day,
            "publication_authorized":False,"sources":[]}


def source(row,when):
    row["original_event"]["occurred_at"]=when
    return row


def evidence(repo="sample/SENTINEL",own=211,related=353,
             model="Qwen4.2",after=True,link=True):
    base=ctx(repo,"issue",own)
    base["sources"]=[
        source(event(0,"issue_comment",
              body=f"Upstream {model} inference has not yet completed. Production cutover is not approved."),
              "2026-10-18T10:00:00Z"),
    ]
    other=ctx(repo,"pull_request",related)
    commit=(f"fix(runtime): pinned {model} runtime. Refs #{own}."
            if link else f"fix(runtime): pinned {model} runtime.")
    other["sources"]=[
        source(event(0,"pull_request_commit",message=commit,
                     source_status="content_addressed_commit_not_push_time"),
               "2026-10-18T10:05:00Z"),
        source(event(1,"pull_request_comment",
                     body=f"On sirius, actual upstream {model} worker is active and healthy/ready. "
                          "This is not a complete production Control cutover."),
               "2026-10-18T11:15:00Z" if after else "2026-10-18T09:15:00Z"),
    ]
    summary=[{"text":f"Actual upstream {model} inference has not yet completed.",
              "source_refs":[0]}]
    return base,other,summary


@pytest.mark.parametrize("repo,issue,pr,model",[
    ("sample/SENTINEL",211,353,"Qwen4.2"),
    ("example/MIRAGE",901,2223,"Gemma5"),
    ("other/ALTAIR",12,7011,"Mistral9"),
])
def test_actual_linked_later_source_requires_review_without_certifying_cutover(
        repo,issue,pr,model):
    base,other,summary=evidence(repo,issue,pr,model)
    saved=deepcopy(base["sources"])
    info=rel.inspect_related_later_progress(base,summary,[other])
    assert info["status"]=="human_recheck_required"
    assert info["model_claim_contradiction_certified"] is False
    assert info["site_or_cutover_attested"] is False
    assert info["publication_authorized"] is False
    assert info["later_related_progress_candidates"][0]["related_entity"]=={
        "repository":repo,"entity_type":"pull_request","number":pr}
    assert info["later_related_progress_candidates"][0]["original_link_ref"]==0
    assert info["later_related_progress_candidates"][0]["later_source_ref"]==1
    assert info["later_related_progress_candidates"][0]["shared_technical_identifiers"]==[model.casefold()]
    assert base["sources"]==saved
    audit=scope.inspect_scope_quality(base,summary,related_contexts=[other])
    assert "later_linked_entity_progress_requires_recheck" in audit["known_rule_violations"]
    assert audit["related_source_recheck"]["status"]=="human_recheck_required"
    assert audit["semantic_completeness_certified"] is False


@pytest.mark.parametrize("change",[
    "no_link","earlier","wrong_model","other_repo","other_day"
])
def test_foreign_or_unbound_source_does_not_satisfy_later_related_candidate(change):
    base,other,summary=evidence()
    if change=="no_link":
        other["sources"][0]["original_event"]["message"]["text"]="A pinned Qwen4.2 runtime."
    if change=="earlier":
        other["sources"][1]["original_event"]["occurred_at"]="2026-10-18T09:10:00Z"
    if change=="wrong_model":
        other["sources"][1]["original_event"]["body"]["text"]="The Qwen6.0 worker is active and healthy."
    if change=="other_repo":
        other["entity"]["repository"]="other/UNRELATED"
    if change=="other_day":
        other["date"]="2026-10-19"
    info=rel.inspect_related_later_progress(base,summary,[other])
    assert info["later_related_progress_candidates"]==[]
    assert info["status"]=="no_linked_candidate_detected"
    audit=scope.inspect_scope_quality(base,summary,related_contexts=[other])
    assert "later_linked_entity_progress_requires_recheck" not in audit["known_rule_violations"]
    assert audit["publication_authorized"] is False


def test_not_done_claim_does_not_infer_actual_cutover_from_ready_worker():
    base,other,summary=evidence()
    summary=[{"text":"Upstream Qwen4.2 was active and ready.", "source_refs":[0]}]
    result=rel.inspect_related_later_progress(base,summary,[other])
    assert result["status"]=="no_linked_candidate_detected"
    assert result["site_or_cutover_attested"] is False


def test_unrelated_pr_target_is_not_a_false_issue_reconciliation():
    base,other,summary=evidence()
    base["entity"]["entity_type"]="pull_request"
    out=rel.inspect_related_later_progress(base,summary,[other])
    assert out["later_related_progress_candidates"]==[]


def test_unbound_refs_and_oversize_related_input_fail_closed():
    base,other,summary=evidence()
    with pytest.raises(rel.RelatedStatusAuditError,match="unbound"):
        rel.inspect_related_later_progress(base,[{"text":summary[0]["text"],"source_refs":[42]}],[other])
    with pytest.raises(rel.RelatedStatusAuditError,match="unbounded"):
        rel.inspect_related_later_progress(base,summary,[other]*9)
    broken=deepcopy(other)
    broken["sources"][1]["source_ref"]=3
    with pytest.raises(rel.RelatedStatusAuditError,match="source order"):
        rel.inspect_related_later_progress(base,summary,[broken])



def test_mismatched_capture_cannot_be_used_as_related_status_evidence():
    base,other,summary=evidence()
    other["capture_sha256"]="b"*64
    out=rel.inspect_related_later_progress(base,summary,[other])
    assert out["later_related_progress_candidates"]==[]
    assert out["site_or_cutover_attested"] is False


@pytest.mark.parametrize("field,value",[
    ("publication_authorized", True),
    ("stage", "model_supplied_claim"),
    ("capture_sha256", "not-sha256"),
])
def test_untrusted_or_unproven_source_context_fails_closed(field,value):
    base,other,summary=evidence()
    other[field]=value
    with pytest.raises(rel.RelatedStatusAuditError,match="invalid source context"):
        rel.inspect_related_later_progress(base,summary,[other])
