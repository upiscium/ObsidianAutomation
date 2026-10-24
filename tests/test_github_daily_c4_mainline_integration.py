"""Isolated C4 producer-to-mainline deterministic digest compatibility (#444).

These synthetic GitHub fixtures never perform network I/O, create trusted
Collector credentials, publish a canonical Daily, or exercise a live UID/ACL.
"""
from datetime import datetime, timezone
import json

import pytest

from obsidian_automation import github_daily_activity as activity
from obsidian_automation import github_daily_source_route as route
from obsidian_automation import github_daily_temporal_evidence as temporal
from obsidian_automation import github_daily_route_capture_index as capture
from obsidian_automation import github_daily_verified_source as verified
from obsidian_automation import github_daily_deterministic_digest as digest
from test_github_daily_source_route import DATE, _config, _FakeClient


@pytest.mark.parametrize('v3', [False, True], ids=['v2-legacy', 'v3-temporal'])
def test_fresh_route_to_mainline_source_digest(tmp_path, monkeypatch, v3):
    monkeypatch.setattr(route, '_utc_now',
                        lambda: datetime(2026, 10, 6, 1, tzinfo=timezone.utc))
    client = _FakeClient(visible_pr=True, include_commit=True,
                         include_review_comment=True)
    source = route.collect_fresh_verified_routes(
        _config(tmp_path), target_date=DATE, client=client,
        period_policy=temporal.POLICY if v3 else None)
    index = capture.build_capture_index(source)
    assert index.sha256 == verified._sha(index.canonical_bytes)
    admitted = verified._verify(index.canonical_bytes,
                                source.evidence.canonical_bytes,
                                source.receipt_bytes, index.sha256)
    result = digest._build(admitted.bundle, index.sha256)
    assert result.date == DATE.isoformat()
    assert result.capture_sha256 == index.sha256
    assert result.original_event_count == len(source.evidence.events)
    assert admitted.witnessed_count == source.route_count
    assert len(result.items) >= 1
    assert result.report()['model_used'] is False
    assert result.report()['publication_authorized'] is False
    assert 'https://github.com/upiscium/Test/issues/1' in result.render()
    assert json.loads(source.receipt_bytes)['record_version'] == (3 if v3 else 2)
    assert bool('temporal_observation' in admitted.bundle.events[0]) is v3


def test_v1_event_json_without_temporal_proof_preserves_legacy_shape():
    row = activity.EvidenceEvent(
        evidence_id='a' * 64, kind='issue_comment', repository='Example/Alpha',
        occurred_at='2026-10-05T02:00:00Z',
        url='https://github.com/Example/Alpha/issues/1#issuecomment-2',
        actor='human', entity_type='issue_comment', number=1,
        source_id='2', sha=None, state=None, draft=None,
        title=None, body=None, message=None)
    raw = row.to_json()
    assert len(raw) == 15
    assert 'temporal_observation' not in raw
    updated = activity.EvidenceEvent(**{**row.__dict__, 'temporal_observation':
        {'policy': 'github-day-snapshot-v1', 'status': 'withheld_unknown_modification_time'}})
    assert 'temporal_observation' in updated.to_json()
    assert row.to_json() == raw


def test_fake_v3_receipt_cannot_be_accepted_by_legacy_v2_header(tmp_path, monkeypatch):
    monkeypatch.setattr(route, '_utc_now',
                        lambda: datetime(2026, 10, 6, 1, tzinfo=timezone.utc))
    source = route.collect_fresh_verified_routes(
        _config(tmp_path), target_date=DATE,
        client=_FakeClient(visible_pr=True), period_policy=temporal.POLICY)
    index = capture.build_capture_index(source)
    good = verified._verify(index.canonical_bytes, source.evidence.canonical_bytes,
                            source.receipt_bytes, index.sha256)
    assert len(good.bundle.events) == len(source.evidence.events)
    # A different, self-consistent v2 header must not be allowed to attest a
    # v3-projected row; evidence identity is fixed to original source bytes.
    tampered = source.receipt_bytes.replace(b'"record_version":3', b'"record_version":2')
    with pytest.raises(verified.VerifiedSourceError):
        verified._verify(index.canonical_bytes, source.evidence.canonical_bytes,
                         tampered, index.sha256)
