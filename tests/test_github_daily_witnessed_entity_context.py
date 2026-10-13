"""C1 v3: private CAS-backed Issue/PR source contexts and candidates (#342)."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from obsidian_automation import github_daily_route_cas as cas
from obsidian_automation import github_daily_source_closure as closure
from obsidian_automation import github_daily_witnessed_entity_context as c1
from obsidian_automation import github_daily_entity_context as v2
from obsidian_automation.github_daily_issue_pr import EntityKey
from test_github_daily_route_cas import safe_layout
from test_github_daily_source_route import _collect


@pytest.fixture()
def source_fixture(tmp_path: Path, safe_layout):
    # Pinned GitHub stub, then real immutable CAS writer and real POSIX ACL.
    collected = _collect(
        tmp_path, visible_pr=True, include_commit=True,
        include_review_comment=True,
    )
    published = cas.publish_fresh_capture(collected)
    return collected, published, safe_layout[0], safe_layout[1]


def _c0_c1(fixture):
    original, published, _, _ = fixture
    return (
        closure.read_verified_source_closure(published.index_sha256),
        c1.build_witnessed_entity_contexts(published.index_sha256),
    )


def _model(candidates):
    return (json.dumps({"candidates": candidates}, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode()


def _context_with_kind(result, kind):
    return next(ctx for ctx in result.contexts if any(
        event["kind"] == kind for event in ctx.events
    ))


def test_c1_releases_only_exact_witnessed_routes_and_keeps_v2_quarantine(
    source_fixture,
):
    collected, pub, _, _ = source_fixture
    c0, out = _c0_c1(source_fixture)
    legacy = v2.build_entity_contexts(collected.evidence.canonical_bytes)
    assert legacy.unverified_issue_comments >= 1
    assert legacy.unverified_pr_comments >= 1
    assert legacy.unverified_pr_commits >= 1
    assert out.capture_sha256 == pub.index_sha256
    assert out.evidence_bundle_sha256 == c0.evidence_sha256
    assert out.route_receipt_sha256 == c0.route_receipt_sha256
    assert out.witnessed_event_count == c0.witnessed_count == 4
    assert out.grouped_event_count == (
        out.witnessed_event_count + out.structural_event_count
    )
    assert out.grouped_event_count + out.unattributed_default_commit_count == c0.event_count
    assert out.publication_authorized is False
    assert out.requires_human_quality_review is True
    all_events = [event for ctx in out.contexts for event in ctx.events]
    all_ids = {event["evidence_id"] for event in all_events}
    assert len(all_ids) == len(all_events)
    assert len(all_ids) == out.grouped_event_count
    assert {binding.evidence_id for binding in c0.bindings} <= all_ids
    for ctx in out.contexts:
        assert ctx.capture_sha256 == pub.index_sha256
        assert ctx.route_receipt_sha256 == c0.route_receipt_sha256
        assert ctx.evidence_bundle_sha256 == c0.evidence_sha256
        assert ctx.date == c0.date
        assert ctx.collection_mode == c0.collection_mode
        assert ctx.to_json_bytes().endswith(b"\n")
        assert len(ctx.to_json_bytes()) <= ctx.max_context_bytes
        assert ctx.sha256 == hashlib.sha256(ctx.to_json_bytes()).hexdigest()
        serialized = json.loads(ctx.to_json_bytes())
        assert serialized["record_version"] == 3
        assert serialized["publication_authorized"] is False
        assert serialized["requires_human_quality_review"] is True
        assert serialized["capture_sha256"] == pub.index_sha256
        assert serialized["route_receipt_sha256"] == pub.receipt_sha256
        assert len(ctx.route_proofs) == sum(
            event["kind"] in c1.route_source.REQUIRES_WITNESS
            for event in ctx.events
        )


def test_verified_issue_comment_and_pr_comment_do_not_collide(source_fixture):
    _, pub, _, _ = source_fixture
    out = c1.build_witnessed_entity_contexts(pub.index_sha256)
    issues = [
        (ctx.key, event, proof)
        for ctx in out.contexts for event in ctx.events
        for proof in ctx.route_proofs
        if proof["evidence_id"] == event["evidence_id"] and
        event["kind"] in {"issue_comment", "pull_request_comment"}
    ]
    by_id = {event["source_id"]: (key, event, proof) for key, event, proof in issues}
    issue_key, issue, ip = by_id["31"]
    pr_key, pr, pp = by_id["33"]
    assert issue_key == EntityKey("upiscium/Test", "issue", 1)
    assert pr_key == EntityKey("upiscium/Test", "pull_request", 2)
    assert ip["entity_type"] == "issue"
    assert pp["entity_type"] == "pull_request"
    assert pr["url"].startswith("https://github.com/upiscium/Test/issues/2")
    assert pp["verified_api_path"].endswith("/issues/comments/33")
    assert pp["linked_entity_api_path"].endswith("/issues/2")
    assert pp["verified_api_path"] != pp["linked_entity_api_path"]


def test_verified_pr_commit_requires_actual_api_membership(source_fixture):
    _, pub, _, _ = source_fixture
    out = c1.build_witnessed_entity_contexts(pub.index_sha256)
    ctx = _context_with_kind(out, "pull_request_commit")
    row = next(e for e in ctx.events if e["kind"] == "pull_request_commit")
    witness = next(p for p in ctx.route_proofs if p["evidence_id"] == row["evidence_id"])
    assert ctx.key.entity_type == "pull_request"
    assert ctx.key.number == 2
    assert witness["verified_api_path"].endswith("/pulls/2/commits")
    assert witness["linked_entity_api_path"].endswith("/pulls/2")
    assert witness["source_id"] == witness["remote_id"]
    assert len(witness["remote_id"]) == 40


def test_old_version_context_identity_cannot_equal_witnessed_v3(source_fixture):
    collected, pub, _, _ = source_fixture
    v2_ctx = v2.build_entity_contexts(collected.evidence.canonical_bytes)
    v3_ctx = c1.build_witnessed_entity_contexts(pub.index_sha256)
    assert all(ctx.sha256 != old.sha256 for ctx in v3_ctx.contexts for old in v2_ctx.contexts)
    assert all(json.loads(ctx.to_json_bytes())["stage"] == c1.CONTEXT_STAGE
               for ctx in v3_ctx.contexts)
    assert v2.RECORD_VERSION == 2 and c1.RECORD_VERSION == 3


def test_c1_deterministic_partition_and_replay(source_fixture):
    _, pub, root, _ = source_fixture
    before = sorted(str(x.relative_to(root)) for x in root.rglob("*"))
    a = c1.build_witnessed_entity_contexts(pub.index_sha256)
    b = c1.build_witnessed_entity_contexts(pub.index_sha256)
    assert a == b
    assert a.report() == b.report()
    assert tuple(ctx.sha256 for ctx in a.contexts) == tuple(
        ctx.sha256 for ctx in b.contexts
    )
    assert sorted(str(x.relative_to(root)) for x in root.rglob("*")) == before


def test_c1_candidate_schema_and_response_bind_cas_sha(source_fixture):
    _, pub, _, _ = source_fixture
    ctx = c1.build_witnessed_entity_contexts(pub.index_sha256).contexts[0]
    schema = c1.witnessed_model_candidate_schema(
        ctx, capture_sha256=pub.index_sha256,
    )
    assert set(schema["properties"]) == {"candidates"}
    assert "entity" not in str(schema)
    assert "publication_authorized" not in str(schema)
    assert schema["properties"]["candidates"]["items"]["properties"]["source_refs"]["items"]["enum"] == list(range(len(ctx.events)))
    response = _model([{"summary": "Reviewed a concrete Issue/PR change", "source_refs": [0]}])
    result = c1.normalize_witnessed_entity_candidates(
        ctx, response, capture_sha256=pub.index_sha256,
    )
    assert result.key == ctx.key
    assert result.capture_sha256 == pub.index_sha256
    assert result.route_receipt_sha256 == ctx.route_receipt_sha256
    assert result.evidence_bundle_sha256 == ctx.evidence_bundle_sha256
    assert result.context_sha256 == ctx.sha256
    assert result.response_sha256 == hashlib.sha256(response).hexdigest()
    assert len(result.candidates) == 1
    assert result.candidates[0].evidence_ids == (ctx.evidence_ids[0],)
    assert result.report()["publication_authorized"] is False
    assert result.report()["grounding_required"] is True
    assert result.report()["semantic_coverage_review_required"] is True
    assert result.uncited_evidence_ids == ctx.evidence_ids[1:]
    assert result == c1.normalize_witnessed_entity_candidates(
        ctx, response, capture_sha256=pub.index_sha256,
    )
    none = c1.normalize_witnessed_entity_candidates(
        ctx, _model([]), capture_sha256=pub.index_sha256,
    )
    assert not none.candidates and none.uncited_evidence_ids == ctx.evidence_ids
    assert none.report()["semantic_coverage_review_required"] is True


@pytest.mark.parametrize("change", [
    lambda ctx: replace(ctx, capture_sha256="a" * 64),
    lambda ctx: replace(ctx, route_receipt_sha256="a" * 64),
    lambda ctx: replace(ctx, evidence_bundle_sha256="a" * 64),
    lambda ctx: replace(ctx, key=EntityKey("upiscium/Test", "issue", 2)),
    lambda ctx: replace(ctx, date="2026-10-06"),
    lambda ctx: replace(ctx, batch_index=100),
    lambda ctx: replace(ctx, batch_count=7),
    lambda ctx: replace(ctx, events=tuple(reversed(ctx.events))),
    lambda ctx: replace(ctx, route_proofs=()),
    lambda ctx: replace(ctx, collection_mode="scheduled_recent_api_reverified"),
])
def test_tampered_context_fails_reconstruction(source_fixture, change):
    _, pub, _, _ = source_fixture
    ctx = _context_with_kind(
        c1.build_witnessed_entity_contexts(pub.index_sha256), "pull_request_comment",
    )
    mutated = change(ctx)
    if mutated == ctx:
        pytest.skip("specific fixture's context is one event")
    with pytest.raises(c1.DailyWitnessedContextError):
        c1.witnessed_model_candidate_schema(mutated, capture_sha256=pub.index_sha256)
    with pytest.raises(c1.DailyWitnessedContextError):
        c1.normalize_witnessed_entity_candidates(
            mutated, _model([]), capture_sha256=pub.index_sha256,
        )


@pytest.mark.parametrize("invalid", [
    None, True, "", "A"*64, "a"*63, "a"*65, "../"+"a"*64, object(),
])
def test_c1_rejects_unapproved_digest(source_fixture, invalid):
    with pytest.raises(c1.DailyWitnessedContextError):
        c1.build_witnessed_entity_contexts(invalid)


def test_c1_missing_cas_receipt_and_wrong_role_fail_closed(
    source_fixture, monkeypatch,
):
    _, pub, root, roles = source_fixture
    monkeypatch.setattr(cas, "_roles", lambda: replace(roles, reader_uid=roles.reader_uid + 1))
    with pytest.raises(c1.DailyWitnessedContextError):
        c1.build_witnessed_entity_contexts(pub.index_sha256)
    monkeypatch.setattr(cas, "_roles", lambda: roles)
    (root / "receipt" / cas._filename("receipt", pub.receipt_sha256)).unlink()
    with pytest.raises(c1.DailyWitnessedContextError):
        c1.build_witnessed_entity_contexts(pub.index_sha256)


def test_c1_missing_witness_cannot_fallback_to_public_url(
    source_fixture, monkeypatch,
):
    _, pub, _, _ = source_fixture
    good = closure.read_verified_source_closure(pub.index_sha256)
    monkeypatch.setattr(
        c1.closure, "read_verified_source_closure",
        lambda digest: replace(
            good, bindings=tuple(x for x in good.bindings if x.source_id != "33"),
            witnessed_count=good.witnessed_count - 1,
        ),
    )
    with pytest.raises(c1.DailyWitnessedContextError, match="lacks API witness"):
        c1.build_witnessed_entity_contexts(pub.index_sha256)


def test_c1_witness_confusion_and_pr_parent_swap_are_rejected(
    source_fixture, monkeypatch,
):
    _, pub, _, _ = source_fixture
    good = closure.read_verified_source_closure(pub.index_sha256)
    target = next(x for x in good.bindings if x.source_kind == "pull_request_commit")
    forged = replace(target, number=999)
    swapped = tuple(forged if x.evidence_id == target.evidence_id else x for x in good.bindings)
    monkeypatch.setattr(c1.closure, "read_verified_source_closure",
                        lambda digest: replace(good, bindings=swapped))
    with pytest.raises(c1.DailyWitnessedContextError, match="API witness"):
        c1.build_witnessed_entity_contexts(pub.index_sha256)


@pytest.mark.parametrize("reply", [
    b'{"candidates":[{"summary":"ok","source_refs":[true]}]}',
    b'{"candidates":[{"summary":"ok","source_refs":[-1]}]}',
    b'{"candidates":[{"summary":"ok","source_refs":[999]}]}',
    b'{"candidates":[{"summary":"ok","source_refs":[0,0]}]}',
    b'{"candidates":[{"summary":"ok","source_refs":[]}]}',
    b'{"candidates":[{"summary":"ok","source_refs":[0],"repo":"evil"}]}',
    b'{"candidates":[{"summary":"ok","source_refs":[0]}],"publication_authorized":true}',
    b'{"candidates":[{"summary":"ok","summary":"changed","source_refs":[0]}]}',
    b'{"candidates":[{"summary":"ok\\nmerged","source_refs":[0]}]}',
    b'{"candidates":[{"summary":"ok\\u2028merged","source_refs":[0]}]}',
    b'{"candidates":[{"summary":"ok","source_refs":[0]},{"summary":"ok","source_refs":[0]}]}',
    b'{"candidates":{}}',
    b'{"candidates":',
])
def test_c1_model_candidate_rejects_adversarial_response(
    source_fixture, reply,
):
    _, pub, _, _ = source_fixture
    ctx = c1.build_witnessed_entity_contexts(pub.index_sha256).contexts[0]
    with pytest.raises(c1.DailyWitnessedContextError):
        c1.normalize_witnessed_entity_candidates(
            ctx, reply, capture_sha256=pub.index_sha256,
        )


def test_c1_no_model_or_writer_calls_on_read(source_fixture, monkeypatch):
    _, pub, _, _ = source_fixture
    def forbidden(*args, **kwargs):
        raise AssertionError("C1 cannot make an external effect")
    monkeypatch.setattr(
        "obsidian_automation.github_daily_source_route.collect_fresh_verified_routes",
        forbidden,
    )
    monkeypatch.setattr(
        "obsidian_automation.github_daily_activity.collect_daily_evidence",
        forbidden,
    )
    monkeypatch.setattr(
        "obsidian_automation.artifact_lifecycle._store_immutable",
        forbidden,
    )
    output = c1.build_witnessed_entity_contexts(pub.index_sha256)
    assert output.contexts
    c1.normalize_witnessed_entity_candidates(
        output.contexts[0], _model([]), capture_sha256=pub.index_sha256,
    )


def test_c1_historical_capture_not_todays_publication(source_fixture):
    _, pub, _, _ = source_fixture
    out = c1.build_witnessed_entity_contexts(pub.index_sha256)
    assert out.date == "2026-10-05"
    assert out.collection_mode == "explicit_historical_reconfirmation"
    assert not out.publication_authorized
    assert all(json.loads(ctx.to_json_bytes())["publication_authorized"] is False
               for ctx in out.contexts)


def test_c1_batch_capacity_and_empty_claims_do_not_erase_evidence(
    source_fixture,
):
    _, pub, _, _ = source_fixture
    baseline = c1.build_witnessed_entity_contexts(pub.index_sha256)
    # A small size budget is either an exact deterministic split or a
    # typed hard failure, never silent event truncation.
    for budget in (1024, 1600, 2400):
        try:
            shrunk = c1.build_witnessed_entity_contexts(
                pub.index_sha256, max_bytes=budget,
            )
        except c1.DailyWitnessedContextError:
            continue
        assert shrunk.grouped_event_count == baseline.grouped_event_count
        assert {eid for ctx in shrunk.contexts for eid in ctx.evidence_ids} == {
            eid for ctx in baseline.contexts for eid in ctx.evidence_ids
        }
        assert all(len(ctx.to_json_bytes()) <= budget for ctx in shrunk.contexts)


def test_c1_prevents_context_self_hash_forgeries(source_fixture):
    _, pub, _, _ = source_fixture
    output = c1.build_witnessed_entity_contexts(pub.index_sha256)
    ctx = output.contexts[0]
    first = dict(ctx.events[0])
    first["actor"] = "forged-actor"
    # Even if the attacker rehashes the candidate Context, source ref must be
    # reconstructed from the trusted original CAS before a reply is processed.
    altered = replace(ctx, events=(first,) + ctx.events[1:])
    with pytest.raises(c1.DailyWitnessedContextError):
        c1.normalize_witnessed_entity_candidates(
            altered, _model([]), capture_sha256=pub.index_sha256,
        )
