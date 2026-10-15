"""One useful private Daily view from independent Issue/PR C5 runs.

No new model calls from digest composition. The C4 renderer owns all source
identity, ordinary repository headers, pending items and Japanese draft text.
"""
from dataclasses import replace
import json
import pytest

from obsidian_automation import github_daily_narrative_runtime as rt
from obsidian_automation import github_daily_witnessed_narrative as c4
from obsidian_automation.github_daily_individual_digest import (
    compose_private_individual_digest, IndividualDigestError,
)
from test_github_daily_narrative_runtime import Transport, backend
from test_github_daily_route_cas import safe_layout
from test_github_daily_witnessed_narrative import case, kwargs


def entities(case):
    contexts = case[5].report()["contexts"]
    assert len(contexts) == 2
    return [tuple(row["context"]["entity"][k] for k in
                  ("repository", "entity_type", "number")) for row in contexts]


def individual(case, target, transport=None):
    t = transport or Transport()
    return rt.run_witnessed_narrative_inference(
        **kwargs(case), target_entity=target, infer=backend(t),
        max_entities=1, max_calls=4)


def compose(case, runs=()):
    return compose_private_individual_digest(**kwargs(case), individual_runs=tuple(runs))


def test_two_separately_reviewed_entity_runs_make_one_complete_daily(case):
    names = entities(case)
    run_a, run_b = (individual(case, t) for t in names)
    expected = rt.run_witnessed_narrative_inference(**kwargs(case),
        infer=backend(Transport()))
    daily = compose(case, [run_b, run_a])  # Reorder must not change C4 source order
    assert daily.preview.sha256 == expected.preview.sha256
    assert daily.markdown == expected.preview.markdown
    assert daily.markdown.startswith("# Project Progress — 2026-10-05")
    assert daily.markdown.count("AI下書き（未承認）") == 2
    assert daily.markdown.count("## upiscium/") == 1
    report = daily.report()
    assert report["selected_entity_count"] == 2
    assert report["requested_entity_count"] == 2
    assert report["unrequested_entity_count"] == 0
    assert report["pending_summary_entity_count"] == 0
    assert all(row["display_state"] == "ai_draft_pending_human_review"
               for row in report["entities"])
    assert report["new_model_calls"] == 0
    assert report["publication_authorized"] is False
    assert report["semantic_coverage_verified"] is False
    assert report["factuality_verified"] is False
    assert "検証処理" not in daily.canonical_bytes.decode()


def test_partial_day_preserves_nonrequested_entity_as_pending(case):
    run_a = individual(case, entities(case)[0])
    report = compose(case, [run_a])
    assert report.report()["selected_entity_count"] == 2
    assert report.report()["requested_entity_count"] == 1
    assert report.report()["unrequested_entity_count"] == 1
    assert report.report()["pending_summary_entity_count"] == 1
    assert report.markdown.count("AI下書き（未承認）") == 1
    assert "本文は未生成、根拠不足、または照合待ちです。" in report.markdown
    assert any(row["display_state"] == "source_item_pending_summary"
               for row in report.report()["entities"])


def test_all_failed_sources_still_visible_not_no_progress(case):
    t = Transport()
    t.fail_stage = "editorial"
    a = individual(case, entities(case)[0], t)
    assert not a.narrative_replies
    digest = compose(case, [a])
    assert digest.report()["pending_summary_entity_count"] == 2
    assert digest.markdown.count("本文は未生成、根拠不足、または照合待ちです。") == 2
    assert "進展なしとは判定していません。" in digest.markdown
    assert digest.report()["publication_authorized"] is False


def test_zero_inference_does_not_silently_mark_entire_day_empty(case):
    daily = compose(case)
    assert daily.report()["selected_entity_count"] == 2
    assert daily.report()["requested_entity_count"] == 0
    assert daily.report()["pending_summary_entity_count"] == 2
    assert daily.report()["entities"][0]["requested_for_inference"] is False
    assert "AI下書き（未承認）" not in daily.markdown


def test_model_rejected_grounding_retains_source_pending(case):
    transport = Transport()
    transport.ground_verdict = "unsupported"
    a = individual(case, entities(case)[0], transport)
    result = compose(case, [a])
    assert result.report()["pending_summary_entity_count"] == 2
    assert result.report()["entities"][0]["pending_source_event_count"] > 0
    assert result.report()["new_model_calls"] == 0


def test_original_c4_replay_needed_not_model_generated_identity(case):
    a = individual(case, entities(case)[0])
    bad = dict(a.report())
    bad["publication_authorized"] = True
    fake = replace(a, canonical_bytes=rt._json(bad))
    with pytest.raises(IndividualDigestError, match="authority"):
        compose(case, [fake])


def test_tampered_preview_or_companion_digest_fails(case):
    a = individual(case, entities(case)[0])
    fake = replace(a, preview=replace(a.preview, canonical_bytes=b'{"fake":true}\n'))
    with pytest.raises(IndividualDigestError):
        compose(case, [fake])
    fake_review = replace(a.source_review, canonical_bytes=b'{}\n')
    with pytest.raises(IndividualDigestError):
        compose(case, [replace(a, source_review=fake_review)])


@pytest.mark.parametrize("mode", ["duplicate","whole","foreign","untyped","not_tuple","too_many"])
def test_duplicate_foreign_or_unscoped_inputs_rejected(case, mode):
    a = individual(case, entities(case)[0])
    if mode == "duplicate":
        runs = (a, a)
    elif mode == "whole":
        runs = (rt.run_witnessed_narrative_inference(**kwargs(case),
                  infer=backend(Transport())),)
    elif mode == "foreign":
        bad = dict(a.report())
        bad["target_entity"]["number"] = 99999
        runs = (replace(a, canonical_bytes=rt._json(bad)),)
    elif mode == "untyped":
        runs = ({"report":a.report()},)
    elif mode == "not_tuple":
        with pytest.raises(IndividualDigestError):
            compose_private_individual_digest(**kwargs(case), individual_runs=[a])
        return
    else:
        runs = (a,) * (c4.MAX_ENTITIES + 1)
    with pytest.raises(IndividualDigestError):
        compose_private_individual_digest(**kwargs(case),individual_runs=runs)


def test_wrong_grounding_context_fails_original_c4_gate(case):
    a = individual(case, entities(case)[0])
    assert a.grounding_replies
    fake_grounding = replace(a.grounding_replies[0], ground_context_sha256="f"*64)
    with pytest.raises(IndividualDigestError):
        compose(case,[replace(a,grounding_replies=(fake_grounding,))])


def test_single_entity_runs_are_not_relabelled_as_execution_attestation(case):
    a = individual(case, entities(case)[0])
    result = compose(case,[a])
    metadata = result.report()
    assert metadata["model_execution_attested"] is False
    assert metadata["requires_human_quality_review"] is True
    assert metadata["publication_authorized"] is False



def test_replaced_unsupported_grounding_cannot_be_promoted_to_supported(case):
    t = Transport()
    t.ground_verdict = "unsupported"
    run = individual(case, entities(case)[0], t)
    assert run.grounding_replies
    baseline = compose(case, [run])
    assert baseline.report()["pending_summary_entity_count"] == 2

    # The replacement is *valid C4 Grounding* for the same exact context.
    # Citation and schema alone cannot prove it was the original C5 reply.
    original = run.grounding_replies[0]
    upgraded = replace(original, response_bytes=rt._json({
        "verdict": "supported", "reason": "改ざんした支持判定です。",
    }))
    fake = replace(run, grounding_replies=(upgraded,))
    with pytest.raises(IndividualDigestError, match="grounding differs"):
        compose(case, [fake])


def test_replaced_valid_editorial_bytes_cannot_change_original_draft(case):
    run = individual(case, entities(case)[0])
    original = run.narrative_replies[0]
    new = json.loads(original.response_bytes)
    new["sentences"][0]["text"] = "別の作業が完了したとする文章です。"
    forged = replace(original, response_bytes=rt._json(new))
    with pytest.raises(IndividualDigestError, match="editorial differs"):
        compose(case, [replace(run, narrative_replies=(forged,))])


def test_changed_grounding_attempt_record_is_not_silently_accepted(case):
    run = individual(case, entities(case)[0])
    old = run.attempts[-1]
    wrong = replace(old, raw_response_bytes='{"verdict":"supported","reason":"別の応答"}'.encode())
    with pytest.raises(IndividualDigestError, match="attempt bytes changed"):
        compose(case, [replace(run, attempts=run.attempts[:-1] + (wrong,))])



def test_complete_day_private_operator_runs_every_selected_entity(case):
    from obsidian_automation.github_daily_individual_digest import (
        run_complete_private_daily,
    )
    t = Transport()
    full = run_complete_private_daily(
        **kwargs(case), infer=backend(t), max_entities=4,
        require_day_policy=False)  # archival/synthetic fixture only
    report = full.report()
    assert report["selected_entity_count"] == 2
    assert report["requested_entity_count"] == 2
    assert report["unrequested_entity_count"] == 0
    assert report["all_selected_entities_requested"]
    assert report["all_inference_responses_complete"]
    assert report["complete_and_all_selected_have_supported_text"]
    assert report["source_period_policy"] == "legacy_unverified_explicit_offline"
    assert report["publication_authorized"] is False
    assert len(full.individual_runs) == 2
    assert full.digest.markdown == full.markdown
    assert full.markdown.count("AI下書き（未承認）") == 2
    assert "検証処理" not in full.canonical_bytes.decode()
    # Same original C4 preview as its single bulk C5 run; no rephrasing.
    compare = rt.run_witnessed_narrative_inference(
        **kwargs(case), infer=backend(Transport()),
        max_entities=2, max_calls=8)
    assert full.digest.preview.sha256 == compare.preview.sha256
    # Two independently scoped C5 runs, rather than another model stage.
    assert len([x for x in t.calls if x[1] == "/api/chat"]) == 4


def test_strict_day_policy_rejects_legacy_without_model_call(case):
    from obsidian_automation.github_daily_individual_digest import (
        run_complete_private_daily,
    )
    transport = Transport()
    with pytest.raises(IndividualDigestError, match="date-scoped"):
        run_complete_private_daily(
            **kwargs(case), infer=backend(transport))
    assert not transport.calls


@pytest.mark.parametrize("changed", [
    {"max_entities": 1}, {"max_total_calls": 7},
    {"max_total_calls": True}, {"max_total_seconds": 0},
    {"per_entity_seconds": False}, {"require_day_policy": None},
])
def test_complete_day_full_budget_preflight_never_silently_drops_entity(case, changed):
    from obsidian_automation.github_daily_individual_digest import (
        run_complete_private_daily,
    )
    t = Transport()
    options = {"require_day_policy": False, **changed}
    with pytest.raises(IndividualDigestError):
        run_complete_private_daily(
            **kwargs(case), infer=backend(t), **options)
    assert not t.calls


def test_complete_day_stops_new_requests_and_keeps_not_started_item_pending(
        case, monkeypatch):
    from obsidian_automation import github_daily_individual_digest as mod
    clock = [0.]
    monkeypatch.setattr(mod, "_monotonic", lambda: clock[0])
    t = Transport()
    base = backend(t)
    def bump(raw, *, deadline):
        reply = base(raw, deadline=deadline)
        clock[0] = 1000.
        return reply
    daily = mod.run_complete_private_daily(
        **kwargs(case), infer=bump, require_day_policy=False,
        max_total_seconds=20, per_entity_seconds=10)
    metadata = daily.report()
    assert metadata["selected_entity_count"] == 2
    assert metadata["requested_entity_count"] == 1
    assert metadata["unrequested_entity_count"] == 1
    assert metadata["stopped_before_new_entity_reason"] == "total_elapsed_budget_exhausted"
    assert metadata["complete_and_all_selected_have_supported_text"] is False
    assert daily.digest.report()["pending_summary_entity_count"] >= 1
    assert "進展なしとは判定していません" in daily.markdown


def test_complete_day_model_failure_is_pending_not_no_progress(case):
    from obsidian_automation.github_daily_individual_digest import (
        run_complete_private_daily,
    )
    t = Transport()
    t.fail_stage = "editorial"
    daily = run_complete_private_daily(
        **kwargs(case), infer=backend(t),
        max_entities=2, require_day_policy=False)
    report = daily.report()
    assert report["all_selected_entities_requested"]
    assert not report["all_inference_responses_complete"]
    assert report["pending_summary_entity_count"] == 2
    assert report["complete_and_all_selected_have_supported_text"] is False
    assert daily.markdown.count("本文は未生成、根拠不足、または照合待ちです。") == 2


def test_explicit_day_policy_gate_rejects_partial_metadata_before_model(case):
    from obsidian_automation import github_daily_individual_digest as mod
    t = Transport()
    # Built-in archived C4 fixture lacks #358's owned temporal policy.
    with pytest.raises(IndividualDigestError):
        mod.run_complete_private_daily(
            **kwargs(case), infer=backend(t), require_day_policy=True)
    assert not t.calls



def test_complete_day_reports_upstream_pending_entities_not_empty_day(case):
    from obsidian_automation.github_daily_individual_digest import (
        run_complete_private_daily,
    )
    args = kwargs(case)
    args["materiality_decisions"] = ()  # support exists; materiality not selected
    transport = Transport()
    report = run_complete_private_daily(
        **args, infer=backend(transport), require_day_policy=False)
    data = report.report()
    assert data["selected_entity_count"] == 0
    assert data["original_source_entity_count"] == 2
    assert data["original_source_event_count"] >= 2
    assert data["unselected_pending_review_count"] == 2
    assert data["unselected_entity_count"] == 2
    assert len(data["unselected_pending_entities"]) == 2
    assert data["source_selection_requires_review"] is True
    assert data["complete_and_all_selected_have_supported_text"] is False
    assert data["requested_entity_count"] == 0
    assert data["publication_authorized"] is False
    assert not transport.calls
