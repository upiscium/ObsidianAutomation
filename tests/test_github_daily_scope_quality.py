"""Cross-project holdout regressions: test-only != completed feature; no GA cutover."""
from __future__ import annotations

from copy import deepcopy

import pytest

from obsidian_automation import github_daily_scope_quality as quality
from obsidian_automation import github_daily_feature_stage as feature
from obsidian_automation import github_daily_d1_priority_shadow as d1
from test_github_daily_c5_source_view import event
from test_github_daily_d1_priority_shadow import CAP


def example(repo: str, number: int, *, title: str, body: str, tail: str = "") -> dict:
    sources = [event(0, "pull_request_created", title=title, body=body)]
    if tail:
        sources.append(event(1, "pull_request_comment", body=tail))
    return {
        "stage": "github_daily_narrative_context_v4",
        "entity": {"repository": repo, "entity_type": "pull_request", "number": number},
        "capture_sha256": CAP,
        "publication_authorized": False,
        "title": title,
        "date": "2026-10-18",
        "sources": sources,
    }


def sentence(text, refs=(0,)):
    return {"text": text, "source_refs": list(refs)}


def suzka():
    # Faithful scope tokens from frozen PROJECT-SUZKA #330, not its raw body.
    return example(
        "upiscium/PROJECT-SUZKA", 330,
        title="test(r15): v10 source-continuity metadata and full-reserve capacity preflight",
        body="## R15 P1b-B — AgentState v10 continuity/capacity preflight (TEST ONLY)\n"
             "Independent experiment; no product code or installed runtime changed.\n"
             "Exactly two NEW test-only files. No StateWAL or production v10.\n",
    )


def tsumgi():
    # Original #155's frozen opt-in, no-GA-mutation qualifier, followed by CI.
    return example(
        "upiscium/TSUMGI", 155,
        title="feat(runtime): opt-in official Bonsai 2 PQ2_0 Nix backend",
        body="### Implemented\n- Added opt-in Nix backend.\n"
             "### Constraints\n"
             "No merge, new release, or change to running GA services without explicit approval.\n",
        tail="Independent reviewer timed out; tests and Nix build SUCCESS.",
    )


def test_suzka_test_only_false_product_completion_is_detected():
    result = quality.inspect_scope_quality(suzka(), [
        sentence("AgentState v10のテスト専用事前検証を実施し、機能実装は完了しています。")
    ])
    assert result["source_claims"]["test_only"] is True
    assert "test_only_pr_claimed_product_feature_complete" in result["known_rule_violations"]
    assert result["status"] == "needs_human_review"
    assert result["publication_authorized"] is False
    assert result["semantic_completeness_certified"] is False


def test_suzka_correct_test_only_progress_does_not_claim_product_completion():
    result = quality.inspect_scope_quality(suzka(), [
        sentence("AgentState v10の継続性と容量について、テスト専用の事前検証を行いました。")
    ])
    assert result["known_rule_violations"] == []
    assert result["semantic_completeness_certified"] is False


def test_tsumgi_feature_without_original_no_deploy_condition_is_flagged():
    result = quality.inspect_scope_quality(tsumgi(), [
        sentence("Bonsai 2 PQ2_0 Nixバックエンドをオプトインで実装しました。")
    ])
    assert result["source_claims"]["no_site_promotion"] is True
    assert "original_no_deploy_condition_omitted" in result["known_rule_violations"]
    assert result["original_source_refs"]["no_site_promotion"] == [0]


def test_tsumgi_feature_plus_explicit_live_gate_is_preserved():
    result = quality.inspect_scope_quality(tsumgi(), [
        sentence("Bonsai 2 PQ2_0 Nixバックエンドのオプトイン実装を追加しました。"),
        sentence("本番の稼働サービスへの反映は未実施です。"),
    ])
    assert result["known_rule_violations"] == []


def test_tsumgi_feature_first_view_prefers_no_ga_cutover_over_later_review():
    ctx = tsumgi()
    plan = d1._original_plan([ctx], CAP)
    view = feature.build_feature_first_view(ctx, plan)
    assert view.structural_feature_anchor is True
    doc = view.report()
    assert doc["source_deleted"] is False
    assert doc["publication_authorized"] is False
    assert doc["feature_implementation_evidence"]
    assert [s["source_ref"] for s in doc["latest_operational_caveat_evidence"]] == [0]
    assert "change to running GA services" in (
        doc["latest_operational_caveat_evidence"][0]["verbatim"])


@pytest.mark.parametrize("source", [
    "## Example output:\nNo change to running GA services.\n\nOnly test CI succeeded.\n",
    "> No change to running GA services.\n",
    "~~~~text\nNo change to running GA services.\n~~~~\n",
])
def test_sample_or_quoted_no_deploy_is_not_operational_authority(source):
    ctx = example("upiscium/Test", 55,
        title="feat: sandbox new backend",
        body="### Implemented\n- Added backend.\n" + source)
    result = quality.inspect_scope_quality(ctx, [
        sentence("バックエンドを実装しました。")
    ])
    assert result["source_claims"]["no_site_promotion"] is False


def test_title_alone_and_inherited_feature_are_not_product_implementation():
    ctx = suzka()
    ctx["sources"][0]["original_event"]["body"]["text"] = (
        "TEST ONLY. Inherited from earlier source. No feature promise.")
    result = quality.inspect_scope_quality(ctx, [
        sentence("既存の検証を継続しました。")
    ])
    assert result["source_claims"]["test_only"] is False


@pytest.mark.parametrize("mutation", ["bool-ref", "other-ref", "foreign-stage",
                                      "missing-source", "invalid-sentence"])
def test_invalid_context_never_returns_source_quality_certification(mutation):
    ctx = suzka()
    text = [sentence("テストのみを実施しました。")]
    if mutation == "bool-ref":
        ctx["sources"][0]["source_ref"] = True
    elif mutation == "other-ref":
        ctx["sources"][0]["source_ref"] = 3
    elif mutation == "foreign-stage":
        ctx["stage"] = "synthetic"
    elif mutation == "missing-source":
        ctx["sources"] = []
    elif mutation == "invalid-sentence":
        text[0]["source_refs"] = [True]
    with pytest.raises(quality.ScopeQualityError):
        quality.inspect_scope_quality(ctx, text)


def test_explicit_later_status_is_still_source_observation_not_current_proof():
    ctx = tsumgi()
    ctx["sources"].append(event(2,"pull_request_comment",
                                 body="CI SUCCESS; good to merge after review."))
    result = quality.inspect_scope_quality(ctx, [
        sentence("本番未反映で、オプトイン実装を検証中です。", (0, 2))
    ])
    assert result["known_rule_violations"] == []
    assert result["model_or_grounding_is_semantic_authority"] is False


def test_later_explicit_real_site_report_overrides_older_site_caveat_as_hint():
    ctx = tsumgi()
    ctx["sources"].append(event(2, "pull_request_comment",
                                 body="Running GA services have now been updated."))
    plan = d1._original_plan([ctx], CAP)
    view = feature.build_feature_first_view(ctx, plan)
    # Do not highlight a historical "not promoted" statement as current
    # after a later direct original source update; neither state is probed.
    assert not view.report()["latest_operational_caveat_evidence"]
    assert view.report()["excluded_obsolete_status_hints"][0]["superseded_scopes"] == ["site"]
    out = quality.inspect_scope_quality(ctx, [
        sentence("バックエンドのオプトイン実装を追加しました。")
    ])
    assert out["source_claims"]["no_site_promotion"] is False
    assert out["status"] == "needs_human_review"
    assert "later_source_site_transition_requires_human_review" in out["known_rule_violations"]
    assert out["original_source_refs"]["superseded_site_reports"] == [0]


@pytest.mark.parametrize("later", [
    "CI SUCCESS. Running GA services have now been updated.",
    "> Running GA services have now been updated.",
    "## Example output:\\nRunning GA services have now been updated.",
    "If approved, running GA services have now been updated.",
    "Running GA services will be updated.",
])
def test_untrusted_later_positive_cannot_erase_real_prior_site_gate(later):
    ctx = tsumgi()
    ctx["sources"].append(event(2,"pull_request_comment", body=later))
    out = quality.inspect_scope_quality(ctx, [sentence("Bonsai backendを追加しました。")])
    assert out["source_claims"]["no_site_promotion"] is True
    assert "original_no_deploy_condition_omitted" in out["known_rule_violations"]
    view = feature.build_feature_first_view(ctx,d1._original_plan([ctx],CAP))
    assert "change to running GA services" in (
        view.report()["latest_operational_caveat_evidence"][0]["verbatim"])


def test_oversize_original_scope_source_never_passes_cleanly():
    ctx = suzka()
    ctx["sources"][0]["original_event"]["body"]["text"] += "x" * 66000
    audit = quality.inspect_scope_quality(ctx, [sentence("テスト専用検証です。")])
    assert audit["source_claims"]["source_scan_incomplete"] is True
    assert audit["status"] == "needs_human_review"
    assert "original_source_scan_incomplete" in audit["known_rule_violations"]


def test_named_product_completion_claim_is_detected_on_test_only_pr():
    result = quality.inspect_scope_quality(suzka(), [
        sentence("テスト専用の事前検証を経て、AgentState v10の実装が完了しました。")
    ])
    assert "test_only_pr_claimed_product_feature_complete" in result["known_rule_violations"]


def test_worker_not_installed_cannot_substitute_for_running_ga_site_condition():
    result = quality.inspect_scope_quality(tsumgi(), [
        sentence("Bonsaiバックエンドを実装しました。特権Workerは未導入です。")
    ])
    assert "original_no_deploy_condition_omitted" in result["known_rule_violations"]


def test_ga_requires_explicit_approval_is_scope_qualifier_not_installed_worker():
    result = quality.inspect_scope_quality(tsumgi(), [
        sentence("本番GAサービスへの変更には明示的なメンテナーの承認が必要です。")
    ])
    assert "original_no_deploy_condition_omitted" not in result["known_rule_violations"]


@pytest.mark.parametrize("inert", [
    "Example output:\\n\x60\x60\x60text\\n## Actual status:\\n"
    "Running GA services have now been updated.\\n\x60\x60\x60\\n",
    "<!--\\nRunning GA services have now been updated.\\n-->\\n",
    "Example output:\\n<!--\\n## Actual status:\\n"
    "Running GA services have now been updated.\\n-->\\n",
    "<!--\\n\x60\x60\x60text\\n## Actual status:\\n"
    "Running GA services have now been updated.\\n\x60\x60\x60\\n-->\\n",
])
def test_hidden_or_fenced_status_cannot_supersede_original_site_constraint(inert):
    # Inert means rendered/example text is not a direct operational report.
    ctx = tsumgi()
    ctx["sources"].append(event(2, "pull_request_comment", body=inert))
    report = quality.inspect_scope_quality(ctx, [
        sentence("Bonsai Nixバックエンドの実装を追加しました。")
    ])
    assert report["source_claims"]["no_site_promotion"] is True
    assert "original_no_deploy_condition_omitted" in report["known_rule_violations"]
    view = feature.build_feature_first_view(ctx, d1._original_plan([ctx], CAP))
    assert "change to running GA services" in (
        view.report()["latest_operational_caveat_evidence"][0]["verbatim"])


def test_truncated_original_source_requires_review():
    ctx = tsumgi()
    ctx["sources"][0]["original_event"]["body"]["truncated"] = True
    report = quality.inspect_scope_quality(ctx, [
        sentence("Bonsaiバックエンドは実装されました。")
    ])
    assert report["source_claims"]["source_scan_incomplete"] is True
    assert report["status"] == "needs_human_review"


def test_c4_title_is_advisory_when_created_event_outside_target_day():
    # Daily C4 can preserve a selected PR title while suppressing the
    # historical create-event's original title for day-scope reasons.
    ctx = suzka()
    ctx["sources"][0]["original_event"]["kind"] = "pull_request_comment"
    ctx["sources"][0]["original_event"]["title"] = None
    report = quality.inspect_scope_quality(ctx, [
        sentence("テスト専用の事前検証でAgentState v10の実装が完了しました。")
    ])
    assert report["source_claims"]["test_only"] is True
    assert "test_only_pr_claimed_product_feature_complete" in report["known_rule_violations"]
    assert report["model_or_grounding_is_semantic_authority"] is False


def test_c4_selected_title_not_a_source_of_implementation_truth():
    ctx = suzka()
    ctx["sources"][0]["original_event"]["kind"] = "pull_request_comment"
    ctx["sources"][0]["original_event"]["title"] = None
    # Test title by itself cannot assert test-only source evidence.
    ctx["sources"][0]["original_event"]["body"]["text"] = "Planning next feature."
    report = quality.inspect_scope_quality(ctx, [
        sentence("次の作業を計画しました。")
    ])
    assert report["source_claims"]["test_only"] is False
    assert report["semantic_completeness_certified"] is False


def test_long_single_original_source_line_never_silently_passes_scope_audit():
    ctx = tsumgi()
    original = ctx["sources"][0]["original_event"]["body"]["text"]
    ctx["sources"][0]["original_event"]["body"]["text"] = original.replace(
        "No merge, new release, or change to running GA services without explicit approval.",
        "No merge, new release, or change to running GA services without explicit approval."
        + " " * 530,
    )
    assert len(ctx["sources"][0]["original_event"]["body"]["text"].splitlines()[-1]) > (
        feature.MAX_SOURCE_LINE_CHARS)
    report = quality.inspect_scope_quality(ctx, [
        sentence("Bonsai 2 PQ2_0バックエンドを実装しました。")
    ])
    assert report["source_claims"]["source_scan_incomplete"] is True
    assert report["status"] == "needs_human_review"
    assert "original_source_scan_incomplete" in report["known_rule_violations"]
    assert report["publication_authorized"] is False
    view = feature.build_feature_first_view(ctx, d1._original_plan([ctx], CAP))
    # A bounded view must not counterfeit a truncated "proof" from a long
    # line. The audit marks the original not fully visible instead.
    assert not view.report()["latest_operational_caveat_evidence"]
    assert view.report()["source_deleted"] is False


def test_malformed_selected_title_fails_scope_audit():
    ctx = suzka()
    ctx["title"] = 42
    with pytest.raises(quality.ScopeQualityError, match="title"):
        quality.inspect_scope_quality(ctx, [sentence("事前検証を行いました。")])


@pytest.mark.parametrize("heading", [
    "Example output", "Sample output", "Model reply",
    "Expected output", "Generated output", "Test output",
])
def test_colonless_example_heading_does_not_supersede_site_gate(heading):
    ctx = tsumgi()
    ctx["sources"].append(event(
        2, "pull_request_comment",
        body=f"## {heading}\nRunning GA services have now been updated.\n",
    ))
    view = feature.build_feature_first_view(ctx, d1._original_plan([ctx], CAP))
    assert "change to running GA services" in (
        view.report()["latest_operational_caveat_evidence"][0]["verbatim"])
    scope = quality.inspect_scope_quality(ctx, [
        sentence("Bonsaiのオプトイン実装を追加しました。")
    ])
    assert "original_no_deploy_condition_omitted" in scope["known_rule_violations"]
    assert scope["source_claims"]["site_transition_reported"] is False


def test_explicit_real_status_after_colonless_example_can_still_report_change():
    ctx = tsumgi()
    ctx["sources"].append(event(
        2, "pull_request_comment",
        body=("## Example output\n"
              "Running GA services have now been updated.\n\n"
              "## Actual status:\n"
              "Running GA services have now been updated.\n"),
    ))
    view = feature.build_feature_first_view(ctx, d1._original_plan([ctx], CAP))
    assert not view.report()["latest_operational_caveat_evidence"]
    scope = quality.inspect_scope_quality(ctx, [
        sentence("本番更新の状況は元コメントで報告されています。")
    ])
    assert scope["source_claims"]["site_transition_reported"] is True
    assert "later_source_site_transition_requires_human_review" in scope["known_rule_violations"]


def test_test_only_japanese_source_scope_and_ambiguous_pr_completion():
    ctx = suzka()
    ctx["sources"][0]["original_event"]["body"]["text"] = (
        "テストのみ。製品コードの変更なし。\n"
        "Product v10 StateWAL integration is a separate gate.\n")
    result = quality.inspect_scope_quality(ctx, [
        sentence("このPRの実装が完了しました。テスト専用の検証です。")
    ])
    assert result["source_claims"]["test_only"] is True
    assert "test_only_pr_completion_scope_ambiguous" in result["known_rule_violations"]
    assert result["status"] == "needs_human_review"


def test_ga_site_remains_unchanged_pending_separate_approval_is_not_lost():
    ctx = tsumgi()
    body = ctx["sources"][0]["original_event"]["body"]
    body["text"] = body["text"].replace(
        "No merge, new release, or change to running GA services without explicit approval.",
        "The opt-in backend is implemented; running GA services remain unchanged "
        "pending separate approval.",
    )
    view = feature.build_feature_first_view(ctx, d1._original_plan([ctx], CAP))
    assert "running GA services remain unchanged" in (
        view.report()["latest_operational_caveat_evidence"][0]["verbatim"])
    report = quality.inspect_scope_quality(ctx, [
        sentence("Bonsai Nixバックエンドを実装しました。")
    ])
    assert report["source_claims"]["no_site_promotion"] is True
    assert "original_no_deploy_condition_omitted" in report["known_rule_violations"]


@pytest.mark.parametrize("label", ["Example output (mock)", "Model reply (synthetic)",
                                    "Sample output (fictional)"])
def test_suffixed_example_heading_cannot_fake_later_site_promotion(label):
    ctx = tsumgi()
    ctx["sources"].append(event(2, "pull_request_comment",
                                 body=f"## {label}\n"
                                      "Running GA services have now been updated.\n"))
    view = feature.build_feature_first_view(ctx, d1._original_plan([ctx], CAP))
    assert "change to running GA services" in (
        view.report()["latest_operational_caveat_evidence"][0]["verbatim"])
    report = quality.inspect_scope_quality(ctx, [
        sentence("新しいNixバックエンドを実装しました。")
    ])
    assert report["source_claims"]["site_transition_reported"] is False
    assert "original_no_deploy_condition_omitted" in report["known_rule_violations"]


def test_explicitly_deployed_japanese_sentence_not_accepted_as_no_deploy():
    report = quality.inspect_scope_quality(tsumgi(), [
        sentence("Bonsai 2 PQ2_0はデプロイ済みで本番稼働中です。")
    ])
    assert "original_no_deploy_condition_omitted" in report["known_rule_violations"]
    assert "original_no_deploy_condition_contradicted" in report["known_rule_violations"]


def test_baseline_undeployed_feature_wording_does_not_get_false_omission():
    report = quality.inspect_scope_quality(tsumgi(), [
        sentence("この実装はまだマージ、デプロイ、公開されていません。")
    ])
    assert "original_no_deploy_condition_omitted" not in report["known_rule_violations"]
    assert "original_no_deploy_condition_contradicted" not in report["known_rule_violations"]


def test_later_actual_merge_detects_stale_unmerged_summary_even_if_grounded():
    ctx=tsumgi()
    ctx["sources"].append(event(2,"pull_request_merged",
                                source_status="event_transition_only"))
    old=quality.inspect_scope_quality(ctx, [
        sentence("ランタイムに公式Bonsai 2のオプトインNixバックエンドを追加しました。"),
        sentence("この実装はDraft PR #155であり、まだマージ、デプロイ、公開されていません。"),
    ])
    assert old["source_claims"]["merged_in_period"] is True
    assert old["original_source_refs"]["merged"]==[2]
    assert "merged_pr_claimed_unmerged" in old["known_rule_violations"]
    assert "merged_pr_transition_omitted" in old["known_rule_violations"]
    assert old["publication_authorized"] is False
    assert old["semantic_completeness_certified"] is False


def test_merged_source_and_not_confirmed_live_qualifier_is_advisory_only():
    ctx=tsumgi()
    ctx["sources"].append(event(2,"pull_request_merged",
                                source_status="event_transition_only"))
    summary=[
        sentence("Bonsai 2のオプトイン実装を追加しました。",refs=(0,)),
        sentence("このPRはマージされましたが、本番反映の有無は未確認です。",refs=(2,)),
    ]
    result=quality.inspect_scope_quality(ctx,summary)
    assert "merged_pr_claimed_unmerged" not in result["known_rule_violations"]
    assert "merged_pr_transition_omitted" not in result["known_rule_violations"]
    assert result["publication_authorized"] is False
    assert result["semantic_completeness_certified"] is False


def test_unmerged_source_never_requires_fictitious_merge_event():
    ctx=tsumgi()
    result=quality.inspect_scope_quality(ctx, [
        sentence("Bonsai 2のオプトイン実装を追加しました。"),
        sentence("このDraft PRは未マージで、本番反映も未実施です。"),
    ])
    assert result["source_claims"]["merged_in_period"] is False
    assert "merged_pr_claimed_unmerged" not in result["known_rule_violations"]
    assert "merged_pr_transition_omitted" not in result["known_rule_violations"]


def test_real_test_commit_only_source_empty_summary_is_review_candidate():
    # SOURCE-only test commit on the target day. This does not prove test
    # success, a product feature, deployment, or today's push instant.
    ctx=example(
        "upiscium/PROJECT-SUZKA", 330,
        title="test(r15): preflight v10 narrative continuation and full-reserve capacity",
        body="TEST ONLY. No product change.",
    )
    ctx["sources"]=[
        event(0,"pull_request_commit",
              message="test(r15): preflight v10 narrative continuation and full-reserve capacity",
              source_status="content_addressed_commit_not_push_time"),
        event(1,"pull_request_created",source_status="event_transition_only"),
    ]
    risk=quality.inspect_scope_quality(ctx, [])
    assert risk["source_claims"]["test_only_commit_progress_candidate"] is True
    assert risk["original_source_refs"]["test_only_commit"]==[0]
    assert "test_only_commit_progress_unreported" in risk["known_rule_violations"]
    assert risk["publication_authorized"] is False
    assert risk["semantic_completeness_certified"] is False
    good=quality.inspect_scope_quality(ctx, [
        sentence("AgentState v10の継続性/容量に関する事前検証用テスト変更が記録されました。")
    ])
    assert "test_only_commit_progress_unreported" not in good["known_rule_violations"]
    assert good["publication_authorized"] is False
