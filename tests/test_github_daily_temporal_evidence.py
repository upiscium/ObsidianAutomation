"""Period-scoped source snapshots; no live GitHub or production filesystem."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from types import SimpleNamespace

import pytest

from obsidian_automation import github_daily_activity as activity
from obsidian_automation import github_daily_source_route as source
from obsidian_automation import github_daily_temporal_evidence as temporal
from obsidian_automation import github_daily_route_capture_index as index
from obsidian_automation import github_daily_route_cas as cas
from obsidian_automation import github_daily_witnessed_entity_context as c1
from obsidian_automation import github_daily_narrative_runtime as runtime
from test_github_daily_source_route import _FakeClient, _config, DATE, BASE, REPO
from test_github_daily_route_cas import safe_layout

NOW = datetime(2026, 10, 6, 1, tzinfo=timezone.utc)
LATER = "2026-10-05T15:00:00Z"  # exact next JST day, exclusive upper bound


def encoded(value):
    return activity._canonical_json_bytes(value)


class Client(_FakeClient):
    def __init__(self):
        super().__init__(visible_pr=True, include_commit=True, include_review_comment=True)
        self.calls = []
        self.issue_update = "2026-10-05T01:00:00Z"
        self.lifecycle = False
        self.review = False
        for k in (f"{BASE}/issues/comments/31", f"{BASE}/issues/comments/33", f"{BASE}/pulls/comments/51"):
            self.responses[k]["updated_at"] = self.responses[k]["created_at"]

    def _paged(self, path):
        self.calls.append(path)
        if "/issues/comments?" in path:
            return [deepcopy(self.responses[f"{BASE}/issues/comments/{n}"]) for n in (31, 33)]
        if "/pulls/comments?" in path:
            return [deepcopy(self.responses[f"{BASE}/pulls/comments/51"])]
        if path.endswith('/issues/2/events') and self.lifecycle:
            return [{"id": 555, "event": "closed", "created_at": "2026-10-05T05:00:00Z",
                     "issue": {"number": 2, "pull_request": {}, "html_url": f"https://github.com/{REPO}/pull/2",
                               "title": "Later changed title", "state": "open"}, "actor": {"login": "human"}}]
        if path.endswith('/pulls/2/reviews') and self.review:
            return [{"id": 66, "submitted_at": "2026-10-05T05:10:00Z", "state": "DISMISSED",
                     "body": "Later review body", "html_url": f"https://github.com/{REPO}/pull/2#pullrequestreview-66",
                     "user": {"login": "human"}}]
        rows = super()._paged(path)
        if "/issues?" in path:
            for row in rows:
                if row['number'] == 2:
                    row['updated_at'] = self.issue_update
                    row.update(title="Current title", body="Later completion or deployment", state="closed", draft=False)
        return rows

    def _request_json(self, path):
        self.calls.append(path)
        return super()._request_json(path)


def collect(tmp_path, monkeypatch, client=None, *, policy=temporal.POLICY):
    monkeypatch.setattr(source, '_utc_now', lambda: NOW)
    return source.collect_fresh_verified_routes(_config(tmp_path), target_date=DATE,
        client=client or Client(), period_policy=policy)


def row(result, kind, source_id=None):
    return next(e.to_json() for e in result.evidence.events
                if e.kind == kind and (source_id is None or e.source_id == source_id))


def test_later_pr_body_title_state_draft_are_not_backdated(tmp_path, monkeypatch):
    client = Client(); client.issue_update = LATER
    result = collect(tmp_path, monkeypatch, client)
    event = row(result, 'pull_request_created')
    assert event['occurred_at'] == '2026-10-05T01:00:00Z'
    assert event['number'] == 2 and event['url'].endswith('/pull/2')
    assert all(event[k] is None for k in ('body', 'title', 'state', 'draft'))
    meta = event['temporal_observation']
    assert meta['status'] == 'withheld_outside_target_day'
    assert meta['resource_updated_at'] == LATER
    assert meta['withheld_fields'] == ['body', 'draft', 'state', 'title']
    receipt = json.loads(result.receipt_bytes)
    assert receipt['record_version'] == 3 and receipt['temporal_policy'] == temporal.POLICY
    saved = next(r for r in receipt['temporal_observations'] if r['evidence_id'] == event['evidence_id'])
    assert saved['original_event']['body']['text'] == 'Later completion or deployment'
    assert saved['original_event']['draft'] is False
    assert saved['original_event']['state'] == 'closed'
    assert len(receipt['temporal_observations']) == len(result.evidence.events)
    assert source.parse_verified_routes(result.receipt_bytes, result.evidence.canonical_bytes)
    assert json.loads(index.build_capture_index(result).canonical_bytes)['source_receipt_version'] == 3


@pytest.mark.parametrize('updated,status', [
    ('2026-10-05T01:00:00Z','day_snapshot_not_event_instant'),
    ('2026-10-05T14:59:59.999999Z','day_snapshot_not_event_instant'),
    (LATER,'withheld_outside_target_day'),
    ('2026-10-05T16:00:00Z','withheld_outside_target_day'),
])
def test_JST_exclusive_boundary_not_string_order(tmp_path, monkeypatch, updated, status):
    client = Client();client.issue_update=updated
    event = row(collect(tmp_path, monkeypatch, client), 'pull_request_created')
    assert event['temporal_observation']['status'] == status
    assert (event['body'] is None) == (status != 'day_snapshot_not_event_instant')
    assert not event['temporal_observation']['historical_instant_verified']


@pytest.mark.parametrize('path,kind,sid', [
    ('issues/comments/31', 'issue_comment', '31'),
    ('issues/comments/33', 'pull_request_comment', '33'),
    ('pulls/comments/51', 'pull_request_review_comment', '51'),
])
@pytest.mark.parametrize('updated', [None, LATER])
def test_edited_or_unknown_comment_time_keeps_identity_without_body(tmp_path, monkeypatch, path, kind, sid, updated):
    client = Client();client.responses[f'{BASE}/{path}']['updated_at']=updated
    result=collect(tmp_path, monkeypatch, client)
    event=row(result,kind,sid)
    assert event['body'] is None and event['source_id']==sid
    routes=source.parse_verified_routes(result.receipt_bytes, result.evidence.canonical_bytes)
    proof=next(r for r in routes if r['evidence_id']==event['evidence_id'])
    assert proof['source_id']==sid and proof['number']==event['number']
    assert event['temporal_observation']['withheld_fields']==['body']
    assert not event['temporal_observation']['historical_instant_verified']


def test_lifecycle_action_does_not_copy_later_nested_state(tmp_path,monkeypatch):
    client=Client();client.lifecycle=True;client.review=True
    result=collect(tmp_path,monkeypatch,client)
    event=row(result,'pull_request_closed')
    assert event['state'] is None and event['title'] is None
    assert event['temporal_observation']['status']=='event_transition_only'
    review=row(result,'pull_request_review')
    assert review['body'] is None and review['state'] is None
    assert review['temporal_observation']['status']=='withheld_unknown_modification_time'
    commit=row(result,'pull_request_commit')
    assert commit['message']['text']=='fix issue source verification'
    assert commit['temporal_observation']['status']=='content_addressed_commit_not_push_time'


@pytest.mark.parametrize('updated', ['invalid', True, {}, '2026-10-04T00:00:00Z', '2026-10-07T00:00:00Z'])
def test_invalid_or_inconsistent_update_cannot_be_admitted(tmp_path,monkeypatch,updated):
    client=Client();client.responses[f'{BASE}/issues/comments/31']['updated_at']=updated
    with pytest.raises((temporal.TemporalEvidenceError,source.DailySourceRouteError)):
        collect(tmp_path,monkeypatch,client)


def test_changed_update_time_with_identical_text_is_not_a_stable_recheck(tmp_path,monkeypatch):
    class Changing(Client):
        def _request_json(self,path):
            value=super()._request_json(path)
            if path==f'{BASE}/issues/comments/31':value['updated_at']=LATER
            return value
    with pytest.raises(temporal.TemporalEvidenceError,match='changed during'):
        collect(tmp_path,monkeypatch,Changing())


def test_receipt_temporal_projection_rejects_forged_values_and_downgrade(tmp_path,monkeypatch):
    client=Client();client.issue_update=LATER
    result=collect(tmp_path,monkeypatch,client)
    saved=json.loads(result.receipt_bytes)
    for change in ('time','original','missing','duplicate','policy','order'):
        receipt=deepcopy(saved)
        if change=='time':receipt['temporal_observations'][0]['observation']['updated_at']='2026-10-05T09:00:00Z'
        if change=='original':receipt['temporal_observations'][0]['original_event']['actor']='invented'
        if change=='missing':receipt['temporal_observations'].pop()
        if change=='duplicate':receipt['temporal_observations'][0]=receipt['temporal_observations'][1]
        if change=='policy':receipt['temporal_policy']='made-up'
        if change=='order':receipt['temporal_observations'].reverse()
        with pytest.raises(source.DailySourceRouteError):
            source.parse_verified_routes(encoded(receipt),result.evidence.canonical_bytes)
    receipt=deepcopy(saved);receipt['record_version']=2
    for k in ('temporal_policy','temporal_observations'):receipt.pop(k)
    with pytest.raises(source.DailySourceRouteError,match='legacy receipt'):
        source.parse_verified_routes(encoded(receipt),result.evidence.canonical_bytes)


def test_exact_canonical_projection_does_not_accept_bool_integer_substitution(tmp_path,monkeypatch):
    result=collect(tmp_path,monkeypatch)
    receipt=json.loads(result.receipt_bytes);events=json.loads(result.evidence.canonical_bytes)['events']
    event=deepcopy(events[0]);event['temporal_observation']['historical_instant_verified']=0
    raw={k:v for k,v in event.items() if k!='evidence_id'}
    old=event['evidence_id'];event['evidence_id']=hashlib.sha256(encoded(raw)).hexdigest()
    record=next(r for r in receipt['temporal_observations'] if r['evidence_id']==old)
    record['evidence_id']=event['evidence_id']
    with pytest.raises(temporal.TemporalEvidenceError,match='exact projection'):
        temporal.validate_records([record],[event],start=activity._date_window(DATE)[0],end=activity._date_window(DATE)[1])


def test_opt_in_keeps_legacy_bytes_and_network_paths_unchanged(tmp_path,monkeypatch):
    one,two=Client(),Client()
    legacy=collect(tmp_path/'legacy',monkeypatch,one,policy=None)
    current=collect(tmp_path/'new',monkeypatch,two)
    assert one.calls==two.calls
    assert json.loads(legacy.receipt_bytes)['record_version']==2
    assert all('temporal_observation' not in e.to_json() for e in legacy.evidence.events)
    assert json.loads(index.build_capture_index(legacy).canonical_bytes)['source_receipt_version']==2
    assert len(current.evidence.events)==len(legacy.evidence.events)
    assert all(e.temporal_observation is not None for e in current.evidence.events)
    assert current.evidence.sha256!=legacy.evidence.sha256


def test_period_aware_actual_cas_C1_and_private_review_dont_expose_withheld_text(tmp_path,monkeypatch,safe_layout):
    monkeypatch.setattr(cas,'_utc_now',lambda:NOW)
    client=Client();client.issue_update=LATER
    result=collect(tmp_path,monkeypatch,client)
    published=cas.publish_fresh_capture(result)
    stored=cas.read_verified_capture(published.index_sha256)
    assert stored.evidence_bytes==result.evidence.canonical_bytes
    assert json.loads(stored.capture_index.canonical_bytes)['source_receipt_version']==3
    contexts=c1.build_witnessed_entity_contexts(published.index_sha256)
    assert b'Later completion or deployment' not in b''.join(c.to_json_bytes() for c in contexts.contexts)
    assert 'Later completion or deployment' in result.receipt_bytes.decode()
    event=row(result,'pull_request_created');entity={'repository':REPO,'entity_type':'pull_request','number':2}
    snapshot=runtime._review_snapshot([{'context_sha256':'a'*64,'context':{'entity':entity,'sources':[{'source_ref':0,'original_event':event}]}}],'b'*64)
    data={'source_context_set_sha256':'b'*64,'capture_sha256':published.index_sha256,'entities':[{'context_sha256':'a'*64,'entity':entity,'sentences':[]}]}
    preview=SimpleNamespace(report=lambda:data,sha256='c'*64,markdown='Private')
    view=runtime._build_source_review(snapshot,preview)
    assert '要約根拠から保留' in view.markdown and '当日時点' in view.markdown
    assert 'Later completion or deployment' not in view.markdown
    assert view.report()['entities'][0]['temporal_observations'][0]['observation']==event['temporal_observation']
    assert view.report()['semantic_coverage_verified'] is False


@pytest.mark.parametrize('bad',[True,[],{},'future-policy'])
def test_unknown_policy_refused_before_API(tmp_path,monkeypatch,bad):
    client=Client()
    with pytest.raises(source.DailySourceRouteError,match='policy'):
        collect(tmp_path,monkeypatch,client,policy=bad)
    assert not client.calls


@pytest.mark.parametrize('bad_time',[None, {}, 'not-a-timestamp'])
def test_rehashed_malformed_original_time_is_a_typed_route_rejection(tmp_path,monkeypatch,bad_time):
    result=collect(tmp_path,monkeypatch)
    receipt=json.loads(result.receipt_bytes);original=receipt['temporal_observations'][0]['original_event']
    original['occurred_at']=bad_time
    original['evidence_id']=hashlib.sha256(encoded({k:v for k,v in original.items() if k!='evidence_id'})).hexdigest()
    with pytest.raises(source.DailySourceRouteError,match='invalid temporal source projection'):
        source.parse_verified_routes(encoded(receipt),result.evidence.canonical_bytes)


def test_projection_event_count_checked_before_retaining_records(tmp_path,monkeypatch):
    legacy=collect(tmp_path,monkeypatch,policy=None)
    monkeypatch.setattr(temporal,'MAX_OBSERVATIONS',1)
    def forbidden(event):raise AssertionError('must reject event count before projection')
    with pytest.raises(temporal.TemporalEvidenceError,match='event count limit'):
        temporal.project_events(list(legacy.evidence.events),SimpleNamespace(for_event=forbidden),
                                start=activity._date_window(DATE)[0],end=activity._date_window(DATE)[1])


def test_receipt_byte_budget_stops_before_encoding_later_members(monkeypatch):
    monkeypatch.setattr(temporal,'MAX_RECEIPT_BYTES',100)
    # A later unserializable object must never be visited after the byte cap.
    with pytest.raises(temporal.TemporalEvidenceError,match='byte limit'):
        temporal.encode_receipt({'a':'x'*1000,'z':object()})
    small={'a':[1,False,None,'日本語']}
    assert temporal.encode_receipt(small)==encoded(small)


def test_projection_enforces_original_record_budget_incrementally(tmp_path,monkeypatch):
    monkeypatch.setattr(temporal,'MAX_RECEIPT_BYTES',500)
    with pytest.raises(temporal.TemporalEvidenceError,match='byte limit'):
        collect(tmp_path,monkeypatch)


def test_wrapper_charges_commit_and_timeline_rows_not_only_observation_map(monkeypatch):
    monkeypatch.setattr(temporal,'MAX_SOURCE_ROWS',3)
    raw=SimpleNamespace(_paged=lambda p:[{'sha':'a'*40}]*2,
                        _request_json=lambda p:[{'sha':'b'*40}]*2)
    wrapped=temporal.ObservationClient(raw,lambda:NOW)
    wrapped._paged(f'{BASE}/issues/2/events')
    assert wrapped.rows_seen==2 and not wrapped.observations
    with pytest.raises(temporal.TemporalEvidenceError,match='source row limit'):
        wrapped._request_json(f'{BASE}/pulls/2/commits?per_page=100&page=1')
    assert wrapped.rows_seen==2


def test_wrapper_bounds_aggregate_returned_source_bytes(monkeypatch):
    monkeypatch.setattr(temporal,'MAX_SOURCE_BYTES',100)
    raw=SimpleNamespace(_paged=lambda p:[{'body':'x'*200}])
    wrapped=temporal.ObservationClient(raw,lambda:NOW)
    with pytest.raises(temporal.TemporalEvidenceError,match='byte limit'):
        wrapped._paged(f'{BASE}/issues/2/events')
    assert wrapped.rows_seen==0 and wrapped.bytes_seen==0
