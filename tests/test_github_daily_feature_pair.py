"""Feature mention routing never bypasses original C5 or C4 Grounding."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import pytest

from obsidian_automation import github_daily_feature_stage as feature
from obsidian_automation import github_daily_feature_pair as pair
from obsidian_automation import github_daily_d1_priority_shadow as d1
from obsidian_automation import github_daily_narrative_runtime as rt
from obsidian_automation import github_daily_witnessed_narrative as c4
from test_github_daily_feature_stage import context_plan, normalized_vote, CAP

MODEL="fixture-gemma4"
DIGEST="c"*64


class FeatureTransport:
    def __init__(self):
        self.calls=[]
    def __call__(self, base_url, *, method, path, payload, timeout):
        self.calls.append((method,path,deepcopy(payload)))
        if path=="/api/tags":
            return {"models":[{"name":MODEL,"model":MODEL,"digest":DIGEST}]}
        assert method=="POST" and path=="/api/chat"
        source=json.loads(payload["messages"][1]["content"])
        assert source["view_version"]==feature.VERSION
        assert source["feature_implementation_evidence"]
        assert source["publication_authorized"] is False
        assert source["source_deleted"] is False
        assert payload["messages"][0]["content"]==rt.FEATURE_FIRST_PROMPT
        refs=source["shown_source_refs"]
        assert refs
        assert payload["format"]["properties"]["sentences"]["items"]["properties"]["source_refs"]["items"]["enum"]==refs
        content=json.dumps({"sentences":[
            {"text":"永続的な操作キューを実装しました。","source_refs":[source["feature_implementation_evidence"][0]["source_ref"]]},
            {"text":"特権Workerは未導入です。","source_refs":[source["latest_operational_caveat_evidence"][0]["source_ref"]]}
        ]},ensure_ascii=False)
        return {"done":True,"done_reason":"stop","model":MODEL,
                "message":{"role":"assistant","content":content}}


def context_for_infer():
    contexts,plan=context_plan()
    ctx=deepcopy(contexts[1])
    ctx["record_version"]=c4.VERSION
    ctx["publication_authorized"]=False
    ctx["system_prompt"]=c4.EDITORIAL_PROMPT
    ctx["prompt_version"]="github-daily-narrative-ja-v1"
    ctx["response_schema"]={
        "type":"object",
        "properties":{"sentences":{
            "type":"array","maxItems":3,
            "items":{"type":"object","properties":{
                "text":{"type":"string","maxLength":360},
                "source_refs":{"type":"array","items":{"type":"integer"}}}}
        }}}
    return ctx,plan


def test_feature_first_infer_model_view_receipt_and_original_admission():
    ctx,plan=context_for_infer()
    transport=FeatureTransport()
    infer=rt.ollama_narrative_infer(
        base_url="http://127.0.0.1:11434",model=MODEL,transport=transport,
        editorial_contract=rt.FEATURE_FIRST_SOURCE_CONTRACT,feature_source_plan=plan)
    assert transport.calls==[]  # no eager network
    request_bytes=rt._json(ctx)
    result=infer(request_bytes)
    assert result.raw_response_bytes
    receipt=json.loads(result.receipt_bytes)
    view=feature.build_feature_first_view(ctx,plan)
    assert receipt["response_contract"]==rt.FEATURE_FIRST_SOURCE_CONTRACT
    assert receipt["feature_first_view_sha256"]==view.sha256
    assert receipt["feature_first_source_plan_sha256"]==plan.sha256
    assert receipt["feature_first_model_has_source_authority"] is False
    assert receipt["model_input_sha256"]==d1._sha(view.canonical_bytes)
    admitted=rt._admit_editorial(
        ctx,result.raw_response_bytes,rt.FEATURE_FIRST_SOURCE_CONTRACT,
        verified_feature_refs=view.shown_source_refs)
    messages=json.loads(admitted)
    assert len(messages["sentences"])==2
    assert sorted(set(messages["pending_source_refs"]+[
        k for v in messages["sentences"] for k in v["source_refs"]
    ]))==list(range(len(ctx["sources"])))
    assert messages["sentences"][0]["source_refs"][0] in view.feature_source_refs
    assert messages["sentences"][1]["source_refs"][0] in view.status_source_refs


def test_feature_first_schema_ref_rejection_cannot_add_unknown_original_event():
    ctx,plan=context_for_infer()
    view=feature.build_feature_first_view(ctx,plan)
    unknown=next(i for i in range(len(ctx["sources"])) if i not in view.shown_source_refs)
    raw=rt._json({"sentences":[{"text":"新機能を実装しました。","source_refs":[unknown]}]})
    with pytest.raises(rt.NarrativeRuntimeError,match="not model-visible"):
        rt._admit_editorial(ctx,raw,rt.FEATURE_FIRST_SOURCE_CONTRACT,
                           verified_feature_refs=view.shown_source_refs)
    with pytest.raises(rt.NarrativeRuntimeError):
        rt._admit_editorial(ctx,raw,rt.FEATURE_FIRST_SOURCE_CONTRACT)


@dataclass
class FakePreview:
    sha256: str = "a"*64

@dataclass
class FakeRun:
    preview: FakePreview = None
    def __post_init__(self):
        self.preview=FakePreview()
    def report(self):
        return {"inference_responses_complete":True}


def test_pair_always_runs_baseline_then_feature_even_if_d1_says_no(monkeypatch):
    contexts,plan=context_plan()
    view=feature.build_feature_first_view(contexts[1],plan)
    # Valid explicit NO observation should NOT be an exclusive gate.
    req=feature.compile_binary_gate_request(view)
    value=json.loads(normalized_vote(req))
    value["choice_id"]="no"
    value["scores"]=[{"id":"yes","score":.1},{"id":"no","score":.9}]
    vote=feature.admit_binary_gate_vote(req,feature._json(value))
    called=[]
    class ContextList:
        def report(self):
            return {"contexts":[{"context":contexts[0],"context_sha256":"b"*64},{"context":contexts[1],"context_sha256":"a"*64}]}
    monkeypatch.setattr(pair.c4,"build_witnessed_narrative_contexts",lambda *a,**kw:ContextList())
    def fake_run(*args,**kw):
        called.append(kw)
        return FakeRun()
    monkeypatch.setattr(pair.rt,"run_witnessed_narrative_inference",fake_run)
    result=pair.run_private_feature_pair(CAP,(),(),target_entity=(
        "upiscium/Test","pull_request",374),source_plan=plan,
        baseline_infer=lambda _:None,feature_infer=lambda _:None,gate_vote=vote)
    assert len(called)==2
    assert "feature_source_plan" not in called[0]
    assert called[1]["feature_source_plan"] is plan
    assert result.report()["routing"]["trigger"]=="structural_fail_open"
    assert result.report()["routing"]["run_baseline_C5"] is True
    assert result.report()["source_evidence_deleted"] is False
    assert result.report()["publication_authorized"] is False


def test_no_feature_no_vote_runs_only_baseline_and_preserves_source(monkeypatch):
    contexts,plan=context_plan()
    no=deepcopy(contexts[1])
    for e in no["sources"]:
        if e["original_event"]["kind"]=="pull_request_commit":
            e["original_event"]["message"]["text"]="docs: process notes"
            e["original_event"]["sha"]="2"*40
    source_plan=d1._original_plan([contexts[0],no],CAP)
    class ContextList:
        def report(self):return {"contexts":[{"context":contexts[0],"context_sha256":"b"*64},{"context":no,"context_sha256":"a"*64}]}
    monkeypatch.setattr(pair.c4,"build_witnessed_narrative_contexts",lambda *a,**k:ContextList())
    called=[]
    def fake_run(*a,**k):
        called.append(k)
        return FakeRun()
    monkeypatch.setattr(pair.rt,"run_witnessed_narrative_inference",fake_run)
    outcome=pair.run_private_feature_pair(CAP,(),(),target_entity=(
        "upiscium/Test","pull_request",374),source_plan=source_plan,
        baseline_infer=lambda _:None,feature_infer=None)
    assert len(called)==1
    assert outcome.feature_first is None
    assert outcome.report()["routing"]["trigger"]=="baseline_only"


def test_optional_feature_failure_does_not_invalidate_completed_baseline(monkeypatch):
    contexts,plan=context_plan()
    class ContextList:
        def report(self):return {"contexts":[{"context":contexts[0],"context_sha256":"b"*64},{"context":contexts[1],"context_sha256":"a"*64}]}
    monkeypatch.setattr(pair.c4,"build_witnessed_narrative_contexts",lambda *a,**kw:ContextList())
    counter=[]
    def fake_run(*args,**kw):
        counter.append(1)
        if len(counter)==2:
            raise rt.NarrativeRuntimeError("feature-first private constraint failure")
        return FakeRun()
    monkeypatch.setattr(pair.rt,"run_witnessed_narrative_inference",fake_run)
    res=pair.run_private_feature_pair(CAP,(),(),target_entity=(
        "upiscium/Test","pull_request",374),source_plan=plan,
        baseline_infer=lambda _:None,feature_infer=lambda _:None)
    assert len(counter)==2
    assert res.baseline is not None
    assert res.feature_first is None
    assert res.report()["feature_first_failure_class"]=="NarrativeRuntimeError"
    assert res.report()["baseline_complete"] is True


def test_optional_feature_callback_missing_keeps_completed_baseline(monkeypatch):
    contexts,plan=context_plan()
    class ContextList:
        def report(self):
            return {"contexts":[
                {"context":contexts[0],"context_sha256":"b"*64},
                {"context":contexts[1],"context_sha256":"a"*64}]}
    monkeypatch.setattr(pair.c4,"build_witnessed_narrative_contexts",lambda *a,**kw:ContextList())
    calls=[]
    def fake_run(*args,**kwargs):
        calls.append(kwargs)
        return FakeRun()
    monkeypatch.setattr(pair.rt,"run_witnessed_narrative_inference",fake_run)
    result=pair.run_private_feature_pair(CAP,(),(),target_entity=(
        "upiscium/Test","pull_request",374),source_plan=plan,
        baseline_infer=lambda _:None,feature_infer=None)
    assert len(calls)==1 and "feature_source_plan" not in calls[0]
    assert result.baseline is not None and result.feature_first is None
    assert result.report()["baseline_complete"] is True
    assert result.report()["feature_first_failure_class"]=="NarrativeRuntimeError"
    assert result.report()["publication_authorized"] is False


def test_invalid_vote_or_optional_source_plan_does_not_stop_baseline(monkeypatch):
    contexts,plan=context_plan()
    class ContextList:
        def report(self):
            return {"contexts":[
                {"context":contexts[0],"context_sha256":"b"*64},
                {"context":contexts[1],"context_sha256":"a"*64}]}
    monkeypatch.setattr(pair.c4,"build_witnessed_narrative_contexts",lambda *a,**kw:ContextList())
    calls=[]
    monkeypatch.setattr(pair.rt,"run_witnessed_narrative_inference",
                        lambda *a,**kw: calls.append(kw) or FakeRun())
    fake_vote=feature.GateVote(feature._json({
        "feature_source_view_sha256":"f"*64,"model_primary":"d1-3B",
        "status":"observed","uncalibrated":True,
        "publication_authorized":False,"source_deleted":False,
    }))
    outcome=pair.run_private_feature_pair(CAP,(),(),target_entity=(
        "upiscium/Test","pull_request",374),source_plan=plan,
        baseline_infer=lambda _:None,feature_infer=lambda _:None,
        gate_vote=fake_vote)
    assert len(calls)==1
    assert outcome.report()["feature_first_failure_class"]=="FeatureStageError"
    outcome2=pair.run_private_feature_pair(CAP,(),(),target_entity=(
        "upiscium/Test","pull_request",374),source_plan=None,
        baseline_infer=lambda _:None,feature_infer=None)
    assert len(calls)==2
    assert outcome2.report()["baseline_complete"] is True
    assert outcome2.report()["feature_first_failure_class"]=="NarrativeRuntimeError"


def test_optional_feature_only_grounding_disambiguates_unavailable_ui_labels():
    assert rt.FEATURE_FIRST_GROUND_PROMPT != rt.SEMANTIC_GROUND_PROMPT
    assert "X and Y stay unavailable" in rt.FEATURE_FIRST_GROUND_PROMPT
    assert "Run once" in rt.FEATURE_FIRST_GROUND_PROMPT
    from test_github_daily_narrative_runtime import MODEL as NAME, DIGEST as REV
    from obsidian_automation import github_daily_witnessed_narrative as verified
    seen=[]
    def transport(base_url,*,method,path,payload,timeout):
        if path=="/api/tags":
            return {"models":[{"name":NAME,"digest":REV,"model":NAME}]}
        assert path=="/api/chat" and method=="POST"
        seen.append(payload["messages"][0]["content"])
        assert payload["format"]["required"]==["verdict","reason"]
        return {"done":True,"model":NAME,"done_reason":"stop",
                "message":{"role":"assistant","content":
                           json.dumps({"verdict":"supported","reason":"同じ両操作の利用不可。"},
                                      ensure_ascii=False)}}
    stage={
        "record_version":verified.VERSION,
        "stage":"github_daily_narrative_ground_context_v4",
        "publication_authorized":False,
        "system_prompt":verified.GROUND_PROMPT,
        "prompt_version":"github-daily-narrative-ground-ja-v1",
        "entity":{"repository":"upiscium/Test","entity_type":"pull_request","number":374},
        "date":"2026-10-16",
        "sentence":{"text":"「Run once」とブラウザの「Execute」は利用できません。"},
        "original_events":[{"source_ref":12,"body":{
             "text":"Run once and all browser Execute paths stay unavailable.",
             "truncated":False}}],
    }
    profile=d1.PriorityPlan(b'{"private":"source-bound-fixture"}')
    specialized=rt.ollama_narrative_infer(
        base_url="http://127.0.0.1:11434",model="fixture",
        editorial_contract=rt.FEATURE_FIRST_SOURCE_CONTRACT,
        feature_source_plan=profile,transport=transport)
    adapted=specialized(rt._json(stage))
    receipt=json.loads(adapted.receipt_bytes)
    assert receipt["effective_prompt_version"]==rt.FEATURE_FIRST_GROUND_PROMPT_VERSION
    assert seen[-1]==rt.FEATURE_FIRST_GROUND_PROMPT
    assert json.loads(adapted.raw_response_bytes)["verdict"]=="supported"
    standard=rt.ollama_narrative_infer(
        base_url="http://127.0.0.1:11434",model="fixture",
        editorial_contract=rt.RECENT_SOURCE_CONTRACT,transport=transport)
    normal=standard(rt._json(stage))
    normal_receipt=json.loads(normal.receipt_bytes)
    assert normal_receipt["effective_prompt_version"]==rt.SEMANTIC_GROUND_PROMPT_VERSION
    assert seen[-1]==rt.SEMANTIC_GROUND_PROMPT


def test_pair_audits_test_only_false_completion_even_without_feature_yes(monkeypatch):
    from test_github_daily_scope_quality import suzka
    ctx = suzka()
    plan = d1._original_plan([ctx], CAP)
    class Contexts:
        def report(self):
            return {"contexts": [{"context": ctx, "context_sha256": "a"*64}]}
    class Preview:
        sha256 = "b"*64
        def report(self):
            return {"entities": [{"sentences": [{
                "text": "テスト専用事前検証の機能実装は完了しました。",
                "source_refs": [0], "verdict": "supported"}]}]}
    class Run:
        preview = Preview()
        def report(self):
            return {"inference_responses_complete": True}
    monkeypatch.setattr(pair.c4, "build_witnessed_narrative_contexts",
                        lambda *a, **kw: Contexts())
    monkeypatch.setattr(pair.rt, "run_witnessed_narrative_inference",
                        lambda **kw: Run())
    result = pair.run_private_feature_pair(
        CAP, (), (), target_entity=("upiscium/PROJECT-SUZKA", "pull_request", 330),
        source_plan=plan, baseline_infer=lambda *_: None, feature_infer=None)
    audit = result.report()["baseline_development_scope_check"]
    assert audit["source_claims"]["test_only"] is True
    assert "test_only_pr_claimed_product_feature_complete" in audit["known_rule_violations"]
    assert result.report()["feature_first_development_scope_check"] is None
    assert result.report()["publication_authorized"] is False


def test_pair_flags_missing_ga_site_gate_in_alternate_not_baseline(monkeypatch):
    from test_github_daily_scope_quality import tsumgi
    ctx = tsumgi()
    plan = d1._original_plan([ctx], CAP)
    class Contexts:
        def report(self):
            return {"contexts": [{"context": ctx, "context_sha256": "a"*64}]}
    class Preview:
        sha256 = "b"*64
        def __init__(self, text):
            self.text = text
        def report(self):
            return {"entities": [{"sentences": [{
                "text": self.text, "source_refs": [0], "verdict": "supported"}]}]}
    class Run:
        def __init__(self, text):
            self.preview = Preview(text)
        def report(self):
            return {"inference_responses_complete": True}
    called = []
    def run_model(**kw):
        called.append(kw)
        return Run("本番への反映は未実施です。" if len(called)==1
                   else "Bonsai 2 PQ2_0バックエンドを実装しました。")
    monkeypatch.setattr(pair.c4, "build_witnessed_narrative_contexts",
                        lambda *a, **kw: Contexts())
    monkeypatch.setattr(pair.rt, "run_witnessed_narrative_inference", run_model)
    result = pair.run_private_feature_pair(
        CAP, (), (), target_entity=("upiscium/TSUMGI", "pull_request", 155),
        source_plan=plan, baseline_infer=lambda *_: None, feature_infer=lambda *_: None)
    report = result.report()
    assert len(called) == 2
    assert report["baseline_development_scope_check"]["known_rule_violations"] == []
    assert "original_no_deploy_condition_omitted" in report[
        "feature_first_development_scope_check"]["known_rule_violations"]
    assert report["publication_authorized"] is False


def test_optional_no_supported_grounding_does_not_claim_quality_green(monkeypatch):
    contexts,plan=context_plan()
    target=contexts[1]
    class Contexts:
        def report(self):
            return {"contexts":[
                {"context":contexts[0],"context_sha256":"b"*64},
                {"context":target,"context_sha256":"a"*64},
            ]}
    class Preview:
        sha256="b"*64
        def __init__(self, supported):
            self.supported=supported
        def report(self):
            return {"entities":[{"sentences":[{
                "text":"根拠のないプロファイリングを実施しました。",
                "source_refs":[1], "verdict":"supported" if self.supported else "unsupported"
            }]}]}
    class Run:
        def __init__(self,supported):
            self.preview=Preview(supported)
        def report(self):
            return {"inference_responses_complete":True}
    called=[]
    def fake(**kw):
        called.append(kw)
        return Run(supported=(len(called)==1))
    monkeypatch.setattr(pair.c4,"build_witnessed_narrative_contexts",
                        lambda *a,**kw:Contexts())
    monkeypatch.setattr(pair.rt,"run_witnessed_narrative_inference",fake)
    result=pair.run_private_feature_pair(
        CAP,(),(),target_entity=("upiscium/Test","pull_request",374),
        source_plan=plan,baseline_infer=lambda *_:None,
        feature_infer=lambda *_:None)
    assert len(called)==2
    doc=result.report()
    assert doc["baseline_complete"] is True
    assert doc["feature_first_complete"] is True
    assert doc["feature_first_has_no_known_operational_inversion"] is False
    assert doc["protected_operational_translation_check"]["status"]=="needs_human_review"
    assert "feature_first_no_supported_sentences" in doc[
        "protected_operational_translation_check"]["known_rule_violations"]
    assert doc["publication_authorized"] is False
