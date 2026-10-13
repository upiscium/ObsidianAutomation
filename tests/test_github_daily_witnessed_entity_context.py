"""C1 tests: exact CAS-bound Identity Context, no v1 quarantine relaxation."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from obsidian_automation import github_daily_witnessed_entity_context as wit
from obsidian_automation import github_daily_route_cas as cas
from obsidian_automation import github_daily_source_closure as c0
from obsidian_automation import github_daily_source_route as source
from obsidian_automation import github_daily_entity_context as old
from obsidian_automation.github_daily_issue_pr import EntityKey
from obsidian_automation.github_daily_summary import parse_evidence_bundle

from test_github_daily_route_cas import safe_layout
from test_github_daily_source_route import _collect


def _publish(tmp_path, *, visible_pr=True, commit=True, review=True):
    collected = _collect(
        tmp_path, visible_pr=visible_pr, include_commit=commit,
        include_review_comment=review,
    )
    saved = cas.publish_fresh_capture(collected)
    return collected, saved


def test_c1_releases_only_c0_verified_issue_pr_comment_and_pr_commit(
    tmp_path, safe_layout,
):
    original, saved = _publish(tmp_path)
    result = wit.build_witnessed_entity_contexts(saved.index_sha256)
    again = wit.build_witnessed_entity_contexts(saved.index_sha256)
    assert result == again
    assert result.capture_sha256 == saved.index_sha256
    assert result.route_receipt_sha256 == original.receipt_sha256
    assert result.evidence_sha256 == original.evidence.sha256
    assert result.original_event_count == len(original.evidence.events)
    assert result.witnessed_event_count == 4
    assert (
        result.grouped_event_count + result.unattributed_default_commit_count
        == result.original_event_count
    )
    assert result.original_only_event_count == result.grouped_event_count - 4
    assert result.report()["publication_authorized"] is False
    assert result.report()["semantic_coverage_review_required"] is True
    assert result.report()["record_version"] == 3
    assert result.collection_mode == "explicit_historical_reconfirmation"

    # The old v2 API *still* quarantines ambiguous comments and PR commits.
    old_result = old.build_entity_contexts(original.evidence.canonical_bytes)
    assert old_result.unverified_issue_comments > 0
    assert old_result.unverified_pr_comments > 0
    assert old_result.unverified_pr_commits > 0
    # Only the new source-bound C1 partition may include witnessed events.
    all_parts = [part for ctx in result.contexts for part in ctx.events]
    witnessed = [part for part in all_parts if part.route_proof is not None]
    assert len(witnessed) == 4
    assert all(part.original["kind"] == part.route_proof.source_kind
               for part in witnessed)
    by_id = {part.original["source_id"]: part for part in witnessed}
    assert by_id["31"].route_proof.entity_type == "issue"
    assert by_id["33"].route_proof.entity_type == "pull_request"
    assert by_id["33"].route_proof.number == 2
    assert any(
        p.original["kind"] == "pull_request_commit"
        and p.route_proof.verified_api_path.endswith("/pulls/2/commits")
        for p in witnessed
    )
    assert len({p.original["evidence_id"] for p in all_parts}) == len(all_parts)

    keys = {part.key for part in result.contexts}
    assert EntityKey("upiscium/Test", "issue", 1) in keys
    assert EntityKey("upiscium/Test", "pull_request", 2) in keys
    assert all(c.sha256 == wit.verify_witnessed_entity_context(
        c, capture_sha256=saved.index_sha256,
    ) for c in result.contexts)


def test_c1_context_v3_json_identity_contains_exact_cas_triple_and_proofs(
    tmp_path, safe_layout,
):
    original, saved = _publish(tmp_path)
    model = wit.build_witnessed_entity_contexts(saved.index_sha256)
    assert model.contexts
    for ctx in model.contexts:
        raw = ctx.to_json_bytes()
        value = json.loads(raw)
        assert hashlib.sha256(raw).hexdigest() == ctx.sha256
        assert len(raw) <= wit.MAX_CONTEXT_BYTES
        assert value["record_version"] == wit.RECORD_VERSION
        assert value["stage"] == wit.CONTEXT_STAGE
        assert value["capture_sha256"] == saved.index_sha256
        assert value["route_receipt_sha256"] == original.receipt_sha256
        assert value["evidence_sha256"] == original.evidence.sha256
        assert value["closure_sha256"] == model.closure_sha256
        assert value["collection_mode"] == "explicit_historical_reconfirmation"
        assert value["publication_authorized"] is False
        assert value["requires_human_quality_review"] is True
        assert value["max_context_bytes"] == wit.MAX_CONTEXT_BYTES
        assert value["batch_count"] == len(model.contexts)
        assert [record["original_event"]["evidence_id"] for record in value["events"]] == list(ctx.evidence_ids)
        for row in value["events"]:
            original_row = row["original_event"]
            if original_row["kind"] in source.REQUIRES_WITNESS:
                proof = row["verified_route"]
                assert proof is not None
                assert proof["evidence_id"] == original_row["evidence_id"]
                assert "kind" not in proof  # C0 uses the source_kind field.
                assert proof["source_kind"] == original_row["kind"]
                assert proof["entity_type"] == ctx.key.entity_type
            else:
                assert row["verified_route"] is None
    assert all("publication_authorized" not in part.original
               for ctx in model.contexts for part in ctx.events)


def test_c1_old_c0_same_evidence_different_receipt_yields_different_context_sha(
    tmp_path, safe_layout, monkeypatch
):
    original_now = source._utc_now
    cas_clock = cas._utc_now
    first = datetime(2026, 10, 13, 5, 0, tzinfo=timezone.utc)
    second = first + timedelta(minutes=5)
    records = []
    for time in (first, second):
        monkeypatch.setattr(source, "_utc_now", lambda now=time: now)
        monkeypatch.setattr(cas, "_utc_now", lambda now=time: now)
        original, saved = _publish(tmp_path, commit=False, review=False)
        records.append((original, saved, wit.build_witnessed_entity_contexts(saved.index_sha256)))
    a, b = records
    assert a[0].evidence.sha256 == b[0].evidence.sha256
    assert a[1].index_sha256 != b[1].index_sha256
    assert a[0].receipt_sha256 != b[0].receipt_sha256
    assert a[2].contexts and b[2].contexts
    assert a[2].contexts[0].evidence_sha256 == b[2].contexts[0].evidence_sha256
    assert a[2].contexts[0].sha256 != b[2].contexts[0].sha256
    assert a[2].contexts[0].closure_sha256 != b[2].contexts[0].closure_sha256
    monkeypatch.setattr(source, "_utc_now", original_now)
    monkeypatch.setattr(cas, "_utc_now", cas_clock)


def test_c1_original_only_events_preserved_distinct_from_route_witness(
    tmp_path, safe_layout
):
    original, saved = _publish(tmp_path, commit=False, review=False)
    model = wit.build_witnessed_entity_contexts(saved.index_sha256)
    all_events = [part for ctx in model.contexts for part in ctx.events]
    assert all(p.route_proof is not None
               for p in all_events if p.original["kind"] in source.REQUIRES_WITNESS)
    assert all(p.route_proof is None
               for p in all_events if p.original["kind"] not in source.REQUIRES_WITNESS)
    assert len(all_events) + model.unattributed_default_commit_count == len(original.evidence.events)
    assert model.original_only_event_count == sum(
        p.route_proof is None for p in all_events
    )


@pytest.mark.parametrize("bad", [None, True, "abcd", "z"*64, "A"*64, "../"+"a"*64])
def test_c1_wrong_capture_digest_is_typed_block_not_empty(
    tmp_path, safe_layout, bad
):
    with pytest.raises(wit.DailyWitnessedContextError):
        wit.build_witnessed_entity_contexts(bad)


@pytest.mark.parametrize("budget", [True, None, "16384", 0, 1000, 65537])
def test_c1_context_budget_requires_actual_bounded_integer(
    tmp_path, safe_layout, budget
):
    _, saved = _publish(tmp_path, commit=False, review=False)
    with pytest.raises(wit.DailyWitnessedContextError, match="budget"):
        wit.build_witnessed_entity_contexts(saved.index_sha256, max_bytes=budget)


def test_c1_invalid_source_role_or_missing_receipt_stays_blocked(
    tmp_path, safe_layout, monkeypatch
):
    root, roles = safe_layout
    _, saved = _publish(tmp_path)
    monkeypatch.setattr(cas, "_roles", lambda: replace(roles, reader_uid=roles.reader_uid + 14))
    with pytest.raises(wit.DailyWitnessedContextError, match="unavailable"):
        wit.build_witnessed_entity_contexts(saved.index_sha256)
    monkeypatch.setattr(cas, "_roles", lambda: roles)
    (root/"receipt"/cas._filename("receipt", saved.receipt_sha256)).unlink()
    with pytest.raises(wit.DailyWitnessedContextError, match="unavailable"):
        wit.build_witnessed_entity_contexts(saved.index_sha256)


def test_c1_private_historical_reconfirmation_is_not_today_publication(
    tmp_path, safe_layout
):
    _, saved = _publish(tmp_path, commit=False, review=False)
    model = wit.build_witnessed_entity_contexts(saved.index_sha256)
    assert model.date == "2026-10-05"
    assert model.collection_mode == "explicit_historical_reconfirmation"
    assert model.report()["publication_authorized"] is False
    assert all(b"publication_authorized\":false" in c.to_json_bytes()
               for c in model.contexts)
    assert model.report()["semantic_coverage_review_required"] is True


def test_c1_context_mutation_and_foreign_c0_object_have_no_authority(
    tmp_path, safe_layout, monkeypatch
):
    _, saved = _publish(tmp_path)
    original = wit.build_witnessed_entity_contexts(saved.index_sha256)
    ctx = original.contexts[0]
    for mutated in (
        replace(ctx, capture_sha256="a"*64),
        replace(ctx, route_receipt_sha256="b"*64),
        replace(ctx, evidence_sha256="c"*64),
        replace(ctx, closure_sha256="d"*64),
        replace(ctx, collection_mode="scheduled_recent_api_reverified"),
        replace(ctx, batch_index=ctx.batch_index + 1),
        replace(ctx, date="2026-10-12"),
        replace(ctx, key=EntityKey("upiscium/Test", "pull_request" if ctx.key.entity_type == "issue" else "issue", ctx.key.number)),
    ):
        with pytest.raises(wit.DailyWitnessedContextError):
            wit.verify_witnessed_entity_context(mutated, capture_sha256=saved.index_sha256)
    proof = next((part for part in ctx.events if part.route_proof is not None), None)
    if proof is not None:
        wrong = replace(proof, route_proof=replace(proof.route_proof, number=998))
        forged = replace(ctx, events=(wrong,)+ctx.events[1:])
        with pytest.raises(wit.DailyWitnessedContextError):
            wit.verify_witnessed_entity_context(forged, capture_sha256=saved.index_sha256)
    real = c0.read_verified_source_closure(saved.index_sha256)
    monkeypatch.setattr(c0, "read_verified_source_closure", lambda _: replace(real, date="2026-10-07"))
    with pytest.raises(wit.DailyWitnessedContextError):
        wit.build_witnessed_entity_contexts(saved.index_sha256)


def test_c1_revalidates_entire_partition_even_when_only_one_context_is_asked(
    tmp_path, safe_layout, monkeypatch,
):
    _, saved = _publish(tmp_path)
    whole = wit.build_witnessed_entity_contexts(saved.index_sha256)
    assert len(whole.contexts) >= 2
    chosen = whole.contexts[0]
    actual = wit.verify_witnessed_entity_context(chosen, capture_sha256=saved.index_sha256)
    assert actual == chosen.sha256
    # A model-reconstructed v3 context with old source hash, other entity or
    # swapped batch is refused even if its own JSON is self-consistent.
    other = whole.contexts[1]
    crafted = replace(chosen, events=other.events)
    with pytest.raises(wit.DailyWitnessedContextError):
        wit.verify_witnessed_entity_context(crafted, capture_sha256=saved.index_sha256)
    monkeypatch.setattr(
        c0, "read_verified_source_closure",
        lambda _: (_ for _ in ()).throw(c0.DailySourceClosureError("missing")),
    )
    with pytest.raises(wit.DailyWitnessedContextError, match="unavailable"):
        wit.verify_witnessed_entity_context(chosen, capture_sha256=saved.index_sha256)


def test_c1_does_not_persist_or_write_model_context_artifacts(
    tmp_path, safe_layout, monkeypatch
):
    _, saved = _publish(tmp_path)
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    from obsidian_automation import artifact_lifecycle
    forbidden = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("No C1 writes"))
    monkeypatch.setattr(artifact_lifecycle, "_store_immutable", forbidden)
    monkeypatch.setattr(source, "collect_fresh_verified_routes", forbidden)
    result = wit.build_witnessed_entity_contexts(saved.index_sha256)
    assert result.contexts
    assert wit.verify_witnessed_entity_context(result.contexts[0], capture_sha256=saved.index_sha256)
    after = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    assert before == after


def test_c1_small_context_budget_must_fail_or_split_without_dropping_original_events(
    tmp_path, safe_layout
):
    original, saved = _publish(tmp_path)
    try:
        result = wit.build_witnessed_entity_contexts(saved.index_sha256, max_bytes=1400)
    except wit.DailyWitnessedContextError as exc:
        assert "bounded model context" in str(exc) or "bound" in str(exc)
    else:
        assert result.grouped_event_count + result.unattributed_default_commit_count == len(original.evidence.events)
        assert all(len(ctx.to_json_bytes()) <= 1400 for ctx in result.contexts)
