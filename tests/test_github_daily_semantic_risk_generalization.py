"""Source-only, unrelated IDs/repos: avoid overfitting Daily semantic guards.

These guards ONLY request review of suspicious cited model sentences.
They cannot rewrite generated Japanese, change original CAS, or promote Daily.
"""
from __future__ import annotations

from copy import deepcopy
import pytest

from obsidian_automation import github_daily_scope_quality as quality
from test_github_daily_c5_source_view import event
from test_github_daily_d1_priority_shadow import CAP


def ctx(repo,number,kind,source):
    return {
        "stage":"github_daily_narrative_context_v4",
        "capture_sha256":CAP,
        "entity":{"repository":repo,"entity_type":kind,"number":number},
        "date":"2026-10-18",
        "publication_authorized":False,
        "sources":[event(0, "pull_request_created" if kind=="pull_request"
                         else "issue_comment", body=source)],
    }


def sentence(value,refs=(0,)):
    return {"text":value,"source_refs":list(refs)}


@pytest.mark.parametrize("repo,number,source",[
    ("sample/ASTRA",71,"Pin upstream Qwen CUDA backend in a separate Nix runtime profile. Default profile unchanged."),
    ("sample/LUMEN",890,"The managed Installer profile is isolated from existing runtime; CUDA selected by immutable Nix profile."),
    ("other/ORBIT",4066,"新增 Nix runtime profile (opt-in); no GPU benchmarking performed."),
])
def test_runtime_profile_is_not_proof_of_performance_profiling(repo,number,source):
    base=ctx(repo,number,"pull_request",source)
    orig=deepcopy(base["sources"])
    risk=quality.inspect_scope_quality(base,[
        sentence("公式CUDAバックエンドを固定し、プロファイリングを行いました。")
    ])
    assert "runtime_profile_misread_as_performance_profiling" in risk["known_rule_violations"]
    assert risk["original_source_refs"]["runtime_profile_semantic_risk"]==[0]
    assert risk["semantic_completeness_certified"] is False
    assert risk["publication_authorized"] is False
    assert base["sources"]==orig


@pytest.mark.parametrize("source,generated",[
    ("Nix runtime profile added, but performance profiling was not run.",
     "Nixランタイムのプロファイルを追加しました。"),
    ("Nix runtime profile added; separate performance profiling run completed with GPU trace.",
     "実GPUでプロファイリングを行いました。"),
    ("Nix runtime profile added, profiling not performed.",
     "プロファイリングは未実施です。"),
])
def test_profile_terms_do_not_create_false_high_confidence_claims(source,generated):
    result=quality.inspect_scope_quality(
        ctx("sample/COMET",191,"pull_request",source),
        [sentence(generated)])
    assert "runtime_profile_misread_as_performance_profiling" not in result["known_rule_violations"]


@pytest.mark.parametrize("repo,number",[
    ("sample/ALTAIR",201),
    ("example/SIRIUS",899),
    ("other/PROCYON",21100),
])
def test_approval_scope_is_not_merely_basis_for_approval(repo,number):
    source=("Independent review recheck. Exact-head CI pending, PR DRAFT. "
            "User authorized merge conditional on independent review, "
            "not installed production rollout or merging entire stacked chain.")
    context=ctx(repo,number,"pull_request",source)
    bad=quality.inspect_scope_quality(context,[
        sentence("独立レビューに基づくマージ承認であり、本番ロールアウトや全スタックのマージに基づくものではない。")
    ])
    assert "approval_scope_misread_as_approval_basis" in bad["known_rule_violations"]
    assert bad["original_source_refs"]["approval_scope_semantic_risk"]==[0]
    assert bad["semantic_completeness_certified"] is False
    good=quality.inspect_scope_quality(context,[
        sentence("独立レビューを条件にマージのみ承認され、本番展開とstack全体のマージは許可されていません。")
    ])
    assert "approval_scope_misread_as_approval_basis" not in good["known_rule_violations"]


def test_approval_basis_only_is_not_inferred_without_explicit_source_limit():
    original="User authorized a production rollout after tests. Actual GA site unchanged."
    result=quality.inspect_scope_quality(
        ctx("sample/NOVA",515,"pull_request",original),
        [sentence("そのマージ承認は本番ロールアウトに基づくものではありません。")])
    assert "approval_scope_misread_as_approval_basis" not in result["known_rule_violations"]


def test_cited_source_ref_controls_the_high_impact_review_warning():
    original=ctx("other/HYDRA",89,"pull_request","Nix runtime profile added.")
    original["sources"].append(event(1,"pull_request_comment",
                                     body="Measured performance profiling using NVIDIA Nsight on GPU."))
    wrong=quality.inspect_scope_quality(original,[sentence("プロファイリングを行いました。",(0,))])
    right=quality.inspect_scope_quality(original,[sentence("プロファイリングを行いました。",(1,))])
    assert "runtime_profile_misread_as_performance_profiling" in wrong["known_rule_violations"]
    assert "runtime_profile_misread_as_performance_profiling" not in right["known_rule_violations"]
    assert original["publication_authorized"] is False


def test_issue_has_no_fictitious_merge_when_technical_claim_is_risky():
    report=quality.inspect_scope_quality(
        ctx("example/MIRA",44,"issue","Pinned a separate Nix CUDA runtime profile."),
        [sentence("性能のプロファイリングを実施しました。")])
    assert report["source_claims"]["merged_in_period"] is False
    assert "merged_pr_transition_omitted" not in report["known_rule_violations"]
    assert "runtime_profile_misread_as_performance_profiling" in report["known_rule_violations"]
