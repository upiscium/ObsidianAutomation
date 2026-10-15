"""C6 private per-entity cross-event challenge. No production model/Writer use."""
from __future__ import annotations

import copy
import json
import pytest

from obsidian_automation.github_daily_entity_consistency import (
    _json, _sha, EntityConsistencyError, build_entity_consistency_packet,
    admit_entity_consistency_reply,
)

TARGET = ("upiscium/ObsidianAutomation", "pull_request", 354)
EIDS = [f"{i+1:064x}" for i in range(4)]
STAMPS = [
    "2026-10-13T15:19:30Z", "2026-10-13T15:21:55Z",
    "2026-10-13T15:28:46Z", "2026-10-13T15:29:43Z",
]
OLD = "現在、重点テストと全体テストを実行中です。"
NEW = "重点34/34 PASS、全体1,826/1,826 PASS。CIもSUCCESSです。"
POLICY = "github-day-snapshot-v1"


def packets(strict=True):
    entity = {"repository": TARGET[0], "entity_type": TARGET[1], "number": TARGET[2]}
    preview = {"record_version": 1, "stage": "github_daily_narrative_private_preview_v4",
        "publication_authorized": False, "date": "2026-10-14",
        "capture_sha256": "a" * 64, "source_context_set_sha256": "b" * 64,
        "entities": [{"entity": entity, "context_sha256": "c" * 64,
            "sentences": [
                {"text": "変更は現在重点・全体テスト中です。", "verdict": "supported",
                 "evidence_ids": [EIDS[2]]},
                {"text": "時刻の順序をパースで修正しました。", "verdict": "supported",
                 "evidence_ids": [EIDS[3]]},
            ]}]}
    fields = [
        (0, "message", "feat: source-bound private C3 output", "pull_request_commit"),
        (1, "body", NEW, "pull_request_created"),
        (1, "title", "Source-bound preview draft", "pull_request_created"),
        (2, "body", OLD, "pull_request_comment"),
        (3, "message", "fix: compare parsed times", "pull_request_commit"),
    ]
    sr = {"record_version": 1, "stage": "c5-literal-source-review-v1",
        "capture_sha256": preview["capture_sha256"],
        "source_context_set_sha256": preview["source_context_set_sha256"],
        "preview_sha256": _sha(_json(preview)), "publication_authorized": False,
        "entities": [{"entity": entity, "context_sha256": "c" * 64,
                      "source_event_count": 4, "fields": [
                          {"source_ref": ref, "evidence_id": EIDS[ref], "field": name,
                           "kind": kind, "occurred_at": STAMPS[ref],
                           "text": text, "text_sha256": _sha(text.encode("utf-8"))}
                          for ref, name, text, kind in fields]}]}
    if strict:
        observations = []
        for ref, eid in enumerate(EIDS):
            status = ("day_snapshot_not_event_instant" if ref in (1, 2)
                      else "content_addressed_commit_not_push_time")
            observations.append({"source_ref": ref, "evidence_id": eid,
                "observation": {"policy": POLICY, "status": status,
                    "observed_at_utc": "2026-10-14T15:05:00Z",
                    "resource_updated_at": (
                        "2026-10-14T04:00:00Z" if ref == 1 else
                        STAMPS[ref] if ref == 2 else None),
                    "original_event_sha256": f"{ref+51:064x}",
                    "withheld_fields": [], "historical_instant_verified": False}})
        sr["entities"][0]["temporal_observations"] = observations
    return sr, preview


def build(sr=None, p=None, legacy=False):
    if sr is None or p is None:
        sr, p = packets(not legacy)
    return build_entity_consistency_packet(
        _json(sr), _json(p), TARGET, legacy_offline_diagnostic=legacy)


def reply(packet):
    return {"packet_sha256": packet.sha256, "checks": [
        {"sentence_index": 0, "status": "potential_conflict",
         "challenger_refs": [1], "reason": "full regression is finished in another event"},
        {"sentence_index": 1, "status": "no_conflict_observed",
         "challenger_refs": [], "reason": "no other recorded source contradicts it"},
    ]}


def test_same_entity_cited_only_grounding_does_not_hide_uncited_final_result():
    packet = build()
    p = packet.report()
    assert p["temporal_source_mode"] == POLICY
    assert p["historical_instant_verified"] is False
    assert len(p["sources"]) == 5
    assert p["sentences"][0]["cited_source_refs"] == [2]
    assert p["sentences"][1]["cited_source_refs"] == [3]
    assert any(x["source_ref"] == 1 and NEW in x["text"] for x in p["sources"])
    assert any(x["source_ref"] == 2 and OLD in x["text"] for x in p["sources"])
    advisory = admit_entity_consistency_reply(packet.canonical_bytes, _json(reply(packet)))
    report = advisory.report()
    assert report["checks"][0]["model_consistency_status"] == "potential_conflict"
    assert report["checks"][0]["challenger_refs"] == [1]
    assert report["publication_authorized"] is False
    assert report["factuality_verified"] is False
    assert report["historical_instant_verified"] is False
    assert report["human_quality_review_completed"] is False
    assert "重点34" not in str(report) and "underway" not in str(report)
    assert "full regression" not in str(report)


def test_legacy_sources_are_explicitly_not_temporally_accepted():
    sr, p = packets(False)
    with pytest.raises(EntityConsistencyError, match="period-safe"):
        build(sr, p)
    packet = build(sr, p, legacy=True)
    assert packet.report()["temporal_source_mode"] == "legacy_unverified_offline_diagnostic"
    a = admit_entity_consistency_reply(packet.canonical_bytes, _json(reply(packet)))
    assert a.report()["historical_instant_verified"] is False
    assert a.report()["publication_authorized"] is False


def test_partial_temporal_notes_never_promote_to_day_validity():
    sr, p = packets()
    sr["entities"][0]["temporal_observations"].pop()
    with pytest.raises(EntityConsistencyError, match="incomplete"):
        build(sr, p)


def test_temporal_withheld_mutable_field_cannot_enter_model_challenge():
    sr, p = packets()
    n = sr["entities"][0]["temporal_observations"][1]["observation"]
    n["status"] = "withheld_outside_target_day"
    n["withheld_fields"] = ["body", "title"]
    with pytest.raises(EntityConsistencyError):
        build(sr, p)


def test_mutable_snapshot_update_must_fall_inside_jst_day():
    sr, p = packets()
    sr["entities"][0]["temporal_observations"][1]["observation"]["resource_updated_at"] = "2026-10-14T15:00:00Z"
    with pytest.raises(EntityConsistencyError, match="outside target day"):
        build(sr, p)


@pytest.mark.parametrize("field,value", [
    ("text", None), ("text", True), ("text", 12), ("evidence_ids", []),
    ("evidence_ids", [True]), ("evidence_ids", ["x" * 64]),
    ("verdict", "verified"), ("verdict", None),
])
def test_invalid_displayed_sentence_fails_before_any_model_call(field, value):
    sr, p = packets()
    p["entities"][0]["sentences"][0][field] = value
    sr["preview_sha256"] = _sha(_json(p))
    with pytest.raises(EntityConsistencyError):
        build(sr, p)


def test_foreign_entity_and_cross_capture_fail():
    sr, p = packets()
    with pytest.raises(EntityConsistencyError):
        build_entity_consistency_packet(_json(sr), _json(p),
            ("upiscium/ObsidianAutomation", "pull_request", 355))
    p["capture_sha256"] = "f" * 64
    sr["preview_sha256"] = _sha(_json(p))
    with pytest.raises(EntityConsistencyError):
        build(sr, p)


def test_uncited_counterevent_must_be_in_same_entity_and_new_reference():
    packet = build()
    for ref in (2, 99, True, "1"):
        v = reply(packet)
        v["checks"][0]["challenger_refs"] = [ref]
        with pytest.raises(EntityConsistencyError):
            admit_entity_consistency_reply(packet.canonical_bytes, _json(v))


@pytest.mark.parametrize("mutation", [
    "foreign_packet", "missing_check", "duplicate_check", "bad_status",
    "empty_counter", "counter_with_no_conflict", "boolean_index",
    "oversize_reason", "duplicate_counter", "missing_key",
])
def test_model_advisory_is_exactly_bounded_and_untrusted(mutation):
    packet = build()
    data = reply(packet)
    if mutation == "foreign_packet":
        data["packet_sha256"] = "f" * 64
    elif mutation == "missing_check":
        data["checks"].pop()
    elif mutation == "duplicate_check":
        data["checks"][1] = copy.deepcopy(data["checks"][0])
    elif mutation == "bad_status":
        data["checks"][0]["status"] = "approved"
    elif mutation == "empty_counter":
        data["checks"][0]["challenger_refs"] = []
    elif mutation == "counter_with_no_conflict":
        data["checks"][1]["challenger_refs"] = [1]
    elif mutation == "boolean_index":
        data["checks"][0]["sentence_index"] = False
    elif mutation == "oversize_reason":
        data["checks"][0]["reason"] = "x" * 241
    elif mutation == "duplicate_counter":
        data["checks"][0]["challenger_refs"] = [1, 1]
    else:
        del data["checks"][0]["reason"]
    with pytest.raises(EntityConsistencyError):
        admit_entity_consistency_reply(packet.canonical_bytes, _json(data))


def test_unsupported_sentences_never_gain_consistency_or_release_authority():
    sr, p = packets()
    p["entities"][0]["sentences"][1]["verdict"] = "unsupported"
    sr["preview_sha256"] = _sha(_json(p))
    packet = build(sr, p)
    assert len(packet.report()["sentences"]) == 1
    data = {"packet_sha256": packet.sha256, "checks": [
        {"sentence_index": 0, "status": "no_conflict_observed",
         "challenger_refs": [], "reason": "advisory only"}
    ]}
    result = admit_entity_consistency_reply(packet.canonical_bytes, _json(data))
    assert len(result.report()["checks"]) == 1
    assert result.report()["factuality_verified"] is False
    assert result.report()["publication_authorized"] is False


def test_unicode_literal_source_retained_and_sha_mismatch_rejected():
    sr, p = packets()
    sr["entities"][0]["fields"][2]["text"] = "試験で34件PASS、未完了の問題は残る"
    with pytest.raises(EntityConsistencyError):
        build(sr, p)
    sr["entities"][0]["fields"][2]["text_sha256"] = _sha(sr["entities"][0]["fields"][2]["text"].encode())
    packet = build(sr, p)
    assert "未完了" in packet.canonical_bytes.decode("utf-8")
    assert packet.report()["publication_authorized"] is False


def test_packet_size_fails_closed_instead_of_silently_dropping_sources(monkeypatch):
    from obsidian_automation import github_daily_entity_consistency as mod
    monkeypatch.setattr(mod, "MAX_PACKET_BYTES", 512)
    with pytest.raises(EntityConsistencyError, match="byte cap"):
        build()


def test_admission_of_empty_supported_set_is_explicitly_not_quality_success():
    sr, p = packets()
    for s in p["entities"][0]["sentences"]:
        s["verdict"] = "unresolved"
    sr["preview_sha256"] = _sha(_json(p))
    packet = build(sr, p)
    assert not packet.report()["sentences"]
    result = admit_entity_consistency_reply(
        packet.canonical_bytes, _json({"packet_sha256": packet.sha256, "checks": []}))
    assert result.report()["human_quality_review_completed"] is False
    assert result.report()["publication_authorized"] is False


def test_conflicting_duplicate_json_reply_keys_rejected():
    packet = build()
    data = _json(reply(packet)).decode("utf-8")
    # Duplicate top-level packet_sha256 must not get 'last wins' semantics.
    bad = data.replace('"checks":', '"packet_sha256":"f", "checks":', 1).encode()
    with pytest.raises(EntityConsistencyError):
        admit_entity_consistency_reply(packet.canonical_bytes, bad)


@pytest.mark.parametrize("component", ["bad_source_entity", "bad_preview_entity", "duplicate_event_id"])
def test_malformed_source_entity_or_foreign_duplicate_event_fails_typed(component):
    sr, p = packets()
    if component == "bad_source_entity":
        sr["entities"].append({"entity": None})
    elif component == "bad_preview_entity":
        p["entities"].append({"entity": [1, 2]})
    else:
        sr["entities"][0]["temporal_observations"][3]["evidence_id"] = EIDS[2]
    sr["preview_sha256"] = _sha(_json(p))
    with pytest.raises(EntityConsistencyError):
        build(sr, p)


def test_temporal_observation_cannot_precede_its_resource_update():
    sr, p = packets()
    sr["entities"][0]["temporal_observations"][1]["observation"]["observed_at_utc"] = "2026-10-14T02:00:00Z"
    with pytest.raises(EntityConsistencyError, match="observation predates"):
        build(sr, p)


def test_more_events_or_fields_than_budget_rejects_without_silent_truncation():
    sr, p = packets()
    sr["entities"][0]["source_event_count"] = 33
    with pytest.raises(EntityConsistencyError):
        build(sr, p)
    sr, p = packets()
    sr["entities"][0]["fields"] += [copy.deepcopy(sr["entities"][0]["fields"][0]) for _ in range(92)]
    with pytest.raises(EntityConsistencyError):
        build(sr, p)


def test_nontext_events_are_counted_not_silently_declared_inactive():
    sr, p = packets()
    sr["entities"][0]["fields"] = [f for f in sr["entities"][0]["fields"]
                                   if f["source_ref"] != 0]
    packet = build(sr, p)
    assert packet.report()["source_event_count"] == 4
    assert packet.report()["source_refs_with_no_retained_text"] == [0]
    assert packet.report()["requires_human_quality_review"]


def test_model_reply_invalid_unicode_and_duplicate_nested_keys_fail():
    packet = build()
    raw = _json(reply(packet)).decode("utf-8").replace(
        '"sentence_index":0', '"sentence_index":0, "status":"fake"', 1)
    with pytest.raises(EntityConsistencyError):
        admit_entity_consistency_reply(packet.canonical_bytes, raw.encode())


@pytest.mark.parametrize("ref,wrong_status", [
    (0, "day_snapshot_not_event_instant"),
    (2, "content_addressed_commit_not_push_time"),
])
def test_temporal_scope_cannot_masquerade_as_wrong_event_kind(ref, wrong_status):
    sr, p = packets()
    observation = sr["entities"][0]["temporal_observations"][ref]["observation"]
    observation["status"] = wrong_status
    if ref == 0:
        observation["resource_updated_at"] = "2026-10-14T04:00:00Z"
    with pytest.raises(EntityConsistencyError):
        build(sr, p)
