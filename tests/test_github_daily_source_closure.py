"""C0: CAS-only, source-by-source nonpublishing closure (#342)."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from obsidian_automation import github_daily_route_cas as cas
from obsidian_automation import github_daily_source_closure as closure
from obsidian_automation import artifact_lifecycle
from test_github_daily_route_cas import safe_layout
from test_github_daily_source_route import _collect


def _published(tmp_path):
    # This synthetic fixture uses the actual immutable CAS writer and reader,
    # including owner/ACL/regular-file/hash validation, not JSON from caller.
    collected = _collect(
        tmp_path, visible_pr=True, include_commit=True,
        include_review_comment=True,
    )
    published = cas.publish_fresh_capture(collected)
    return collected, published


def test_c0_readonly_source_closure_binds_all_four_authenticated_routes(
    tmp_path, safe_layout
):
    original, published = _published(tmp_path)
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    result = closure.read_verified_source_closure(published.index_sha256)
    assert result == closure.read_verified_source_closure(published.index_sha256)
    assert hashlib.sha256(result.canonical_bytes).hexdigest() == result.sha256
    assert result.capture_sha256 == published.index_sha256
    assert result.evidence_sha256 == original.evidence.sha256
    assert result.route_receipt_sha256 == original.receipt_sha256
    assert result.date == "2026-10-05"
    assert result.collection_mode == "explicit_historical_reconfirmation"
    assert result.captured_at_utc.endswith("Z")
    assert result.witnessed_count == 4
    assert result.event_count >= 4
    assert (
        result.witnessed_count + result.unattributed_default_commit_count
        + result.other_unwitnessed_event_count
    ) == result.event_count
    assert result.publication_authorized is False
    assert result.requires_human_quality_review is True
    assert result.canonical_bytes.endswith(b"\n")
    assert sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")) == before
    serialized = json.loads(result.canonical_bytes)
    assert serialized["record_version"] == 1
    assert serialized["capture_sha256"] == published.index_sha256
    assert serialized["publication_authorized"] is False
    assert serialized["requires_human_quality_review"] is True
    assert serialized["witnessed_source_count"] == 4
    assert len(serialized["routes"]) == 4
    assert all("body" not in route and "message" not in route
               and "actor" not in route and "model" not in route
               for route in serialized["routes"])
    assert b"Implemented the validation" not in result.canonical_bytes


def test_c0_pr_conversation_route_is_api_proven_not_guessed_from_issues_url(
    tmp_path, safe_layout
):
    original, published = _published(tmp_path)
    result = closure.read_verified_source_closure(published.index_sha256)
    by_source = {entry.source_id: entry for entry in result.bindings}
    issue = by_source["31"]
    pr = by_source["33"]
    assert (issue.source_kind, issue.entity_type, issue.number) == (
        "issue_comment", "issue", 1,
    )
    assert (pr.source_kind, pr.entity_type, pr.number) == (
        "pull_request_comment", "pull_request", 2,
    )
    assert pr.verified_api_path.endswith("/issues/comments/33")
    assert pr.linked_entity_api_path.endswith("/issues/2")
    assert pr.remote_id == "1002"
    assert issue.remote_id == "1001"
    assert any(
        event.url == "https://github.com/upiscium/Test/issues/2#issuecomment-33"
        and event.kind == "pull_request_comment"
        for event in original.evidence.events
    )


def test_c0_pr_commit_membership_is_exact_not_a_public_commit_link(
    tmp_path, safe_layout
):
    _, published = _published(tmp_path)
    result = closure.read_verified_source_closure(published.index_sha256)
    commit = next(x for x in result.bindings if x.source_kind == "pull_request_commit")
    assert commit.entity_type == "pull_request"
    assert commit.number == 2
    assert commit.verified_api_path.endswith("/pulls/2/commits")
    assert commit.linked_entity_api_path.endswith("/pulls/2")
    assert commit.source_id == commit.remote_id
    assert len(commit.source_id) == 40
    review = next(
        x for x in result.bindings
        if x.source_kind == "pull_request_review_comment"
    )
    assert review.entity_type == "pull_request"
    assert review.number == 2
    assert review.verified_api_path.endswith("/pulls/comments/51")
    assert review.linked_entity_api_path.endswith("/pulls/2")
    assert review.verified_api_path != review.linked_entity_api_path


@pytest.mark.parametrize("invalid", [
    None, True, "a"*63, "a"*65, "A"*64, "../"+("a"*64), object(),
])
def test_only_exact_capture_digest_is_allowed(tmp_path, safe_layout, invalid):
    with pytest.raises(closure.DailySourceClosureError):
        closure.read_verified_source_closure(invalid)


def test_does_not_accept_caller_supplied_stored_handle_or_index(
    tmp_path, safe_layout
):
    _, published = _published(tmp_path)
    stored = cas.read_verified_capture(published.index_sha256)
    with pytest.raises(closure.DailySourceClosureError):
        closure.read_verified_source_closure(stored)
    with pytest.raises(closure.DailySourceClosureError):
        closure.read_verified_source_closure(stored.capture_index)
    with pytest.raises(closure.DailySourceClosureError):
        closure.read_verified_source_closure(stored.evidence_bytes)


def test_read_model_does_not_trust_mutable_stored_routes(
    tmp_path, safe_layout, monkeypatch
):
    _, published = _published(tmp_path)
    real_reader = cas.read_verified_capture
    original = real_reader(published.index_sha256)
    # This tampers with the ordinary Python return value; exact source bytes
    # remain unchanged. C0 must reconstruct from bytes, not accept the list.
    fake_record = dict(original.routes[0], entity="pull_request", number=9999)
    monkeypatch.setattr(
        cas, "read_verified_capture",
        lambda _: replace(original, routes=(fake_record,)),
    )
    derived = closure.read_verified_source_closure(published.index_sha256)
    assert derived.witnessed_count == 4
    assert all(x.number != 9999 for x in derived.bindings)


def test_forged_storage_metadata_and_swapped_source_bytes_fail_closed(
    tmp_path, safe_layout, monkeypatch
):
    _, published = _published(tmp_path)
    actual = cas.read_verified_capture(published.index_sha256)
    for altered in (
        replace(actual, index_sha256="b"*64),
        replace(actual, collector_storage_attested=False),
        replace(actual, daily_publication_authorized=True),
        replace(actual, evidence_bytes=actual.evidence_bytes + b" "),
        replace(actual, receipt_bytes=actual.receipt_bytes + b" "),
        replace(actual, capture_index=replace(actual.capture_index, date="2026-10-06")),
    ):
        monkeypatch.setattr(cas, "read_verified_capture", lambda _, obj=altered: obj)
        with pytest.raises(closure.DailySourceClosureError):
            closure.read_verified_source_closure(published.index_sha256)


def test_missing_receipt_and_wrong_reader_identity_are_pending_not_empty(
    tmp_path, safe_layout, monkeypatch
):
    root, roles = safe_layout
    _, published = _published(tmp_path)
    monkeypatch.setattr(
        cas, "_roles",
        lambda: replace(roles, reader_uid=roles.reader_uid + 20),
    )
    with pytest.raises(closure.DailySourceClosureError, match="unavailable"):
        closure.read_verified_source_closure(published.index_sha256)
    monkeypatch.setattr(cas, "_roles", lambda: roles)
    (root/"receipt"/cas._filename("receipt", published.receipt_sha256)).unlink()
    with pytest.raises(closure.DailySourceClosureError, match="unavailable"):
        closure.read_verified_source_closure(published.index_sha256)


def test_no_writer_model_network_or_vault_activity_on_closure_read(
    tmp_path, safe_layout, monkeypatch
):
    _, published = _published(tmp_path)
    def forbidden(*args, **kwargs):
        raise AssertionError("C0 must only read existing trusted CAS")

    monkeypatch.setattr(artifact_lifecycle, "_store_immutable", forbidden)
    monkeypatch.setattr(
        "obsidian_automation.github_daily_source_route.collect_fresh_verified_routes",
        forbidden,
    )
    monkeypatch.setattr(
        "obsidian_automation.github_daily_activity.collect_daily_evidence",
        forbidden,
    )
    assert closure.read_verified_source_closure(published.index_sha256).witnessed_count == 4


def test_old_capture_readback_does_not_implicitly_reclassify_as_today(
    tmp_path, safe_layout, monkeypatch
):
    _, published = _published(tmp_path)
    # This read is deliberately independent of initial-write age limits.
    monkeypatch.setattr(
        cas, "_utc_now",
        lambda: (_ for _ in ()).throw(AssertionError("not a Writer eligibility check")),
    )
    result = closure.read_verified_source_closure(published.index_sha256)
    assert result.date == "2026-10-05"
    assert result.collection_mode == "explicit_historical_reconfirmation"
    assert result.publication_authorized is False


def test_canonical_closure_overflow_is_error_not_partial_list(
    tmp_path, safe_layout, monkeypatch
):
    _, published = _published(tmp_path)
    monkeypatch.setattr(closure, "MAX_CLOSURE_BYTES", 64)
    with pytest.raises(closure.DailySourceClosureError, match="byte budget"):
        closure.read_verified_source_closure(published.index_sha256)


def test_read_only_api_does_not_expose_freeform_metadata_or_paths(
    tmp_path, safe_layout
):
    _, published = _published(tmp_path)
    result = closure.read_verified_source_closure(published.index_sha256)
    data = json.loads(result.canonical_bytes)
    assert set(data) == {
        "record_version", "capture_sha256", "evidence_sha256",
        "route_receipt_sha256", "date", "captured_at_utc", "collection_mode",
        "evidence_event_count", "witnessed_source_count",
        "unattributed_default_branch_commit_count",
        "other_unwitnessed_event_count", "routes", "publication_authorized",
        "requires_human_quality_review",
    }
    assert not hasattr(result, "collector_storage_attested")
    assert not hasattr(result, "evidence_bytes")
    assert b"/var/lib/" not in result.canonical_bytes
    assert "source_id" in data["routes"][0]
    assert "verified_api_path" in data["routes"][0]
    assert "linked_entity_api_path" in data["routes"][0]
    assert "entity_api_path" not in data["routes"][0]
