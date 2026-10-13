"""No-network, no-write capture index checks for Daily v2 (#336 stage A)."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from obsidian_automation.github_daily_route_capture_index import (
    DailyCaptureIndexError, MAX_CAPTURE_INDEX_BYTES,
    build_capture_index, parse_capture_index,
)
from test_github_daily_source_route import _collect


def _canonical(payload):
    return (json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def _capture(tmp_path):
    collected = _collect(tmp_path)
    index = build_capture_index(collected)
    return collected, index


def test_index_binds_complete_fresh_collection_and_is_nonpublishing(tmp_path):
    collected, index = _capture(tmp_path)
    assert index == build_capture_index(collected)
    assert index == parse_capture_index(
        index.canonical_bytes, collected.evidence.canonical_bytes, collected.receipt_bytes
    )
    payload = json.loads(index.canonical_bytes)
    assert payload == {
        "record_version": 1,
        "source_receipt_version": 2,
        "date": "2026-10-05",
        "evidence_sha256": collected.evidence.sha256,
        "route_receipt_sha256": collected.receipt_sha256,
        "route_count": collected.route_count,
        "collection_mode": "explicit_historical_reconfirmation",
        "captured_at_utc": index.captured_at_utc,
        "producer_attestation_verified": False,
        "publication_authorized": False,
    }
    assert len(index.canonical_bytes) < MAX_CAPTURE_INDEX_BYTES
    assert b"Implemented the validation" not in index.canonical_bytes


@pytest.mark.parametrize("column,value", [
    ("record_version", 2),
    ("source_receipt_version", 1),
    ("date", "2026-10-06"),
    ("evidence_sha256", "f" * 64),
    ("route_receipt_sha256", "f" * 64),
    ("route_count", 1),
    ("route_count", True),
    ("collection_mode", "scheduled_recent_api_reverified"),
    ("captured_at_utc", "2026-10-06T00:00:00Z"),
    ("producer_attestation_verified", True),
    ("publication_authorized", True),
])
def test_self_consistent_rehashed_index_mutations_still_fail(tmp_path, column, value):
    capture, index = _capture(tmp_path)
    payload = json.loads(index.canonical_bytes)
    payload[column] = value
    with pytest.raises(DailyCaptureIndexError):
        parse_capture_index(
            _canonical(payload), capture.evidence.canonical_bytes, capture.receipt_bytes
        )


def test_extra_missing_reordered_duplicate_and_noncanonical_json_fail(tmp_path):
    capture, index = _capture(tmp_path)
    payload = json.loads(index.canonical_bytes)
    cases = [
        _canonical({**payload, "unexpected": "field"}),
        _canonical({k: v for k, v in payload.items() if k != "route_count"}),
        json.dumps(payload, indent=2).encode(),
        index.canonical_bytes + b"\n",
        index.canonical_bytes.replace(
            b'"publication_authorized":false',
            b'"publication_authorized":false,"publication_authorized":false',
        ),
        index.canonical_bytes[::-1],
        b"x" * (MAX_CAPTURE_INDEX_BYTES + 1),
    ]
    for wrong in cases:
        with pytest.raises(DailyCaptureIndexError):
            parse_capture_index(wrong, capture.evidence.canonical_bytes, capture.receipt_bytes)


def test_changes_to_evidence_or_receipt_cannot_reuse_index(tmp_path):
    capture, index = _capture(tmp_path)
    evidence = capture.evidence.canonical_bytes
    receipt = capture.receipt_bytes
    for evidence_bytes, route_bytes in [
        (evidence + b" ", receipt),
        (evidence[:-1], receipt),
        (evidence, receipt + b" "),
        (evidence, receipt[:-1]),
        (b"", receipt),
        (evidence, b""),
    ]:
        with pytest.raises(DailyCaptureIndexError):
            parse_capture_index(index.canonical_bytes, evidence_bytes, route_bytes)


def test_fresh_source_object_hashes_counts_and_date_are_checked(tmp_path):
    capture, _ = _capture(tmp_path)
    altered = [
        replace(capture, receipt_sha256="0" * 64),
        replace(capture, route_count=capture.route_count + 1),
        replace(capture, evidence=replace(capture.evidence, sha256="0" * 64)),
        replace(capture, evidence=replace(capture.evidence, target_date="2026-10-06")),
    ]
    for item in altered:
        with pytest.raises(DailyCaptureIndexError):
            build_capture_index(item)


def test_invalid_nonbytes_and_forged_authority_are_rejected(tmp_path):
    capture, index = _capture(tmp_path)
    with pytest.raises(DailyCaptureIndexError):
        build_capture_index(object())
    for value in (bytearray(index.canonical_bytes), index.canonical_bytes.decode(), None):
        with pytest.raises(DailyCaptureIndexError):
            parse_capture_index(value, capture.evidence.canonical_bytes, capture.receipt_bytes)
    with pytest.raises(DailyCaptureIndexError):
        parse_capture_index(index.canonical_bytes, bytearray(capture.evidence.canonical_bytes), capture.receipt_bytes)


def test_capture_index_does_not_persist_files_or_invoke_remote_sources(tmp_path, monkeypatch):
    capture = _collect(tmp_path)
    before = {p.relative_to(tmp_path) for p in tmp_path.rglob("*")}
    monkeypatch.setattr(
        "obsidian_automation.github_daily_source_route.collect_fresh_verified_routes",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not recollect"))
    )
    index = build_capture_index(capture)
    parse_capture_index(index.canonical_bytes, capture.evidence.canonical_bytes, capture.receipt_bytes)
    assert before == {p.relative_to(tmp_path) for p in tmp_path.rglob("*")}


def test_boolean_route_count_is_not_valid_integer_even_with_one_route(tmp_path):
    from obsidian_automation.github_daily_source_route import collect_fresh_verified_routes
    from test_github_daily_source_route import _FakeClient, _config, DATE

    class SingleComment(_FakeClient):
        def _paged(self, path):
            values = super()._paged(path)
            return values[:1] if "/issues/comments?" in path else values

    collection = collect_fresh_verified_routes(
        _config(tmp_path), target_date=DATE, client=SingleComment(),
        explicit_historical_reconfirmation=True,
    )
    assert collection.route_count == 1
    with pytest.raises(DailyCaptureIndexError, match="collection payload hash mismatch"):
        build_capture_index(replace(collection, route_count=True))


@pytest.mark.parametrize("bad", [None, object(), "evidence", b"not-an-object"])
def test_invalid_nested_evidence_uses_typed_error(tmp_path, bad):
    collection, _ = _capture(tmp_path)
    with pytest.raises(DailyCaptureIndexError, match="DailyEvidenceBundle"):
        build_capture_index(replace(collection, evidence=bad))


def test_deep_source_json_recursion_is_typed_rejection(tmp_path):
    collection, index = _capture(tmp_path)
    recursive = b"[" * 1300 + b"0" + b"]" * 1300
    for evidence, receipt in (
        (recursive, collection.receipt_bytes),
        (collection.evidence.canonical_bytes, recursive),
    ):
        with pytest.raises(DailyCaptureIndexError):
            parse_capture_index(index.canonical_bytes, evidence, receipt)


def test_malformed_source_capture_time_has_constant_typed_diagnostic(tmp_path):
    collected, index = _capture(tmp_path)
    payload = json.loads(collected.receipt_bytes)
    payload["captured_at_utc"] = "NOT_A_TIMESTAMP_PRIVATE_TOKEN"
    with pytest.raises(DailyCaptureIndexError) as ex:
        parse_capture_index(
            index.canonical_bytes,
            collected.evidence.canonical_bytes,
            _canonical(payload),
        )
    assert str(ex.value) == "capture sources failed route verification"
    assert ex.value.__cause__ is None
