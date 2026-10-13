"""C3 offline selection/UI fixtures, real local CAS, no live inference."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json

import pytest

from obsidian_automation import github_daily_witnessed_progress_preview as preview
from obsidian_automation import github_daily_witnessed_entity_rollup as roll
from obsidian_automation import github_daily_witnessed_entity_context as c1a
from obsidian_automation import github_daily_witnessed_entity_grounding as c2a
from obsidian_automation import github_daily_route_cas as cas
from obsidian_automation import github_daily_source_route as route
from test_github_daily_route_cas import safe_layout
from test_github_daily_source_route import _FakeClient, _config, DATE, BASE


def encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))+"\n").encode()


class PreviewClient(_FakeClient):
    def __init__(self, *, routine=False, lifecycle=(), bot=False, title=None, silent=False):
        super().__init__(visible_pr=True)
        self.lifecycle, self.title, self.silent = lifecycle, title, silent
        for number in (31, 33):
            row = self.responses[f"{BASE}/issues/comments/{number}"]
            if routine:
                row["body"] = "Next" if number == 31 else "Acknowledged"
            if bot:
                row["user"] = {"login": "github-actions[bot]"}

    def _paged(self, path):
        if self.silent:
            return []
        if "/issues/comments?" in path:
            return [deepcopy(self.responses[f"{BASE}/issues/comments/{n}"]) for n in (31,33)]
        if "/issues?" in path:
            if not self.lifecycle and self.title is None:
                return []  # comments can exist with no same-day created/snapshot rows
            rows = super()._paged(path)
            for row in rows:
                row["created_at"] = "2026-09-01T00:00:00Z"
                if self.title is not None:
                    row["title"] = self.title
            return rows
        if path.endswith("/issues/2/events"):
            return [{"id": 100 + i, "event": kind, "created_at": at,
                     "actor": {"login":"human"}} for i,(kind,at) in enumerate(self.lifecycle)]
        return super()._paged(path)


def saved(tmp_path, client=None):
    collection = route.collect_fresh_verified_routes(
        _config(tmp_path), target_date=DATE, client=client or PreviewClient(),
        explicit_historical_reconfirmation=True,
    )
    return collection, cas.publish_fresh_capture(collection)


def responses(cap, summary="PRIVATE_MODEL PRがマージされました。 https://bad.invalid"):
    contexts = c1a.build_witnessed_entity_contexts(cap.index_sha256)
    partials, grounds = [], []
    for ctx in contexts.contexts:
        raw = encode({"candidates":[{"summary":summary,"source_refs":[0]}]})
        partials.append(roll.PartialReply(ctx.sha256, raw))
        for ground in c2a.build_witnessed_grounding_contexts(ctx, raw, capture_sha256=cap.index_sha256):
            grounds.append(roll.GroundReply(ground.sha256, encode({"verdict":"supported","reason":"PRIVATE_REASON"})))
    return tuple(partials), tuple(grounds)


def decide(cap, partials, grounds, classification="progress"):
    current = roll.build_witnessed_entity_rollup(cap.index_sha256, partials, grounds)
    return tuple(preview.MaterialityDecision(current.sha256, c["candidate_sha256"], classification)
                 for row in current.report()["entities"] for c in row["candidates"])


def run(cap, partials=(), grounds=(), decisions=(), **kwargs):
    return preview.build_witnessed_progress_preview(cap.index_sha256, partials, grounds, decisions, **kwargs)


def test_supported_is_not_automatically_material_and_missing_is_not_no_progress(tmp_path, safe_layout):
    _, cap = saved(tmp_path)
    partials, grounds = responses(cap)
    out = run(cap, partials, grounds)
    data = out.report()
    assert not data["selected_entities"]
    assert data["response_set_complete"] is True
    assert data["pending_evidence_count"] == 2
    assert "## upiscium/Test" not in out.markdown
    assert "進展の有無を確定できません" in out.markdown
    assert data["publication_authorized"] is False
    assert data["materiality_provenance_verified"] is False
    assert run(cap).report()["response_set_complete"] is False
    for d in run(cap).report()["entity_diagnostics"]:
        assert d["coverage"]["unprocessed_evidence_ids"] == d["source_evidence_ids"]


def test_selected_one_item_each_under_only_active_repo_no_raw_model_prose(tmp_path, safe_layout):
    collection, cap = saved(tmp_path)
    partials, grounds = responses(cap)
    decisions = decide(cap, partials, grounds)
    out = run(cap, partials, grounds, decisions)
    data = out.report()
    assert len(data["selected_entities"]) == 2
    assert data["selected_repository_count"] == 1
    assert out.markdown.count("## upiscium/Test") == 1
    assert out.markdown.count("- [Issue #1") == 1 and out.markdown.count("- [PR #2") == 1
    assert "/issues/1)" in out.markdown and "/pull/2)" in out.markdown
    assert "実質的進展として選択された候補" in out.markdown
    assert "PRIVATE_MODEL" not in out.markdown and "PRIVATE_REASON" not in out.markdown
    assert "マージされました" not in out.markdown and "bad.invalid" not in out.markdown
    assert b"PRIVATE_MODEL" not in out.canonical_bytes
    assert "非公開プレビュー・未承認" in out.markdown
    ids = {eid for d in data["entity_diagnostics"] for eid in d["source_evidence_ids"]}
    assert ids == {e.evidence_id for e in collection.evidence.events}
    assert data["markdown_sha256"] == hashlib.sha256(out.markdown.encode()).hexdigest()
    assert out.sha256 == hashlib.sha256(out.canonical_bytes).hexdigest()
    assert out == run(cap, tuple(reversed(partials)), tuple(reversed(grounds)), tuple(reversed(decisions)))


def test_routine_only_is_hidden_even_if_grounding_supported(tmp_path, safe_layout):
    _, cap = saved(tmp_path, PreviewClient(routine=True, bot=True))
    p,g = responses(cap)
    out = run(cap,p,g)
    assert not out.report()["selected_entities"]
    assert out.report()["pending_evidence_count"] == 0
    assert "## upiscium/Test" not in out.markdown
    with pytest.raises(preview.DailyProgressPreviewError, match="routine-only"):
        run(cap,p,g,decide(cap,p,g))


def test_unknown_bot_content_is_not_silently_discarded(tmp_path, safe_layout):
    _, cap = saved(tmp_path, PreviewClient(bot=True))
    out = run(cap)
    assert not out.report()["selected_entities"]
    assert out.report()["pending_evidence_count"] == 2


def test_explicit_routine_decisions_are_advisory_not_human_acceptance(tmp_path, safe_layout):
    _, cap = saved(tmp_path)
    p,g = responses(cap)
    out = run(cap,p,g,decide(cap,p,g,"routine"))
    assert out.report()["selected_entities"] == []
    assert out.report()["materiality_provenance_verified"] is False
    assert out.report()["semantic_coverage_review_required"] is True
    assert "進展のあったIssue・PRはありません" not in out.markdown


@pytest.mark.parametrize("event,expected", [
    ("merged", "PRがマージされました。"),
    ("ready_for_review", "PRがレビュー可能になりました。"),
    ("closed", "PRがクローズされました（マージ未確認）。"),
    ("reopened", "PRが再オープンされました。"),
])
def test_actual_same_day_transition_selected_without_model_reply(tmp_path, safe_layout, event, expected):
    _, cap = saved(tmp_path, PreviewClient(lifecycle=((event,"2026-10-05T10:00:00Z"),)))
    out = run(cap)
    assert len(out.report()["selected_entities"]) == 1
    item = out.report()["selected_entities"][0]
    assert item["entity"]["entity_type"] == "pull_request" and item["entity"]["number"] == 2
    assert item["summary_ja"] == expected
    assert item["selection_basis"] == "source_transition"
    assert item["transition_evidence_ids"]
    assert "- [Issue #1" not in out.markdown


def test_later_or_tied_reopen_is_not_reported_as_final_merged(tmp_path, safe_layout):
    _, cap = saved(tmp_path, PreviewClient(lifecycle=(("merged","2026-10-05T10:00:00Z"),
                                                    ("reopened","2026-10-05T11:00:00Z"))))
    out = run(cap)
    assert "マージの記録があります" in out.markdown
    assert "後続・同時刻の状態は要確認" in out.markdown
    assert "PRがマージされました。" not in out.markdown


def test_next_JST_day_merge_excluded_no_approval_text_as_merge(tmp_path, safe_layout):
    _, cap = saved(tmp_path, PreviewClient(lifecycle=(("merged","2026-10-05T15:00:00Z"),)))
    out = run(cap)
    assert not out.report()["selected_entities"]
    assert "マージ" not in out.markdown


def test_source_title_markup_is_escaped_not_executable(tmp_path, safe_layout):
    _, cap = saved(tmp_path, PreviewClient(title="![embed](https://evil) <script> [[Private]]\n## fake",
                  lifecycle=(("merged","2026-10-05T10:00:00Z"),)))
    out = run(cap)
    assert "<script>" not in out.markdown and "[[Private]]" not in out.markdown
    assert "![embed]" not in out.markdown and "\n## fake" not in out.markdown
    assert "&lt;script&gt;" in out.markdown


@pytest.mark.parametrize("bad", [None, True, [], (object(),), (preview.MaterialityDecision("x","a"*64,"progress"),),
    (preview.MaterialityDecision("a"*64,"b"*64,True),)])
def test_bad_decisions_rejected_before_source_access(monkeypatch,bad):
    monkeypatch.setattr(roll,"build_witnessed_entity_rollup",lambda *a,**k: (_ for _ in ()).throw(AssertionError("no CAS")))
    with pytest.raises(preview.DailyProgressPreviewError):
        preview.build_witnessed_progress_preview("a"*64,(),(),bad)


def test_duplicate_foreign_and_changed_reply_decisions_reject(tmp_path,safe_layout):
    _,cap=saved(tmp_path); p,g=responses(cap); d=decide(cap,p,g)
    with pytest.raises(preview.DailyProgressPreviewError,match="duplicate"):
        run(cap,p,g,(d[0],d[0]))
    with pytest.raises(preview.DailyProgressPreviewError,match="foreign"):
        run(cap,p,g,(replace(d[0],candidate_sha256="f"*64),))
    new=(replace(g[0],response_bytes=encode({"verdict":"supported","reason":"new reason"})),)+g[1:]
    with pytest.raises(preview.DailyProgressPreviewError,match="stale"):
        run(cap,p,new,d)


def test_unsupported_cannot_be_marked_material(tmp_path,safe_layout):
    _,cap=saved(tmp_path); p,g=responses(cap)
    g=tuple(replace(x,response_bytes=encode({"verdict":"unsupported","reason":"no"})) for x in g)
    d=decide(cap,p,g)
    with pytest.raises(preview.DailyProgressPreviewError,match="supported"):
        run(cap,p,g,d)


def test_snapshot_decisions_and_extra_CAS_read_only(tmp_path,safe_layout,monkeypatch):
    _,cap=saved(tmp_path); p,g=responses(cap); d=decide(cap,p,g)
    expected=run(cap,p,g,d)
    original=roll.build_witnessed_entity_rollup
    def mutated(*a,**k):
        for x in d: object.__setattr__(x,"classification","routine")
        return original(*a,**k)
    monkeypatch.setattr(roll,"build_witnessed_entity_rollup",mutated)
    assert run(cap,p,g,d)==expected


def test_no_effects_and_whole_output_bounds(tmp_path,safe_layout,monkeypatch):
    _,cap=saved(tmp_path); p,g=responses(cap); d=decide(cap,p,g)
    def forbid(*a,**k): raise AssertionError("no external effect")
    monkeypatch.setattr("socket.create_connection",forbid)
    monkeypatch.setattr(route,"collect_fresh_verified_routes",forbid)
    monkeypatch.setattr("obsidian_automation.artifact_lifecycle._store_immutable",forbid)
    assert run(cap,p,g,d).report()["publication_authorized"] is False
    monkeypatch.setattr(preview,"MAX_MARKDOWN_BYTES",1)
    with pytest.raises(preview.DailyProgressPreviewError,match="budget"): run(cap,p,g,d)
    monkeypatch.setattr(preview,"MAX_MARKDOWN_BYTES",2*1024*1024)
    monkeypatch.setattr(preview,"MAX_PREVIEW_BYTES",1)
    with pytest.raises(preview.DailyProgressPreviewError,match="budget"): run(cap,p,g,d)


def test_empty_capture_does_not_list_silent_repositories(tmp_path,safe_layout):
    _,cap=saved(tmp_path,PreviewClient(silent=True))
    out=run(cap)
    assert not out.report()["selected_entities"]
    assert not out.report()["entity_diagnostics"]
    assert "## upiscium/Test" not in out.markdown
    assert "採用された進展項目はありません" in out.markdown


def test_missing_CAS_is_error_not_empty_preview(tmp_path,safe_layout):
    _,cap=saved(tmp_path)
    (safe_layout[0]/"receipt"/cas._filename("receipt",cap.receipt_sha256)).unlink()
    with pytest.raises(preview.DailyProgressPreviewError): run(cap)


def test_actual_merge_not_suppressed_by_routine_candidate_selection(tmp_path,safe_layout):
    _,cap=saved(tmp_path,PreviewClient(lifecycle=(("merged","2026-10-05T10:00:00Z"),)))
    p,g=responses(cap)
    out=run(cap,p,g,decide(cap,p,g,"routine"))
    assert any(item["entity"]["entity_type"]=="pull_request" for item in out.report()["selected_entities"])
    assert "PRがマージされました。" in out.markdown
    assert out.report()["publication_authorized"] is False


def test_supported_selection_does_not_erase_overlapping_unresolved_candidate(tmp_path,safe_layout):
    _,cap=saved(tmp_path)
    contexts=c1a.build_witnessed_entity_contexts(cap.index_sha256)
    ctx=contexts.contexts[0]
    raw=encode({"candidates":[{"summary":"first","source_refs":[0]}, {"summary":"second","source_refs":[0]}]})
    singleton=c2a.build_witnessed_grounding_contexts(ctx,raw,capture_sha256=cap.index_sha256)
    p=(roll.PartialReply(ctx.sha256,raw),)
    g=(roll.GroundReply(singleton[0].sha256,encode({"verdict":"supported","reason":"ok"})),)
    report=roll.build_witnessed_entity_rollup(cap.index_sha256,p,g)
    first=report.report()["entities"][0]["candidates"][0]
    decision=preview.MaterialityDecision(report.sha256,first["candidate_sha256"],"progress")
    out=run(cap,p,g,(decision,)).report()
    assert len(out["selected_entities"])==1
    assert out["entity_diagnostics"][0]["coverage"]["unresolved_evidence_ids"]
    assert out["entity_diagnostics"][0]["pending_candidate_sha256s"]
    assert out["pending_evidence_count"]>0


def test_source_chain_change_after_rollup_rejected(tmp_path,safe_layout,monkeypatch):
    _,cap=saved(tmp_path)
    real=cas.read_verified_capture
    build=roll.build_witnessed_entity_rollup
    def finish(*a,**k):
        result=build(*a,**k)
        monkeypatch.setattr(cas,"read_verified_capture",lambda digest: replace(real(digest),index_sha256="f"*64))
        return result
    monkeypatch.setattr(roll,"build_witnessed_entity_rollup",finish)
    with pytest.raises(preview.DailyProgressPreviewError,match="source differ"):
        run(cap)
