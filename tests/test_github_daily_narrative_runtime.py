"""C5: actual local source CAS + synthetic Ollama protocol; no live model."""
import copy
import hashlib
import json
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import pytest

from obsidian_automation import github_daily_narrative_runtime as rt
from obsidian_automation import github_daily_witnessed_narrative as c4
from obsidian_automation import github_daily_route_cas as cas
from test_github_daily_route_cas import safe_layout
from test_github_daily_witnessed_narrative import case, kwargs

MODEL = "fixture:latest"
DIGEST = "b"*64
TEXT = "検証処理の競合を修正し、回帰テストを追加しました。マージと本番反映は未実施です。"


class Transport:
    def __init__(self):
        self.calls = []
        self.digest = DIGEST
        self.change_during = False
        self.fail_stage = None
        self.bad_editorial = False
        self.stop = "stop"
        self.echo = MODEL
        self.ground_verdict = "supported"

    def __call__(self, base_url, *, method, path, payload, timeout):
        self.calls.append((method, path, copy.deepcopy(payload), timeout))
        if path == "/api/tags":
            return {"models": [{"name": MODEL, "model": MODEL, "digest": self.digest}]}
        assert path == "/api/chat" and method == "POST"
        ctx = json.loads(payload["messages"][1]["content"])
        stage = "editorial" if "sources" in ctx else "grounding"
        if self.fail_stage == stage:
            raise OSError("PRIVATE_ENDPOINT_TOKEN must never enter report")
        assert payload["stream"] is False and payload["think"] is False
        if "system_prompt" in ctx:
            assert payload["messages"][0]["content"] == ctx["system_prompt"]
        else:
            assert payload["messages"][0]["content"] in (c4.GROUND_PROMPT, rt.SEMANTIC_GROUND_PROMPT)
            assert set(ctx) == {"entity", "date", "sentence", "original_events"}
        assert payload["format"]["additionalProperties"] is False
        if stage == "editorial":
            raw = {"sentences": [{"text": TEXT, "source_refs": [0]}],
                   "pending_source_refs": list(range(1,len(ctx["sources"])))}
            if self.bad_editorial:
                raw["sentences"][0]["source_refs"] = [True]
        else:
            raw = {"verdict": self.ground_verdict, "reason": "元の証拠と一致します。"}
        if self.change_during:
            self.digest = "c"*64
        return {"done": True, "done_reason": self.stop, "model": self.echo,
                "message": {"role": "assistant", "content": json.dumps(raw,ensure_ascii=False)}}


def backend(t, **kw):
    return rt.ollama_narrative_infer(base_url="http://127.0.0.1:11434",
                                     model="fixture",transport=t,**kw)


def run(case, transport=None, **kw):
    return rt.run_witnessed_narrative_inference(**kwargs(case),
            infer=backend(transport or Transport()),**kw)


def test_c5_real_pipeline_provenance_and_new_grounding(case):
    t=Transport(); output=run(case,t)
    data=output.report()
    assert data["entity_count"] == 2
    assert data["inference_responses_complete"] is True
    assert len(output.narrative_replies)==len(output.grounding_replies)==2
    assert [a.stage for a in output.attempts]==["editorial"]*2+["grounding"]*2
    assert all(a.state == "accepted" for a in output.attempts)
    assert [p for _,p,_,_ in t.calls] == ["/api/tags","/api/chat","/api/tags"]*4
    for attempt, request in zip(output.attempts, [c[2] for c in t.calls if c[1]=="/api/chat"]):
        r=json.loads(attempt.receipt_bytes)
        assert r["context_sha256"]==attempt.context_sha256
        assert r["raw_response_sha256"]==hashlib.sha256(attempt.raw_response_bytes).hexdigest()
        assert r["request_sha256"]==hashlib.sha256(rt._json(request).removesuffix(b"\n")).hexdigest()
        assert r["system_prompt_sha256"]==hashlib.sha256(request["messages"][0]["content"].encode()).hexdigest()
        assert r["model_identifier"]==MODEL and r["observed_model_revision"]==DIGEST
        assert r["config"]["options"] == {"temperature":0}
        assert r["model_execution_attested"] is False
    assert TEXT in output.preview.markdown
    assert "AI下書き（未承認）" in output.preview.markdown
    assert output.preview.report()["model_execution_attested"] is False
    assert data["publication_authorized"] is False
    assert data["semantic_coverage_verified"] is False
    assert data["date"]=="2026-10-05"
    assert "text" not in data and TEXT not in output.canonical_bytes.decode()


def test_no_network_during_config_and_options_snapshot():
    t=Transport(); opts={"temperature":0,"seed":8}
    infer=backend(t,options=opts)
    opts["seed"]=99
    assert t.calls==[]
    assert callable(infer)


def test_actual_loopback_http_transport_no_real_model(case):
    t=Transport()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*a): pass
        def do_GET(self): self.respond(None)
        def do_POST(self):
            self.respond(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        def respond(self,payload):
            data=rt._json(t("local",method=self.command,path=self.path,payload=payload,timeout=60))
            self.send_response(200); self.send_header("Content-Type","application/json")
            self.send_header("Content-Length",str(len(data))); self.end_headers(); self.wfile.write(data)
    server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
    th=threading.Thread(target=server.serve_forever,daemon=True);th.start()
    try:
        infer=rt.ollama_narrative_infer(base_url=f"http://127.0.0.1:{server.server_port}",model="fixture")
        out=rt.run_witnessed_narrative_inference(**kwargs(case),infer=infer)
        assert out.report()["inference_responses_complete"] is True
        assert TEXT in out.preview.markdown
    finally:
        server.shutdown();server.server_close();th.join(timeout=5)


@pytest.mark.parametrize("failure",["editorial","grounding"])
def test_provider_failure_leaves_source_pending(case,failure):
    t=Transport();t.fail_stage=failure;out=run(case,t)
    assert out.report()["inference_responses_complete"] is False
    assert all(row["pending_evidence_ids"] for row in out.preview.report()["entities"])
    assert TEXT not in out.preview.markdown
    assert "PRIVATE_ENDPOINT_TOKEN" not in out.canonical_bytes.decode()
    if failure=="editorial": assert not out.grounding_replies


def test_one_failed_entity_does_not_erase_other_success(case):
    t=Transport(); base=backend(t); n=[0]
    def infer(raw, **kw):
        n[0]+=1
        if n[0]==1: raise OSError("fixture failure")
        return base(raw, **kw)
    out=rt.run_witnessed_narrative_inference(**kwargs(case),infer=infer)
    assert not out.report()["inference_responses_complete"]
    assert len(out.narrative_replies)==len(out.grounding_replies)==1
    assert any(e["pending_evidence_ids"] for e in out.preview.report()["entities"])
    assert TEXT in out.preview.markdown


def test_invalid_editorial_retained_privately_but_never_grounded(case):
    t=Transport();t.bad_editorial=True;out=run(case,t)
    assert not out.narrative_replies and not out.grounding_replies
    assert all(a.raw_response_bytes is not None for a in out.attempts)
    assert all(a.state=="inference_or_validation_failed" for a in out.attempts)
    assert out.report()["inference_responses_complete"] is False


def test_unsupported_sentence_is_not_provider_failure_or_displayed(case):
    t=Transport();t.ground_verdict="unsupported";out=run(case,t)
    assert out.report()["inference_responses_complete"] is True
    assert TEXT not in out.preview.markdown
    assert all(e["pending_evidence_ids"] for e in out.preview.report()["entities"])


@pytest.mark.parametrize("setting",[{"max_entities":1},{"max_calls":7},
    {"max_entities":True},{"max_calls":0},{"max_elapsed_seconds":float("nan")},
    {"max_elapsed_seconds":True},{"max_elapsed_seconds":601}])
def test_whole_request_budget_before_inference(case,setting):
    t=Transport()
    with pytest.raises(rt.NarrativeRuntimeError): run(case,t,**setting)
    assert not t.calls


def test_aggregate_source_work_limit_precedes_model(case,monkeypatch):
    t=Transport();monkeypatch.setattr(rt,"MAX_SOURCE_WORK_BYTES",1)
    with pytest.raises(rt.NarrativeRuntimeError,match="source-read"):run(case,t)
    assert not t.calls


def test_aggregate_reconstruction_count_precedes_source(case,monkeypatch):
    t=Transport();monkeypatch.setattr(rt,"MAX_SOURCE_RECONSTRUCTIONS",0)
    def no_read(*a,**kw): raise AssertionError("must not access source")
    monkeypatch.setattr(c4,"build_witnessed_narrative_contexts",no_read)
    with pytest.raises(rt.NarrativeRuntimeError,match="reconstruction"):run(case,t)
    assert not t.calls


def test_counts_actual_CAS_reads_not_per_entity(case,monkeypatch):
    n=[0];real=cas.read_verified_capture
    def counted(*a,**kw):n[0]+=1;return real(*a,**kw)
    monkeypatch.setattr(cas,"read_verified_capture",counted)
    out=run(case)
    assert n[0]==out.report()["planned_cas_triplet_reads"]


def test_deadline_stops_further_model_calls_preserving_pending(case,monkeypatch):
    clock=[0.0];monkeypatch.setattr(rt.time,"monotonic",lambda:clock[0])
    t=Transport(); real=backend(t)
    def slow(raw, **kw):
        result=real(raw, **kw);clock[0]=121;return result
    out=rt.run_witnessed_narrative_inference(**kwargs(case),infer=slow,max_elapsed_seconds=120)
    assert len([p for _,p,_,_ in t.calls if p=="/api/chat"])==1
    assert not out.narrative_replies
    assert all(a.state=="deadline_pending" for a in out.attempts)


@pytest.mark.parametrize("mode",["changed","truncated","wrong-model"])
def test_model_identity_or_completion_failure_not_admitted(case,mode):
    t=Transport()
    if mode=="changed":t.change_during=True
    elif mode=="truncated":t.stop="length"
    else:t.echo="another-model"
    out=run(case,t)
    assert not out.narrative_replies
    assert not out.report()["inference_responses_complete"]


@pytest.mark.parametrize("bad",[None,True,{},b"{}"])
def test_wrong_inference_result_is_bounded_failure(case,bad):
    out=rt.run_witnessed_narrative_inference(**kwargs(case),infer=lambda raw, **kw:bad)
    assert not out.report()["inference_responses_complete"]
    assert not out.narrative_replies


@pytest.mark.parametrize("mode",["digest","authority","raw-type","receipt-type","oversize"])
def test_receipt_forgery_does_not_admit_output(case,mode):
    base=backend(Transport())
    def bad(raw, **kw):
        result=base(raw, **kw);meta=json.loads(result.receipt_bytes)
        if mode=="digest":meta["context_sha256"]="f"*64
        if mode=="authority":meta["model_execution_attested"]=True
        content=bytearray(result.raw_response_bytes) if mode=="raw-type" else result.raw_response_bytes
        receipt={} if mode=="receipt-type" else rt._json(meta)
        if mode=="oversize":receipt=b"x"*(rt.MAX_RECEIPT_BYTES+1)
        return rt.NarrativeCallResult(content,receipt)
    out=rt.run_witnessed_narrative_inference(**kwargs(case),infer=bad)
    assert not out.narrative_replies
    assert all(a.raw_response_bytes is None or type(a.raw_response_bytes) is bytes for a in out.attempts)
    assert all(a.receipt_bytes is None or type(a.receipt_bytes) is bytes for a in out.attempts)


def test_caller_records_changed_during_inference_do_not_affect_run(case):
    # Exact primitive snapshots must predate the first callback.
    partials=tuple(replace(p) for p in case[2]);decisions=tuple(replace(d) for d in case[4])
    base=backend(Transport());first=[True]
    def mutate(raw, **kw):
        if first[0]:
            first[0]=False
            object.__setattr__(partials[0],"response_bytes",b"forged")
            object.__setattr__(decisions[0],"classification","routine")
        return base(raw, **kw)
    args=kwargs(case);args.update(partial_replies=partials,materiality_decisions=decisions)
    out=rt.run_witnessed_narrative_inference(**args,infer=mutate)
    assert out.report()["inference_responses_complete"] is True


def test_missing_source_before_call_never_contacts_model(case):
    cap=case[1];(cas.ROOT/"receipt"/cas._filename("receipt",cap.receipt_sha256)).unlink()
    t=Transport()
    with pytest.raises(rt.NarrativeRuntimeError):run(case,t)
    assert not t.calls


@pytest.mark.parametrize("url",["http://remote.invalid","https://user:secret@host","https://host/?q=x"])
def test_existing_endpoint_safety_policy_preserved(url):
    with pytest.raises(rt.NarrativeRuntimeError):
        rt.ollama_narrative_infer(base_url=url,model="fixture")


def test_disjoint_reminder_is_bound_to_request_not_silently_added(case):
    t=Transport();out=run(case,t)
    requests=[r[2] for r in t.calls if r[1]=="/api/chat"]
    for a,r in zip(out.attempts,requests):
        receipt=json.loads(a.receipt_bytes)
        if a.stage=="editorial":
            assert r["messages"][-1]=={"role":"user","content":rt.EDITORIAL_REMINDER}
            assert receipt["runtime_instruction_version"]==rt.EDITORIAL_REMINDER_VERSION
            assert receipt["runtime_instruction_sha256"]==rt._sha(rt.EDITORIAL_REMINDER.encode())
        else:
            assert len(r["messages"])==2
            assert receipt["runtime_instruction_sha256"] is None
        assert receipt["request_sha256"]==rt._sha(rt._json(r).removesuffix(b"\n"))


def test_observed_model_overlap_failure_is_not_auto_repaired(case):
    base=backend(Transport())
    def actual_failure_pattern(raw, **kw):
        result=base(raw, **kw);data=json.loads(result.raw_response_bytes)
        data["pending_source_refs"]=[0]  # Native small-model probe failure.
        response=rt._json(data);receipt=json.loads(result.receipt_bytes)
        receipt["raw_response_sha256"]=rt._sha(response)
        return rt.NarrativeCallResult(response,rt._json(receipt))
    out=rt.run_witnessed_narrative_inference(**kwargs(case),infer=actual_failure_pattern)
    assert not out.narrative_replies and not out.grounding_replies
    assert all(a.state=="inference_or_validation_failed" for a in out.attempts)
    assert all(e["pending_evidence_ids"] for e in out.preview.report()["entities"])


def test_mutated_options_do_not_change_later_requests(case):
    options={"temperature":0,"seed":7};t=Transport();infer=backend(t,options=options)
    options["seed"]=8
    rt.run_witnessed_narrative_inference(**kwargs(case),infer=infer)
    assert all(r[2]["options"]["seed"]==7 for r in t.calls if r[1]=="/api/chat")


def test_ambiguous_installed_model_fails_without_chat(case):
    t=Transport()
    def duplicate(*a,**kw):
        value=t(*a,**kw)
        if kw['path']=="/api/tags":
            value['models'].append({"name":MODEL,"model":MODEL,"digest":"c"*64})
        return value
    out=run(case,duplicate)
    assert not out.narrative_replies
    assert all(c[1]=="/api/tags" for c in t.calls)


def test_deadline_crossed_during_tags_prevents_chat(case,monkeypatch):
    clock=[0.0];monkeypatch.setattr(rt.time,"monotonic",lambda:clock[0])
    t=Transport()
    def delayed_tags(*a,**kw):
        response=t(*a,**kw)
        if kw["path"]=="/api/tags":clock[0]=121
        return response
    out=run(case,delayed_tags,max_elapsed_seconds=120)
    assert [c[1] for c in t.calls]==["/api/tags"]
    assert all(a.state=="deadline_pending" for a in out.attempts)
    assert not out.narrative_replies and not out.grounding_replies


def test_native_transport_timeouts_use_remaining_deadline(case,monkeypatch):
    clock=[10.0];monkeypatch.setattr(rt.time,"monotonic",lambda:clock[0])
    t=Transport()
    def ticking(*a,**kw):
        response=t(*a,**kw);clock[0]+=1;return response
    out=run(case,ticking,max_elapsed_seconds=20)
    assert out.report()["inference_responses_complete"] is True
    assert [c[3] for c in t.calls]==list(range(20,8,-1))
    for i,a in enumerate(out.attempts):
        meta=json.loads(a.receipt_bytes)
        assert [r["timeout_seconds"] for r in meta["http_requests"]]==[c[3] for c in t.calls[i*3:i*3+3]]


def test_recorded_request_hash_matches_existing_wire_serializer(case):
    t=Transport();out=run(case,t)
    request=[c[2] for c in t.calls if c[1]=="/api/chat"][0]
    wire=json.dumps(request,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False).encode()
    assert json.loads(out.attempts[0].receipt_bytes)["request_sha256"]==hashlib.sha256(wire).hexdigest()


class SentencesTransport(Transport):
    """Provider fixture follows the NEW requested wire contract, not C4 JSON."""
    def __init__(self, editorial=None):
        super().__init__()
        self.editorial = editorial

    def __call__(self, base_url, **kw):
        result = super().__call__(base_url, **kw)
        if kw['path'] == '/api/chat':
            payload = kw['payload']
            ctx = json.loads(payload['messages'][1]['content'])
            if 'sources' in ctx:
                assert set(payload['format']['properties']) == {'sentences'}
                assert ctx['prompt_version'] == rt.SENTENCES_ONLY_PROMPT_VERSION
                assert ctx['system_prompt'] == rt.SENTENCES_ONLY_PROMPT
                value = {'sentences': [{'text': TEXT, 'source_refs': [0]}]} if self.editorial is None else self.editorial
                result['message']['content'] = json.dumps(value, ensure_ascii=False)
        return result


def single(case, transport=None, target=('upiscium/Test','issue',1), **kw):
    t = transport or SentencesTransport()
    return rt.run_witnessed_narrative_inference(**kwargs(case),
        infer=backend(t, editorial_contract=rt.SENTENCES_ONLY_CONTRACT),
        target_entity=target, max_entities=1, max_calls=4, **kw)


@pytest.mark.parametrize('kind,number', [('issue',1),('pull_request',2)])
def test_explicit_single_entity_runs_only_that_entity_without_global_reduction(case,kind,number):
    t=SentencesTransport(); out=single(case,t,target=('upiscium/Test',kind,number))
    report=out.report()
    assert report['run_scope']=='single_entity'
    assert report['target_entity']=={'repository':'upiscium/Test','entity_type':kind,'number':number}
    assert report['entity_count']==1 and report['selected_entity_count']==2
    assert report['unrequested_entity_count']==1 and not report['all_selected_entities_requested']
    assert report['inference_responses_complete'] is True
    assert [a.stage for a in out.attempts]==['editorial','grounding']
    assert len(out.narrative_replies)==len(out.grounding_replies)==1
    assert len([p for _,p,_,_ in t.calls if p=='/api/chat'])==2
    rows=out.preview.report()['entities']
    chosen=next(r for r in rows if r['entity']['entity_type']==kind)
    other=next(r for r in rows if r['entity']['entity_type']!=kind)
    assert chosen['sentences'][0]['verdict']=='supported'
    assert not other['response_received'] and other['pending_evidence_ids']
    assert not report['publication_authorized'] and not report['semantic_coverage_verified']


def test_sentences_only_raw_and_projected_C4_bytes_are_separately_traceable(case):
    t=SentencesTransport(); out=single(case,t)
    a=out.attempts[0]; meta=json.loads(a.receipt_bytes)
    raw=json.loads(a.raw_response_bytes); mapped=json.loads(out.narrative_replies[0].response_bytes)
    assert set(raw)=={'sentences'} and 'pending_source_refs' not in raw
    assert mapped==dict(raw,pending_source_refs=[])
    assert a.raw_response_bytes != out.narrative_replies[0].response_bytes
    assert meta['response_contract']==rt.SENTENCES_ONLY_CONTRACT
    assert meta['raw_response_sha256']==rt._sha(a.raw_response_bytes)
    assert a.admitted_response_sha256==rt._sha(out.narrative_replies[0].response_bytes)
    assert out.report()['attempts'][0]['admitted_response_sha256']==a.admitted_response_sha256
    payload=next(p for _,path,p,_ in t.calls if path=='/api/chat')
    assert meta['request_sha256']==rt._sha(rt._json(payload).removesuffix(b'\n'))
    assert meta['effective_system_prompt_sha256']==rt._sha(payload['messages'][0]['content'].encode())
    assert meta['system_prompt_sha256']==rt._sha(c4.EDITORIAL_PROMPT.encode())
    assert meta['response_schema_sha256']==rt._sha(rt._json(payload['format']))
    assert not meta['model_execution_attested']


def test_empty_sentences_keeps_all_sources_pending_without_grounding(case):
    out=single(case,SentencesTransport({'sentences':[]}))
    assert len(out.attempts)==1 and out.attempts[0].state=='accepted'
    assert not out.grounding_replies
    assert all(r['pending_evidence_ids'] for r in out.preview.report()['entities'])
    assert all(not r['sentences'] for r in out.preview.report()['entities'])
    assert out.report()['inference_responses_complete'] is True # requested calls, not semantic coverage
    assert not out.report()['semantic_coverage_verified']


@pytest.mark.parametrize('obj',[
 {'sentences':[{'text':TEXT,'source_refs':[0]}],'pending_source_refs':[]},
 {'sentences':[{'text':TEXT,'source_refs':[0]}],'pending_source_refs':[0]},
 {'sentences':[{'text':TEXT,'source_refs':[True]}]},
 {'sentences':[{'text':TEXT,'source_refs':[-1]}]},
 {'sentences':[{'text':TEXT,'source_refs':[99]}]},
 {'sentences':[{'text':TEXT,'source_refs':[]}]},
 {'sentences':[{'text':TEXT,'source_refs':[0,0]}]},
 {'sentences':[{'text':TEXT,'source_refs':[0],'url':'https://invalid'}]},
 {'sentences':[{'text':TEXT,'source_refs':[0]}],'publication_authorized':True},
 {'sentences':[{'text':TEXT+'\n改行','source_refs':[0]}]},
 {'sentences':{}},
])
def test_new_contract_never_repairs_malformed_output_or_accepts_extra_pending(case,obj):
    out=single(case,SentencesTransport(obj))
    assert out.attempts[0].state=='inference_or_validation_failed'
    assert out.attempts[0].raw_response_bytes is not None
    assert out.attempts[0].admitted_response_sha256 is None
    assert not out.narrative_replies and not out.grounding_replies
    assert all(r['pending_evidence_ids'] for r in out.preview.report()['entities'])


def test_complement_uses_original_source_count_and_does_not_infer_semantic_coverage():
    ctx={'sources':[{'original_event':{'evidence_id':str(i)}} for i in range(4)]}
    raw=rt._json({'sentences':[{'text':TEXT,'source_refs':[2,0]}]})
    admitted=rt._admit_editorial(ctx,raw,rt.SENTENCES_ONLY_CONTRACT)
    assert json.loads(admitted)['pending_source_refs']==[1,3]
    assert json.loads(admitted)['sentences'][0]['source_refs']==[2,0]
    legacy=rt._json({'sentences':[{'text':TEXT,'source_refs':[0]}],'pending_source_refs':[0,1,2,3]})
    with pytest.raises(c4.DailyNarrativeError): rt._admit_editorial(ctx,legacy,rt.LEGACY_EDITORIAL_CONTRACT)


@pytest.mark.parametrize('target',[
 ['upiscium/Test','issue',1], ('upiscium/Test','issue',True),
 ('upiscium/Test','issue',0), ('upiscium/Test','unknown',1),
 ('','issue',1), ('upiscium/Test','issue',99), ('upiscium/Test','issue',2),
])
def test_invalid_or_missing_explicit_target_never_starts_model(case,target):
    t=SentencesTransport()
    with pytest.raises(rt.NarrativeRuntimeError): single(case,t,target=target)
    assert not t.calls


@pytest.mark.parametrize('mode',[None,True,[], 'future-unknown'])
def test_unknown_wire_contract_rejected_before_network(mode):
    t=Transport()
    with pytest.raises(rt.NarrativeRuntimeError):backend(t,editorial_contract=mode)
    assert not t.calls


def test_unsupported_newly_generated_text_is_not_displayed_and_sources_stay_pending(case):
    t=SentencesTransport();t.ground_verdict='unsupported';out=single(case,t)
    assert out.report()['inference_responses_complete']
    assert TEXT not in out.preview.markdown
    assert all(r['pending_evidence_ids'] for r in out.preview.report()['entities'])


def test_individual_fact_policy_is_versioned_separately_from_wire_contract(case):
    t=SentencesTransport();out=single(case,t)
    editorial=out.attempts[0];meta=json.loads(editorial.receipt_bytes)
    requests=[p for _,path,p,_ in t.calls if path=='/api/chat']
    payload=requests[0];visible=json.loads(payload['messages'][1]['content'])
    assert rt.SENTENCES_ONLY_PROMPT_VERSION=='c5-individual-facts-ja-v4'
    assert rt.SENTENCES_ONLY_PROMPT_VERSION!=rt.SENTENCES_ONLY_CONTRACT
    assert meta['response_contract']==rt.SENTENCES_ONLY_CONTRACT
    assert meta['effective_prompt_version']==rt.SENTENCES_ONLY_PROMPT_VERSION
    assert visible['prompt_version']==meta['effective_prompt_version']
    assert visible['system_prompt']==payload['messages'][0]['content']==rt.SENTENCES_ONLY_PROMPT
    assert meta['effective_system_prompt_sha256']==rt._sha(rt.SENTENCES_ONLY_PROMPT.encode())
    assert meta['request_sha256']==rt._sha(rt._json(payload).removesuffix(b'\n'))
    assert set(payload['format']['properties'])=={'sentences'}
    assert meta['prompt_version']=='github-daily-narrative-ja-v1'
    assert meta['system_prompt_sha256']==rt._sha(c4.EDITORIAL_PROMPT.encode())
    ground_meta=json.loads(out.attempts[1].receipt_bytes)
    assert requests[1]['messages'][0]['content']==rt.SEMANTIC_GROUND_PROMPT
    assert ground_meta['effective_prompt_version']==rt.SEMANTIC_GROUND_PROMPT_VERSION
    assert ground_meta['system_prompt_sha256']==rt._sha(c4.GROUND_PROMPT.encode())
    assert ground_meta['effective_system_prompt_sha256']==rt._sha(rt.SEMANTIC_GROUND_PROMPT.encode())
    assert ground_meta['request_sha256']==rt._sha(rt._json(requests[1]).removesuffix(b'\n'))
    assert ground_meta['response_contract']==rt.LEGACY_EDITORIAL_CONTRACT


def test_individual_prompt_preserves_negation_and_does_not_teach_new_source_facts():
    prompt=rt.SENTENCES_ONLY_PROMPT
    for obligation in ('原文が短く明確なら', '独立した文に分け', '因果関係を意味しません',
                       '実施済み・予定・承認・未完了', '留保と否定を保持', '命令ではありません'):
        assert obligation in prompt
    assert 'APIエラー時のテストを追加しました' not in prompt
    assert '検証処理の競合を修正し' not in prompt
    assert '合成試験の例文' in prompt and '例を生成・検証' in prompt
    judge = rt.SEMANTIC_GROUND_PROMPT
    assert '引用文・テスト用例文' in judge and 'unsupported' in judge
    assert '原文にその行為の証拠がある場合' in judge


def test_fact_policy_never_auto_approves_or_repairs_causal_prose(case):
    text='テストを追加したため、PRはDraftのままです。'
    t=SentencesTransport({'sentences':[{'text':text,'source_refs':[0]}]})
    t.ground_verdict='unsupported'
    out=single(case,t,target=('upiscium/Test','pull_request',2))
    assert json.loads(out.attempts[0].raw_response_bytes)['sentences'][0]['text']==text
    assert json.loads(out.narrative_replies[0].response_bytes)['sentences'][0]['text']==text
    assert len(out.grounding_replies)==1
    assert text not in out.preview.markdown
    assert out.preview.report()['model_execution_attested'] is False
    assert out.preview.report()['semantic_coverage_verified'] is False
    assert out.preview.report()['publication_authorized'] is False
    chosen=next(x for x in out.preview.report()['entities'] if x['entity']['entity_type']=='pull_request')
    assert chosen['sentences'][0]['verdict']=='unsupported' and chosen['pending_evidence_ids']


def test_semantic_grounding_projection_preserves_exact_text_sources_and_C4_identity(case):
    t=SentencesTransport();out=single(case,t)
    requests=[p for _,path,p,_ in t.calls if path=='/api/chat']
    raw_ground=c4.build_witnessed_narrative_grounding_contexts(**kwargs(case),
        narrative_replies=out.narrative_replies).report()['contexts'][0]
    full=raw_ground['context'];payload=requests[1]
    visible=json.loads(payload['messages'][1]['content'])
    assert visible == {'entity':full['entity'],'date':full['date'],
        'sentence':full['sentence']['text'],
        'original_events':[{k:v for k,v in ev.items() if k!='evidence_id'} for ev in full['original_events']]}
    assert payload['messages'][0]['content']==rt.SEMANTIC_GROUND_PROMPT
    assert payload['format']==rt.GROUND_SCHEMA
    meta=json.loads(out.attempts[1].receipt_bytes)
    assert meta['context_sha256']==raw_ground['ground_context_sha256']
    assert meta['context_sha256']==rt._sha(rt._json(full))
    assert meta['model_input_version']==rt.SEMANTIC_GROUND_VIEW_VERSION
    assert meta['model_input_sha256']==rt._sha(payload['messages'][1]['content'].encode())
    assert meta['request_sha256']==rt._sha(rt._json(payload).removesuffix(b'\n'))
    for name in ('capture_sha256','closure_sha256','source_status_ja','raw_editorial_response_sha256'):
        assert name not in visible
    assert all('evidence_id' not in event for event in visible['original_events'])
    assert meta['publication_authorized'] is False and meta['model_execution_attested'] is False


def test_legacy_grounding_model_view_and_prompt_are_unchanged(case):
    t=Transport();out=run(case,t)
    for attempt,payload in zip(out.attempts,[p for _,path,p,_ in t.calls if path=='/api/chat']):
        meta=json.loads(attempt.receipt_bytes)
        assert meta['model_input_version']=='c4-canonical-context-v1'
        assert meta['model_input_sha256']==meta['context_sha256']
        visible=json.loads(payload['messages'][1]['content'])
        if attempt.stage=='grounding':
            assert visible['stage']=='github_daily_narrative_ground_context_v4'
            assert visible['system_prompt']==c4.GROUND_PROMPT
            assert visible['original_events'][0]['evidence_id']


def test_semantic_projection_cannot_borrow_other_entity_or_unquoted_claim(case):
    t=SentencesTransport();out=single(case,t,target=('upiscium/Test','pull_request',2))
    ground=[p for _,path,p,_ in t.calls if path=='/api/chat'][1]
    visible=json.loads(ground['messages'][1]['content'])
    assert visible['entity']=={'repository':'upiscium/Test','entity_type':'pull_request','number':2}
    assert all(e['number']==2 and e['repository']=='upiscium/Test' for e in visible['original_events'])
    assert 'selected_candidate_sha256s' not in visible and 'rollup_sha256' not in visible
    # Model prompt still demands support for the whole sentence and rejects
    # invented causality. Its verdict never becomes a source fact or approval.
    assert '因果関係' in ground['messages'][0]['content']
    assert '引用文' in ground['messages'][0]['content']
    assert out.preview.report()['publication_authorized'] is False


def review_fields(out):
    return [f for e in out.source_review.report()['entities'] for f in e['fields']]


def test_original_qualification_remains_visible_despite_event_citation(case):
    work = '検証処理の競合を修正し、回帰テストを追加しました。'
    t = SentencesTransport({'sentences': [{'text': work, 'source_refs': [0]}]})
    out = single(case, t)
    chosen = next(e for e in out.preview.report()['entities'] if e['entity']['entity_type']=='issue')
    assert chosen['pending_evidence_ids'] == []  # old event-level signal misses the qualification
    field = next(f for f in review_fields(out) if f['field']=='body')
    assert field['text'] == TEXT
    assert [field['text'][a:b] for a,b in field['unmatched_source_ranges']] == ['マージと本番反映は未実施です。']
    assert TEXT in out.review_markdown
    assert '文字列の一致は正確さの保証ではなく' in out.review_markdown
    assert out.source_review.report()['semantic_coverage_verified'] is False
    assert out.report()['source_review_required'] is True


def test_known_missing_permanent_fix_qualification(case):
    # Source-reading algorithm regression independent of a live model; the
    # real-CAS runtime path is exercised by the neighboring tests.
    original = '不具合の原因を特定し、再現テストを追加しました。恒久対応は調査中です。'
    sentence = '不具合の原因を特定し、再現テストを追加しました。'
    start = original.find(sentence)
    ranges = rt._unmatched_ranges(original, [(start,start+len(sentence))])
    assert [original[a:b] for a,b in ranges] == ['恒久対応は調査中です。']


def test_false_positive_grounding_cannot_hide_nonliteral_causality(case):
    invented = 'テストを追加したため、PRはDraftのままです。'
    t = SentencesTransport({'sentences': [{'text': invented, 'source_refs': [0]}]})
    t.ground_verdict = 'supported'  # explicitly reproduce the judge's false positive
    out = single(case,t,target=('upiscium/Test','pull_request',2))
    assert invented in out.preview.markdown  # do NOT secretly repair or reverse the verdict
    source = out.source_review.report()
    assert len(source['entities']) == 1 and source['entities'][0]['entity']['number']==2
    assert source['entities'][0]['nonliteral_displayed_sentence_indices'] == [0]
    assert source['factuality_verified'] is False and source['publication_authorized'] is False
    assert TEXT in out.review_markdown
    assert '言い換え・因果関係・否定を要確認' in out.review_markdown


def test_exact_literal_match_does_not_grant_semantic_or_factual_approval(case):
    out = single(case)
    field = next(f for f in review_fields(out) if f['field']=='body')
    assert field['unmatched_source_ranges'] == []
    assert field['displayed_literal_spans'] == [{'sentence_index':0,'start':0,'end':len(TEXT)}]
    info = out.source_review.report()
    assert info['factuality_verified'] is False
    assert info['semantic_coverage_verified'] is False
    assert info['requires_human_quality_review'] is True
    assert info['offset_unit'] == 'unicode_code_point'
    assert info['preview_sha256'] == out.preview.sha256
    assert info['capture_sha256'] == case[1].index_sha256


@pytest.mark.parametrize('state', ['unsupported','empty','invalid','failure'])
def test_no_displayed_sentence_still_exposes_complete_original_source(case,state):
    t=SentencesTransport()
    if state=='unsupported': t.ground_verdict='unsupported'
    if state=='empty': t.editorial={'sentences':[]}
    if state=='invalid': t.editorial={'sentences':[{'text':TEXT,'source_refs':[True]}]}
    if state=='failure': t.fail_stage='editorial'
    out=single(case,t)
    field=next(f for f in review_fields(out) if f['field']=='body')
    assert field['displayed_literal_spans']==[]
    assert field['unmatched_source_ranges']==[[0,len(TEXT)]]
    assert TEXT in out.source_review.markdown
    assert not out.source_review.report()['publication_authorized']


def test_source_companion_does_not_copy_unrequested_entity_or_raw_reason(case):
    out=single(case)
    info=out.source_review.report()
    assert len(info['entities'])==1 and info['entities'][0]['entity']['entity_type']=='issue'
    assert out.report()['unrequested_entity_count']==1
    assert len(out.preview.report()['entities'])==2  # retained pending, not erased
    assert TEXT not in out.canonical_bytes.decode()
    assert '元の証拠と一致します。' not in out.source_review.canonical_bytes.decode()
    assert out.report()['source_review_sha256']==out.source_review.sha256
    assert out.source_review.sha256==rt._sha(out.source_review.canonical_bytes)
    assert info['markdown_sha256']==rt._sha(out.source_review.markdown.encode())


def test_source_companion_adds_zero_model_calls_and_zero_CAS_reads(case,monkeypatch):
    reads=[0]; old=cas.read_verified_capture
    def read(*a,**kw): reads[0]+=1; return old(*a,**kw)
    monkeypatch.setattr(cas,'read_verified_capture',read)
    t=SentencesTransport();out=single(case,t)
    assert reads[0]==out.report()['planned_cas_triplet_reads']
    assert len([p for _,p,_,_ in t.calls if p=='/api/chat'])==2


def test_source_companion_budget_is_checked_before_model(case,monkeypatch):
    monkeypatch.setattr(rt,'MAX_REVIEW_INPUT_BYTES',1)
    t=SentencesTransport()
    with pytest.raises(rt.NarrativeRuntimeError,match='source-review input'):single(case,t)
    assert t.calls==[]


@pytest.mark.parametrize('bound',['MAX_REVIEW_BYTES','MAX_REVIEW_MARKDOWN_BYTES'])
def test_source_companion_never_silently_truncates(case,monkeypatch,bound):
    monkeypatch.setattr(rt,bound,1)
    with pytest.raises(rt.NarrativeRuntimeError,match='source-review output'):single(case)


def test_source_original_snapshot_precedes_callback_and_owns_nested_text(case,monkeypatch):
    old=rt._review_snapshot; captured=[]
    def snap(rows,sha):
        captured.append(rows)
        return old(rows,sha)
    monkeypatch.setattr(rt,'_review_snapshot',snap)
    infer=backend(SentencesTransport(),editorial_contract=rt.SENTENCES_ONLY_CONTRACT)
    def mutate(raw,**kw):
        # Only mutate the caller-accessible in-memory object AFTER it was
        # handed as immutable bytes to the adapter. Snapshot remains owned.
        captured[0][0]['context']['sources'][0]['original_event']['body']['text']='PRIVATE_FORGED'
        return infer(raw,**kw)
    out=rt.run_witnessed_narrative_inference(**kwargs(case),infer=mutate,
          target_entity=('upiscium/Test','issue',1))
    assert TEXT in out.source_review.markdown
    assert 'PRIVATE_FORGED' not in out.source_review.canonical_bytes.decode()


@pytest.mark.parametrize('text',['^block-id','%%hidden%%','[x](https://invalid)','![[embed]]','<script>x</script>','$math$','==mark==','```\ncode','a\u202eb','x\x00y','a\u2028b'])
def test_source_text_markup_and_controls_are_literal_in_companion(text):
    import html
    rendered=rt._review_escape(text)
    assert '^' not in rendered and '<' not in rendered and '[' not in rendered
    assert '\u202e' not in rendered and '\x00' not in rendered and '\u2028' not in rendered
    expected=''.join(ch if ch.isprintable() else f'[U+{ord(ch):04X}]' for ch in text)
    assert html.unescape(rendered)==expected


def test_unmatched_intervals_preserve_gaps_duplicates_unicode_and_full_source():
    text='未対応です。対応済みです。未対応です。'
    # A match never certifies semantics; repeated source passages are NOT
    # erased merely because one occurrence is displayed.
    assert rt._unmatched_ranges(text,[])==[[0,len(text)]]
    assert rt._unmatched_ranges(text,[(0,6),(3,9)])==[[9,len(text)]]
    assert rt._unmatched_ranges(text,[(6,13)])==[[0,6],[13,len(text)]]
    assert rt._unmatched_ranges(text,[(0,len(text))])==[]


@pytest.mark.parametrize('field_name',[None,'title','body','message'])
def test_nontext_counter_and_label_mean_all_captured_text_fields(field_name):
    entity={'repository':'upiscium/Test','entity_type':'issue','number':1}
    ev={'evidence_id':'e'*64,'kind':'issue_comment','occurred_at':'2026-10-05T01:00:00Z',
        'title':None,'body':None,'message':None}
    if field_name is not None: ev[field_name]={'text':'取得した原文。','truncated':False,'included_bytes':21,'original_bytes':21}
    rows=[{'context_sha256':'a'*64,'context':{'entity':entity,'sources':[{'source_ref':0,'original_event':ev}]}}]
    snap=rt._review_snapshot(rows,'b'*64)
    preview=c4.NarrativePreview(rt._json({'capture_sha256':'c'*64,'source_context_set_sha256':'b'*64,
        'entities':[{'context_sha256':'a'*64,'entity':entity,'sentences':[]}]}),'')
    view=rt._build_source_review(snap,preview)
    assert view.report()['entities'][0]['nontext_event_count']==(1 if field_name is None else 0)
    assert ('取得済みテキストフィールドのない元イベント: 1件' in view.markdown)==(field_name is None)
    assert '本文フィールドのない元イベント' not in view.markdown
    if field_name is not None: assert '取得した原文。' in view.markdown


def test_original_truncation_metadata_is_preserved_and_visible():
    ev={'evidence_id':'e'*64,'kind':'issue_comment','occurred_at':'2026-10-05T01:00:00Z',
        'body':{'text':'取得した範囲だけ。','truncated':True,'included_bytes':27,'original_bytes':999}}
    entity={'repository':'upiscium/Test','entity_type':'issue','number':1}
    row={'context_sha256':'a'*64,'context':{'entity':entity,'sources':[{'source_ref':0,'original_event':ev}]}}
    snap=rt._review_snapshot([row],'b'*64)
    preview=c4.NarrativePreview(rt._json({'capture_sha256':'c'*64,'source_context_set_sha256':'b'*64,
        'entities':[{'context_sha256':'a'*64,'entity':entity,'sentences':[]}]}),'')
    out=rt._build_source_review(snap,preview)
    assert out.report()['entities'][0]['fields'][0]['original_value']==ev['body']
    assert '取得時点で切り詰められています。未取得部分は照合できません。' in out.markdown


def test_individual_editorial_language_policy_distinguishes_source_from_output():
    prompt = rt.SENTENCES_ONLY_PROMPT
    assert "日本語の原文はその表現を優先" in prompt
    assert "原文が英語でも、各textは日本語に翻訳" in prompt
    assert "英語のコミット題名を文全体として転記しない" in prompt
    assert "不具合の発見・評価結果・未解決事項の報告も要約対象" in prompt
    assert "根拠のある文を作れなければsentencesを空配列" in prompt


def test_language_guidance_does_not_admit_english_only_provider_sentence(case):
    raw = {"sentences": [{"text": "fix(daily): correct ordering and add regression tests", "source_refs": [0]}]}
    out = single(case, SentencesTransport(raw))
    assert out.attempts[0].state == "inference_or_validation_failed"
    assert not out.narrative_replies and not out.grounding_replies
    assert json.loads(out.attempts[0].raw_response_bytes) == raw
    assert all(row["pending_evidence_ids"] for row in out.preview.report()["entities"])


def test_latest_milestone_opt_in_contract_keeps_existing_c4_response_shape(case):
    from obsidian_automation import github_daily_narrative_runtime as rt
    assert rt.LATEST_MILESTONE_CONTRACT != rt.SENTENCES_ONLY_CONTRACT
    assert rt.LATEST_MILESTONE_PROMPT_VERSION != rt.SENTENCES_ONLY_PROMPT_VERSION
    assert "最後の実質的な進展" in rt.LATEST_MILESTONE_PROMPT
    assert "未マージ" in rt.LATEST_MILESTONE_PROMPT
    assert "本番未反映" in rt.LATEST_MILESTONE_PROMPT
    assert "元イベントが表示されていない場合は推測しない" in rt.LATEST_MILESTONE_PROMPT
    assert "最新の進捗と残る制約" in rt.LATEST_MILESTONE_PROMPT
    context=case[5].report()["contexts"][0]["context"]
    raw=rt._json({"sentences":[{"text":"対象日の実装が進展しました。",
                                "source_refs":[0]}]})
    accepted=rt._admit_editorial(context,raw,rt.LATEST_MILESTONE_CONTRACT)
    decoded=json.loads(accepted)
    assert decoded["sentences"] == json.loads(raw)["sentences"]
    assert decoded["pending_source_refs"] == list(range(1,len(context["sources"])))
    assert rt._admit_editorial(context,raw,rt.SENTENCES_ONLY_CONTRACT)==accepted
    with pytest.raises(rt.NarrativeRuntimeError):
        rt._admit_editorial(context,b'{"sentences":[],"pending_source_refs":[]}',
                           rt.LATEST_MILESTONE_CONTRACT)
