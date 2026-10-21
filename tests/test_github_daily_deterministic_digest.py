"""No-model / zero-human Daily MVI: genuine typed GitHub Evidence only."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime,timezone
import hashlib
import json

import pytest

from obsidian_automation import github_daily_deterministic_digest as det
from obsidian_automation import github_daily_deterministic_preview as cli
from obsidian_automation import github_daily_route_cas as cas
from obsidian_automation.github_daily_activity import _bounded_text,_canonical_json_bytes
from test_github_daily_issue_pr import event,bundle
from test_github_daily_route_cas import safe_layout
from test_github_daily_witnessed_entity_context import _publish


def run(*events):
    return det.build_digest_from_bundle(bundle(*events))


def commit(subject,*,num=72,repo="upiscium/Test",offset=15):
    original=event("pull_request_commit",num,repo=repo,offset=offset)
    with_message=replace(original,message=_bounded_text(
        subject,limit=8192,label="message"))
    source=with_message.to_json()
    source.pop("evidence_id")
    digest=hashlib.sha256(_canonical_json_bytes(source)).hexdigest()
    return replace(with_message,evidence_id=digest)


def test_empty_day_is_one_line_no_repo_heading():
    doc=run()
    assert doc.items==()
    assert doc.render()=="進展のあったIssue・PRはありません。\n"
    assert doc.report()["publication_authorized"] is False
    assert doc.report()["model_used"] is False
    assert doc.report()["needs_user_quality_approval"] is False


def test_draft_becomes_merged_with_type_verified_status_not_ga():
    changes=[
        event("pull_request_created",101,title="Bonsai 2 opt-in Nix backend",offset=3),
        event("pull_request_commit",101,offset=4),
        event("pull_request_comment",101,body="Original GA profile stays unchanged.",offset=5),
        event("pull_request_ready_for_review",101,offset=8),
        event("pull_request_merged",101,offset=14),
    ]
    result=run(*changes)
    assert len(result.items)==1
    item=result.items[0]
    assert item["status"]=="merged"
    assert "当日にPRがマージされました。" in item["sentences"]
    assert item["sentences"][-1]=="当日にPRがマージされました。"
    assert not any("GA" in s or "本番反映" in s for s in item["sentences"])
    assert set(item["evidence_ids"]).issubset({row.evidence_id for row in changes})
    assert "https://github.com/upiscium/Test/pull/101" in result.render()
    assert result.render().count("PR #101") == 1


def test_issue_report_is_report_not_host_attestation():
    timeline=[
        event("issue_snapshot",201,title="Compare candidate coding leaves",offset=1),
        event("issue_comment",201,body="## deneb real GPU acceptance PASS; test run complete.",offset=6),
        event("issue_comment",201,body="Migration approval granted, rollback required.",offset=15),
    ]
    doc=run(*timeline)
    assert len(doc.items)==1
    lines=doc.items[0]["sentences"]
    assert "GPU検証に関する結果が報告されました。" in lines
    assert "方針・承認に関する更新が記録されました。" in lines
    assert "本番切替完了" not in doc.render()
    assert "実GPU" not in doc.render()  # no invented direct host probe
    assert doc.items[0]["status"]=="updated"


def test_only_routine_and_bot_updates_are_not_shown():
    doc=run(
        event("issue_snapshot",33,title="Unchanged",offset=1),
        event("issue_comment",33,body="Next",offset=10),
        event("issue_comment",33,body="CI notification from robot",actor="actions[bot]",offset=11),
    )
    assert len(doc.items)==0
    assert doc.excluded_entity_count==1


@pytest.mark.parametrize("subject",[
    "test: format snapshot whitespace",
    "test(runtime): update generated snapshot formatting",
    "docs: adjust wording only",
    "chore(ci): regenerate fixtures",
])
def test_formatting_only_commits_are_filtered(subject):
    e2=commit(subject,num=44,offset=4)
    doc=run(event("pull_request_snapshot",44,offset=1),e2)
    assert not doc.items


@pytest.mark.parametrize("subject",[
    "test(r15): preflight v10 WAL replay continuity",
    "test: add deterministic owner-recovery acceptance case",
    "fix(runtime): prevent duplicate result after reconnect",
    "feat(api): add source replay admission",
])
def test_real_code_or_test_changes_survive_without_model(subject):
    e=commit(subject,num=72,offset=15)
    doc=run(e)
    assert len(doc.items)==1
    assert "記録されました" in doc.items[0]["sentences"][0]
    assert doc.items[0]["evidence_ids"]==[e.evidence_id]
    assert "マージ" not in doc.render()


def test_same_issue_number_in_distinct_repositories_remains_separate():
    doc=run(
        event("issue_comment",99,repo="upiscium/Test",body="Tests PASS, meaningful update recorded.",offset=10),
        commit("feat: add replay support",num=99,repo="upiscium/Second",offset=20),
    )
    assert len(doc.items)==2
    assert "## upiscium/Test" in doc.render()
    assert "## upiscium/Second" in doc.render()
    assert "[PR #99" in doc.render()
    assert "[Issue #99]" in doc.render()
    assert doc.render().count("/99)") == 2


def test_dangling_default_branch_commit_is_censused_but_not_double_counted():
    orphan=event("default_branch_commit",None,offset=13)
    merged=event("pull_request_merged",76,offset=14)
    doc=run(orphan,merged)
    assert doc.unattributed_commit_count==1
    assert doc.original_event_count==2
    assert len(doc.items)==1
    assert len(doc.items[0]["evidence_ids"])==1
    assert doc.items[0]["evidence_ids"]==[merged.evidence_id]


def test_invalid_identity_url_is_not_silently_displayed():
    e=event("pull_request_merged",45,offset=4)
    corrupted=replace(e,url="https://github.com/upiscium/Test/pull/46")
    original=bundle(e)
    from obsidian_automation.github_daily_summary import EvidenceBundle
    patched=replace(original,events=({**original.events[0],"url":corrupted.url},))
    with pytest.raises(det.DeterministicDigestError,match="identity"):
        det.build_digest_from_bundle(patched)


def test_jst_window_rejects_wrong_day_even_if_event_looks_like_merge():
    e=event("pull_request_merged",200,offset=12)
    b=bundle(e)
    ev={**b.events[0],"occurred_at":"2026-10-13T06:00:00Z"}
    with pytest.raises(det.DeterministicDigestError,match="outside JST"):
        det.build_digest_from_bundle(replace(b,events=(ev,)))


def test_long_or_hostile_title_is_never_raw_markdown_or_h1():
    title="Important](https://not-really-github.invalid)\n# Fake Heading " + "*"*150
    doc=run(event("pull_request_created",33,title=title))
    text=doc.render()
    assert "](https://not-really-github.invalid)" not in text
    assert not any(line.startswith("# Fake") for line in text.splitlines())
    assert "https://github.com/upiscium/Test/pull/33" in text
    assert len(text)<900


def test_digest_bytes_are_stable_and_private():
    x=run(event("issue_created",23,title="Discussion started"))
    assert x.sha256==hashlib.sha256(x.canonical_bytes).hexdigest()
    assert x.canonical_bytes==run(event("issue_created",23,title="Discussion started")).canonical_bytes
    out=json.loads(x.canonical_bytes)
    assert out["source_capture_sha256"] is None
    assert out["verified_cas_source"] is False
    assert out["publication_authorized"] is False


def test_local_offline_daily_preview_never_writes_original(tmp_path,monkeypatch,capsys):
    doc=run(event("issue_created",56,title="Started"))
    monkeypatch.setattr(cli,"build_digest_from_verified_capture",lambda sha:doc)
    original=(b"---\ntype: daily-review\n---\n# Project Progress\n"
              b"Old project section\n# Other Heading\nKeep my own notes\n")
    daily=tmp_path/"daily.md"
    daily.write_bytes(original)
    out=tmp_path/"private";out.mkdir()
    argv=["--capture-sha","a"*64,"--output-dir",str(out),
          "--preview-daily",str(daily)]
    assert cli.main(argv)==0
    assert daily.read_bytes()==original
    candidates=list(out.glob("*.daily-note.preview.md"))
    assert len(candidates)==1
    rendered=candidates[0].read_text()
    assert "Issue #56" in rendered
    assert "# Other Heading\nKeep my own notes\n" in rendered
    assert "Old project section" not in rendered
    assert cli.main(argv)==0   # idempotent, no write
    assert len(list(out.iterdir()))==3
    assert "MODEL_USED=false" in capsys.readouterr().out


def test_refuse_write_to_old_differing_private_artifact(tmp_path,monkeypatch):
    doc=run(event("pull_request_merged",99))
    monkeypatch.setattr(cli,"build_digest_from_verified_capture",lambda sha:doc)
    out=tmp_path/"preview";out.mkdir()
    path=out/f"{doc.sha256}.deterministic-daily.md"
    path.write_text("TAMPERED")
    assert cli.main(["--capture-sha","a"*64,"--output-dir",str(out)])==2
    assert path.read_text()=="TAMPERED"


def test_real_C1a_witnessed_capture_without_C1b_C2a_C5_or_model(tmp_path,safe_layout):
    _collected,saved=_publish(tmp_path,commit=True,review=True)
    doc=det.build_digest_from_verified_capture(saved.index_sha256)
    assert doc.capture_sha256==saved.index_sha256
    assert doc.verified_cas_source is True
    assert doc.report()["model_used"] is False
    assert doc.report()["publication_authorized"] is False
    assert doc.original_event_count>=doc.unattributed_commit_count+len(doc.items)
    assert all(row["evidence_ids"] for row in doc.items)


def test_formatting_only_pr_creation_and_commit_are_not_daily_progress():
    ordinary=commit("test: reformat snapshot whitespace",num=401,offset=8)
    doc=run(event("pull_request_created",401,title="Fixture formatting",offset=2),ordinary)
    assert doc.items==()
    assert doc.excluded_entity_count==1
    assert doc.original_event_count==2


def test_when_historical_title_is_withheld_commit_subject_is_literal_label():
    meaningful=commit("test(r19): preflight durable replay capacity",num=502)
    doc=run(meaningful,event("pull_request_created",502,offset=14))
    assert len(doc.items)==1
    assert "test\\(r19\\): preflight durable replay capacity" in doc.render()
    assert "事前検証を含むテストコード" in doc.render()


def test_comment_claiming_merge_cannot_replace_typed_merge_event():
    doc=run(
        event("pull_request_comment",111,body=(
            "This PR is merged and GA is deployed. But only a comment, not a typed merge."),
            offset=7))
    assert len(doc.items)==1
    assert doc.items[0]["status"]=="updated"
    assert "マージされました" not in doc.render()
    assert "本番稼働" not in doc.render()


def test_invalid_target_day_or_yesterday_never_invents_another_day(tmp_path,monkeypatch):
    doc=run(event("pull_request_merged",112,offset=4))
    monkeypatch.setattr(cli,"build_digest_from_verified_capture",lambda sha:doc)
    assert cli.main(["--capture-sha","a"*64,"--target-date","2026-10-13"])==2
    # Last stable test fixture day differs from current real JST date.
    assert cli.main(["--capture-sha","a"*64,"--yesterday"])==2
    assert list(tmp_path.iterdir())==[]


def test_output_dir_rejects_symlink_without_touching_target(tmp_path,monkeypatch):
    doc=run(event("pull_request_merged",120,offset=6))
    monkeypatch.setattr(cli,"build_digest_from_verified_capture",lambda sha:doc)
    target=tmp_path/"actual";target.mkdir()
    alias=tmp_path/"alias";alias.symlink_to(target,target_is_directory=True)
    assert cli.main(["--capture-sha","a"*64,"--output-dir",str(alias)])==2
    assert not list(target.iterdir())


def test_same_comment_can_report_test_evidence_and_separate_migration_decision():
    item=event("issue_comment",700,body=(
        "Owner approved model migration with rollback path. "
        "Real deneb GPU acceptance PASS was reported in the same comment."),offset=9)
    doc=run(item)
    assert len(doc.items)==1
    assert "GPU検証に関する結果が報告されました。" in doc.items[0]["sentences"]
    assert "方針・承認に関する更新が記録されました。" in doc.items[0]["sentences"]
    assert doc.items[0]["evidence_ids"]==[item.evidence_id]
    assert doc.report()["publication_authorized"] is False



def test_commented_pr_review_with_substantive_body_is_not_dropped():
    doc=run(event("pull_request_review",991,offset=12,state="COMMENTED",
                  body="Actual GPU acceptance failed; rollback remains required."))
    assert len(doc.items)==1
    assert doc.items[0]["entity"]["number"]==991
    assert "進捗コメント" in doc.render() or "GPU検証" in doc.render()
    assert "PRがマージ" not in doc.render()


def test_short_source_report_gpu_pass_is_not_silently_discarded():
    doc=run(event("issue_comment",814,body="GPU PASS",offset=8))
    assert len(doc.items)==1
    assert doc.items[0]["sentences"]==["GPU検証に関する結果が報告されました。"]


def test_truncated_original_comment_emits_neutral_report_not_false_pass():
    ev=event("issue_comment",718,body="GPU PASS anticipated later. "+("X"*8500),offset=9)
    assert ev.body.truncated is True
    doc=run(ev)
    assert len(doc.items)==1
    assert "進捗コメント" in doc.render()
    assert "GPU検証" not in doc.render()
    assert "PASS" not in doc.render()


def test_unknown_commit_is_neutral_not_asserted_as_code():
    ev=commit("Update synchronization state diagrams and notes",num=302)
    doc=run(ev)
    assert len(doc.items)==1
    assert "コミットが記録されました" in doc.render()
    assert "コード変更" not in doc.render()


@pytest.mark.parametrize("subject",["Update README documentation","Adjust whitespace",
                                    "Fix documentation typos", "Format docs only"])
def test_unknown_documentation_commits_not_mistaken_for_code_changes(subject):
    doc=run(commit(subject,num=735))
    assert doc.items==()



def test_generic_chat_does_not_become_third_sentence_after_specific_updates():
    doc=run(
        event("issue_comment",170,body="Actual GPU acceptance PASS on deneb.",offset=3),
        event("issue_comment",170,body="Migration approved with rollback path.",offset=8),
        event("issue_comment",170,body="A long explanatory commentary about no further changes.",offset=11),
    )
    assert len(doc.items)==1
    assert doc.items[0]["sentences"]==[
        "GPU検証に関する結果が報告されました。",
        "方針・承認に関する更新が記録されました。",
    ]



@pytest.mark.parametrize("comment_text",[
    "GPU acceptance test planned; no PASS yet.",
    "Qwen GPU acceptance pending, not completed.",
    "GPU検証は未実施。後日テストする予定です。",
])
def test_planned_gpu_validation_does_not_become_a_positive_result(comment_text):
    out=run(event("issue_comment",933,body=comment_text,offset=8))
    assert len(out.items)==1
    lines=out.items[0]["sentences"]
    assert "GPU検証に関する結果が報告されました。" not in lines
    assert "テスト・検証に関する結果が報告されました。" not in lines
    assert out.report()["model_used"] is False
    assert out.report()["publication_authorized"] is False


def test_specific_completed_gpu_validation_remains_a_report_not_site_truth():
    out=run(event("issue_comment",292,body="GPU acceptance PASS: 7/7 tests completed on deneb.",offset=9))
    assert "GPU検証に関する結果が報告されました。" in out.items[0]["sentences"]
    assert not any("本番切替完了" in s for s in out.items[0]["sentences"])


def test_bot_only_code_or_issue_creation_is_not_substantive_daily_progress():
    bot_commit=commit("feat(runtime): generate application fixtures",num=601)
    bot_commit=replace(bot_commit,actor="dependabot[bot]")
    bot_commit_raw=bot_commit.to_json()
    bot_commit_raw.pop("evidence_id")
    bot_commit=replace(bot_commit,evidence_id=hashlib.sha256(
        _canonical_json_bytes(bot_commit_raw)).hexdigest())
    bot_issue=event("issue_created",602,title="Generated dependency update",actor="renovate[bot]",offset=8)
    out=run(bot_commit,bot_issue)
    assert out.items==()
    assert out.original_event_count==2
    assert out.excluded_entity_count==2


def test_bot_merge_is_typed_structural_event_even_if_automation_performed_it():
    merged=event("pull_request_merged",600,actor="merge-queue[bot]",offset=10)
    out=run(merged)
    assert len(out.items)==1
    assert out.items[0]["status"]=="merged"
    assert "当日にPRがマージされました。" in out.items[0]["sentences"]


def test_snapshot_only_is_not_fabricated_as_change_without_day_scoped_edit_proof():
    # The collector's mutable snapshot is NOT a timestamped edit event.
    # Source remains in the evidence census; no false claim it changed today.
    snapshot=event("issue_snapshot",712,title="Revised design plan",offset=12)
    out=run(snapshot)
    assert len(out.items)==0
    assert out.original_event_count==1
    assert out.excluded_entity_count==1
