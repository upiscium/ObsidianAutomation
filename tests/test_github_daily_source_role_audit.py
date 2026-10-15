"""Source-role audit is a bounded diagnostic, never a fact or Writer authority."""
import copy
import pytest
from obsidian_automation.github_daily_source_role_audit import (
    audit_individual_source_roles, SourceRoleAuditError, _canonical, _sha,
)

ENTITY = {"repository": "upiscium/ObsidianAutomation", "entity_type": "pull_request", "number": 355}
TARGET = ("upiscium/ObsidianAutomation", "pull_request", 355)
EID = "a" * 64
BODY = ("Markdown表示を実際に修正しました。\n\n合成試験の生成出力:\n"
        "> 検証処理の競合を修正し、回帰テストを追加しました。マージは未実施です。\n")
DONE = "Markdown表示を実際に修正しました。"
EXAMPLE = "検証処理の競合を修正し、回帰テストを追加しました。"
REAL_SUMMARY = "Markdown表示処理を修正しました。"
BAD_SUMMARY = "合成プローブで検証処理の競合修正と回帰テスト追加を実施しました。"


def fixtures():
    p = {"capture_sha256": "b" * 64, "source_context_set_sha256": "c" * 64,
         "publication_authorized": False, "entities": [{
             "context_sha256": "d" * 64, "entity": ENTITY, "sentences": [
                 {"verdict": "supported", "text": REAL_SUMMARY, "evidence_ids": [EID]},
                 {"verdict": "supported", "text": BAD_SUMMARY, "evidence_ids": [EID]},
             ]}]}
    sr = {"record_version": 1, "stage": "c5-literal-source-review-v1",
          "capture_sha256": p["capture_sha256"],
          "source_context_set_sha256": p["source_context_set_sha256"],
          "preview_sha256": _sha(_canonical(p)), "publication_authorized": False,
          "entities": [{"entity": ENTITY, "context_sha256": "d" * 64,
                        "fields": [{"evidence_id": EID, "source_ref": 1,
                                    "field": "body", "text": BODY,
                                    "text_sha256": _sha(BODY.encode())}]}]}
    return sr, p


def anchors(real=True):
    phrase = DONE if real else EXAMPLE
    index = BODY.index(phrase)
    return [{"source_ref": 1, "field": "body", "start": index,
             "end": index + len(phrase),
             "role": "direct_action" if real else "quoted_example"}]


def annotation(i, real=True):
    return {"sentence_index": i, "claim_kind": "performed_action",
            "assessment": "entailed", "anchors": anchors(real)}


def run(anns=()):
    sr, p = fixtures()
    return audit_individual_source_roles(
        _canonical(sr), _canonical(p), TARGET, anns).report()


def test_same_cited_event_cannot_launder_quoted_example_into_actual_work():
    d = run([annotation(0, True), annotation(1, False)])
    assert d["supported_sentence_count"] == 2
    assert d["scope_conflict_count"] == 1
    assert [r["source_scope_state"] for r in d["results"]] == [
        "no_scope_conflict_detected_not_verified", "quoted_example_only"]
    assert d["results"][1]["operator_assessment"] == "entailed"
    assert d["publication_authorized"] is False
    assert d["annotations_independently_attested"] is False
    assert d["factuality_verified"] is False
    assert BAD_SUMMARY not in str(d) and BODY not in str(d)


def test_partial_annotations_stay_unreviewed():
    d = run([annotation(0, True)])
    assert d["supported_sentence_count"] == 2 and d["annotated_sentence_count"] == 1
    assert d["results"][1]["source_scope_state"] == "unreviewed"
    assert d["requires_human_quality_review"]


def test_non_example_pending_scope_does_not_count_as_action():
    a = annotation(1, False)
    a["anchors"][0]["role"] = "proposed"
    d = run([a])
    assert d["results"][1]["source_scope_state"] == "performed_action_not_established_by_anchors"


@pytest.mark.parametrize("bad", [
    "wrong_field", "foreign_ref", "outside_range", "bool_start",
    "bool_ref", "duplicate_span", "extra_key",
])
def test_invalid_or_cross_reference_annotations_fail_closed(bad):
    a = annotation(1, False)
    e = a["anchors"][0]
    if bad == "wrong_field":
        e["field"] = "title"
    if bad == "foreign_ref":
        e["source_ref"] = 2
    if bad == "outside_range":
        e["end"] = len(BODY) + 1
    if bad == "bool_start":
        e["start"] = True
    if bad == "bool_ref":
        e["source_ref"] = False
    if bad == "duplicate_span":
        a["anchors"].append(copy.deepcopy(e))
    if bad == "extra_key":
        e["publication_authorized"] = True
    with pytest.raises(SourceRoleAuditError):
        run([a])


def test_rehashed_unbound_or_noncanonical_sources_are_rejected():
    sr, p = fixtures()
    sr["preview_sha256"] = "9" * 64
    with pytest.raises(SourceRoleAuditError):
        audit_individual_source_roles(_canonical(sr), _canonical(p), TARGET)
    sr, p = fixtures()
    with pytest.raises(SourceRoleAuditError):
        audit_individual_source_roles(_canonical(sr) + b" ", _canonical(p), TARGET)


def test_annotation_cannot_borrow_another_event():
    sr, p = fixtures()
    p["entities"][0]["sentences"][1]["evidence_ids"] = ["e" * 64]
    sr["preview_sha256"] = _sha(_canonical(p))
    with pytest.raises(SourceRoleAuditError):
        audit_individual_source_roles(
            _canonical(sr), _canonical(p), TARGET, [annotation(1, False)])


def test_bool_and_missing_entity_disallowed():
    sr, p = fixtures()
    with pytest.raises(SourceRoleAuditError):
        audit_individual_source_roles(_canonical(sr), _canonical(p),
                                      (ENTITY["repository"], ENTITY["entity_type"], True))
    with pytest.raises(SourceRoleAuditError):
        audit_individual_source_roles(_canonical(sr), _canonical(p),
                                      (ENTITY["repository"], ENTITY["entity_type"], 354))


def test_claim_is_not_certified_even_when_annotation_says_entailed():
    d = run([annotation(0, True), annotation(1, False)])
    assert d["scope_conflict_count"] == 1
    assert not d["factuality_verified"] and not d["semantic_coverage_verified"]
    assert not d["publication_authorized"]


def test_no_annotations_is_not_no_progress():
    d = run()
    assert d["supported_sentence_count"] == 2
    assert d["annotated_sentence_count"] == 0
    assert all(r["source_scope_state"] == "unreviewed" for r in d["results"])
    assert not d["publication_authorized"]
