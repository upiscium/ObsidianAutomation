"""Optional d1 hint layer overlays the exact existing verified C5 source refs."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
import json

import pytest

from obsidian_automation import github_daily_d1_c5_presentation as a
from obsidian_automation import github_daily_d1_priority_shadow as d1
from obsidian_automation import github_daily_narrative_runtime as rt
from obsidian_automation import github_daily_c5_source_view as recent
from obsidian_automation import github_daily_witnessed_narrative as c4

from test_github_daily_d1_priority_shadow import (
    CAP, PROFILE, PROFILE_REVISION, contexts, response,
)
from test_github_daily_narrative_runtime import (
    case, kwargs, backend, MODEL, DIGEST, TEXT, safe_layout,
)


def model_plan():
    cs=contexts()
    plan=d1._original_plan(cs,CAP)
    return cs[-1],plan


def make_shadow(plan, segment_ids, *, cls="must_include", state="observed"):
    entries=plan.report()["paragraphs"]
    assert all(id_ in {e["id"] for e in entries} for id_ in segment_ids)
    rows=[]
    for id_ in segment_ids:
        req=d1.build_d1_decision_request(plan,id_,profile=PROFILE,
                                         profile_revision=PROFILE_REVISION)
        if state=="observed":
            row=d1.admit_tsumgi_d1_observation(req,response(req,choice=cls))
        else:
            row={"segment_id":id_,"status":"abstained","recommendation":None,
                 "authority":"advisory_only","source_deleted":False,
                 "publication_authorized":False}
        rows.append(row)
    return d1.ShadowResult(d1._json({
        "record_version":d1.VERSION,
        "stage":"daily_c5_d1_priority_shadow",
        "source_plan_sha256":plan.sha256,
        "source_capture_sha256":plan.report()["source_capture_sha256"],
        "segment_count":len(entries),
        "requested_segment_count":len(rows),
        "unrequested_segment_count":len(entries)-len(rows),
        "observations":rows,
        "summary":{k:sum(row["status"]==k for row in rows)
                   for k in ("observed","abstained","pending")},
        "model_primary":d1.MODEL,
        "question_version":d1.QUESTION_VERSION,
        "score_calibrated":False,
        "source_evidence_deleted":False,
        "may_filter_C5_source":False,
        "publication_authorized":False,
    }))


def test_d1_advisory_hint_changes_presentation_only_not_c4_original_refs():
    ctx,plan=model_plan()
    target=next(row for row in plan.report()["paragraphs"]
                if row["entity"]["number"]==374
                and row["original_kind"]=="pull_request_commit"
                and row["source_ref"]==1)
    shadow=make_shadow(plan,[target["id"]])
    baseline=recent.build_recent_c5_source_view(ctx)
    original=deepcopy(ctx)
    view=a.build_private_advisory_c5_view(ctx,plan,shadow)
    parsed=view.report()
    assert view.hint_count==1
    assert view.shown_source_refs==baseline.shown_source_refs
    assert parsed["shown_source_refs"]==list(baseline.shown_source_refs)
    assert parsed["omitted_source_refs"]==list(baseline.omitted_source_refs)
    assert parsed["primary_progress_source_ref"]==baseline.primary_source_ref
    assert parsed["sources"]==baseline.report()["sources"]
    assert parsed["source_event_count"]==baseline.source_event_count
    assert parsed["advisory_model_unverified"] is True
    assert parsed["advisory_has_no_selection_authority"] is True
    assert parsed["advisory_original_plan_sha256"]==plan.sha256
    assert parsed["advisory_observation_sha256"]==d1._sha(shadow.canonical_bytes)
    hint=parsed["advisory_priority_hints"][0]
    assert hint["source_ref"]==1
    assert hint["verbatim_original_excerpt"]==target["text"]
    assert target["text"]==ctx["sources"][1]["original_event"]["message"]["text"][hint["start"]:hint["end"]]
    assert hint["importance_certified"] is False
    assert hint["source_authority"]=="original_C4_only"
    assert ctx==original
    assert view.canonical_bytes!=baseline.canonical_bytes
    assert len(view.canonical_bytes)<=recent.MAX_WIRE_BYTES


def test_d1_abstention_or_background_keeps_exact_prior_c5_source_view():
    ctx,plan=model_plan()
    id_=next(e["id"] for e in plan.report()["paragraphs"]
             if e["entity"]["number"]==374 and e["source_ref"]==1)
    for state,cls in (("abstained","must_include"),("observed","background")):
        shadow=make_shadow(plan,[id_],state=state,cls=cls)
        view=a.build_private_advisory_c5_view(ctx,plan,shadow)
        baseline=recent.build_recent_c5_source_view(ctx)
        assert view.canonical_bytes==baseline.canonical_bytes
        assert view.hint_count==0
        assert view.shown_source_refs==baseline.shown_source_refs


def test_d1_observed_important_omitted_ref_adds_original_verbatim_citable_excerpt():
    ctx,plan=model_plan()
    item=next(e for e in plan.report()["paragraphs"] if e["entity"]["number"]==374 and e["source_ref"]==0)
    shadow=make_shadow(plan,[item["id"]])
    view=a.build_private_advisory_c5_view(ctx,plan,shadow)
    assert view.hint_count==1
    assert view.supplemental_source_refs == (0,)
    assert view.shown_source_refs == (0,1,2)
    assert view.report()["baseline_recent_source_refs"]==[1,2]
    assert view.report()["supplemental_excerpt_source_refs"]==[0]
    hint=view.report()["advisory_priority_hints"][0]
    assert hint["source_ref"]==0
    assert hint["verbatim_original_excerpt"]==item["text"]
    assert view.report()["omitted_source_refs"]==[]
    attempt=rt._json({"sentences":[{"text":"対象日内の実装が進展しました。",
                                     "source_refs":[0]}]})
    # Without the trusted C4/d1 sidecar, a direct admission must reject.
    with pytest.raises(rt.NarrativeRuntimeError,match="missing verified"):
        rt._admit_editorial(ctx,attempt,rt.D1_ADVISORY_SOURCE_CONTRACT)
    admitted=rt._admit_editorial(
        ctx,attempt,rt.D1_ADVISORY_SOURCE_CONTRACT,
        verified_advisory_refs=view.shown_source_refs)
    assert json.loads(admitted)["pending_source_refs"]==[1,2]


def test_forged_relevance_score_cannot_become_unchecked_source_admission():
    ctx,plan=model_plan()
    item=next(e for e in plan.report()["paragraphs"] if e["entity"]["number"]==374 and e["source_ref"]==1)
    shadow=make_shadow(plan,[item["id"]])
    original=shadow.report()
    for change in ("wrong-cas","wrong-plan","nonfinite","forged-deployment",
                   "source-deleted","unknown-segment","huge-number","forged-counts"):
        q=deepcopy(original)
        if change=="wrong-cas":q["source_capture_sha256"]="b"*64
        elif change=="wrong-plan":q["source_plan_sha256"]="a"*64
        elif change=="nonfinite":q["observations"][0]["scores"]["must_include"]=float("nan")
        elif change=="forged-deployment":q["observations"][0]["observed_deployment_revision"]="sha256:"+"f"*64
        elif change=="source-deleted":q["observations"][0]["source_deleted"]=True
        elif change=="unknown-segment":q["observations"][0]["segment_id"]="forged-segment"
        elif change=="huge-number":q["observations"][0]["scores"]["must_include"]=10**400
        elif change=="forged-counts":q["summary"]["observed"]=123
        raw=json.dumps(q,ensure_ascii=False,allow_nan=True).encode()
        with pytest.raises(a.D1C5PresentationError):
            a.build_private_advisory_c5_view(ctx,plan,d1.ShadowResult(raw))


def test_c4_original_evidence_fragment_mismatch_fails_without_repair():
    ctx,plan=model_plan()
    item=next(e for e in plan.report()["paragraphs"] if e["entity"]["number"]==374 and e["source_ref"]==1)
    shadow=make_shadow(plan,[item["id"]])
    for corruption in ("message","evidence_id","commit","draft","field"):
        changed=deepcopy(ctx)
        evt=changed["sources"][1]["original_event"]
        if corruption=="message":evt["message"]["text"]+=" forged text"
        elif corruption=="evidence_id":evt["evidence_id"]="forged"
        elif corruption=="commit":evt["sha"]="a"*40
        elif corruption=="draft":evt["draft"]=True
        else:evt["message"]=None
        with pytest.raises(a.D1C5PresentationError):
            a.build_private_advisory_c5_view(changed,plan,shadow)


def test_c4_original_context_cannot_be_bound_to_wrong_cas_date_or_entity():
    ctx,plan=model_plan()
    item=next(e for e in plan.report()["paragraphs"] if e["entity"]["number"]==374 and e["source_ref"]==1)
    shadow=make_shadow(plan,[item["id"]])
    altered=deepcopy(ctx)
    altered["capture_sha256"]="b"*64
    with pytest.raises(a.D1C5PresentationError):
        a.build_private_advisory_c5_view(altered,plan,shadow)


class D1AdvisoryTransport:
    def __init__(self):
        self.calls=[]
    def __call__(self, base_url, *, method, path, payload, timeout):
        self.calls.append((method,path,deepcopy(payload),timeout))
        if path=="/api/tags":
            return {"models":[{"name":MODEL,"model":MODEL,"digest":DIGEST}]}
        assert path=="/api/chat" and method=="POST"
        source=json.loads(payload["messages"][1]["content"])
        if "sources" in source:
            refs=source["shown_source_refs"]
            assert refs
            assert len(refs)<=3
            assert set(source["omitted_source_refs"])==set(range(source["source_event_count"]))-set(refs)
            assert payload["format"]["properties"]["sentences"]["items"]["properties"]["source_refs"]["items"]["enum"]==refs
            if "advisory_priority_hints" in source:
                assert payload["messages"][0]["content"]==rt.D1_ADVISORY_PROMPT
            else:
                assert payload["messages"][0]["content"]==rt.RECENT_SOURCE_PROMPT
            raw={"sentences":[{"text":TEXT,"source_refs":[refs[-1]]}]}
        else:
            assert "original_events" in source
            assert payload["messages"][0]["content"]==rt.SEMANTIC_GROUND_PROMPT
            raw={"verdict":"supported","reason":"元の証拠と一致します。"}
        return {"done":True,"done_reason":"stop","model":MODEL,
                "message":{"role":"assistant","content":json.dumps(raw,ensure_ascii=False)}}


def test_d1_optional_model_request_and_full_original_post_edit_grounding(case):
    args=kwargs(case)
    plan=d1.build_private_d1_priority_plan(**args)
    contexts_=c4.build_witnessed_narrative_contexts(**args).report()["contexts"]
    assert len(contexts_)==2
    visible=recent.build_recent_c5_source_view(contexts_[0]["context"])
    entity=contexts_[0]["context"]["entity"]
    candidates=[p for p in plan.report()["paragraphs"]
                if p["entity"]==entity and p["source_ref"] in visible.shown_source_refs]
    assert candidates
    shadow=make_shadow(plan,[candidates[0]["id"]])
    transport=D1AdvisoryTransport()
    infer=rt.ollama_narrative_infer(
        base_url="http://127.0.0.1:11434",model="fixture",
        transport=transport,editorial_contract=rt.D1_ADVISORY_SOURCE_CONTRACT,
        d1_priority_plan=plan,d1_priority_shadow=shadow)
    assert transport.calls==[]
    out=rt.run_witnessed_narrative_inference(
        **args,infer=infer,max_entities=2,max_calls=8,max_elapsed_seconds=120,
        d1_priority_plan=plan,d1_priority_shadow=shadow)
    assert out.report()["inference_responses_complete"] is True
    assert len(out.attempts)==4
    assert all(a.state=="accepted" for a in out.attempts)
    assert len(out.narrative_replies)==2
    assert len(out.grounding_replies)==2
    assert all(a.receipt_bytes is not None for a in out.attempts)
    editorial_receipts=[json.loads(a.receipt_bytes) for a in out.attempts if a.stage=="editorial"]
    assert all(m["response_contract"]==rt.D1_ADVISORY_SOURCE_CONTRACT for m in editorial_receipts)
    assert all(m["d1_advisory_plan_sha256"]==plan.sha256 for m in editorial_receipts)
    assert all(m["d1_advisory_shadow_sha256"]==d1._sha(shadow.canonical_bytes) for m in editorial_receipts)
    assert all(m["d1_advisory_model_has_source_authority"] is False for m in editorial_receipts)
    assert out.report()["publication_authorized"] is False
    assert out.report()["semantic_coverage_verified"] is False


def test_d1_contract_requires_private_source_plan_and_shadow_without_network():
    transport=D1AdvisoryTransport()
    with pytest.raises(rt.NarrativeRuntimeError,match="requires supplied"):
        rt.ollama_narrative_infer(base_url="http://127.0.0.1:11434",
            model="fixture",transport=transport,
            editorial_contract=rt.D1_ADVISORY_SOURCE_CONTRACT)
    ctx,plan=model_plan()
    shadow=make_shadow(plan,[])
    with pytest.raises(rt.NarrativeRuntimeError,match="must not affect"):
        rt.ollama_narrative_infer(base_url="http://127.0.0.1:11434",
            model="fixture",transport=transport,
            editorial_contract=rt.RECENT_SOURCE_CONTRACT,
            d1_priority_plan=plan,d1_priority_shadow=shadow)
    assert transport.calls==[]


def test_d1_supplement_does_not_erase_pending_original_snapshot_or_allow_citing_it():
    ctx,plan=model_plan()
    item=next(x for x in plan.report()["paragraphs"]
              if x["entity"]["number"]==374 and x["source_ref"]==0)
    from test_github_daily_c5_source_view import event
    appended=event(3,"pull_request_snapshot",body="Earlier snapshot remains in immutable C4 CAS.")
    ctx["sources"].append(appended)
    shadow=make_shadow(plan,[item["id"]])
    view=a.build_private_advisory_c5_view(ctx,plan,shadow)
    assert set(view.shown_source_refs)=={0,1,2}
    assert view.report()["omitted_source_refs"]==[3]
    malicious=rt._json({"sentences":[{"text":"不要な古い情報を引用しました。",
                                      "source_refs":[3]}]})
    with pytest.raises(rt.NarrativeRuntimeError,match="not model-visible"):
        rt._admit_editorial(
            ctx,malicious,rt.D1_ADVISORY_SOURCE_CONTRACT,
            verified_advisory_refs=view.shown_source_refs)


def test_full_model_c5_receipt_must_match_derived_priority_plan_and_input(case):
    args=kwargs(case)
    plan=d1.build_private_d1_priority_plan(**args)
    contexts_=c4.build_witnessed_narrative_contexts(**args).report()["contexts"]
    valid=recent.build_recent_c5_source_view(contexts_[0]["context"])
    identity=contexts_[0]["context"]["entity"]
    item=next(x for x in plan.report()["paragraphs"]
              if x["entity"]==identity and x["source_ref"] in valid.shown_source_refs)
    shadow=make_shadow(plan,[item["id"]])
    t=D1AdvisoryTransport()
    original=rt.ollama_narrative_infer(
        base_url="http://127.0.0.1:11434",model="fixture",transport=t,
        editorial_contract=rt.D1_ADVISORY_SOURCE_CONTRACT,
        d1_priority_plan=plan,d1_priority_shadow=shadow)
    def forged(raw, **kwargs):
        result=original(raw,**kwargs)
        meta=json.loads(result.receipt_bytes)
        if meta["stage"]=="github_daily_narrative_context_v4":
            meta["d1_advisory_shadow_sha256"]="f"*64
        return rt.NarrativeCallResult(result.raw_response_bytes,rt._json(meta))
    out=rt.run_witnessed_narrative_inference(
        **args,infer=forged,max_entities=2,max_calls=8,
        d1_priority_plan=plan,d1_priority_shadow=shadow)
    assert not out.narrative_replies
    assert not out.grounding_replies
    assert out.report()["inference_responses_complete"] is False
    assert out.report()["publication_authorized"] is False
    assert all(a.state=="inference_or_validation_failed" for a in out.attempts)


def test_preflight_source_binding_rejects_forged_d1_plan_before_model_calls(case):
    args=kwargs(case)
    plan=d1.build_private_d1_priority_plan(**args)
    row=deepcopy(plan.report())
    assert row["paragraphs"]
    row["paragraphs"][0]["text"] += " forged"
    forged_plan=d1.PriorityPlan(d1._json(row))
    shadow=make_shadow(plan,[])
    calls=[]
    with pytest.raises(rt.NarrativeRuntimeError):
        rt.run_witnessed_narrative_inference(
            **args,infer=lambda req,**kw:calls.append(req),
            d1_priority_plan=forged_plan,d1_priority_shadow=shadow)
    assert not calls


def test_advisory_schema_bounded_short_sentences_when_d1_hints_exist(case):
    args=kwargs(case)
    plan=d1.build_private_d1_priority_plan(**args)
    contexts_=c4.build_witnessed_narrative_contexts(**args).report()["contexts"]
    source=contexts_[0]["context"]
    b=recent.build_recent_c5_source_view(source)
    item=next(x for x in plan.report()["paragraphs"]
              if x["entity"]==source["entity"] and x["source_ref"] in b.shown_source_refs)
    shadow=make_shadow(plan,[item["id"]])
    adapter=D1AdvisoryTransport()
    backend=rt.ollama_narrative_infer(
        base_url="http://127.0.0.1:11434",model="fixture",
        transport=adapter,editorial_contract=rt.D1_ADVISORY_SOURCE_CONTRACT,
        d1_priority_plan=plan,d1_priority_shadow=shadow)
    run=rt.run_witnessed_narrative_inference(
        **args,infer=backend,max_entities=2,max_calls=8,
        d1_priority_plan=plan,d1_priority_shadow=shadow)
    assert run.report()["inference_responses_complete"] is True
    request=next(c[2] for c in adapter.calls
                 if c[1]=="/api/chat" and "advisory_priority_hints" in c[2]["messages"][1]["content"])
    fields=request["format"]["properties"]["sentences"]["items"]["properties"]
    assert fields["text"]["maxLength"]==180
    assert fields["text"]["pattern"]==r"^[^\r\n]+$"
