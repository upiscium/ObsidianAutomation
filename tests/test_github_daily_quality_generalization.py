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
