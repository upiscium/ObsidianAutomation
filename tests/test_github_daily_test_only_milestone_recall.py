"""Generic test-only commit milestone recall without fabricated product rollout.

This validates the original-source citation contract and prompt; semantic
performance still requires an independently reviewed live-model holdout.
"""
from copy import deepcopy
import json

import pytest

from obsidian_automation import github_daily_narrative_runtime as runtime
from obsidian_automation import github_daily_c5_source_view as source_view
from test_github_daily_c5_source_view import event


@pytest.mark.parametrize("repository,pr_number,commit_subject", [
    ("sample/LUMEN",16,"test(core): preflight lease replay capacity"),
    ("sample/MERIDIAN",205,"test(api): verify source-continuity metadata"),
    ("other/KITE",901,"test(scheduler): simulate interrupted job recovery"),
    ("other/POLARIS",5872,"test(v10): verify full-reserve WAL limits"),
    ("other/NOVA",124,"test: stage owner state handoff"),
])
def test_test_only_source_commit_is_displayed_and_citable(
    repository: str, pr_number: int, commit_subject: str,
) -> None:
    ctx={
        "stage":"github_daily_narrative_context_v4",
        "entity":{"repository":repository,"entity_type":"pull_request","number":pr_number},
        "date":"2026-10-16",
        "publication_authorized":False,
        "sources":[
            event(0,"pull_request_commit",message=commit_subject,
                  source_status="content_addressed_commit_not_push_time"),
            event(1,"pull_request_created",
                  source_status="withheld_outside_target_day",
                  withheld=("body","title","draft","state")),
        ],
    }
    original=deepcopy(ctx["sources"])
    view=source_view.build_recent_c5_source_view(ctx)
    assert 0 in view.shown_source_refs
    assert view.report()["publication_authorized"] is False
    assert view.report()["semantic_coverage_verified"] is False
    sentence={"text":"事前検証用のテスト変更が元コミットに記録されています。",
              "source_refs":[0]}
    admitted=json.loads(runtime._admit_editorial(
        ctx,runtime._json({"sentences":[sentence]}),
        runtime.RECENT_SOURCE_CONTRACT,
    ))
    assert admitted["sentences"][0]["source_refs"]==[0]
    assert 1 in admitted["pending_source_refs"]
    assert ctx["sources"]==original


def test_test_only_commit_remains_pending_if_model_abstains():
    ctx={
        "stage":"github_daily_narrative_context_v4",
        "entity":{"repository":"other/NEBULA","entity_type":"pull_request","number":441},
        "date":"2026-10-16",
        "publication_authorized":False,
        "sources":[
            event(0,"pull_request_commit",
                  message="test(runtime): preflight worker pause and resume",
                  source_status="content_addressed_commit_not_push_time"),
        ],
    }
    received=json.loads(runtime._admit_editorial(
        ctx,runtime._json({"sentences":[]}),
        runtime.RECENT_SOURCE_CONTRACT,
    ))
    assert received["sentences"]==[]
    assert received["pending_source_refs"]==[0]
    assert ctx["sources"][0]["original_event"]["message"]["text"].startswith("test(")


def test_generic_prompt_recall_and_temporal_qualifiers_are_versioned():
    assert runtime.RECENT_SOURCE_PROMPT_VERSION=="c5-recent-source-milestone-ja-v4"
    prompt=runtime.RECENT_SOURCE_PROMPT
    assert "pull_request_commit" in prompt
    assert "test(...)" in prompt
    assert "具体的な検証対象" in prompt
    assert "単なる test(...)" in prompt
    assert "既存fixture・snapshotの空白調整" in prompt
    assert "新しい検証ケース" in prompt
    assert "具体性が不足する場合も空配列が適切" in prompt
    assert "製品機能の完成" in prompt
    assert "source_ref" in prompt
    assert "当日" in prompt and "push" in prompt
    assert "本番反映" in prompt
    assert "重要な元イベントも" in prompt
    assert runtime.RECENT_SOURCE_CONTRACT=="c4-sentences-recent-source-v3"



@pytest.mark.parametrize("repo,number,message,description", [
    ("synthetic/ALTAIR",31,"test: format snapshot fixture whitespace",
     "Only fixture whitespace was reformatted; there are no new tests or behavior."),
    ("synthetic/VEGA",807,"test(runtime): generated sample updates",
     "Generated snapshots were regenerated without expanding verification coverage."),
    ("synthetic/SIRIUS",5001,"test: maintenance","No concrete new validation."),
])
def test_routine_test_only_changes_can_abstain_with_original_events_pending(
    repo: str, number: int, message: str, description: str,
) -> None:
    # Pure source-contract control: no fabricated publication candidate on
    # abstention, and the original GitHub Evidence is never discarded.
    ctx = {
        "stage":"github_daily_narrative_context_v4",
        "entity":{"repository":repo,"entity_type":"pull_request","number":number},
        "date":"2026-10-18","publication_authorized":False,
        "sources":[
            event(0,"pull_request_commit",message=message,
                  source_status="content_addressed_commit_not_push_time"),
            event(1,"pull_request_comment",body=description),
        ],
    }
    saved=deepcopy(ctx["sources"])
    shown=source_view.build_recent_c5_source_view(ctx)
    assert shown.shown_source_refs == (0,1)
    raw=runtime._json({"sentences":[]})
    admitted=json.loads(runtime._admit_editorial(
        ctx,raw,runtime.RECENT_SOURCE_CONTRACT,
    ))
    assert admitted["sentences"]==[]
    assert admitted["pending_source_refs"]==[0,1]
    assert ctx["publication_authorized"] is False
    assert ctx["sources"]==saved
