from __future__ import annotations

from copy import deepcopy
import pytest

from obsidian_automation import github_daily_d1_line_shadow as lines
from obsidian_automation import github_daily_d1_priority_shadow as d1
from test_github_daily_d1_priority_shadow import contexts, CAP, PROFILE, PROFILE_REVISION


def test_split_exact_original_line_without_losing_negation_or_source():
    original=contexts()
    original[1]["sources"][2]["original_event"]["body"]["text"]=(
        "Implemented durable queue.\n"
        "Not installed. Run once is unavailable.\n"
        "\n"
        "Draft and not merged.\n"
    )
    parent=d1._original_plan(original,CAP)
    projected=lines.build_line_priority_plan(parent)
    assert projected.sha256!=parent.sha256
    assert projected.report()["original_paragraph_plan_sha256"]==parent.sha256
    assert projected.report()["source_original_event_count"]==parent.report()["source_original_event_count"]
    assert projected.report()["publication_authorized"] is False
    pieces=[x for x in projected.report()["paragraphs"] if x["entity"]["number"]==374
            and x["source_ref"]==2 and x["field"]=="body"]
    assert len(pieces)==3
    assert "Not installed" in pieces[1]["text"]
    assert "unavailable" in pieces[1]["text"]
    assert pieces[1]["text"].endswith("unavailable.")
    assert pieces[2]["text"]=="Draft and not merged.\n"
    text=original[1]["sources"][2]["original_event"]["body"]["text"]
    assert all(text[x["start"]:x["end"]]==x["text"] for x in pieces)
    assert all(x["field_text_sha256"]==pieces[0]["field_text_sha256"] for x in pieces)


def test_unchanged_single_line_is_same_citable_span():
    parent=d1._original_plan(contexts(),CAP)
    projected=lines.build_line_priority_plan(parent)
    p={x["id"]:x for x in parent.report()["paragraphs"]}
    for x in projected.report()["paragraphs"]:
        assert x["line_original_parent_id"] in p
        assert x["source_ref"]==p[x["line_original_parent_id"]]["source_ref"]
        assert x["original_evidence_id"]==p[x["line_original_parent_id"]]["original_evidence_id"]
        assert x["field_text_sha256"]==p[x["line_original_parent_id"]]["field_text_sha256"]
    assert len(projected.report()["paragraphs"]) >= len(parent.report()["paragraphs"])
    assert projected.report()["paragraph_count"] == len(projected.report()["paragraphs"])


def test_too_long_line_is_not_truncated_to_fit_model_window():
    original=contexts()
    original[1]["sources"][2]["original_event"]["body"]["text"]="A"*1300 + " not installed."
    parent=d1._original_plan(original,CAP)
    projected=lines.build_line_priority_plan(parent)
    rows=[x for x in projected.report()["paragraphs"] if x["entity"]["number"]==374
          and x["source_ref"]==2 and x["field"]=="body"]
    assert len(rows)==1
    assert rows[0]["text"].endswith("not installed.")
    assert rows[0]["line_exceeds_safe_char_hint"] is True
    assert len(rows[0]["text"])==1315
    with pytest.raises(d1.D1PriorityShadowError,match="profile input limit"):
        d1.build_d1_decision_request(projected,rows[0]["id"],profile=PROFILE,
                                     profile_revision=PROFILE_REVISION)


def test_bad_original_source_or_plan_rejected():
    parent=d1._original_plan(contexts(),CAP)
    cases=[
        lambda x:x.update(publication_authorized=True),
        lambda x:x["paragraphs"][0].update(text="not the source"),
        lambda x:x["paragraphs"][0].update(start=True),
        lambda x:x["paragraphs"][0].update(end=x["paragraphs"][0]["end"]+1),
    ]
    for change in cases:
        obj=parent.report()
        change(obj)
        with pytest.raises(d1.D1PriorityShadowError):
            lines.build_line_priority_plan(d1.PriorityPlan(d1._json(obj)))


def test_d1_managed_binding_and_original_sha_unaffected_by_line_plan():
    parent=d1._original_plan(contexts(),CAP)
    projected=lines.build_line_priority_plan(parent)
    x=projected.report()["paragraphs"][0]
    req=d1.build_d1_decision_request(projected,x["id"],
                                     profile=PROFILE,profile_revision=PROFILE_REVISION)
    assert x["text"] in req.report()["state"]
    assert req.plan_sha256==projected.sha256
    assert req.report()["profile"]==d1.PINNED_D1_PROFILE_ID
