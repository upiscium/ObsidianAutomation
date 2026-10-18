"""Source-preserving d1 Yes/No routing and paired feature-first C5 view."""
from __future__ import annotations

from copy import deepcopy
import json

import pytest

from obsidian_automation import github_daily_feature_stage as stage
from obsidian_automation import github_daily_d1_priority_shadow as d1
from obsidian_automation import github_daily_narrative_runtime as rt
from test_github_daily_d1_priority_shadow import contexts, CAP
from test_github_daily_route_cas import safe_layout
from test_github_daily_narrative_runtime import case, kwargs


def context_plan():
    data=contexts()
    # Both PRs still have exact same first commit SHA. PR 374's separate
    # second feature commit is uniquely identified by its actual SHA.
    return data, d1._original_plan(data,CAP)


def make_original_feature_context():
    data=contexts()
    target=data[1]
    e=target["sources"][2]["original_event"]
    e["body"]["text"]="No production worker is installed; Execute remains disabled."
    created={
       "kind":"pull_request_created","occurred_at":"2026-10-16T11:00:00Z",
       "actor":"original-post-author","evidence_id":"9"*64,"state":"open",
       "draft":True,
       "title":{"text":"feat(#300): audited operation broker with pause/resume","truncated":False},
       "body":{"text":(
           "## #300 Phase B — broker\n"
           "### Implemented\n"
           "- Added disabled-by-default OperationBroker core.\n"
           "- Added peer-verified pause and resume queue.\n"
           "### Evidence\n"
           "- Tests PASS; reviewer had no blocking findings.\n"
           "### Required before enabling controls\n"
           "- Privileged worker is not installed.\n"),"truncated":False},
       "message":None,
       "temporal_observation":{"policy":"github-day-snapshot-v1",
           "status":"day_snapshot_not_event_instant","withheld_fields":[],
           "historical_instant_verified":False}}
    target["sources"].append({"source_ref":3,"original_event":created})
    return data,d1._original_plan(data,CAP)


def normalized_vote(req,choice="yes",abstained=False):
    return stage._json({
     "object":"astrumweaver.decision",
     "profile":d1.PINNED_D1_PROFILE_ID,
     "choice_id":None if abstained else choice,
     "abstained":abstained,
     "scores":[{"id":"yes","score":.84},{"id":"no","score":.16}],
     "score_kind":"choice_set_probability",
     "calibration":{"status":"uncalibrated","reference_sha256":None},
     "usage":{"input_tokens":120,"output_tokens":0},
     "x_astrumweaver":{
       "profile_revision":d1.PINNED_D1_PROFILE_REVISION,
       "decision_semantics_id":d1.PINNED_D1_SEMANTICS_ID,
       "deployment_revision":d1.PINNED_D1_DEPLOYMENT_REVISION,
       "serving_contract_revision":d1.PINNED_D1_SERVING_CONTRACT_REVISION,
       "authority":"recommendation-only","mode":"shadow",
     }
    })


def test_feature_created_section_exposes_exact_quotes_without_test_bullets():
    ctx,plan=make_original_feature_context()
    view=stage.build_feature_first_view(ctx[1],plan)
    assert view.structural_feature_anchor
    obj=view.report()
    assert obj["source_deleted"] is False
    assert obj["publication_authorized"] is False
    assert obj["semantic_coverage_verified"] is False
    assert view.shown_source_refs == (3,)
    quotes=obj["feature_implementation_evidence"]
    assert len(quotes)==3
    assert "broker" in quotes[0]["verbatim"].lower()
    assert "OperationBroker" in quotes[1]["verbatim"]
    assert "pause and resume" in quotes[2]["verbatim"]
    assert all("Tests PASS" not in q["verbatim"] for q in quotes)
    raw=ctx[1]["sources"][3]["original_event"]["body"]["text"]
    for q in quotes[1:]:
        assert raw[q["start"]:q["end"]]==q["verbatim"]
    statuses=obj["latest_operational_caveat_evidence"]
    assert statuses and any("not installed" in q["verbatim"] for q in statuses)


def test_unique_feature_commit_reused_sha_is_not_child_feature():
    ctx,plan=context_plan()
    # PR 374 may cite its ref 1 own feature commit. Inherited ref 0 is NOT
    # its own implementation milestone despite the tempting feat prefix.
    view=stage.build_feature_first_view(ctx[1],plan)
    assert view.feature_source_refs == (1,)
    assert all(q["source_ref"]==1 for q in view.report()["feature_implementation_evidence"])
    assert 0 not in view.shown_source_refs
    assert view.report()["source_deleted"] is False


def test_no_feature_source_uses_baseline_without_dropping_issue():
    ctx,plan=context_plan()
    neg=deepcopy(ctx[1])
    for row in neg["sources"]:
        evt=row["original_event"]
        if evt["kind"]=="pull_request_commit":
            evt["message"]["text"]="docs: inherited process notes"
            evt["sha"]="2"*40
    custom=d1._original_plan([ctx[0],neg],CAP)
    view=stage.build_feature_first_view(neg,custom)
    assert view.structural_feature_anchor is False
    route=stage.route_feature_pair(view,None)
    assert route["run_baseline_C5"] is True
    assert route["run_feature_first_C5"] is False
    assert route["trigger"]=="baseline_only"


def test_managed_two_way_d1_yes_or_no_never_disables_baseline():
    ctx,plan=context_plan()
    view=stage.build_feature_first_view(ctx[1],plan)
    req=stage.compile_binary_gate_request(view)
    assert req.view_sha256==view.sha256
    assert req.source_ref==1
    raw=normalized_vote(req)
    vote=stage.admit_binary_gate_vote(req,raw)
    assert vote.report()["label"]=="yes"
    assert vote.report()["response_sha256"]==d1._sha(raw)
    decision=stage.route_feature_pair(view,vote)
    assert decision["run_baseline_C5"] is True
    assert decision["run_feature_first_C5"] is True
    assert decision["trigger"]=="model_yes_and_structural"
    unknown=stage.admit_binary_gate_vote(req,normalized_vote(req,abstained=True))
    assert stage.route_feature_pair(view,unknown)["trigger"]=="structural_fail_open"
    assert stage.route_feature_pair(view,None)["run_feature_first_C5"] is True


@pytest.mark.parametrize("changed",[
    ("profile","fake-profile"),
    ("choice_id","nonexistent"),
    ("scores",[{"id":"no","score":.95},{"id":"yes","score":.05}]),
    ("scores",[{"id":"yes","score":.4},{"id":"no","score":.4}]),
    ("usage",{"input_tokens":1,"output_tokens":1}),
])
def test_invalid_d1_vote_never_binds_to_feature_view(changed):
    ctx,plan=context_plan()
    view=stage.build_feature_first_view(ctx[1],plan)
    req=stage.compile_binary_gate_request(view)
    response=json.loads(normalized_vote(req))
    response[changed[0]]=changed[1]
    with pytest.raises(stage.FeatureStageError):
        stage.admit_binary_gate_vote(req,stage._json(response))


def test_d1_gate_wrong_view_hash_is_rejected_without_bypass():
    ctx,plan=context_plan()
    view=stage.build_feature_first_view(ctx[1],plan)
    request=stage.compile_binary_gate_request(view)
    vote=stage.admit_binary_gate_vote(request,normalized_vote(request))
    counterfeit=stage.GateVote(stage._json(dict(vote.report(),feature_source_view_sha256="x"*64)))
    with pytest.raises(stage.FeatureStageError):
        stage.route_feature_pair(view,counterfeit)


def test_existing_C5_contracts_cannot_bind_feature_source_plan():
    ctx,plan=context_plan()
    with pytest.raises(rt.NarrativeRuntimeError, match="requires"):
        rt.ollama_narrative_infer(base_url="http://127.0.0.1:11434",
                                 model="fixture",editorial_contract=rt.FEATURE_FIRST_SOURCE_CONTRACT)
    with pytest.raises(rt.NarrativeRuntimeError, match="cannot affect"):
        rt.ollama_narrative_infer(base_url="http://127.0.0.1:11434",
                                 model="fixture",feature_source_plan=plan,
                                 editorial_contract=rt.RECENT_SOURCE_CONTRACT)


def test_feature_c5_cannot_cite_original_ref_not_shown():
    ctx,plan=context_plan()
    view=stage.build_feature_first_view(ctx[1],plan)
    ref=view.shown_source_refs[0]
    reply=rt._json({"sentences":[{"text":"新しい機能を実装しました。","source_refs":[ref]}]})
    admitted=rt._admit_editorial(ctx[1],reply,rt.FEATURE_FIRST_SOURCE_CONTRACT,
                               verified_feature_refs=view.shown_source_refs)
    projected=json.loads(admitted)
    assert projected["sentences"][0]["source_refs"]==[ref]
    assert sorted(projected["pending_source_refs"]+list([ref]))==list(range(len(ctx[1]["sources"])))
    unknown=next(i for i in range(len(ctx[1]["sources"])) if i not in view.shown_source_refs)
    with pytest.raises(rt.NarrativeRuntimeError, match="not model-visible"):
        rt._admit_editorial(ctx[1],
            rt._json({"sentences":[{"text":"新機能を実装しました。","source_refs":[unknown]}]}),
            rt.FEATURE_FIRST_SOURCE_CONTRACT,verified_feature_refs=view.shown_source_refs)


def test_feature_view_tampered_source_sha_rejected():
    ctx,plan=make_original_feature_context()
    ctx[1]["sources"][3]["original_event"]["body"]["text"]+="bogus"
    with pytest.raises(stage.FeatureStageError,match="source content changed"):
        stage.build_feature_first_view(ctx[1],plan)


def test_preview_is_not_mistaken_for_a_review_only_line():
    allcontexts, _ = make_original_feature_context()
    created=allcontexts[1]["sources"][3]["original_event"]
    created["body"]["text"] = (
        "### What is implemented\n"
        "- Added a Preview selector for five fixed services with Execute disabled.\n"
        "- GET /api/v1/operation-session issues bounded CSRF nonce.\n"
        "### Evidence\n"
        "- GitHub Actions tests and reviewer PASS.\n"
        "### Still blocking\n"
        "- No live control and execution disabled.\n")
    plan=d1._original_plan(allcontexts,CAP)
    view=stage.build_feature_first_view(allcontexts[1],plan)
    assert view.structural_feature_anchor
    quotes=[x["verbatim"] for x in view.report()["feature_implementation_evidence"]]
    assert any("Preview selector" in x for x in quotes)
    assert all("GitHub Actions" not in x for x in quotes)
    assert any("No live" in x["verbatim"] for x in
               view.report()["latest_operational_caveat_evidence"])


def test_stale_snapshot_must_not_override_recent_operational_gate():
    allcontexts,_=make_original_feature_context()
    from test_github_daily_c5_source_view import event
    snapshot=event(4,"pull_request_snapshot",body="No production and disabled; stale snapshot")
    allcontexts[1]["sources"].append(snapshot)
    plan=d1._original_plan(allcontexts,CAP)
    view=stage.build_feature_first_view(allcontexts[1],plan)
    assert view.structural_feature_anchor
    assert 4 not in view.status_source_refs
    assert all(x["source_ref"]!=4 for x in
               view.report()["latest_operational_caveat_evidence"])


def test_collection_truncated_final_line_never_becomes_feature_claim():
    allcontexts,_=make_original_feature_context()
    event=allcontexts[1]["sources"][3]["original_event"]
    event["title"]=None
    event["body"]={
        "text":"### Implemented\n- Added source-verified queue.\n"
               "- Added live operation without",
        "truncated":True,
    }
    plan=d1._original_plan(allcontexts,CAP)
    view=stage.build_feature_first_view(allcontexts[1],plan)
    assert view.structural_feature_anchor
    features=[row["verbatim"] for row in view.report()["feature_implementation_evidence"]]
    assert features==["- Added source-verified queue.\n"]
    assert all("live operation" not in text for text in features)


def test_only_truncated_final_line_is_not_a_feature_anchor():
    allcontexts,_=make_original_feature_context()
    event=allcontexts[1]["sources"][3]["original_event"]
    event["title"]=None
    event["body"]={"text":"### Implemented\n- Added live operation without", "truncated":True}
    # No fallback new feature commit may override the intended created-body
    # truncation test; all commit messages in this fixture are historical docs.
    for item in allcontexts[1]["sources"]:
        if item["original_event"]["kind"]=="pull_request_commit":
            item["original_event"]["message"]["text"]="docs: pre-existing notes"
    plan=d1._original_plan(allcontexts,CAP)
    view=stage.build_feature_first_view(allcontexts[1],plan)
    assert view.structural_feature_anchor is False


def test_later_installed_worker_avoids_outdated_negative_status():
    allcontexts,_=make_original_feature_context()
    source=allcontexts[1]["sources"][3]["original_event"]
    source["body"]["text"]+= "- Privileged effect worker is not installed.\n"
    from test_github_daily_c5_source_view import event
    update=event(4,"pull_request_comment",
        body="The privileged effect worker is installed and enabled.")
    allcontexts[1]["sources"].append(update)
    plan=d1._original_plan(allcontexts,CAP)
    view=stage.build_feature_first_view(allcontexts[1],plan)
    assert view.structural_feature_anchor
    selected=[row["verbatim"] for row in view.report()["latest_operational_caveat_evidence"]]
    assert not any("worker is not installed" in x for x in selected)


def test_no_worker_not_translated_as_installed_positive_status():
    allcontexts,_=make_original_feature_context()
    source=allcontexts[1]["sources"][3]["original_event"]
    source["body"]["text"]+= "- No production worker is installed.\n"
    plan=d1._original_plan(allcontexts,CAP)
    view=stage.build_feature_first_view(allcontexts[1],plan)
    assert view.structural_feature_anchor
    assert any("No production worker" in row["verbatim"] for row in
               view.report()["latest_operational_caveat_evidence"])


def test_cannot_accept_conditional_run_once_for_explicitly_unavailable_action():
    allcontexts,_=make_original_feature_context()
    event=allcontexts[1]["sources"][3]["original_event"]
    event["body"]["text"]+=(
        "- Explicit blocker: Run once and all browser Execute paths stay unavailable.\n")
    plan=d1._original_plan(allcontexts,CAP)
    view=stage.build_feature_first_view(allcontexts[1],plan)
    assert view.structural_feature_anchor
    facts=view.report()["protected_operational_terms"]
    assert len(facts)==1
    assert facts[0]["canonical_japanese"]=="「Run once」とブラウザの「Execute」は利用できません。"
    assert facts[0]["source_ref"]==3
    wrong=stage.inspect_operational_translation(view,[
       {"text":"Run onceを実行してもブラウザのExecuteパスは利用できません。","source_refs":[3]}
    ])
    assert wrong["status"]=="needs_human_review"
    assert "conditionalized_unavailable_action" in wrong["known_rule_violations"]
    correct=stage.inspect_operational_translation(view,[
       {"text":"「Run once」とブラウザの「Execute」は利用できません。","source_refs":[3]}
    ])
    assert correct["known_rule_violations"]==[]
    assert correct["status"]=="no_known_inversion_detected"


def test_unwired_inverted_japanese_flagged_even_when_grounding_supported():
    allcontexts,_=make_original_feature_context()
    event=allcontexts[1]["sources"][3]["original_event"]
    event["body"]["text"]=(
        "### Implemented\n"
        "- A deliberately **unwired**, disabled-by-default OperationBroker was added.\n"
        "### Required\n"
        "- No production systemd effect was invoked.\n"
    )
    plan=d1._original_plan(allcontexts,CAP)
    view=stage.build_feature_first_view(allcontexts[1],plan)
    assert any(f["rule_id"]=="unwired-disabled-component-v1" for f in
               view.report()["protected_operational_terms"])
    wrong=stage.inspect_operational_translation(view,[
       {"text":"このコードは配線されたままに保たれる必要があります。","source_refs":[3]}
    ])
    assert "unwired_component_status_inverted" in wrong["known_rule_violations"]
    with pytest.raises(stage.FeatureStageError):
        stage.inspect_operational_translation(view,[
            {"text":"コードが実装されました。","source_refs":[999]}
        ])
