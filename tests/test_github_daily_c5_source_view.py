"""Opt-in C5 latest substantive source view; no model, network, or Writer."""
from __future__ import annotations

from copy import deepcopy
import json

import pytest

from obsidian_automation import github_daily_c5_source_view as view
from obsidian_automation import github_daily_narrative_runtime as rt


def event(ref, kind, body=None, message=None, title=None, *, draft=None,
          source_status="day_snapshot_not_event_instant", withheld=()):
    result = {"kind":kind, "occurred_at":"2026-10-16T11:00:00Z",
              "actor":"trusted-source-only", "draft":draft, "state":None,
              "body":None if body is None else {"text":body, "truncated":False},
              "message":None if message is None else {"text":message, "truncated":False},
              "title":None if title is None else {"text":title, "truncated":False},
              "evidence_id":str(ref).zfill(64)}
    result["temporal_observation"]={
        "policy":"github-day-snapshot-v1", "status":source_status,
        "withheld_fields":list(withheld), "historical_instant_verified":False}
    return {"source_ref":ref, "original_event":result}


def source_context():
    return {"stage":"github_daily_narrative_context_v4",
            "entity":{"repository":"upiscium/ObsidianAutomation",
                      "entity_type":"pull_request","number":374},
            "date":"2026-10-16",
            "sources":[
                event(0,"pull_request_comment",body="以前の設計方針を確認しました。"),
                event(1,"pull_request_commit",message="feat(#300): durable peer-verified pause/resume handoff",
                      source_status="content_addressed_commit_not_push_time"),
                event(2,"pull_request_created",source_status="withheld_outside_target_day",
                      withheld=("body","state","title","draft")),
                event(3,"pull_request_comment",body="特権Workerは未インストールです。Run onceはまだ利用できません。"),
                event(4,"pull_request_snapshot",body="最終的な古い長文設計の説明。"),
            ]}


def test_latest_commit_and_followup_comment_not_buried_under_later_snapshot():
    ctx=source_context()
    got=view.build_recent_c5_source_view(ctx)
    assert got.shown_source_refs == (1,3)
    assert got.omitted_source_refs == (0,2,4)
    assert got.primary_source_ref == 1
    obj=got.report()
    assert obj["primary_progress_source_ref"]==1
    assert obj["shown_source_refs"]==[1,3]
    assert sorted(obj["shown_source_refs"]+obj["omitted_source_refs"])==list(range(5))
    assert obj["source_event_count"]==5
    assert obj["historical_instant_verified"] is False
    assert obj["publication_authorized"] is False
    assert obj["semantic_coverage_verified"] is False
    assert len(got.canonical_bytes)<=view.MAX_WIRE_BYTES
    for i,row in zip(got.shown_source_refs,obj["sources"]):
        source=ctx["sources"][i]["original_event"]
        assert row["source_ref"]==i
        for field in view.CONTENTS:
            assert row["original_event"][field] == source[field]
        assert row["original_event"]["temporal_source_status"] == source["temporal_observation"]["status"]
        assert row["original_event"]["draft"]==source["draft"]
        assert row["original_event"]["historical_instant_verified"] is False


def test_issue_last_two_comments_override_late_snapshot():
    ctx=source_context()
    ctx["entity"]["entity_type"]="issue"
    ctx["entity"]["number"]=300
    ctx["sources"]=[
        event(0,"issue_comment",body="Phase G"),
        event(1,"issue_comment",body="Phase H：Preview UI"),
        event(2,"issue_comment",body="Phase I：Draft #374 durable handoff。特権Worker未インストール。"),
        event(3,"issue_snapshot",body="長い一般方針。") ]
    got=view.build_recent_c5_source_view(ctx)
    assert got.shown_source_refs==(1,2)
    assert got.primary_source_ref==2
    assert "Draft #374" in got.canonical_bytes.decode("utf-8")
    assert "長い一般方針" not in got.canonical_bytes.decode("utf-8")


def test_c5_wire_rejects_omitted_ref_even_if_c4_original_would_accept():
    ctx=source_context()
    raw=rt._json({"sentences":[{"text":"記録された進展です。","source_refs":[0]}]})
    # Source ref 0 is a valid original C4 event but was *not* shown to
    # the model. Raw data must not become a source claim via this projection.
    with pytest.raises(rt.NarrativeRuntimeError,match="not model-visible"):
        rt._admit_editorial(ctx,raw,rt.RECENT_SOURCE_CONTRACT)
    correct=rt._json({"sentences":[
        {"text":"永続的なキューを実装しました。","source_refs":[1]},
        {"text":"特権Workerは未導入です。","source_refs":[3]}]})
    accepted=rt._admit_editorial(ctx,correct,rt.RECENT_SOURCE_CONTRACT)
    obj=json.loads(accepted)
    assert [e["source_refs"] for e in obj["sentences"]]==[[1],[3]]
    assert obj["pending_source_refs"]==[0,2,4]
    # Original full C4 now receives the unchanged raw summaries and complete
    # citation complement; no false full-event semantic coverage.
    assert ctx["sources"][2]["original_event"]["body"] is None
    assert ctx["sources"][4]["original_event"]["body"]["text"] == "最終的な古い長文設計の説明。"


def test_c5_recent_view_rejects_more_than_two_claims_or_old_wire_pending():
    ctx=source_context()
    raw=rt._json({"sentences":[
        {"text":f"検証対象です{i}。","source_refs":[1]} for i in range(3)]})
    with pytest.raises(rt.NarrativeRuntimeError,match="too many recent-source"):
        rt._admit_editorial(ctx,raw,rt.RECENT_SOURCE_CONTRACT)
    old=rt._json({"sentences":[],"pending_source_refs":[0,1,2,3,4]})
    with pytest.raises(rt.NarrativeRuntimeError):
        rt._admit_editorial(ctx,old,rt.RECENT_SOURCE_CONTRACT)


def test_c5_recent_view_no_excerpt_reassembly_or_opaque_source_id():
    ctx=source_context()
    got=view.build_recent_c5_source_view(ctx)
    assert b"evidence_id" not in got.canonical_bytes
    assert b"original_event_sha256" not in got.canonical_bytes
    assert "body" in got.report()["sources"][0]["original_event"]
    assert got.report()["sources"][0]["original_event"]["body"] is None
    assert "commit" in got.report()["sources"][0]["original_event"]["kind"]
    assert got.report()["sources"][1]["original_event"]["body"]["text"].startswith("特権Workerは")


def test_c5_recent_view_byte_bound_does_not_truncate_full_final_event(monkeypatch):
    ctx=source_context()
    before=deepcopy(ctx)
    monkeypatch.setattr(view,"MAX_WIRE_BYTES",200)
    with pytest.raises(view.C5RecentSourceViewError,match="cannot fit"):
        view.build_recent_c5_source_view(ctx)
    assert ctx==before
    monkeypatch.setattr(view,"MAX_WIRE_BYTES",4*1024)
    got=view.build_recent_c5_source_view(ctx)
    assert len(got.canonical_bytes) <= 4*1024


@pytest.mark.parametrize("bad",[
    {"stage":"invalid"},
    {"stage":"github_daily_narrative_context_v4","sources":[]},
    None,
])
def test_bad_context_never_represented(bad):
    with pytest.raises(view.C5RecentSourceViewError):
        view.build_recent_c5_source_view(bad)


def test_c5_temporal_v3_withheld_not_reintroduced():
    ctx=source_context()
    # Only a withheld state/draft source event remains eligible.
    ctx["sources"]=[event(0,"pull_request_created",
                           source_status="withheld_outside_target_day",
                           withheld=("body","draft","state","title"))]
    obj=view.build_recent_c5_source_view(ctx).report()
    e=obj["sources"][0]["original_event"]
    assert e["state"] is None and e["draft"] is None and e["body"] is None
    assert e["temporal_source_status"]=="withheld_outside_target_day"
    assert e["withheld_fields"]==["body","draft","state","title"]
    assert not e["historical_instant_verified"]


def test_c5_recent_view_rejects_boolean_original_ref_and_kind_type():
    ctx=source_context()
    ctx["sources"][1]["source_ref"]=True
    with pytest.raises(view.C5RecentSourceViewError):
        view.build_recent_c5_source_view(ctx)
    ctx=source_context()
    ctx["sources"][0]["original_event"]["kind"]=None
    with pytest.raises(view.C5RecentSourceViewError):
        view.build_recent_c5_source_view(ctx)


def test_v3_prefers_recent_real_feature_commit_over_later_refactor_docs():
    ctx=source_context()
    ctx["sources"]=[
       event(0,"pull_request_commit",message="feat(#300): implement preview UI"),
       event(1,"pull_request_created",body="Original PR feature discussion."),
       event(2,"pull_request_commit",message="fix(#300): resolve UI race"),
       event(3,"pull_request_commit",message="docs(#300): update README"),
       event(4,"pull_request_commit",message="refactor(#300): relocate routes"),
       event(5,"pull_request_comment",
             body="Source-only review passed. No live operations; Run once still unavailable."),
       event(6,"pull_request_snapshot",body="Old comprehensive design.")]
    got=view.build_recent_c5_source_view(ctx)
    assert got.shown_source_refs==(2,5)
    assert got.primary_source_ref==2
    assert got.omitted_source_refs==(0,1,3,4,6)
    obj=got.report()
    assert obj["explicit_limitation_quotes"]
    hint=obj["explicit_limitation_quotes"][0]
    assert hint["source_ref"]==5
    assert "Run once still unavailable" in hint["verbatim_line"]
    original=ctx["sources"][hint["source_ref"]]["original_event"][hint["field"]]["text"]
    assert hint["verbatim_line"]==original.splitlines()[hint["line_index"]]
    assert obj["limitation_quotes_are_source_text_only"] is True


def test_v3_last_original_limitations_are_literally_quoted_not_new_claims():
    ctx=source_context()
    ctx["sources"][3]["original_event"]["body"]["text"] = (
       "- **Security R2:** NO_BLOCKING_FINDINGS_SOURCE_ONLY.\n"
       "- **Explicit blocker:** nothing installs a privileged worker; "
       "Run once and browser Execute paths stay unavailable."
    )
    obj=view.build_recent_c5_source_view(ctx).report()
    assert obj["shown_source_refs"]==[1,3]
    q=obj["explicit_limitation_quotes"]
    assert len(q)==1
    assert q[0]["source_ref"]==3
    assert q[0]["verbatim_line"].startswith("- **Explicit blocker:**")
    assert "NO_BLOCKING_FINDINGS" not in q[0]["verbatim_line"]
    assert q[0]["field"]=="body" and q[0]["line_index"]==1
    assert ctx["sources"][3]["original_event"]["body"]["text"].splitlines()[1]==q[0]["verbatim_line"]
