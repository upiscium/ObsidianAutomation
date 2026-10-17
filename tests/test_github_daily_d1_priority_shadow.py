"""d1-3B only, source-safe optional shadow priority before existing C5."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import math

import pytest

from obsidian_automation import github_daily_d1_priority_shadow as d1
from test_github_daily_c5_source_view import event
from test_github_daily_narrative_runtime import case, kwargs
from test_github_daily_route_cas import safe_layout


CAP = "a" * 64
PROFILE_REVISION = d1.PINNED_D1_PROFILE_REVISION
PROFILE = d1.PINNED_D1_PROFILE_ID
SEMANTICS = d1.PINNED_D1_SEMANTICS_ID
DEPLOYMENT = d1.PINNED_D1_DEPLOYMENT_REVISION
CONTRACT = d1.PINNED_D1_SERVING_CONTRACT_REVISION
COMMIT = "f" * 40


def contexts():
    old = event(0, "pull_request_commit", message="feat(#300): old shared broker",
                source_status="content_addressed_commit_not_push_time")
    old["original_event"]["sha"] = COMMIT
    new = event(1, "pull_request_commit", message="feat(#300): newly scoped pause/resume",
                source_status="content_addressed_commit_not_push_time")
    new["original_event"]["sha"] = "1" * 40
    note = event(2, "pull_request_comment", body="Phase I advanced.\n\n"
                 "Not installed.\nNo live effects or production cutover.")
    old2 = deepcopy(old)
    inherited_issue = {
        "entity": {"repository": "upiscium/Test",
                   "entity_type": "pull_request", "number": 367},
        "stage": "github_daily_narrative_context_v4",
        "capture_sha256": CAP,
        "publication_authorized": False, "date": "2026-10-16",
        "title": "PR #367 old broker", "sources": [old],
    }
    child = {
        "entity": {"repository": "upiscium/Test",
                   "entity_type": "pull_request", "number": 374},
        "stage": "github_daily_narrative_context_v4",
        "capture_sha256": CAP,
        "publication_authorized": False, "date": "2026-10-16",
        "title": "PR #374 new pause/resume", "sources": [old2, new, note],
    }
    return [inherited_issue, child]


def response(request, *, choice="must_include", abstain=False):
    question = json.loads(request.canonical_bytes)
    scores = {"must_include": .9, "supporting": .08, "background": .02}
    if choice == "background":
        scores = {"must_include": .01, "supporting": .02, "background": .97}
    if choice == "supporting":
        scores = {"must_include": .04, "supporting": .9, "background": .06}
    return d1._json({
        "object": "astrumweaver.decision",
        "profile": question["profile"],
        "choice_id": None if abstain else choice,
        "abstained": abstain,
        "scores": [{"id": k, "score": scores[k]} for k in d1.CATEGORIES],
        "score_kind": "choice_set_probability",
        "calibration": {"status": "uncalibrated", "reference_sha256": None},
        "usage": {"input_tokens": 251, "output_tokens": 0},
        "x_astrumweaver": {
            "profile_revision": PROFILE_REVISION,
            "deployment_revision": DEPLOYMENT,
            "serving_contract_revision": CONTRACT,
            "decision_semantics_id": SEMANTICS,
            "provider_score_semantics": "llama-cpp-systemone-temperature-softmax-v1",
            "mode": "shadow",
            "authority": "recommendation-only",
            "abstain_below": .65,
        },
    })


def build():
    return d1._original_plan(contexts(), CAP)


def test_verified_same_commit_sha_cross_pr_provenance_is_advisory_not_a_date_or_approval():
    plan = build()
    data = plan.report()
    assert data["source_capture_sha256"] == CAP
    assert data["selected_entity_count"] == 2
    assert data["source_original_event_count"] == 4
    assert data["source_evidence_deleted"] is False
    assert data["publication_authorized"] is False
    assert data["model_selected"] == "d1-3B"
    assert data["model_question_version"] == d1.QUESTION_VERSION
    p1, p2 = data["paragraphs"][0], data["paragraphs"][1]
    assert p1["entity"]["number"] == 367
    assert p1["same_commit_other_pr_numbers"] == [374]
    assert p1["same_commit_earlier_numbered_prs"] == []
    assert p2["entity"]["number"] == 374
    assert p2["same_commit_other_pr_numbers"] == [367]
    assert p2["same_commit_earlier_numbered_prs"] == [367]
    assert p2["source_ref"] == 0
    assert p2["commit_sha"] == COMMIT
    assert p2["historical_push_at_verified"] is False
    assert p2["source_temporal_status"] == "content_addressed_commit_not_push_time"
    own = next(x for x in data["paragraphs"] if x["commit_sha"] == "1" * 40)
    assert own["same_commit_other_pr_numbers"] == []
    assert own["same_commit_object_verified_from_collector"] is False


def test_exact_original_segment_offsets_and_long_paragraphs_have_no_lost_non_whitespace():
    raw = "Phase I: A.\n\n  More details.\n\n" + "実装" * 1300 + "\nend."
    spans = d1._ranges(raw)
    assert len(spans) >= 4
    assert all(0 <= start < stop <= len(raw) and stop-start <= d1.MAX_SEGMENT_CHARS
               for start, stop in spans)
    observed = set()
    for start, stop in spans:
        assert raw[start:stop].strip()
        observed.update(range(start, stop))
    assert all(index in observed or char.isspace() for index, char in enumerate(raw))
    assert [raw[s:e] for s, e in spans][0] == "Phase I: A."
    assert any("実装" in raw[s:e] for s, e in spans)


def test_no_snapshot_field_or_withheld_temporal_content_restored():
    c = contexts()
    c[1]["sources"].append(event(3, "pull_request_snapshot", body="Old snapshot scope.",
                                 source_status="day_snapshot_not_event_instant"))
    c[1]["sources"].append(event(4, "pull_request_created",
                                 source_status="withheld_outside_target_day",
                                 withheld=("body", "title", "draft", "state")))
    p = d1._original_plan(c, CAP)
    rows = p.report()["paragraphs"]
    assert any(r["original_kind"] == "pull_request_snapshot" for r in rows)
    assert not any(r["source_ref"] == 4 and r["entity"]["number"] == 374 for r in rows)
    assert all(r["historical_push_at_verified"] is False for r in rows)
    assert p.report()["source_original_event_count"] == 6
    assert not p.report()["source_evidence_deleted"]


@pytest.mark.parametrize("kind", ["bad-index", "missing-id", "wrong-sha", "bad-temporal",
                                 "bad-type", "foreign-capture"])
def test_invalid_original_source_rejected_without_calling_model(kind):
    c = contexts()
    if kind == "bad-index":
        c[1]["sources"][0]["source_ref"] = True
    elif kind == "missing-id":
        c[1]["sources"][0]["original_event"].pop("evidence_id")
    elif kind == "wrong-sha":
        c[1]["sources"][0]["original_event"]["sha"] = "bad"
    elif kind == "bad-temporal":
        c[1]["sources"][0]["original_event"]["temporal_observation"]["historical_instant_verified"] = True
    elif kind == "bad-type":
        c[1]["sources"][0]["original_event"]["message"]["truncated"] = 1
    elif kind == "foreign-capture":
        c[1]["capture_sha256"] = "c" * 64
    with pytest.raises(d1.D1PriorityShadowError):
        d1._original_plan(c, CAP)


def test_request_maps_original_exact_span_and_bound_verified_sha_to_existing_tsumgi_contract():
    plan = build()
    segment = next(x for x in plan.report()["paragraphs"]
                   if x["entity"]["number"] == 374 and x["commit_sha"] == COMMIT)
    request = d1.build_d1_decision_request(plan, segment["id"], profile=PROFILE,
                                            profile_revision=PROFILE_REVISION)
    payload = request.report()
    assert set(payload) == {"profile", "profile_revision", "state", "question", "choices"}
    assert payload["profile"] == PROFILE
    assert payload["profile_revision"] == PROFILE_REVISION
    assert [x["id"] for x in payload["choices"]] == list(d1.CATEGORIES)
    assert payload["state"].endswith(segment["text"])
    assert COMMIT not in payload["state"]
    assert "#367" in payload["state"]
    assert "not proof" in payload["state"].lower()
    assert len(request.canonical_bytes) <= d1.MAX_REQUEST_BYTES
    assert request.plan_sha256 == plan.sha256
    assert d1._sha(request.canonical_bytes) == request.sha256


def test_valid_managed_d1_observation_remains_uncalibrated_advisory():
    plan = build()
    id_ = plan.report()["paragraphs"][0]["id"]
    request = d1.build_d1_decision_request(plan, id_, profile=PROFILE,
                                            profile_revision=PROFILE_REVISION)
    data = response(request)
    record = d1.admit_tsumgi_d1_observation(request, data)
    assert record["recommendation"] == "must_include"
    assert record["scores"]["must_include"] == .9
    assert record["raw_response_sha256"] == d1._sha(data)
    assert record["status"] == "observed"
    assert record["profile_revision"] == PROFILE_REVISION
    assert record["source_deleted"] is False
    assert record["publication_authorized"] is False
    assert record["score_calibrated"] is False
    assert record["authority"] == "advisory_only"


def test_abstention_recommendation_none_is_not_no_progress():
    plan = build()
    id_ = plan.report()["paragraphs"][0]["id"]
    req = d1.build_d1_decision_request(plan, id_, profile=PROFILE,
                                        profile_revision=PROFILE_REVISION)
    row = d1.admit_tsumgi_d1_observation(req, response(req, abstain=True))
    assert row["status"] == "abstained"
    assert row["recommendation"] is None
    assert row["scores"]["must_include"] > .5


@pytest.mark.parametrize("mutation", [
    ("scores", [{"id":"background","score":1.0}]),
    ("scores", [{"id":"must_include","score":.5},
                {"id":"supporting","score":.3},
                {"id":"background","score":.3}]),
    ("scores", [{"id":"must_include","score":float("nan")},
                {"id":"supporting","score":0.0},
                {"id":"background","score":0.0}]),
    ("score_kind", "fake_probability"),
    ("profile", "different-profile"),
    ("choice_id", "unknown"),
    ("abstained", None),
    ("calibration", {"status":"calibrated","reference_sha256":"sha256:"+ "a"*64}),
    ("usage", {"input_tokens":1,"output_tokens":1}),
])
def test_malformed_or_unapproved_managed_response_rejected(mutation):
    plan = build()
    seg = plan.report()["paragraphs"][0]["id"]
    req = d1.build_d1_decision_request(plan, seg, profile=PROFILE,
                                        profile_revision=PROFILE_REVISION)
    payload = json.loads(response(req))
    payload[mutation[0]] = mutation[1]
    with pytest.raises(d1.D1PriorityShadowError):
        d1.admit_tsumgi_d1_observation(
            req, json.dumps(payload, allow_nan=True).encode("utf-8"))


def test_wrong_review_authority_revision_and_legacy_response_never_admitted():
    plan = build()
    seg = plan.report()["paragraphs"][0]["id"]
    req = d1.build_d1_decision_request(plan, seg, profile=PROFILE,
                                        profile_revision=PROFILE_REVISION)
    payload = json.loads(response(req))
    for field, value in (("authority","approve"), ("mode","live"), ("profile_revision","sha256:"+"9"*64)):
        altered = deepcopy(payload)
        altered["x_astrumweaver"][field] = value
        with pytest.raises(d1.D1PriorityShadowError):
            d1.admit_tsumgi_d1_observation(req, d1._json(altered))
    with pytest.raises(d1.D1PriorityShadowError):
        d1.admit_tsumgi_d1_observation(req, b"{broken")


def test_shadow_transport_called_only_when_injected_and_source_never_filtered():
    plan = build()
    before = plan.canonical_bytes
    called = []
    def model(raw):
        payload = json.loads(raw)
        called.append(payload)
        fake = d1.DecisionRequest("not-needed", raw, PROFILE_REVISION, plan.sha256)
        return response(fake, choice="background")
    ids = tuple(p["id"] for p in plan.report()["paragraphs"][:4])
    result = d1.run_private_d1_priority_shadow(plan, profile=PROFILE,
        profile_revision=PROFILE_REVISION, infer=model, max_calls=3,
        selected_segment_ids=ids)
    data = result.report()
    assert len(called) == 3
    assert data["requested_segment_count"] == 3
    assert data["unrequested_segment_count"] == len(plan.report()["paragraphs"])-3
    assert data["summary"]["observed"] == 3
    assert all(row["recommendation"] == "background" for row in data["observations"])
    assert data["question_version"] == d1.QUESTION_VERSION
    assert data["may_filter_C5_source"] is False
    assert data["source_evidence_deleted"] is False
    assert data["publication_authorized"] is False
    assert plan.canonical_bytes == before


def test_transport_or_malformed_model_reply_remains_pending():
    plan = build()
    ids = tuple(p["id"] for p in plan.report()["paragraphs"][:3])
    calls=[TimeoutError("secret unavailable"), b"bad-json", b'{"wrong":"shape"}']
    def failing_model(_raw):
        val = calls.pop(0)
        if isinstance(val, Exception):
            raise val
        return val
    result=d1.run_private_d1_priority_shadow(plan, profile=PROFILE,
        profile_revision=PROFILE_REVISION, infer=failing_model,
        selected_segment_ids=ids, max_calls=3)
    data=result.report()
    assert data["summary"]["pending"] == 3
    assert all(row["status"] == "pending" and row["recommendation"] is None for row in data["observations"])
    assert all(row["source_deleted"] is False and row["publication_authorized"] is False for row in data["observations"])
    assert "secret unavailable" not in result.canonical_bytes.decode()
    assert "no_progress" not in result.canonical_bytes.decode()


def test_source_budget_fail_closed_before_model_request(monkeypatch):
    c = contexts()
    c[1]["sources"][2]["original_event"]["body"]["text"] = "IMPORTANT." * 200
    monkeypatch.setattr(d1, "MAX_SEGMENTS_PER_ENTITY", 1)
    with pytest.raises(d1.D1PriorityShadowError, match="paragraph budget"):
        d1._original_plan(c, CAP)


def test_real_cas_verified_C4_rebuilt_for_d1_plan(case):
    plan = d1.build_private_d1_priority_plan(**kwargs(case))
    x = plan.report()
    assert x["publication_authorized"] is False
    assert x["source_evidence_deleted"] is False
    assert x["selected_entity_count"] == 2
    assert x["source_capture_sha256"] != CAP
    assert x["paragraph_count"] > 0
    assert x["source_original_event_count"] > 0
    assert all(r["original_evidence_id"] and r["source_ref"] >= 0 for r in x["paragraphs"])
    assert all(not r["historical_push_at_verified"] for r in x["paragraphs"])



def test_deployed_profile_bound_2048_state_bytes_failure_is_pending_not_truncated():
    c=contexts()
    c[1]["sources"][2]["original_event"]["body"]["text"]="今後の具体的な未完了事項。" * 200
    plan=d1._original_plan(c,CAP)
    target=next(x for x in plan.report()["paragraphs"]
                if x["entity"]["number"]==374 and x["original_kind"]=="pull_request_comment"
                and x["field"]=="body")
    with pytest.raises(d1.D1PriorityShadowError,match="profile input limit"):
        d1.build_d1_decision_request(plan,target["id"],
                                     profile=PROFILE,profile_revision=PROFILE_REVISION)
    calls=[]
    result=d1.run_private_d1_priority_shadow(
        plan,profile=PROFILE,profile_revision=PROFILE_REVISION,
        infer=lambda b: calls.append(b), max_calls=1,
        selected_segment_ids=(target["id"],)).report()
    assert not calls
    assert result["summary"]=={"observed":0,"abstained":0,"pending":1}
    assert result["may_filter_C5_source"] is False


def test_same_commit_subject_different_sha_is_not_marked_inherited():
    contexts_ = contexts()
    contexts_[1]["sources"][0]["original_event"]["sha"] = "2" * 40
    # Same human-readable subject is not a matching immutable Git object.
    plan = d1._original_plan(contexts_, CAP)
    matching = [x for x in plan.report()["paragraphs"]
                if x["original_kind"] == "pull_request_commit"
                and x["original_evidence_id"] == contexts_[1]["sources"][0]["original_event"]["evidence_id"]
                and x["entity"]["number"] == 374]
    assert matching
    assert all(not x["same_commit_object_verified_from_collector"] for x in matching)
    assert all(x["same_commit_other_pr_numbers"] == [] for x in matching)


def test_shared_sha_in_another_repository_is_not_a_reused_pr_commit():
    ctx=contexts()
    ctx[0]["entity"]["repository"] = "another-owner/same-sha-repo"
    plan = d1._original_plan(ctx, CAP)
    assert all(not x["same_commit_object_verified_from_collector"]
               for x in plan.report()["paragraphs"])


def test_original_source_cas_plan_is_deterministic_independent_of_input_order():
    c=contexts()
    first = d1._original_plan(c, CAP).report()
    second = d1._original_plan(list(reversed(c)), CAP).report()
    # Order of per-entity display follows verified C4 order; recorded commit
    # SHA attribution is identical across contexts despite that order.
    a={(x["entity"]["number"],x["source_ref"],x["field"]):
       tuple(x["same_commit_other_pr_numbers"]) for x in first["paragraphs"]}
    b={(x["entity"]["number"],x["source_ref"],x["field"]):
       tuple(x["same_commit_other_pr_numbers"]) for x in second["paragraphs"]}
    assert a==b


def test_runtime_will_not_start_inference_for_unbound_segment_or_zero_budget():
    plan=build()
    calls=[]
    for requested in (("not-real",), tuple(p["id"] for p in plan.report()["paragraphs"][:1])*2):
        with pytest.raises(d1.D1PriorityShadowError):
            d1.run_private_d1_priority_shadow(
                plan,profile=PROFILE,profile_revision=PROFILE_REVISION,
                infer=lambda x:calls.append(x),max_calls=1,
                selected_segment_ids=requested)
    with pytest.raises(d1.D1PriorityShadowError):
        d1.run_private_d1_priority_shadow(
            plan,profile=PROFILE,profile_revision=PROFILE_REVISION,
            infer=lambda x:calls.append(x),max_calls=0)
    assert not calls


def test_previously_withheld_source_does_not_become_a_model_visible_text():
    c=contexts()
    e=event(3,"pull_request_created",body=None,title=None,draft=None,
            source_status="withheld_unknown_modification_time",
            withheld=("body","title","state","draft"))
    c[1]["sources"].append(e)
    plan=d1._original_plan(c,CAP)
    assert not any(x["source_ref"]==3 and x["entity"]["number"]==374
                   for x in plan.report()["paragraphs"])
    assert plan.report()["source_original_event_count"]==5


def test_independent_review_p2_huge_integer_score_rejected_as_pending():
    plan=build()
    segment=plan.report()["paragraphs"][0]["id"]
    req=d1.build_d1_decision_request(
        plan,segment,profile=PROFILE,profile_revision=PROFILE_REVISION)
    forged=json.loads(response(req))
    forged["scores"][0]["score"]=10**400
    def huge_score(_):
        return json.dumps(forged).encode()
    record=d1.run_private_d1_priority_shadow(
        plan,profile=PROFILE,profile_revision=PROFILE_REVISION,
        infer=huge_score,max_calls=1,selected_segment_ids=(segment,)).report()
    assert record["summary"]=={"observed":0,"abstained":0,"pending":1}
    assert record["observations"][0]["reason"]=="D1PriorityShadowError"
    assert record["may_filter_C5_source"] is False


def test_independent_review_p2_transport_runtime_error_never_aborts_other_cases():
    plan=build()
    ids=tuple(r["id"] for r in plan.report()["paragraphs"][:2])
    seen=[]
    def intermittent(req):
        seen.append(req)
        if len(seen)==1:
            raise RuntimeError("worker unavailable")
        return response(d1.DecisionRequest(
            "ignored",req,PROFILE_REVISION,plan.sha256))
    r=d1.run_private_d1_priority_shadow(
        plan,profile=PROFILE,profile_revision=PROFILE_REVISION,
        infer=intermittent,max_calls=2,selected_segment_ids=ids).report()
    assert len(seen)==2
    assert r["summary"]=={"observed":1,"abstained":0,"pending":1}
    assert r["observations"][0]["reason"]=="RuntimeError"
    assert all(v["source_deleted"] is False for v in r["observations"])


def test_independent_review_p2_other_pinned_or_model_profile_must_not_be_d1():
    plan=build()
    seg=plan.report()["paragraphs"][0]["id"]
    for wrong_profile,wrong_rev in (
            ("clef-shadow-v1",PROFILE_REVISION),
            (PROFILE,"sha256:"+"a"*64),
            ("d1-3b-system-one-v1","sha256:"+"b"*64)):
        with pytest.raises(d1.D1PriorityShadowError):
            d1.build_d1_decision_request(
                plan,seg,profile=wrong_profile,profile_revision=wrong_rev)
    req=d1.build_d1_decision_request(
        plan,seg,profile=PROFILE,profile_revision=PROFILE_REVISION)
    for key in ("deployment_revision","decision_semantics_id",
                "serving_contract_revision"):
        x=json.loads(response(req))
        x["x_astrumweaver"][key]="sha256:"+"9"*64
        with pytest.raises(d1.D1PriorityShadowError,match="binding changed"):
            d1.admit_tsumgi_d1_observation(req,d1._json(x))
