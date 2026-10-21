"""Independent, non-case-numbered holdout for optional Daily v2 quality rules.

Different repositories/PR IDs/features and source sequences intentionally
avoid the original #155/#330/#158 identifiers. All assertions are advisory;
no final model output or Writer/Timer authority is certified by these tests.
"""
from copy import deepcopy

import pytest

from obsidian_automation import github_daily_d1_priority_shadow as d1
from obsidian_automation import github_daily_feature_stage as feature
from obsidian_automation import github_daily_scope_quality as scope
from obsidian_automation import github_daily_narrative_runtime as rt
from test_github_daily_c5_source_view import event
from test_github_daily_d1_priority_shadow import CAP


def build(repo, number, *, merged, name, blank_before_merge=False):
    ctx={
        "stage":"github_daily_narrative_context_v4",
        "capture_sha256":CAP, "publication_authorized":False,
        "date":"2026-10-18", "title":f"feat(runtime): add {name}",
        "entity":{"repository":repo,"entity_type":"pull_request","number":number},
        "sources":[
            event(0,"pull_request_created",
                  title=f"feat(runtime): add {name}",
                  body=f"## Implemented\n- Added {name} in a disabled-by-default module.\n"
                       "No merge, no release, no change to running GA services."),
            event(1,"pull_request_comment",
                  body=f"CI smoke for {name} succeeded. No production rollout."),
        ],
    }
    if blank_before_merge:
        ctx["sources"].append(event(2,"pull_request_comment",
                    body="The previous test-only sibling remains unmerged."))
    if merged:
        ref=len(ctx["sources"])
        ctx["sources"].append(event(ref,"pull_request_merged",
                    source_status="event_transition_only"))
    return ctx


@pytest.mark.parametrize("repo,number,name",[
    ("sample/LUMEN",71,"user-facing preview selector"),
    ("sample/MERIDIAN",620,"durable queue"),
    ("alternate/KITE",4096,"Nix inference plugin"),
    ("alternate/POLARIS",17,"document indexing backend"),
])
@pytest.mark.parametrize("merged", [True,False])
def test_feature_lifecycle_is_generic_across_repos_ids_and_functionality(
        repo,number,name,merged):
    ctx=build(repo,number,merged=merged,name=name)
    before=deepcopy(ctx["sources"])
    view=feature.build_feature_first_view(ctx,d1._original_plan([ctx],CAP))
    assert view.structural_feature_anchor is True
    doc=view.report()
    assert doc["source_deleted"] is False
    assert doc["publication_authorized"] is False
    assert doc["original_source_event_count"]==len(ctx["sources"])
    assert ctx["sources"]==before
    assert all(i in doc["shown_source_refs"]+doc["omitted_source_refs"]
               for i in range(len(ctx["sources"])))
    if merged:
        terminal=doc["latest_lifecycle_transition"]
        assert terminal["kind"]=="pull_request_merged"
        assert terminal["source_ref"]==len(ctx["sources"])-1
        assert terminal["original_evidence_id"]==ctx["sources"][-1]["original_event"]["evidence_id"]
        assert terminal["source_ref"] in view.shown_source_refs
        assert not any("No merge" in row["verbatim"] for row in
                       doc["latest_operational_caveat_evidence"])
        assert any("pr_lifecycle" in row["superseded_scopes"] for row in
                   doc["excluded_obsolete_status_hints"])
    else:
        assert doc["latest_lifecycle_transition"] is None
        # Status presentation favors the *later* no-production condition,
        # not necessarily the older "No merge" wording. Both originals stay.
        assert any("No production rollout" in row["verbatim"] for row in
                   doc["latest_operational_caveat_evidence"])
    report=scope.inspect_scope_quality(ctx, [
        {"text":f"{name}の実装変更を追加しました。","source_refs":[0]},
    ])
    assert report["source_claims"]["merged_in_period"] is merged
    assert report["semantic_completeness_certified"] is False
    assert report["publication_authorized"] is False
    assert ("merged_pr_transition_omitted" in report["known_rule_violations"]) is merged


@pytest.mark.parametrize("number", [18,208,12088])
def test_real_merge_status_is_citable_but_is_not_deployment_or_release(number):
    ctx=build("example/NOVA",number,merged=True,name="experimental worker")
    plan=d1._original_plan([ctx],CAP)
    view=feature.build_feature_first_view(ctx,plan)
    merged_ref=ctx["sources"][-1]["source_ref"]
    raw=rt._json({"sentences":[
        {"text":"実験用Workerを追加しました。","source_refs":[0]},
        {"text":"このPRはマージされましたが、本番反映は確認できません。","source_refs":[merged_ref]},
    ]})
    accepted=rt._admit_editorial(ctx,raw,rt.FEATURE_FIRST_SOURCE_CONTRACT,
                                  verified_feature_refs=view.shown_source_refs)
    assert accepted
    summary=scope.inspect_scope_quality(ctx, [
        {"text":"このPRはマージされましたが、本番反映は確認できません。",
         "source_refs":[merged_ref]}
    ])
    assert "merged_pr_transition_omitted" not in summary["known_rule_violations"]
    assert "merged_pr_claimed_unmerged" not in summary["known_rule_violations"]
    assert summary["publication_authorized"] is False


@pytest.mark.parametrize("repo,number", [
    ("sample/LUMEN",205),("sample/MERIDIAN",804),
    ("alternate/KITE",1100),
])
def test_unrelated_merge_mentions_do_not_replace_own_typed_lifecycle(repo,number):
    ctx=build(repo,number,merged=False,name="audit component")
    ctx["sources"].append(event(2,"pull_request_comment",
                                 body="The sibling PR #88 has merged. This PR remains unmerged."))
    report=scope.inspect_scope_quality(ctx, [
        {"text":"関連する別PRがマージされました。","source_refs":[2]}
    ])
    assert report["source_claims"]["merged_in_period"] is False
    assert "merged_pr_transition_omitted" not in report["known_rule_violations"]
    view=feature.build_feature_first_view(ctx,d1._original_plan([ctx],CAP))
    assert view.report()["latest_lifecycle_transition"] is None


@pytest.mark.parametrize("repo,number", [
    ("sample/LUMEN",92),("sample/MERIDIAN",1001),
    ("alternate/KITE",777),
])
def test_unrelated_merge_sentence_does_not_discharge_own_merge_transition(repo,number):
    ctx=build(repo,number,merged=True,name="routing plugin")
    report=scope.inspect_scope_quality(ctx, [
        {"text":"関連する別のPRがマージされました。","source_refs":[1]}
    ])
    assert report["source_claims"]["merged_in_period"] is True
    assert "merged_pr_transition_omitted" in report["known_rule_violations"]


@pytest.mark.parametrize("repo,number,test_title", [
    ("sample/LUMEN",48,"test(runtime): preflight serialized cache capacity"),
    ("sample/MERIDIAN",772,"test(core): simulate lock lease"),
    ("alternate/KITE",10002,"test(api): validate result format"),
])
def test_empty_test_only_commit_is_pending_not_no_progress(repo,number,test_title):
    ctx={
        "stage":"github_daily_narrative_context_v4",
        "entity":{"repository":repo,"entity_type":"pull_request","number":number},
        "capture_sha256":CAP,"publication_authorized":False,
        "date":"2026-10-18","title":test_title,
        "sources":[
            event(0,"pull_request_commit",message=test_title,
                  source_status="content_addressed_commit_not_push_time"),
            event(1,"pull_request_created",source_status="event_transition_only")
        ],
    }
    original=deepcopy(ctx["sources"])
    result=scope.inspect_scope_quality(ctx,[])
    assert "test_only_commit_progress_unreported" in result["known_rule_violations"]
    assert result["original_source_refs"]["test_only_commit"]==[0]
    assert result["publication_authorized"] is False
    assert result["semantic_completeness_certified"] is False
    assert ctx["sources"]==original


def test_issue_only_progress_is_not_turned_into_pr_merge_or_feature_gate():
    ctx={
        "stage":"github_daily_narrative_context_v4",
        "entity":{"repository":"sample/LUMEN","entity_type":"issue","number":909},
        "capture_sha256":CAP,"publication_authorized":False,"title":"Bugfix policy",
        "date":"2026-10-18",
        "sources":[
            event(0,"issue_comment",body="Fixed a request parser bug."),
            event(1,"issue_comment",body="Tests and review still pending."),
        ],
    }
    report=scope.inspect_scope_quality(ctx,[{"text":"要求解析器を修正しました。",
                                                "source_refs":[0]}])
    assert report["source_claims"]["merged_in_period"] is False
    assert report["publication_authorized"] is False
    view=feature.build_feature_first_view(ctx,d1._original_plan([ctx],CAP))
    assert view.report()["source_deleted"] is False if view.structural_feature_anchor else not view.structural_feature_anchor


@pytest.mark.parametrize("repo,number", [
    ("sample/LUMEN",411),("alternate/POLARIS",8202),
])
def test_foreign_pr_co_citing_target_merge_does_not_hide_omission(repo,number):
    ctx=build(repo,number,merged=True,name="distributed index")
    merged_ref=len(ctx["sources"])-1
    result=scope.inspect_scope_quality(ctx,[{
        "text":"別PRがマージされました。",
        "source_refs":[1,merged_ref],
    }])
    assert result["source_claims"]["merged_in_period"] is True
    assert "merged_pr_transition_omitted" in result["known_rule_violations"]


@pytest.mark.parametrize("repo,number", [
    ("sample/MERIDIAN",99),("alternate/KITE",531),
])
def test_foreign_pr_unmerged_does_not_impute_unmerged_to_target(repo,number):
    ctx=build(repo,number,merged=True,name="event receiver")
    result=scope.inspect_scope_quality(ctx,[{
        "text":"関連する別PRは未マージです。",
        "source_refs":[1],
    }])
    assert "merged_pr_claimed_unmerged" not in result["known_rule_violations"]
    assert "merged_pr_transition_omitted" in result["known_rule_violations"]
    own=scope.inspect_scope_quality(ctx,[{
        "text":"このPRはまだ未マージです。",
        "source_refs":[1],
    }])
    assert "merged_pr_claimed_unmerged" in own["known_rule_violations"]


@pytest.mark.parametrize("repo,number", [
    ("sample/LUMEN",29),("sample/MERIDIAN",9999),
    ("alternate/POLARIS",588),
])
def test_foreign_no_live_caveat_cannot_satisfy_current_pr_original_site_gate(repo,number):
    ctx=build(repo,number,merged=True,name="preview renderer")
    unrelated=scope.inspect_scope_quality(ctx,[{
        "text":"別PRは本番未反映です。",
        "source_refs":[0,1,len(ctx["sources"])-1],
    }])
    assert unrelated["source_claims"]["no_site_promotion"] is True
    assert "original_no_deploy_condition_omitted" in unrelated["known_rule_violations"]
    own=scope.inspect_scope_quality(ctx,[{
        "text":"このPRはマージされましたが本番未反映です。",
        "source_refs":[0,len(ctx["sources"])-1],
    }])
    assert "original_no_deploy_condition_omitted" not in own["known_rule_violations"]
    assert "merged_pr_transition_omitted" not in own["known_rule_violations"]


def test_explicit_other_number_with_target_merge_citation_is_not_own_merge():
    ctx=build("sample/LUMEN",218,merged=True,name="recovery trigger")
    merge_ref=len(ctx["sources"])-1
    report=scope.inspect_scope_quality(ctx,[{
        "text":"PR #505がマージされました。",
        "source_refs":[merge_ref],
    }])
    assert "merged_pr_transition_omitted" in report["known_rule_violations"]


@pytest.mark.parametrize("repo,number", [
    ("sample/LUMEN",309),("alternate/KITE",4205),
])
def test_foreign_and_target_clauses_do_not_cross_claim_merge_or_live(repo,number):
    ctx=build(repo,number,merged=True,name="merge-aware controller")
    merge_ref=len(ctx["sources"])-1
    foreign_merge=scope.inspect_scope_quality(ctx,[{
        "text":"PR #505がマージされました。このPRは本番未反映です。",
        "source_refs":[0,merge_ref],
    }])
    assert "merged_pr_transition_omitted" in foreign_merge["known_rule_violations"]
    assert "original_no_deploy_condition_omitted" not in foreign_merge["known_rule_violations"]

    foreign_unmerged=scope.inspect_scope_quality(ctx,[{
        "text":"関連する別PRは未マージです。このPRはマージされました。",
        "source_refs":[merge_ref],
    }])
    assert "merged_pr_claimed_unmerged" not in foreign_unmerged["known_rule_violations"]
    assert "merged_pr_transition_omitted" not in foreign_unmerged["known_rule_violations"]

    foreign_live=scope.inspect_scope_quality(ctx,[{
        "text":"PR #505が本番未反映です。このPRはマージされました。",
        "source_refs":[0,merge_ref],
    }])
    assert "original_no_deploy_condition_omitted" in foreign_live["known_rule_violations"]
    assert "merged_pr_transition_omitted" not in foreign_live["known_rule_violations"]


@pytest.mark.parametrize("container", [
    "<blockquote>\nProduction site is now deployed.\n</blockquote>\n",
    "<blockquote class='example'>\n<!-- </blockquote> -->\n"
    "Production site is now deployed.\n</blockquote>\n",
    "<blockquote>\n<code>\nProduction site is now deployed.\n"
    "</code>\n</blockquote>\n",
])
def test_html_quote_cannot_supersede_actual_no_production_status(container):
    ctx=build("alternate/POLARIS",122,merged=False,name="job scheduler")
    original=deepcopy(ctx["sources"])
    ctx["sources"].append(event(2,"pull_request_comment",body=container))
    plan=d1._original_plan([ctx],CAP)
    view=feature.build_feature_first_view(ctx,plan)
    assert view.structural_feature_anchor
    doc=view.report()
    assert not doc["excluded_obsolete_status_hints"]
    # Latest legitimate negative presentation can be the later no-rollout
    # line, not necessarily the earlier no-GA line; neither is superseded
    # by quoted positive deployment text.
    assert doc["latest_operational_caveat_evidence"]
    assert all("Production site is now deployed." not in row["verbatim"]
               for row in doc["latest_operational_caveat_evidence"])
    source=scope.inspect_scope_quality(ctx,[{
        "text":"このPRは本番未反映です。","source_refs":[0],
    }])
    assert source["source_claims"]["site_transition_reported"] is False
    assert "later_source_site_transition_requires_human_review" not in source["known_rule_violations"]
    assert ctx["sources"][:2]==original
    assert doc["source_deleted"] is False


def test_html_quote_closed_then_explicit_actual_site_status_remains_visible():
    ctx=build("sample/LUMEN",616,merged=False,name="cache selector")
    ctx["sources"].append(event(2,"pull_request_comment",body=(
        "<blockquote>\nProduction site is now deployed.\n</blockquote>\n"
        "## Actual status:\nProduction site is now deployed.\n"
    )))
    report=scope.inspect_scope_quality(ctx,[{
        "text":"本番反映状態を確認しています。","source_refs":[2]
    }])
    assert report["source_claims"]["site_transition_reported"] is True
    assert "later_source_site_transition_requires_human_review" in report["known_rule_violations"]
