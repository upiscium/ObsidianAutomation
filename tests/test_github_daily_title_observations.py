"""#444 v4 current-title provenance from bounded pinned API (no network)."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json

import pytest

from obsidian_automation import github_daily_activity as activity
from obsidian_automation import github_daily_source_route as route
from obsidian_automation import github_daily_route_capture_index as index
from obsidian_automation import github_daily_standard_source as standard
from obsidian_automation import github_daily_verified_source as verifier
from obsidian_automation import github_daily_title_observations as titles
from obsidian_automation import github_daily_deterministic_digest as digest
from obsidian_automation.github_project_watcher import GitHubAPIHTTPError
from test_github_daily_source_route import _FakeClient, _config, DATE, BASE
from test_github_daily_standard_source import private_dirs

OBSERVED = datetime(2026, 10, 6, 1, tzinfo=timezone.utc)
TITLE = "Current PR two title (observed Oct 6)"


def client_without_pr_snapshot():
    source = _FakeClient(visible_pr=False, include_commit=True, include_review_comment=True)
    source.responses[f"{BASE}/issues/2"]["title"] = TITLE
    return source


def collect(tmp_path, monkeypatch, source=None):
    monkeypatch.setattr(route, "_utc_now", lambda: OBSERVED)
    source = source or client_without_pr_snapshot()
    return route.collect_fresh_verified_routes(
        _config(tmp_path), target_date=DATE, client=source,
        period_policy=route.temporal.POLICY,
        explicit_historical_reconfirmation=True,
    )


def capture_tuple(result):
    cap = index.build_capture_index(result)
    return cap, verifier._verify(
        cap.canonical_bytes, result.evidence.canonical_bytes,
        result.receipt_bytes, cap.sha256,
    )


def test_comment_only_pr_observes_real_current_title_without_fake_dated_event(tmp_path, monkeypatch):
    result = collect(tmp_path, monkeypatch)
    receipt = json.loads(result.receipt_bytes)
    assert receipt["record_version"] == titles.RECEIPT_VERSION
    observed = receipt["entity_title_observations"]
    assert len(observed) == 1
    assert observed[0] == {
        "repository": "upiscium/Test", "entity_type": "pull_request",
        "number": 2, "route": f"{BASE}/issues/2",
        "observed_at_utc": "2026-10-06T01:00:00Z", "title": TITLE,
    }
    assert result.evidence.target_date == "2026-10-05"
    assert all(getattr(e, "title") is None for e in result.evidence.events
               if e.kind in ("pull_request_comment", "pull_request_commit"))
    idx, admitted = capture_tuple(result)
    assert admitted.title_observations == tuple(observed)
    plain = digest._build(admitted.bundle, idx.sha256)
    assert TITLE not in plain.render()
    updated = digest._build_admitted_source(admitted)
    assert "[PR #2](https://github.com/upiscium/Test/pull/2): Current PR two title \\(observed Oct 6\\)" in updated.render()
    target = next(v for v in updated.items if v["entity"]["number"] == 2)
    assert target["title_source"] == "collection_observation"
    assert target["title_observed_at_utc"] == "2026-10-06T01:00:00Z"
    assert target["status"] != "merged"
    assert updated.report()["verified_cas_source"] is True
    assert updated.report()["publication_authorized"] is False
    assert len(updated.items) == len(plain.items)
    for a, b in zip(plain.items, updated.items):
        for field in ("entity","status","sentences","evidence_ids","source_event_count"):
            assert a[field] == b[field]


def test_private_standard_read_uses_v4_and_repeated_collection_is_immutable(tmp_path, monkeypatch):
    monkeypatch.setattr(route, "_utc_now", lambda: OBSERVED)
    store = private_dirs(tmp_path)
    conf = _config(tmp_path)
    sha = standard.collect_standard_source(store, conf, client_without_pr_snapshot(), DATE)
    next_sha = standard.collect_standard_source(store, conf, client_without_pr_snapshot(), DATE)
    assert sha == next_sha
    loaded = standard.read_standard_digest(store, sha)
    assert "Current PR two title \\(observed Oct 6\\)" in loaded.render()
    assert len(loaded.items) == 2
    assert loaded.report()["record_version"] == "github-daily-deterministic-digest-v3"
    assert loaded.report()["verified_cas_source"] is True
    assert len(list((store/"receipt").iterdir())) == 1


@pytest.mark.parametrize("change", [
    {"title": ""},
    {"title": " "},
    {"title": "w" * (titles.MAX_TITLE_BYTES + 1)},
    {"repository": "evil/repository"},
    {"entity_type": "issue"},
    {"number": 3},
    {"route": "/repos/upiscium/Test/issues/999"},
    {"route": "https://evil.example/issues/2"},
    {"observed_at_utc": "2026-10-05T01:00:00Z"},
    {"observed_at_utc": "2026-10-06T04:00:00Z"},
    {"observed_at_utc": "2026-10-06T01:00:00+00:00"},
])
def test_invalid_v4_title_receipt_rejected_even_with_recomputed_hash(tmp_path, monkeypatch, change):
    collection = collect(tmp_path, monkeypatch)
    raw = json.loads(collection.receipt_bytes)
    raw["entity_title_observations"][0].update(change)
    candidate = activity._canonical_json_bytes(raw)
    with pytest.raises(route.DailySourceRouteError):
        route.parse_verified_routes(candidate, collection.evidence.canonical_bytes)
    # Re-signing a JSON digest with a new hash is not sufficient to admit an
    # invalid typed source observation.
    with pytest.raises(index.DailyCaptureIndexError):
        index._validated_index(collection.evidence.canonical_bytes,candidate)


def test_repeated_or_out_of_order_records_cannot_be_admitted(tmp_path, monkeypatch):
    collection = collect(tmp_path, monkeypatch)
    raw = json.loads(collection.receipt_bytes)
    raw["entity_title_observations"].append(deepcopy(raw["entity_title_observations"][0]))
    with pytest.raises(route.DailySourceRouteError):
        route.parse_verified_routes(activity._canonical_json_bytes(raw),
                                    collection.evidence.canonical_bytes)


def test_current_api_title_cannot_override_existing_projected_event_title(tmp_path, monkeypatch):
    source = _FakeClient(visible_pr=True, include_commit=True, include_review_comment=True)
    source.responses[f"{BASE}/issues/2"]["title"] = "Mismatched current label"
    collection = collect(tmp_path, monkeypatch, source)
    receipt = json.loads(collection.receipt_bytes)
    # PR created on the dated event and the observed title is already in
    # original source: no sideband record is necessary or allowed.
    assert receipt["entity_title_observations"] == []
    idx, admitted = capture_tuple(collection)
    result = digest._build_admitted_source(admitted)
    assert "PR two" in result.render()
    assert "Mismatched current label" not in result.render()
    raw = json.loads(collection.receipt_bytes)
    raw["entity_title_observations"] = [{
        "repository":"upiscium/Test","entity_type":"pull_request","number":2,
        "route":f"{BASE}/issues/2","title":"Mismatched current label",
        "observed_at_utc":"2026-10-06T01:00:00Z",
    }]
    with pytest.raises(route.DailySourceRouteError):
        route.parse_verified_routes(activity._canonical_json_bytes(raw),
                                    collection.evidence.canonical_bytes)


def test_optional_title_endpoint_http_failure_preserves_original_source(tmp_path,monkeypatch):
    class FailingSecondRead(_FakeClient):
        def __init__(self):
            super().__init__(visible_pr=False,include_commit=True,include_review_comment=True)
            self.count = 0
        def _request_json(self,path):
            if path == f"{BASE}/issues/2":
                self.count += 1
                if self.count == 2:
                    raise GitHubAPIHTTPError(status=429,path=path,detail="fixture rate limit")
            return super()._request_json(path)
    client = FailingSecondRead()
    collection = collect(tmp_path,monkeypatch,client)
    assert client.count == 1  # Cached Issue classification response, no repeat GET.
    assert json.loads(collection.receipt_bytes)["entity_title_observations"] == []
    # Exercise optional fallback without the validated classification cache,
    # as for a PR known only from its commit/review route.
    extras = route._collect_missing_entity_titles(
        route.temporal.ObservationClient(client, route._utc_now),
        list(collection.evidence.events),
        captured_at_utc="2026-10-06T01:00:00Z",
        cached_issues={})
    assert client.count == 2
    assert extras == []
    idx, admitted = capture_tuple(collection)
    out = digest._build(admitted.bundle,idx.sha256)
    assert len(out.items)==2
    assert "[PR #2](https://github.com/upiscium/Test/pull/2): タイトル未取得" in out.render()


def test_identity_mismatch_fail_closed_not_silently_corrected(tmp_path,monkeypatch):
    class ChangedNumberSecondRead(_FakeClient):
        def __init__(self):
            super().__init__(visible_pr=False,include_commit=True,include_review_comment=True)
            self.count=0
        def _request_json(self,path):
            x=super()._request_json(path)
            if path==f"{BASE}/issues/2":
                self.count += 1
                if self.count==2:
                    x["number"]=999
            return x
    client = ChangedNumberSecondRead()
    collected = collect(tmp_path,monkeypatch,client)
    assert client.count == 1
    with pytest.raises(route.DailySourceRouteError,match="title source"):
        route._collect_missing_entity_titles(client, list(collected.evidence.events),
            captured_at_utc="2026-10-06T01:00:00Z", cached_issues={})
    assert client.count == 2


def test_unverified_bundle_cannot_accept_additional_titles(tmp_path, monkeypatch):
    collection = collect(tmp_path, monkeypatch)
    receipt = json.loads(collection.receipt_bytes)
    original = collection.evidence
    from obsidian_automation.github_daily_summary import parse_evidence_bundle
    bundle = parse_evidence_bundle(original.canonical_bytes)
    forged_titles = tuple(receipt["entity_title_observations"])
    with pytest.raises(TypeError,match="title_observations"):
        digest._build(bundle,"0"*64,title_observations=forged_titles)
    fake_source = verifier.VerifiedSource(
        "0"*64,bundle,1,0,forged_titles)
    with pytest.raises(digest.DeterministicDigestError,match="admitted"):
        digest._build_admitted_source(fake_source)
    # The admitted receipt cannot be changed after verification while
    # retaining the original gate and per-capture observation fingerprint.
    from dataclasses import replace
    collected=collect(tmp_path,monkeypatch)
    _, authentic=capture_tuple(collected)
    forged=replace(authentic,title_observations=({
        **authentic.title_observations[0],
        'title':'Forged summary, not observed at the GitHub endpoint',
    },))
    with pytest.raises(digest.DeterministicDigestError,match="admitted"):
        digest._build_admitted_source(forged)


def test_title_html_and_markdown_still_escaped_by_v3_digest(tmp_path, monkeypatch):
    special = "<img onerror=alert(1)> [evil](https://attacker.invalid)"
    source = client_without_pr_snapshot()
    source.responses[f"{BASE}/issues/2"]["title"]=special
    collection = collect(tmp_path,monkeypatch,source)
    idx,admitted = capture_tuple(collection)
    result=digest._build_admitted_source(admitted)
    text=result.render()
    assert "&lt;img" in text and "<img" not in text
    assert r"\[evil\]\(https://attacker.invalid\)" in text
    assert "https://github.com/upiscium/Test/pull/2" in text


def test_comment_classification_title_reuses_one_existing_api_request(tmp_path, monkeypatch):
    class CountingClient(_FakeClient):
        def __init__(self):
            super().__init__(visible_pr=False, include_commit=True, include_review_comment=True)
            self.responses[f"{BASE}/issues/2"]["title"] = TITLE
            self.count = 0
        def _request_json(self, path):
            if path == f"{BASE}/issues/2":
                self.count += 1
            return super()._request_json(path)
    client = CountingClient()
    result = collect(tmp_path,monkeypatch,client)
    assert client.count == 1
    assert json.loads(result.receipt_bytes)["entity_title_observations"][0]["title"] == TITLE
    idx, verified = capture_tuple(result)
    out = digest._build_admitted_source(verified)
    assert out.report()["items"][-1]["title_source"] == "collection_observation"


def test_v4_sideband_cannot_be_forged_in_v3_receipt(tmp_path, monkeypatch):
    result=collect(tmp_path,monkeypatch)
    r=json.loads(result.receipt_bytes)
    assert r["record_version"] == 4
    r["record_version"]=3
    with pytest.raises(route.DailySourceRouteError,match="version/evidence binding"):
        route.parse_verified_routes(activity._canonical_json_bytes(r),
                                    result.evidence.canonical_bytes)


def test_title_lookup_public_identity_fails_closed(tmp_path, monkeypatch):
    class SpoofedPublicUrl(_FakeClient):
        def __init__(self):
            super().__init__(visible_pr=False,include_commit=True,include_review_comment=True)
            self.count = 0
        def _request_json(self,path):
            x=super()._request_json(path)
            if path == f"{BASE}/issues/2":
                self.count += 1
                if self.count==2:
                    x["html_url"]="https://github.com/evil/example/pull/2"
            return x
    source=SpoofedPublicUrl()
    first=collect(tmp_path,monkeypatch,source)
    assert source.count==1
    with pytest.raises(route.DailySourceRouteError,match="public identity"):
        route._collect_missing_entity_titles(
            source, list(first.evidence.events),
            captured_at_utc="2026-10-06T01:00:00Z",cached_issues={})
    assert source.count==2


def test_bounded_number_of_title_observations(tmp_path,monkeypatch):
    result=collect(tmp_path,monkeypatch)
    rec=json.loads(result.receipt_bytes)
    assert rec["entity_title_observations"]
    one=rec["entity_title_observations"][0]
    rec["entity_title_observations"]=[dict(one,number=i) for i in range(1,130)]
    with pytest.raises(route.DailySourceRouteError,match="title"):
        route.parse_verified_routes(
            activity._canonical_json_bytes(rec),
            result.evidence.canonical_bytes)


def test_strict_capture_reader_admits_v4_titles_only_after_original_triplet_verification(
        tmp_path, monkeypatch):
    collection=collect(tmp_path,monkeypatch)
    ix=index.build_capture_index(collection)
    monkeypatch.setattr(verifier,'_capture',
        lambda digest_value: (
            ix.canonical_bytes,
            collection.evidence.canonical_bytes,
            collection.receipt_bytes)
        if digest_value==ix.sha256 else
        (_ for _ in ()).throw(verifier.VerifiedSourceError('no such capture')))
    out=digest.build_digest_from_verified_capture(ix.sha256)
    assert out.capture_sha256==ix.sha256
    assert out.report()['verified_cas_source'] is True
    assert out.report()['original_event_count']==len(collection.evidence.events)
    assert TITLE in out.items[-1]['title']
    assert out.items[-1]['title_source']=='collection_observation'
    assert out.items[-1]['title_observed_at_utc']=='2026-10-06T01:00:00Z'
    # This test fakes the private read only as an isolated fixture. The real
    # strict reader must enforce service UID, ACL and no-follow filesystem.
