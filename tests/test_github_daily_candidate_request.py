"""#378 experimental compact C1b request, without any model or publication."""
from dataclasses import replace
import hashlib
import json

import pytest

from obsidian_automation import github_daily_candidate_request as compact
from obsidian_automation import github_daily_witnessed_entity_candidate as c1b
from obsidian_automation import github_daily_route_cas as cas
from test_github_daily_route_cas import safe_layout
from test_github_daily_witnessed_entity_candidate import _fixture, _reply


def test_compact_prompt_reverified_from_cas_and_models_no_permission(tmp_path, safe_layout):
    _, saved, contexts, ctx = _fixture(tmp_path)
    before = {(x.relative_to(cas.ROOT), x.stat().st_size)
              for x in cas.ROOT.rglob("*") if x.is_file()}
    request = compact.build_bounded_candidate_request(
        ctx, capture_sha256=saved.index_sha256)
    again = compact.build_bounded_candidate_request(
        ctx, capture_sha256=saved.index_sha256)
    assert request == again
    source = request.model_source_view()
    allowed = request.shown_source_refs
    assert request.context_sha256 == ctx.sha256
    assert request.capture_sha256 == saved.index_sha256
    assert len(request.source_view_bytes) <= compact.MAX_VIEW_BYTES
    assert source["view_policy"] == compact.PROMPT_VERSION
    assert source["entity"]["repository"] == ctx.key.repository
    assert source["original_event_count"] == len(ctx.events)
    assert source["shown_source_refs"] == list(allowed)
    assert source["omitted_source_refs"] == list(request.omitted_source_refs)
    assert list(allowed) == list(range(max(0, len(ctx.events)-12), len(ctx.events)))
    assert all("source_ref" in row and "occurred_at" in row for row in source["events"])
    assert source["all_source_text_visible"] == request.all_source_text_visible
    schema=request.model_schema()
    assert schema["properties"]["candidates"]["maxItems"] == 2
    claim=schema["properties"]["candidates"]["items"]["properties"]
    assert claim["summary"]["maxLength"] == compact.MAX_PROMPT_SUMMARY_CHARS
    assert claim["source_refs"]["items"]["enum"] == list(allowed)
    assert request.report()["publication_authorized"] is False
    assert request.report()["model_execution_attested"] is False
    assert "model_source_view" not in request.report()
    assert "source_event" not in str(request.report())
    assert "candidates" not in request.report()
    assert hashlib.sha256(request.source_view_bytes).hexdigest()==request.source_view_sha256
    assert request.report()["request_sha256"] == request.request_sha256
    after={(x.relative_to(cas.ROOT), x.stat().st_size)
           for x in cas.ROOT.rglob("*") if x.is_file()}
    assert before == after  # Neither CAS writes nor external calls.


def test_compact_normalizer_is_original_c1b_and_never_repairs_response(
    tmp_path, safe_layout
):
    _, saved, _, ctx = _fixture(tmp_path)
    req = compact.build_bounded_candidate_request(ctx, capture_sha256=saved.index_sha256)
    ref=req.shown_source_refs[-1]
    short = _reply([{"summary":"確認した進展は元イベントに記録されています。","source_refs":[ref]}])
    got=c1b.normalize_witnessed_entity_candidates(
        ctx,short,capture_sha256=saved.index_sha256)
    assert got.candidates[0].evidence_ids == (ctx.evidence_ids[ref],)
    assert got.uncited_evidence_ids == tuple(
        eid for i,eid in enumerate(ctx.evidence_ids) if i!=ref)
    with pytest.raises(c1b.DailyWitnessedCandidateError, match="one valid line"):
        c1b.normalize_witnessed_entity_candidates(
            ctx,_reply([{"summary":"進展はありました。\nそのため全て成功しました。",
                         "source_refs":[ref]}]),
            capture_sha256=saved.index_sha256)
    assert req.report()["publication_authorized"] is False


def test_compact_window_has_explicit_omitted_original_refs(tmp_path, safe_layout):
    _, saved, _, ctx=_fixture(tmp_path)
    assert len(ctx.events) >= 2
    req=compact.build_bounded_candidate_request(
        ctx,capture_sha256=saved.index_sha256,max_shown_events=1)
    assert req.shown_source_refs==(len(ctx.events)-1,)
    assert req.omitted_source_refs==tuple(range(len(ctx.events)-1))
    assert req.all_source_text_visible is False
    src=req.model_source_view()
    assert src["shown_source_refs"]==[len(ctx.events)-1]
    assert src["all_source_text_visible"] is False
    assert len(src["events"])==1


@pytest.mark.parametrize("invalid", [0, True, -1, 13, "2", None])
def test_invalid_max_source_window_refused_before_source(tmp_path,safe_layout,invalid):
    _, saved, _, ctx=_fixture(tmp_path)
    with pytest.raises(compact.BoundedCandidateRequestError):
        compact.build_bounded_candidate_request(
            ctx,capture_sha256=saved.index_sha256,max_shown_events=invalid)


def test_tampered_or_unbound_context_rejected_before_model(tmp_path,safe_layout):
    _, saved, _, ctx=_fixture(tmp_path)
    cases=(replace(ctx,capture_sha256="b"*64),
           replace(ctx,route_receipt_sha256="e"*64),
           replace(ctx,events=()),
           replace(ctx,context_bytes=b"\x00") if hasattr(ctx,"context_bytes") else
           replace(ctx,events=ctx.events[:-1]))
    for case in cases:
        with pytest.raises(c1b.DailyWitnessedCandidateError):
            compact.build_bounded_candidate_request(
                case,capture_sha256=saved.index_sha256)
    with pytest.raises(c1b.DailyWitnessedCandidateError):
        compact.build_bounded_candidate_request(
            ctx,capture_sha256="f"*64)


def test_source_view_limit_rejects_instead_of_silent_truncate(
    tmp_path,safe_layout,monkeypatch
):
    _, saved, _, ctx=_fixture(tmp_path)
    monkeypatch.setattr(compact,"MAX_VIEW_BYTES",64)
    with pytest.raises(compact.BoundedCandidateRequestError,match="no bounded"):
        compact.build_bounded_candidate_request(ctx,capture_sha256=saved.index_sha256)


def test_compact_reply_is_admitted_exactly_and_rejects_oversized_short_policy(
    tmp_path, safe_layout
):
    _, saved, _, ctx = _fixture(tmp_path)
    req=compact.build_bounded_candidate_request(
        ctx,capture_sha256=saved.index_sha256,max_shown_events=1)
    ref=req.shown_source_refs[0]
    short=_reply([{"summary":"当日の作業を確認しました。","source_refs":[ref]}])
    batch=compact.admit_bounded_candidate_reply(
        ctx,req,short,capture_sha256=saved.index_sha256)
    assert len(batch.candidates)==1
    assert batch.candidates[0].summary=="当日の作業を確認しました。"
    # Original unchanged C1b would admit this printable 161-char text,
    # but the opt-in compact contract must not promote it.
    too_long=_reply([{"summary":"要"*161,"source_refs":[ref]}])
    assert c1b.normalize_witnessed_entity_candidates(
        ctx,too_long,capture_sha256=saved.index_sha256).candidates
    with pytest.raises(compact.BoundedCandidateRequestError,match="outside"):
        compact.admit_bounded_candidate_reply(
            ctx,req,too_long,capture_sha256=saved.index_sha256)
    if req.omitted_source_refs:
        out_of_view=_reply([{"summary":"当日の作業を確認しました。",
                              "source_refs":[req.omitted_source_refs[0]]}])
        with pytest.raises(compact.BoundedCandidateRequestError,match="outside"):
            compact.admit_bounded_candidate_reply(
                ctx,req,out_of_view,capture_sha256=saved.index_sha256)
    with pytest.raises(c1b.DailyWitnessedCandidateError):
        compact.admit_bounded_candidate_reply(
            ctx,req,_reply([{"summary":"当日\n完了","source_refs":[ref]}]),
            capture_sha256=saved.index_sha256)


def test_compact_request_tamper_cannot_approve_model_reply(tmp_path,safe_layout):
    _, saved, _, ctx=_fixture(tmp_path)
    req=compact.build_bounded_candidate_request(ctx,capture_sha256=saved.index_sha256)
    raw=_reply([{"summary":"進捗を記録しました。",
                 "source_refs":[req.shown_source_refs[0]]}])
    for changed in (
        replace(req,source_view_sha256="f"*64),
        replace(req,shown_source_refs=(999,)),
        replace(req,request_sha256="f"*64),
        replace(req,model_schema_bytes=b"{}"),
    ):
        with pytest.raises(compact.BoundedCandidateRequestError):
            compact.admit_bounded_candidate_reply(
                ctx,changed,raw,capture_sha256=saved.index_sha256)


def test_compact_accepted_reply_preserves_original_c2a_source_identity(
    tmp_path, safe_layout
):
    from obsidian_automation import github_daily_witnessed_entity_grounding as c2a
    from obsidian_automation import github_daily_witnessed_entity_rollup as c2b
    _, saved, _, ctx = _fixture(tmp_path)
    request = compact.build_bounded_candidate_request(
        ctx, capture_sha256=saved.index_sha256)
    ref=request.shown_source_refs[-1]
    raw=_reply([{"summary":"進捗確認が記録されました。","source_refs":[ref]}])
    batch=compact.admit_bounded_candidate_reply(
        ctx,request,raw,capture_sha256=saved.index_sha256)
    contexts = c2a.build_witnessed_grounding_contexts(
        ctx,raw,capture_sha256=saved.index_sha256)
    assert len(contexts)==1
    # The synthetic verdict below tests typed closure only, not truth.
    ground=b'{"reason":"evidence fixture only","verdict":"supported"}'
    normalized=c2a.normalize_witnessed_grounding_verdict(
        contexts[0],ground,partial_context=ctx,
        partial_response_bytes=raw,capture_sha256=saved.index_sha256)
    partials=(c2b.PartialReply(ctx.sha256,raw),)
    grounds=(c2b.GroundReply(contexts[0].sha256,ground),)
    rollup=c2b.build_witnessed_entity_rollup(
        saved.index_sha256,partials,grounds)
    assert rollup.report()["capture_sha256"]==saved.index_sha256
    rows=[row for row in rollup.report()["entities"]
          if row["entity"]["number"]==ctx.key.number]
    assert len(rows)==1
    assert rows[0]["candidates"][0]["evidence_ids"]==list(batch.candidates[0].evidence_ids)
    assert rollup.report()["publication_authorized"] is False
