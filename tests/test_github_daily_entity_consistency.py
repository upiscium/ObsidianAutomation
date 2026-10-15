"""Opt-in per-entity contradiction evidence checks; never a factuality certificate."""
import copy
import pytest

from obsidian_automation.github_daily_entity_consistency import (
    EntityConsistencyError, audit_entity_consistency,
)
from obsidian_automation.github_daily_source_role_audit import _canonical, _sha

ENTITY = {"repository": "upiscium/ObsidianAutomation",
          "entity_type": "pull_request", "number": 354}
TARGET = ("upiscium/ObsidianAutomation", "pull_request", 354)
OLD = "修正は隔離されており、現在、重点・全体回帰テスト中です。"
NEW = "修正とテストが完了。34/34重点、1826/1826全体PASS。"
SUMMARY = "修正は現在テスト中であり、PRはDraftです。"
EID1, EID2 = "1" * 64, "2" * 64


def fixture(temporal=True, *, sentences=None):
    if sentences is None:
        sentences = [{"text": SUMMARY, "verdict": "supported",
                      "evidence_ids": [EID2]}]
    p = {"publication_authorized": False, "date": "2026-10-14",
         "source_context_set_sha256": "c" * 64, "capture_sha256": "b" * 64,
         "entities": [{"entity": ENTITY, "context_sha256": "d" * 64,
                       "sentences": sentences}]}
    fields = [
        {"source_ref": 1, "evidence_id": EID1, "field": "body", "text": NEW,
         "original_value": {"text": NEW, "included_bytes": len(NEW.encode()),
                            "original_bytes": len(NEW.encode()), "truncated": False},
         "text_sha256": _sha(NEW.encode()), "kind": "pull_request_created",
         "occurred_at": "2026-10-14T01:00:00Z"},
        {"source_ref": 2, "evidence_id": EID2, "field": "body", "text": OLD,
         "text_sha256": _sha(OLD.encode()), "kind": "pull_request_comment",
         "occurred_at": "2026-10-14T02:00:00Z"},
    ]
    sr = {"record_version": 1, "stage": "c5-literal-source-review-v1",
          "publication_authorized": False, "source_context_set_sha256": "c" * 64,
          "capture_sha256": "b" * 64, "preview_sha256": _sha(_canonical(p)),
          "entities": [{"entity": ENTITY, "context_sha256": "d" * 64,
                        "source_event_count": 3, "fields": fields}]}
    if temporal:
        observations = []
        for i, eid in enumerate(("0" * 64, EID1, EID2)):
            observations.append({
                "source_ref": i, "evidence_id": eid,
                "observation": {
                    "policy": "github-day-snapshot-v1",
                    "status": ("content_addressed_commit_not_push_time" if i == 0
                               else "day_snapshot_not_event_instant"),
                    "observed_at_utc": "2026-10-15T03:00:00Z",
                    "resource_updated_at": "2026-10-14T04:00:00Z",
                    "original_event_sha256": "a" * 64,
                    "withheld_fields": [],
                    "historical_instant_verified": False}})
        sr["entities"][0]["temporal_observations"] = observations
    return sr, p


def candidate(*, relation="possible_supersession"):
    return {
        "sentence_index": 0,
        "claim_start": SUMMARY.index("現在テスト中"),
        "claim_end": SUMMARY.index("現在テスト中") + len("現在テスト中"),
        "support": {"source_ref": 2, "field": "body",
                    "start": OLD.index("現在"), "end": OLD.index("現在") + len("現在、重点・全体回帰テスト中")},
        "counter": {"source_ref": 1, "field": "body",
                    "start": NEW.index("34/34"), "end": len(NEW)},
        "relation": relation,
    }


def check(source=None, preview=None, candidates=None, *, require_day_policy=True):
    if source is None or preview is None:
        source, preview = fixture()
    if candidates is None:
        candidates = [candidate()]
    return audit_entity_consistency(
        _canonical(source), _canonical(preview), TARGET,
        candidates, require_day_policy=require_day_policy).report()


def test_old_current_state_and_completed_tests_are_not_conflated():
    d = check()
    assert d["source_period_policy"] == "github-day-snapshot-v1"
    assert d["day_snapshot_provenance_present"] is True
    assert d["supported_sentence_count"] == 1
    assert d["potential_conflict_count"] == 1
    row = d["results"][0]
    assert row["consistency_state"] == "potential_cross_event_contradiction"
    assert row["advisory_relation"] == "possible_supersession"
    assert row["cited_support"]["evidence_id"] == EID2
    assert row["potential_counter"]["evidence_id"] == EID1
    assert row["historical_order_state"] == "historical_order_unverified"
    assert d["historical_instant_verified"] is False
    assert not d["publication_authorized"] and not d["factuality_verified"]
    assert OLD not in str(d) and NEW not in str(d) and SUMMARY not in str(d)


def test_contradiction_is_only_advisory_and_does_not_override_grounding():
    result = check(candidates=[candidate(relation="possible_contradiction")])
    assert result["results"][0]["model_grounding_verdict"] == "supported"
    assert result["results"][0]["advisory_relation"] == "possible_contradiction"
    assert not result["advisory_relations_independently_attested"]
    assert not result["all_atomic_claims_reviewed"]


def test_consistent_relation_not_a_false_conflict():
    d = check(candidates=[candidate(relation="consistent")])
    assert d["potential_conflict_count"] == 0
    assert d["results"][0]["consistency_state"] == "no_advisory_conflict_not_verified"
    assert d["factuality_verified"] is False


def test_no_annotations_is_unreviewed_not_no_progress():
    d = check(candidates=[])
    assert d["supported_sentence_count"] == 1
    assert d["annotated_claim_count"] == 0
    assert d["results"][0]["consistency_state"] == "unreviewed"
    assert not d["semantic_coverage_verified"]


def test_no_displayed_sentences_preserves_pending():
    source, preview = fixture(sentences=[])
    out = check(source, preview, candidates=[])
    assert out["supported_sentence_count"] == 0
    assert out["results"] == []
    assert not out["publication_authorized"]


def test_unresolved_sentence_is_not_a_displayed_fact():
    source, preview = fixture()
    preview["entities"][0]["sentences"][0]["verdict"] = "unresolved"
    source["preview_sha256"] = _sha(_canonical(preview))
    d = check(source, preview, candidates=[])
    assert not d["results"]
    with pytest.raises(EntityConsistencyError):
        check(source, preview, candidates=[candidate()])


def test_legacy_unverified_is_strictly_excluded_by_default():
    source, preview = fixture(temporal=False)
    with pytest.raises(EntityConsistencyError):
        check(source, preview)
    d = check(source, preview, require_day_policy=False)
    assert d["source_period_policy"] == "legacy_unverified"
    assert d["historical_instant_verified"] is False
    assert d["potential_conflict_count"] == 1


def test_quote_only_counter_does_not_become_truth():
    source, preview = fixture()
    source["entities"][0]["fields"][0]["text"] = '合成例: "テストPASS"'
    source["entities"][0]["fields"][0]["original_value"] = {"text": '合成例: "テストPASS"'}
    source["entities"][0]["fields"][0]["text_sha256"] = _sha('合成例: "テストPASS"'.encode())
    ann = candidate()
    ann["counter"]["start"] = 0
    ann["counter"]["end"] = len(source["entities"][0]["fields"][0]["text"])
    d = check(source, preview, [ann])
    assert d["potential_conflict_count"] == 1
    assert d["historical_instant_verified"] is False
    assert not d["factuality_verified"]


@pytest.mark.parametrize("mode", [
    "bad_target", "wrong_preview_digest", "foreign_context", "bad_snapshot_digest",
    "wrong_citation", "same_event", "bad_counter_event", "negative_index",
    "bool_index", "out_of_range", "extra_annotation_key", "unsupported_relation",
    "untrusted_period_false", "duplicated_notes", "missing_notes",
    "withheld_counter", "out_of_day_field", "wrong_note_eid",
    "bad_original_field_hash", "corrupt_original_text",
    "bool_claim_offset", "unclosed_source", "noncanonical_preview",
    "missing_target_date", "bad_day_update", "updated_after_observed",
    "out_of_day_status_for_same_day", "noncanonical_day", "naive_observed",
])
def test_malicious_and_mismatched_evidence_rejected(mode):
    source, preview = fixture()
    ann = candidate()
    if mode == "bad_target":
        with pytest.raises(EntityConsistencyError):
            audit_entity_consistency(_canonical(source), _canonical(preview),
                                     ("other/repo", "pull_request", 354), [ann])
        return
    if mode == "wrong_preview_digest":
        source["preview_sha256"] = "f" * 64
    if mode == "foreign_context":
        preview["entities"][0]["context_sha256"] = "e" * 64
        source["preview_sha256"] = _sha(_canonical(preview))
    if mode == "bad_snapshot_digest":
        source["source_context_set_sha256"] = "f" * 64
    if mode == "wrong_citation":
        preview["entities"][0]["sentences"][0]["evidence_ids"] = [EID1]
        source["preview_sha256"] = _sha(_canonical(preview))
    if mode == "same_event":
        ann["counter"] = copy.deepcopy(ann["support"])
    if mode == "bad_counter_event":
        ann["counter"]["source_ref"] = 3
    if mode == "negative_index":
        ann["sentence_index"] = -1
    if mode == "bool_index":
        ann["sentence_index"] = True
    if mode == "out_of_range":
        ann["claim_end"] = len(SUMMARY) + 1
    if mode == "extra_annotation_key":
        ann["publication_authorized"] = True
    if mode == "unsupported_relation":
        ann["relation"] = "definitive_proven_supersession"
    if mode == "untrusted_period_false":
        source["entities"][0]["temporal_observations"][1]["observation"]["historical_instant_verified"] = True
    if mode == "duplicated_notes":
        source["entities"][0]["temporal_observations"][1] = copy.deepcopy(
            source["entities"][0]["temporal_observations"][0])
    if mode == "missing_notes":
        source["entities"][0]["temporal_observations"].pop()
    if mode == "withheld_counter":
        source["entities"][0]["temporal_observations"][1]["observation"]["withheld_fields"] = ["body"]
    if mode == "out_of_day_field":
        source["entities"][0]["temporal_observations"][1]["observation"]["status"] = "withheld_outside_target_day"
    if mode == "wrong_note_eid":
        source["entities"][0]["temporal_observations"][2]["evidence_id"] = "a" * 64
    if mode == "bad_original_field_hash":
        source["entities"][0]["fields"][0]["text_sha256"] = "a" * 64
    if mode == "corrupt_original_text":
        source["entities"][0]["fields"][0]["original_value"]["text"] = "forged"
    if mode == "bool_claim_offset":
        ann["claim_start"] = False
    if mode == "unclosed_source":
        ann["support"]["start"] = len(OLD)
    if mode == "missing_target_date":
        preview.pop("date")
        source["preview_sha256"] = _sha(_canonical(preview))
    if mode == "noncanonical_day":
        preview["date"] = "2026-10-14T00:00:00"
        source["preview_sha256"] = _sha(_canonical(preview))
    if mode == "bad_day_update":
        source["entities"][0]["temporal_observations"][1]["observation"]["resource_updated_at"] = "2026-10-13T11:00:00Z"
    if mode == "updated_after_observed":
        source["entities"][0]["temporal_observations"][1]["observation"]["resource_updated_at"] = "2026-10-16T11:00:00Z"
    if mode == "out_of_day_status_for_same_day":
        source["entities"][0]["temporal_observations"][1]["observation"]["status"] = "withheld_outside_target_day"
    if mode == "naive_observed":
        source["entities"][0]["temporal_observations"][1]["observation"]["observed_at_utc"] = "2026-10-15T03:00:00"
    if mode == "noncanonical_preview":
        with pytest.raises(EntityConsistencyError):
            audit_entity_consistency(_canonical(source), _canonical(preview) + b" ", TARGET, [ann])
        return
    with pytest.raises(EntityConsistencyError):
        check(source, preview, [ann])
