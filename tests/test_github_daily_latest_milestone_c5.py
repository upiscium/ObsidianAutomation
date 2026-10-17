"""Opt-in C5 latest material event selection, full original C4 admission.

No new LLM stage, publisher, live GitHub API or production Writer/Timer.
"""
from copy import deepcopy
import hashlib
import json

import pytest

from obsidian_automation import github_daily_narrative_runtime as rt
from obsidian_automation import github_daily_witnessed_narrative as c4
from test_github_daily_route_cas import safe_layout
from test_github_daily_witnessed_narrative import case, kwargs
from test_github_daily_narrative_runtime import MODEL, DIGEST, TEXT, Transport


def _event(kind, at, text=None, *, draft=None, status="day_snapshot_not_event_instant"):
    return {"kind": kind, "occurred_at": at, "actor": "upiscium",
            "number": 300, "repository": "upiscium/ObsidianAutomation",
            "title": None, "body": None if text is None else
                {"text": text, "truncated": False}, "message": None,
            "draft": draft, "state": None,
            "temporal_observation": {
                "policy": "github-day-snapshot-v1",
                "historical_instant_verified": False,
                "status": status, "withheld_fields": []}}


def test_select_last_two_material_dated_events_not_later_issue_snapshot():
    sources = [
        _event("issue_comment", "2026-10-16T07:30:00Z",
               "Earlier overall responsibility decision."),
        _event("issue_comment", "2026-10-16T09:12:00Z",
               "New Draft UI preview is implemented. No live effects."),
        _event("issue_comment", "2026-10-16T11:58:00Z",
               "Latest durable handoff implemented but no privileged worker installed."),
        _event("issue_snapshot", "2026-10-16T14:59:00Z",
               "Long issue body contains old overall design notes."),
    ]
    ctx={"entity":{"repository":"upiscium/ObsidianAutomation",
                   "entity_type":"issue","number":300},
         "date":"2026-10-16","source_status_ja":"",
         "sources":[{"source_ref":i,"original_event":e} for i,e in enumerate(sources)]}
    original=deepcopy(ctx)
    projection, refs=rt._latest_milestone_view(ctx)
    assert refs == (3,1,2)
    assert projection["shown_source_refs"] == [3,1,2]
    assert projection["omitted_source_refs"] == [0]
    assert projection["auxiliary_updated_snapshot_ref"] == 3
    assert projection["original_source_event_count"] == 4
    assert projection["source_event_coverage_verified"] is False
    assert [x["original_event"] for x in projection["sources"]] == [sources[3],sources[1],sources[2]]
    assert ctx == original  # no mutation of host's full source
    assert "no privileged worker" in rt._json(projection).decode()
    assert "old overall design" in rt._json(projection).decode()
    assert "source_status_ja" not in projection
    assert len(rt._json(projection)) < rt.MAX_LATEST_MILESTONE_VIEW_BYTES


def test_latest_commit_is_prioritized_over_later_review_without_ref_renumbering():
    items=[
        _event("pull_request_commit","2026-10-16T10:21:00Z",
               "feat: add durable pause/resume intake",status="content_addressed_commit_not_push_time"),
        _event("pull_request_comment","2026-10-16T11:58:00Z",
               "Security and Correctness source-only review passed; no privileged worker installed."),
    ]
    ctx={"entity":{"repository":"upiscium/Test","entity_type":"pull_request","number":374},
         "date":"2026-10-16","source_status_ja":"PRが作成されました。",
         "sources":[{"source_ref":i,"original_event":e} for i,e in enumerate(items)]}
    view,refs=rt._latest_milestone_view(ctx)
    assert refs == (0,1)
    assert view["shown_implementation_commit_refs"] == [0]
    assert view["sources"][0]["original_event"]["kind"] == "pull_request_commit"
    assert view["sources"][1]["original_event"]["kind"] == "pull_request_comment"
    assert view["sources"][0]["source_ref"] == 0
    assert "content_addressed_commit_not_push_time" in rt._json(view).decode()
    assert "後続の原文で撤回" in rt.LATEST_MILESTONE_PROMPT
    assert "対象Issue/PRそれ自体" in rt.LATEST_MILESTONE_PROMPT
    assert "mainにはマージしていない" in rt.LATEST_MILESTONE_PROMPT


def test_omitted_lifecycle_status_is_not_laundered_into_model_input():
    ctx={"entity":{"repository":"upiscium/Test","entity_type":"pull_request","number":2},
         "date":"2026-10-16","source_status_ja":"PRがマージされました。",
         "sources":[{"source_ref":i,"original_event":event}
                    for i,event in enumerate([
                        _event("pull_request_merged","2026-10-16T07:00:00Z"),
                        _event("pull_request_comment","2026-10-16T09:00:00Z",
                               "No more worker calls in this draft."),
                        _event("pull_request_comment","2026-10-16T10:00:00Z",
                               "Local test-only checkpoint remains."),
                    ])]}
    view,refs=rt._latest_milestone_view(ctx)
    assert refs == (1,2)
    assert view["omitted_source_refs"] == [0]
    assert "source_status_ja" not in view
    assert "PRがマージされました" not in rt._json(view).decode()


def test_all_snapshot_fallback_does_not_readd_older_auxiliary_snapshot():
    events=[
        _event("issue_snapshot","2026-10-16T08:00:00Z","old state"),
        _event("issue_snapshot","2026-10-16T10:00:00Z","updated state"),
        _event("issue_snapshot","2026-10-16T12:00:00Z","latest state"),
    ]
    ctx={"entity":{"repository":"upiscium/T","entity_type":"issue","number":8},
         "date":"2026-10-16","source_status_ja":"",
         "sources":[{"source_ref":i,"original_event":e} for i,e in enumerate(events)]}
    view,refs=rt._latest_milestone_view(ctx)
    assert refs == (1,2)
    assert view["auxiliary_updated_snapshot_ref"] is None
    assert view["omitted_source_refs"] == [0]


def test_later_retraction_is_present_before_superseded_commit():
    items=[
        _event("pull_request_commit","2026-10-16T08:00:00Z",
               "feat: proposed preliminary queue handler",
               status="content_addressed_commit_not_push_time"),
        _event("pull_request_comment","2026-10-16T11:00:00Z",
               "This handler was reverted after failure; it is no longer implemented."),
    ]
    ctx={"entity":{"repository":"upiscium/T","entity_type":"pull_request","number":5},
         "date":"2026-10-16","source_status_ja":"",
         "sources":[{"source_ref":i,"original_event":e} for i,e in enumerate(items)]}
    view,refs=rt._latest_milestone_view(ctx)
    assert refs == (0,1)
    assert "reverted after failure" in view["sources"][-1]["original_event"]["body"]["text"]
    assert view["shown_implementation_commit_refs"] == [0]
    assert "撤回" in rt.LATEST_MILESTONE_PROMPT


def test_day_updated_snapshot_remains_citable_even_with_comments():
    events=[
        _event("issue_comment","2026-10-16T08:00:00Z",
               "Earlier context."),
        _event("issue_comment","2026-10-16T11:00:00Z",
               "Latest progress call."),
        _event("issue_snapshot","2026-10-16T14:00:00Z",
               "A newly recorded decision changes the accepted rollout scope."),
    ]
    ctx={"entity":{"repository":"upiscium/T","entity_type":"issue","number":3},
         "date":"2026-10-16","source_status_ja":"",
         "sources":[{"source_ref":i,"original_event":e} for i,e in enumerate(events)]}
    view,refs=rt._latest_milestone_view(ctx)
    assert refs == (2,0,1)
    assert view["auxiliary_updated_snapshot_ref"] == 2
    assert set(view["shown_source_refs"]) == {0,1,2}
    assert view["omitted_source_refs"] == []
    assert "newly recorded decision" in view["sources"][0]["original_event"]["body"]["text"]


def test_too_large_recent_full_event_dropped_or_fails_before_model(monkeypatch):
    monkeypatch.setattr(rt,"MAX_LATEST_MILESTONE_VIEW_BYTES",1100)
    src=[_event("issue_comment",f"2026-10-16T0{i}:00:00Z",
                "A"*520 if i==1 else "進展"+str(i))
         for i in (1,2,3)]
    ctx={"entity":{"repository":"upiscium/T","entity_type":"issue","number":1},
         "date":"2026-10-16","source_status_ja":"",
         "sources":[{"source_ref":i,"original_event":v} for i,v in enumerate(src)]}
    projection, refs=rt._latest_milestone_view(ctx)
    assert refs == (2,) or refs == (2,1)
    assert projection["omitted_source_refs"] == [i for i in range(3) if i not in refs]
    monkeypatch.setattr(rt,"MAX_LATEST_MILESTONE_VIEW_BYTES",60)
    with pytest.raises(rt.NarrativeRuntimeError,match="no bounded"):
        rt._latest_milestone_view(ctx)


class MilestoneTransport(Transport):
    def __init__(self, *, cite_unshown=False):
        super().__init__()
        self.cite_unshown=cite_unshown

    def __call__(self, base_url, **kw):
        if kw.get("path") != "/api/chat":
            return super().__call__(base_url,**kw)
        payload=kw["payload"]
        visible=json.loads(payload["messages"][1]["content"])
        if visible.get("model_scope") == rt.LATEST_MILESTONE_PROMPT_VERSION:
            assert len(visible["sources"]) <= 2
            assert visible["source_event_coverage_verified"] is False
            assert payload["messages"][0]["content"] == rt.LATEST_MILESTONE_PROMPT
            assert payload["format"]["properties"]["sentences"]["items"]["properties"]["source_refs"]["items"]["enum"] == visible["shown_source_refs"]
            if self.cite_unshown and visible["omitted_source_refs"]:
                cited=visible["omitted_source_refs"][0]
            else:
                cited=visible["shown_source_refs"][-1]
            value={"sentences":[{"text":TEXT,"source_refs":[cited]}]}
            self.calls.append((kw["method"],kw["path"],deepcopy(payload),kw["timeout"]))
            return {"done":True,"done_reason":"stop","model":MODEL,
                    "message":{"role":"assistant","content":json.dumps(value,ensure_ascii=False)}}
        assert "sentence" in visible and "original_events" in visible
        assert "sources" not in visible
        assert payload["messages"][0]["content"] == rt.SEMANTIC_GROUND_PROMPT
        self.calls.append((kw["method"],kw["path"],deepcopy(payload),kw["timeout"]))
        return {"done":True,"done_reason":"stop","model":MODEL,
                "message":{"role":"assistant",
                           "content":json.dumps({"verdict":"supported",
                                                   "reason":"元の証拠に一致します。"},ensure_ascii=False)}}


def _run(case, transport):
    infer=rt.ollama_narrative_infer(
        base_url="http://127.0.0.1:11434",model="fixture",
        transport=transport,
        editorial_contract=rt.LATEST_MILESTONE_CONTRACT)
    return rt.run_witnessed_narrative_inference(
        **kwargs(case),infer=infer,
        target_entity=("upiscium/Test","issue",1),
        max_entities=1,max_calls=4)


def test_opt_in_milestone_transport_source_bound_preview_and_receipt(case):
    t=MilestoneTransport()
    run=_run(case,t)
    assert run.report()["inference_responses_complete"] is True
    assert [x.stage for x in run.attempts] == ["editorial","grounding"]
    assert [x.state for x in run.attempts] == ["accepted","accepted"]
    assert len(run.narrative_replies) == 1
    assert len(run.grounding_replies) == 1
    assert TEXT in run.preview.markdown
    assert run.report()["publication_authorized"] is False
    original=next(x["context"] for x in case[5].report()["contexts"]
                  if x["context"]["entity"]["number"]==1)
    raw=json.loads(run.narrative_replies[0].response_bytes)
    shown=set(rt._latest_milestone_view(original)[1])
    assert set(raw["sentences"][0]["source_refs"]) <= shown
    assert raw["pending_source_refs"] == [i for i in range(len(original["sources"])) if i not in shown]
    editorial=run.attempts[0]
    meta=json.loads(editorial.receipt_bytes)
    assert meta["response_contract"] == rt.LATEST_MILESTONE_CONTRACT
    assert meta["effective_prompt_version"] == rt.LATEST_MILESTONE_PROMPT_VERSION
    assert meta["model_visible_source_refs"] == sorted(shown)
    assert meta["model_input_source_coverage_verified"] is False
    assert meta["model_execution_attested"] is False
    assert meta["publication_authorized"] is False
    request=next(x[2] for x in t.calls if x[1] == "/api/chat")
    assert meta["request_sha256"] == rt._sha(rt._json(request).removesuffix(b"\n"))
    assert meta["raw_response_sha256"] == rt._sha(editorial.raw_response_bytes)
    assert meta["context_sha256"] == rt._sha(rt._json(original))


def test_unshown_event_cannot_be_admitted_even_if_valid_original_ref(monkeypatch):
    monkeypatch.setattr(rt,"MAX_LATEST_MILESTONE_SOURCES",1)
    events=[
        _event("issue_comment","2026-10-16T07:00:00Z","Earlier unrelated detail"),
        _event("issue_comment","2026-10-16T09:00:00Z","Latest handoff is still Draft"),
    ]
    ctx={"entity":{"repository":"upiscium/Test","entity_type":"issue","number":1},
         "date":"2026-10-16","source_status_ja":"",
         "sources":[{"source_ref":i,"original_event":v}
                    for i,v in enumerate(events)]}
    visible, refs=rt._latest_milestone_view(ctx)
    assert refs == (1,)
    assert visible["omitted_source_refs"] == [0]
    # Source 0 is a real original integer index, but it was NOT shown
    # to the model. This must fail before the standard C4 normalizer.
    forged=rt._json({"sentences":[{"text":"古い作業が完了しました。",
                                    "source_refs":[0]}]})
    with pytest.raises(rt.NarrativeRuntimeError,match="unshown"):
        rt._admit_editorial(ctx,forged,rt.LATEST_MILESTONE_CONTRACT)


def test_legacy_adapter_remains_unmodified_in_default_mode(case):
    from test_github_daily_narrative_runtime import SentencesTransport, backend
    t=SentencesTransport()
    out=rt.run_witnessed_narrative_inference(
        **kwargs(case),
        infer=backend(t,editorial_contract=rt.SENTENCES_ONLY_CONTRACT),
        target_entity=("upiscium/Test","issue",1),max_entities=1,max_calls=4)
    assert out.report()["inference_responses_complete"] is True
    assert json.loads(out.attempts[0].receipt_bytes)["effective_prompt_version"] == rt.SENTENCES_ONLY_PROMPT_VERSION
    assert out.report()["publication_authorized"] is False


@pytest.mark.parametrize("invalid",["unknown-contract",None,True])
def test_unrecognized_milestone_config_rejected_without_network(invalid):
    with pytest.raises(rt.NarrativeRuntimeError):
        rt.ollama_narrative_infer(
            base_url="http://127.0.0.1:11434",model="fixture",
            editorial_contract=invalid)
