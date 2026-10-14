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
        assert payload["messages"][0]["content"] == ctx["system_prompt"]
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
        assert r["request_sha256"]==hashlib.sha256(rt._json(request)).hexdigest()
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
    def infer(raw):
        n[0]+=1
        if n[0]==1: raise OSError("fixture failure")
        return base(raw)
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
    def slow(raw):
        result=real(raw);clock[0]=121;return result
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
    out=rt.run_witnessed_narrative_inference(**kwargs(case),infer=lambda raw:bad)
    assert not out.report()["inference_responses_complete"]
    assert not out.narrative_replies


@pytest.mark.parametrize("mode",["digest","authority","raw-type","receipt-type","oversize"])
def test_receipt_forgery_does_not_admit_output(case,mode):
    base=backend(Transport())
    def bad(raw):
        result=base(raw);meta=json.loads(result.receipt_bytes)
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
    def mutate(raw):
        if first[0]:
            first[0]=False
            object.__setattr__(partials[0],"response_bytes",b"forged")
            object.__setattr__(decisions[0],"classification","routine")
        return base(raw)
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
