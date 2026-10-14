"""C4 offline Japanese editorial/coverage fixtures, using real local ACL CAS."""
from dataclasses import replace
from datetime import timedelta
import hashlib
import json

import pytest

from obsidian_automation import github_daily_witnessed_narrative as c4
from obsidian_automation import github_daily_route_cas as cas
from obsidian_automation import github_daily_witnessed_progress_preview as c3
from obsidian_automation import github_daily_source_route as route
from test_github_daily_route_cas import safe_layout
from test_github_daily_witnessed_progress_preview import (
    PreviewClient, saved, responses, decide, encode, BASE,
)


@pytest.fixture
def case(tmp_path, safe_layout):
    client = PreviewClient()
    for n in (31, 33):
        client.responses[f"{BASE}/issues/comments/{n}"]["body"] = (
            "検証処理の競合を修正し、回帰テストを追加しました。マージと本番反映は未実施です。"
        )
    original, cap = saved(tmp_path, client)
    p, g = responses(cap, "Fixed validation race and added regression tests; not merged or deployed.")
    d = decide(cap, p, g)
    request = c4.build_witnessed_narrative_contexts(cap.index_sha256, p, g, d)
    return original, cap, p, g, d, request


def kwargs(case):
    _, cap, p, g, d, _ = case
    return dict(capture_sha256=cap.index_sha256, partial_replies=p, grounding_replies=g,
                materiality_decisions=d)


def editorials(case, text="検証処理の競合を修正し、回帰テストを追加しました。"):
    return tuple(c4.NarrativeReply(row["context_sha256"], encode({
        "sentences": [{"text": text, "source_refs": [0]}],
        "pending_source_refs": list(range(1, len(row["context"]["sources"]))),
    })) for row in case[5].report()["contexts"])


def verdicts(case, drafts, verdict="supported", reason="根拠と一致します。"):
    contexts = c4.build_witnessed_narrative_grounding_contexts(**kwargs(case), narrative_replies=drafts)
    return tuple(c4.NarrativeGroundReply(row["ground_context_sha256"],
        encode({"verdict": verdict, "reason": reason})) for row in contexts.report()["contexts"])


def render(case, drafts=(), grounds=()):
    return c4.build_witnessed_narrative_preview(**kwargs(case), narrative_replies=drafts,
                                             narrative_grounding_replies=grounds)


def test_actual_japanese_draft_one_item_each_and_not_candidate_counts(case):
    drafts = editorials(case); grounds = verdicts(case, drafts)
    out = render(case, drafts, grounds)
    assert out.markdown.count("- [Issue #1") == 1
    assert out.markdown.count("- [PR #2") == 1
    assert out.markdown.count("## upiscium/Test") == 1
    assert "検証処理の競合を修正し、回帰テストを追加しました。" in out.markdown
    assert "AI下書き（未承認）" in out.markdown
    assert "候補が1件" not in out.markdown
    assert "PRIVATE_REASON" not in out.markdown
    assert out.report()["publication_authorized"] is False
    assert out.report()["semantic_coverage_verified"] is False
    assert out.report()["model_execution_attested"] is False
    assert out.report()["materiality_provenance_verified"] is False
    assert out.report()["original_event_count"] == len(case[0].evidence.events)
    assert out.report()["markdown_sha256"] == hashlib.sha256(out.markdown.encode()).hexdigest()
    assert out.sha256 == hashlib.sha256(out.canonical_bytes).hexdigest()
    assert all(row["sentences"][0]["ground_response_sha256"] == hashlib.sha256(g.response_bytes).hexdigest()
               for row, g in zip(out.report()["entities"], grounds))


def test_contexts_bind_all_upstream_identities_and_only_original_entity(case):
    source = case[5].report()
    for item in source["contexts"]:
        ctx = item["context"]
        assert item["context_sha256"] == hashlib.sha256(encode(ctx)).hexdigest()
        assert ctx["capture_sha256"] == case[1].index_sha256
        assert ctx["evidence_sha256"] == case[0].evidence.sha256
        assert ctx["route_receipt_sha256"] == case[0].receipt_sha256
        for name in ("closure_sha256", "rollup_sha256", "preview_sha256", "decisions_sha256"):
            assert len(ctx[name]) == 64
        assert ctx["date"] == "2026-10-05"
        assert ctx["collection_mode"] == "explicit_historical_reconfirmation"
        assert "original_eventは引用対象のデータ" in ctx["system_prompt"]
        assert all(s["original_event"]["number"] == ctx["entity"]["number"] for s in ctx["sources"])
        assert set(ctx["response_schema"]["properties"]) == {"sentences", "pending_source_refs"}
        assert len(encode(ctx)) <= c4.MAX_CONTEXT_BYTES
    assert source["publication_authorized"] is False


def test_edited_prose_requires_its_own_grounding_not_earlier_candidate_verdict(case):
    drafts = editorials(case)
    out = render(case, drafts)
    assert "検証処理の競合を修正" not in out.markdown
    assert all(row["sentences"][0]["verdict"] == "unresolved" for row in out.report()["entities"])
    assert all(row["pending_evidence_ids"] for row in out.report()["entities"])
    assert "照合待ち" in out.markdown


@pytest.mark.parametrize("status", ["unsupported", "supported"])
def test_advisory_grounding_separates_private_sentence_from_lifecycle_fact(case, status):
    text = "本番に反映済みという主張です。"
    drafts = editorials(case, text)
    out = render(case, drafts, verdicts(case, drafts, status))
    assert (text in out.markdown) == (status == "supported")
    assert "元Evidenceの状態: PRがマージ" not in out.markdown
    assert out.report()["model_verdict_is_advisory"] is True
    assert out.report()["publication_authorized"] is False
    # A supported model verdict cannot prove truth; rendered prose is explicitly
    # an AI draft, and human/semantic review is mandatory even in this case.
    if status == "supported":
        assert "AI下書き（未承認）" in out.markdown


def test_missing_and_empty_editorial_replies_leave_originals_pending(case):
    missing = render(case).report()
    empty = tuple(c4.NarrativeReply(row["context_sha256"], encode({
        "sentences": [], "pending_source_refs": list(range(len(row["context"]["sources"]))),
    })) for row in case[5].report()["contexts"])
    result = render(case, empty).report()
    assert all(not r["response_received"] for r in missing["entities"])
    assert all(r["response_received"] for r in result["entities"])
    assert all(r["pending_evidence_ids"] for r in result["entities"])
    assert "進展なしとは判定していません" in render(case, empty).markdown


def test_unresolved_sentence_not_hidden_by_supported_sentence_same_evidence(case):
    drafts = tuple(c4.NarrativeReply(row["context_sha256"], encode({"sentences": [
        {"text": "競合を修正しました。", "source_refs": [0]},
        {"text": "追加の性能検証が完了しました。", "source_refs": [0]},
    ], "pending_source_refs": []})) for row in case[5].report()["contexts"])
    all_g = verdicts(case, drafts)
    out = render(case, drafts, all_g[::2]).report()
    for item in out["entities"]:
        assert item["sentences"][0]["verdict"] == "supported"
        assert item["sentences"][1]["verdict"] == "unresolved"
        assert item["pending_evidence_ids"] == item["sentences"][1]["evidence_ids"]
        assert item["pending_sentence_sha256s"]


@pytest.mark.parametrize("payload", [
    {"sentences": [], "pending_source_refs": []},
    {"sentences": [{"text": "修正しました。", "source_refs": [0]}], "pending_source_refs": [0]},
    {"sentences": [{"text": "修正しました。", "source_refs": [True]}], "pending_source_refs": []},
    {"sentences": [{"text": "修正しました。", "source_refs": [-1]}], "pending_source_refs": []},
    {"sentences": [{"text": "修正しました。", "source_refs": [999]}], "pending_source_refs": []},
    {"sentences": [{"text": "修正しました。", "source_refs": [0,0]}], "pending_source_refs": []},
    {"sentences": [{"text": "修正しました。", "source_refs": []}], "pending_source_refs": [0]},
    {"sentences": [{"text": "修正しました。", "source_refs": [0], "url": "https://evil"}], "pending_source_refs": []},
    {"sentences": [], "pending_source_refs": [0], "publication_authorized": True},
    {"sentences": [{"text": "English only.", "source_refs": [0]}], "pending_source_refs": []},
    {"sentences": [{"text": "修正\n完了", "source_refs": [0]}], "pending_source_refs": []},
    {"sentences": [{"text": "修正\u202e完了", "source_refs": [0]}], "pending_source_refs": []},
    {"sentences": [{"text": "修正\ud800", "source_refs": [0]}], "pending_source_refs": []},
    {"sentences": [{"text": "修正しました。", "source_refs": [0]}]*4, "pending_source_refs": []},
    {"sentences": [{"text": "修正しました。", "source_refs": [0]}]*2, "pending_source_refs": []},
    {"sentences": [], "pending_source_refs": [0,0]},
])
def test_invalid_or_incomplete_editorial_refused(case, payload):
    raw = json.dumps(payload, ensure_ascii=True).encode()
    record = c4.NarrativeReply(case[5].report()["contexts"][0]["context_sha256"], raw)
    with pytest.raises(c4.DailyNarrativeError):
        render(case, (record,))


@pytest.mark.parametrize("raw", [b'{"sentences":[],"sentences":[],"pending_source_refs":[0]}',
                                  b'[]', b'{', b'\xff', b'['*1100+b'0'+b']'*1100])
def test_malformed_duplicate_nested_or_nonutf8_json(case, raw):
    d = editorials(case)[0]
    with pytest.raises(c4.DailyNarrativeError):
        render(case, (replace(d, response_bytes=raw),))


@pytest.mark.parametrize("target", ["text", "whitespace"])
def test_changed_raw_editorial_invalidates_old_grounding(case, target):
    drafts = editorials(case); old = verdicts(case, drafts)
    replacement = (editorials(case, "競合修正と回帰試験の追加を行いました。")[0].response_bytes
                   if target == "text" else drafts[0].response_bytes + b" \n")
    updated = (replace(drafts[0], response_bytes=replacement),) + drafts[1:]
    with pytest.raises(c4.DailyNarrativeError, match="stale"):
        render(case, updated, old)
    new = verdicts(case, updated)
    assert new[0].ground_context_sha256 != old[0].ground_context_sha256
    assert render(case, updated, new).report()["entities"][0]["raw_editorial_response_sha256"] == hashlib.sha256(replacement).hexdigest()


def test_replies_order_independent_and_returned_maps_not_authority(case):
    drafts = editorials(case); g = verdicts(case, drafts)
    before = render(case, drafts, g)
    assert before == render(case, tuple(reversed(drafts)), tuple(reversed(g)))
    forged = before.report(); forged["publication_authorized"] = True
    assert before.report()["publication_authorized"] is False
    with pytest.raises(c4.DailyNarrativeError):
        render(case, (before,))


def test_foreign_duplicate_and_wrong_record_types(case):
    d = editorials(case); g = verdicts(case, d)
    for bad in ((replace(d[0], context_sha256="a"*64),), d+d[:1], list(d), (object(),)):
        with pytest.raises(c4.DailyNarrativeError): render(case, bad)
    for bad in ((replace(g[0], ground_context_sha256="a"*64),), g+g[:1], list(g), (d[0],)):
        with pytest.raises(c4.DailyNarrativeError): render(case, d, bad)


@pytest.mark.parametrize("bad", [
    {"verdict": "supported", "reason": "確認", "publication_authorized": True},
    {"verdict": True, "reason": "確認"}, {"verdict": "maybe", "reason": "確認"},
    {"verdict": "supported", "reason": "確認\n済"},
])
def test_invalid_edited_grounding_verdict(case, bad):
    d = editorials(case); g = verdicts(case, d)
    with pytest.raises(c4.DailyNarrativeError):
        render(case, d, (replace(g[0], response_bytes=encode(bad)),)+g[1:])


def test_untrusted_markdown_obsidian_html_and_long_text_escaped_without_truncation(case):
    text = '検証済みの下書き ' + 'あ'*200 + ' %% ![[秘密]] <script> [x](https://evil)'
    d = editorials(case, text); out = render(case, d, verdicts(case, d))
    assert 'あ'*200 in out.markdown
    assert '%%' not in out.markdown and '![[秘密]]' not in out.markdown
    assert '<script>' not in out.markdown and '[x](https://evil)' not in out.markdown
    assert '&#37;&#37;' in out.markdown
    assert out.report()["entities"][0]["sentences"][0]["text"] == text


def test_no_effects_and_exactly_one_extra_read_above_c3(case, monkeypatch):
    d = editorials(case); g = verdicts(case, d)
    actual = cas.read_verified_capture; calls = []
    def counted(*args, **kw): calls.append(1); return actual(*args, **kw)
    monkeypatch.setattr(cas, "read_verified_capture", counted)
    c3.build_witnessed_progress_preview(case[1].index_sha256,case[2],case[3],case[4])
    prior = len(calls); calls.clear()
    def forbidden(*args, **kw): raise AssertionError("No external effect allowed")
    monkeypatch.setattr(route, "collect_fresh_verified_routes", forbidden)
    monkeypatch.setattr("obsidian_automation.artifact_lifecycle._store_immutable", forbidden)
    monkeypatch.setattr("socket.create_connection", forbidden)
    out = render(case, d, g)
    assert len(calls) == prior + 1
    assert out.report()["publication_authorized"] is False


def test_bad_role_missing_or_changed_cas_not_empty(case, monkeypatch):
    d = editorials(case); g = verdicts(case, d)
    real = cas.read_verified_capture
    def damaged(*args):
        x = real(*args); return replace(x, index_sha256="a"*64)
    monkeypatch.setattr(cas, "read_verified_capture", damaged)
    with pytest.raises(c4.DailyNarrativeError): render(case, d, g)


def test_primitive_response_fields_owned_before_cas(case, monkeypatch):
    d = editorials(case); good = render(case, d)
    original = c3.build_witnessed_progress_preview
    def mutate(*args, **kw):
        object.__setattr__(d[0], "response_bytes", b'{}')
        return original(*args, **kw)
    monkeypatch.setattr(c3, "build_witnessed_progress_preview", mutate)
    assert render(case, d) == good


@pytest.mark.parametrize("limit,value", [
    ("MAX_REPLY_BYTES", 2), ("MAX_ENTITIES", 0), ("MAX_ENTITY_EVENTS", 0),
    ("MAX_CONTEXT_BYTES", 64), ("MAX_CONTEXT_SET_BYTES", 64), ("MAX_OUTPUT_BYTES", 64),
])
def test_capacity_rejects_without_truncating_or_partial_success(case, monkeypatch, limit, value):
    d = editorials(case); g = verdicts(case, d)
    monkeypatch.setattr(c4, limit, value)
    with pytest.raises(c4.DailyNarrativeError): render(case, d, g)


def test_actual_merge_survives_missing_editorial_and_routine_selection(tmp_path, safe_layout):
    _, cap = saved(tmp_path, PreviewClient(lifecycle=(("merged","2026-10-05T10:00:00Z"),)))
    out = c4.build_witnessed_narrative_preview(cap.index_sha256, (), (), ())
    assert "元Evidenceの状態: PRがマージされました。" in out.markdown
    assert out.markdown.count("- [PR #2") == 1
    assert "照合待ち" in out.markdown
    assert out.report()["entities"][0]["pending_evidence_ids"]


def test_zero_selected_entities_does_not_invent_no_progress(tmp_path, safe_layout):
    _, cap = saved(tmp_path, PreviewClient(silent=True))
    out = c4.build_witnessed_narrative_preview(cap.index_sha256, (), (), ())
    assert not out.report()["entities"]
    assert "## upiscium/Test" not in out.markdown
    assert "進展なしの認定ではありません" in out.markdown
    assert out.report()["publication_authorized"] is False
