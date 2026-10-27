"""Read-only admission of a collector-owned C4 capture and its complete source census.

Hashes bind bytes, not authors. The fixed host roles, no-follow directory chain and
ACLs are the local trust anchor. This module never collects from GitHub or publishes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import stat
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .github_daily_cas_admission import (
    _acl_from_fd, DailyCASAdmissionError, USER_OBJ, GROUP_OBJ, USER, MASK, OTHER,
)
from .github_daily_summary import EvidenceBundle, parse_evidence_bundle
from .github_daily_activity import _date_window
from . import github_daily_title_observations as titles

ROOT = Path('/var/lib/obsidian-github-pipeline/daily-progress/15-VerifiedSource')
TRUST_ANCHOR = Path('/')  # Only an isolated test fixture may override this.
OWNER = 'obsidian-github-collector'
READER = 'obsidian-github-summarizer'
SHA = re.compile(r'[0-9a-f]{64}\Z')
REPO = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z')
REQUIRED = {'issue_comment', 'pull_request_comment', 'pull_request_commit',
            'pull_request_review_comment'}
EVENT_FIELDS = {'evidence_id', 'kind', 'repository', 'occurred_at', 'url', 'actor',
                'entity_type', 'number', 'source_id', 'sha', 'state', 'draft',
                'title', 'body', 'message'}
MUTABLE = ('body', 'draft', 'message', 'state', 'title')
TEMPORAL_POLICY = 'github-day-snapshot-v1'
SIZES = {'capture': 4096, 'evidence': 32 * 1024 * 1024, 'receipt': 4 * 1024 * 1024}
SUFFIX = {'capture': 'capture-index', 'evidence': 'evidence', 'receipt': 'source-route'}
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


class VerifiedSourceError(ValueError):
    """Source provenance or the complete dated event census is unavailable."""


# Opaque in-process admission marker minted only by the receipt verifier.
# A synthetic VerifiedSource or arbitrary SHA cannot authorize title labels.
_TITLE_OBSERVATION_GATE = object()


@dataclass(frozen=True)
class VerifiedSource:
    capture_sha256: str
    bundle: EvidenceBundle
    witnessed_count: int
    unattributed_count: int
    title_observations: tuple[dict, ...] = ()
    _title_gate: object | None = field(default=None, repr=False, compare=False)
    _title_binding: str | None = field(default=None, repr=False, compare=False)


def _title_binding(capture: str, evidence_sha: str, rows: tuple[dict, ...]) -> str:
    # A mutation detector tied to this already admitted capture and Evidence.
    # This hash is not authentication; private-CAS admission and the gate are.
    return _sha(_canonical({
        'capture_sha256': capture, 'evidence_sha256': evidence_sha,
        'entity_title_observations': list(rows),
    }))


def admitted_title_observations(source: VerifiedSource) -> tuple[dict, ...]:
    """Only the exact validated v4 receipt can supply extra current titles."""
    if type(source) is not VerifiedSource:
        raise VerifiedSourceError('source is not a verified capture')
    if source.title_observations:
        if (source._title_gate is not _TITLE_OBSERVATION_GATE
                or source._title_binding != _title_binding(
                    source.capture_sha256, source.bundle.sha256,
                    source.title_observations)):
            raise VerifiedSourceError('supplemental titles lack verified source admission')
    return source.title_observations


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')


def _object(data: bytes) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise VerifiedSourceError('duplicate JSON property')
            result[key] = value
        return result
    obj = json.loads(data, object_pairs_hook=unique)
    if type(obj) is not dict or _canonical(obj) != data:
        raise VerifiedSourceError('noncanonical source object')
    return obj


def _acl(fd: int, owner: int, reader: int, *, directory: bool) -> None:
    keys = {(USER_OBJ, None), (GROUP_OBJ, None), (MASK, None),
            (OTHER, None), (USER, reader)}
    for attr, mask in ((('system.posix_acl_access', 5),
                        ('system.posix_acl_default', 7)) if directory else
                       (('system.posix_acl_access', 4),)):
        entries = _acl_from_fd(fd, attr=attr)
        if (set(entries) != keys or entries[(USER_OBJ, None)] != (7 if directory else 6)
                or entries[(GROUP_OBJ, None)] != 0 or entries[(OTHER, None)] != 0
                or entries[(MASK, None)] != mask
                or (directory and entries[(USER, reader)] != 5)):
            raise VerifiedSourceError('collector ACL mismatch')
        if not directory and (entries[(USER, reader)] & mask) != 4:
            raise VerifiedSourceError('collector reader ACL mismatch')


def _directory(fd: int, owner: int, group: int, reader: int) -> None:
    st = os.fstat(fd)
    if (not stat.S_ISDIR(st.st_mode) or st.st_uid != owner or st.st_gid != group
            or stat.S_IMODE(st.st_mode) != 0o750):
        raise VerifiedSourceError('collector directory identity mismatch')
    _acl(fd, owner, reader, directory=True)


def _read(fd: int, stage: str, digest: str, owner: int, group: int, reader: int) -> bytes:
    name = f'{digest}.github-daily-{SUFFIX[stage]}.json'
    handle = os.open(name, FILE_FLAGS, dir_fd=fd)
    try:
        before = os.fstat(handle)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_uid != owner or before.st_gid != group
                or stat.S_IMODE(before.st_mode) != 0o640
                or not 0 < before.st_size <= SIZES[stage]):
            raise VerifiedSourceError('collector object inode mismatch')
        _acl(handle, owner, reader, directory=False)
        with os.fdopen(os.dup(handle), 'rb') as stream:
            data = stream.read(SIZES[stage] + 1)
        after = os.fstat(handle)
        stable = lambda st: (st.st_dev, st.st_ino, st.st_mode, st.st_nlink,
                             st.st_uid, st.st_gid, st.st_size, st.st_mtime_ns,
                             st.st_ctime_ns)
        if (stable(after) != stable(before) or len(data) != before.st_size
                or _sha(data) != digest):
            raise VerifiedSourceError('collector object bytes mismatch')
        return data
    finally:
        os.close(handle)


def _capture(digest: str) -> tuple[bytes, bytes, bytes]:
    if type(digest) is not str or not SHA.fullmatch(digest):
        raise VerifiedSourceError('invalid capture SHA')
    owner = pwd.getpwnam(OWNER)
    reader = pwd.getpwnam(READER)
    if (owner.pw_uid == 0 or owner.pw_gid == 0 or reader.pw_uid == 0
            or reader.pw_uid == owner.pw_uid or os.geteuid() not in
            (reader.pw_uid, owner.pw_uid)):
        raise VerifiedSourceError('collector/reader role not admitted')
    # No caller-controlled root or symlinked component. The system root is
    # trusted; every non-root ancestor is non-group/world-writable and owned
    # by root or the designated collector.
    if (not ROOT.is_absolute() or '..' in ROOT.parts or ROOT == Path('/')
            or not TRUST_ANCHOR.is_absolute() or '..' in TRUST_ANCHOR.parts
            or ROOT == TRUST_ANCHOR or not ROOT.is_relative_to(TRUST_ANCHOR)):
        raise VerifiedSourceError('invalid fixed collector root')
    handles = []
    try:
        parent = os.open(TRUST_ANCHOR, DIR_FLAGS)
        handles.append(parent)
        for component in ROOT.relative_to(TRUST_ANCHOR).parts:
            child = os.open(component, DIR_FLAGS, dir_fd=parent)
            handles.append(child)
            st = os.fstat(child)
            if st.st_uid not in (0, owner.pw_uid) or stat.S_IMODE(st.st_mode) & 0o022:
                raise VerifiedSourceError('untrusted collector ancestor')
            parent = child
        _directory(parent, owner.pw_uid, owner.pw_gid, reader.pw_uid)
        stages = {}
        for stage in SUFFIX:
            child = os.open(stage, DIR_FLAGS, dir_fd=parent)
            handles.append(child)
            _directory(child, owner.pw_uid, owner.pw_gid, reader.pw_uid)
            stages[stage] = child
        index_bytes = _read(stages['capture'], 'capture', digest,
                            owner.pw_uid, owner.pw_gid, reader.pw_uid)
        index = _object(index_bytes)
        if (set(index) != {'record_version', 'source_receipt_version', 'date',
                           'evidence_sha256', 'route_receipt_sha256', 'route_count',
                           'collection_mode', 'captured_at_utc',
                           'producer_attestation_verified', 'publication_authorized'}
                or type(index['record_version']) is not int
                or index['record_version'] != 1
                or index['publication_authorized'] is not False
                or index['producer_attestation_verified'] is not False
                or type(index['evidence_sha256']) is not str
                or type(index['route_receipt_sha256']) is not str
                or not SHA.fullmatch(index['evidence_sha256'])
                or not SHA.fullmatch(index['route_receipt_sha256'])):
            raise VerifiedSourceError('capture index contract mismatch')
        evidence = _read(stages['evidence'], 'evidence', index['evidence_sha256'],
                         owner.pw_uid, owner.pw_gid, reader.pw_uid)
        receipt = _read(stages['receipt'], 'receipt', index['route_receipt_sha256'],
                        owner.pw_uid, owner.pw_gid, reader.pw_uid)
        return index_bytes, evidence, receipt
    finally:
        for fd in reversed(handles):
            os.close(fd)


def _stamp(value: object) -> datetime:
    if type(value) is not str or not value.endswith('Z'):
        raise VerifiedSourceError('noncanonical UTC source time')
    instant = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if instant.tzinfo != timezone.utc or instant.isoformat().replace('+00:00', 'Z') != value:
        raise VerifiedSourceError('noncanonical UTC source time')
    return instant


def _temporal_projection(original: dict, observation: dict,
                         start: datetime, end: datetime) -> dict:
    """Reconstruct a v3 projected event from the private original, never its text."""
    if (type(original) is not dict or set(original) != EVENT_FIELDS
            or type(observation) is not dict or set(observation) !=
            {'created_at', 'updated_at', 'submitted_at', 'observed_at_utc'}):
        raise VerifiedSourceError('temporal original/observation schema invalid')
    eid = original['evidence_id']
    if (type(eid) is not str or not SHA.fullmatch(eid)
            or _sha(_canonical({k: v for k, v in original.items() if k != 'evidence_id'})) != eid):
        raise VerifiedSourceError('temporal original hash invalid')
    stamps = {name: None if value is None and name != 'observed_at_utc' else _stamp(value)
              for name, value in observation.items()}
    if any(stamps[name] is not None and stamps[name].isoformat().replace('+00:00', 'Z') != value
           for name, value in observation.items()):
        raise VerifiedSourceError('temporal observation not canonical')
    occurred = _stamp(original['occurred_at'])
    observed, updated = stamps['observed_at_utc'], stamps['updated_at']
    if not start <= occurred < end or observed < occurred:
        raise VerifiedSourceError('temporal day/observation invalid')
    kind = original['kind']
    if kind in ('pull_request_commit', 'default_branch_commit'):
        status, keep = 'content_addressed_commit_not_push_time', True
    elif kind in {'issue_created', 'pull_request_created', 'issue_snapshot',
                  'pull_request_snapshot', 'issue_comment', 'pull_request_comment',
                  'pull_request_review_comment', 'pull_request_review'}:
        identity = ('updated_at' if kind.endswith('snapshot') else
                    'submitted_at' if kind == 'pull_request_review' else 'created_at')
        if observation[identity] != original['occurred_at']:
            raise VerifiedSourceError('temporal source identity does not match event')
        created = stamps['submitted_at'] if kind == 'pull_request_review' else stamps['created_at']
        if updated is not None and (updated > observed or
                                    created is not None and updated < created):
            raise VerifiedSourceError('temporal resource update invalid')
        keep = updated is not None and start <= updated < end
        status = ('day_snapshot_not_event_instant' if keep else
                  'withheld_unknown_modification_time' if updated is None else
                  'withheld_outside_target_day')
    else:
        keep, status = False, 'event_transition_only'
    withheld = [field for field in MUTABLE if not keep and original[field] is not None]
    row = {key: value for key, value in original.items() if key != 'evidence_id'}
    for field in withheld:
        row[field] = None
    row['temporal_observation'] = {
        'policy': TEMPORAL_POLICY, 'status': status,
        'observed_at_utc': observation['observed_at_utc'],
        'resource_updated_at': observation['updated_at'],
        'original_event_sha256': eid, 'withheld_fields': withheld,
        'historical_instant_verified': False,
    }
    return {'evidence_id': _sha(_canonical(row)), **row}


def _originals_from_v3(records: object, bundle: EvidenceBundle,
                       start: datetime, end: datetime) -> list[dict]:
    if (type(records) is not list or len(records) != len(bundle.events)
            or len(records) > 50000):
        raise VerifiedSourceError('incomplete temporal observation census')
    projected = {row['evidence_id']: row for row in bundle.events}
    if len(projected) != len(bundle.events):
        raise VerifiedSourceError('duplicate projected source')
    seen, original_by_id, order = set(), {}, []
    for record in records:
        if (type(record) is not dict or set(record) !=
                {'evidence_id', 'observation', 'original_event'}):
            raise VerifiedSourceError('invalid temporal receipt record')
        eid = record['evidence_id']
        if type(eid) is not str or eid in seen or eid not in projected:
            raise VerifiedSourceError('temporal receipt contains foreign/duplicate event')
        rebuilt = _temporal_projection(record['original_event'], record['observation'],
                                       start, end)
        if rebuilt != projected[eid] or eid != rebuilt['evidence_id']:
            raise VerifiedSourceError('temporal projection differs from original')
        seen.add(eid)
        order.append(eid)
        original_by_id[eid] = record['original_event']
    if order != sorted(order) or seen != set(projected):
        raise VerifiedSourceError('temporal receipt incomplete or unordered')
    return [original_by_id[event['evidence_id']] for event in bundle.events]


def _original_event(row: dict, start: datetime, end: datetime) -> None:
    from .github_daily_issue_pr import _event_key
    if type(row) is not dict or set(row) != EVENT_FIELDS:
        raise VerifiedSourceError('original event schema invalid')
    if not start <= _stamp(row['occurred_at']) < end:
        raise VerifiedSourceError('original event outside target JST date')
    eid = row['evidence_id']
    if (type(eid) is not str or not SHA.fullmatch(eid)
            or _sha(_canonical({k: v for k, v in row.items() if k != 'evidence_id'})) != eid):
        raise VerifiedSourceError('original source ID changed')
    _event_key(row)
    if (type(row['source_id']) is not str or not row['source_id']
            or row['actor'] is not None and type(row['actor']) is not str
            or row['state'] is not None and type(row['state']) is not str
            or row['draft'] is not None and type(row['draft']) is not bool):
        raise VerifiedSourceError('original event scalar invalid')
    for field in ('title', 'body', 'message'):
        value = row[field]
        if value is not None and (type(value) is not dict or set(value) !=
                {'text', 'truncated', 'original_bytes', 'included_bytes'}
                or type(value['text']) is not str or type(value['truncated']) is not bool
                or type(value['original_bytes']) is not int
                or type(value['included_bytes']) is not int
                or not 0 <= value['included_bytes'] <= value['original_bytes']
                or value['included_bytes'] != len(value['text'].encode())):
            raise VerifiedSourceError('original excerpt envelope invalid')
    kind = row['kind']
    parsed = urlsplit(row['url'])
    if kind in {'issue_comment', 'pull_request_comment',
                'pull_request_review_comment'}:
        fragment = ('discussion_r' if kind == 'pull_request_review_comment'
                    else 'issuecomment-') + row['source_id']
        if parsed.fragment != fragment:
            raise VerifiedSourceError('comment original URL/source mismatch')
    elif kind == 'pull_request_review':
        if parsed.fragment not in ('', 'pullrequestreview-' + row['source_id']):
            raise VerifiedSourceError('review original URL/source mismatch')
    elif parsed.fragment:
        raise VerifiedSourceError('noncomment original URL contains fragment')
    if kind in ('pull_request_commit', 'default_branch_commit'):
        if row['sha'] != row['source_id']:
            raise VerifiedSourceError('original commit SHA/source mismatch')


def _verify(index_bytes: bytes, evidence: bytes, receipt_bytes: bytes, digest: str) -> VerifiedSource:
    from .github_daily_issue_pr import _event_key, DailyEntityPreviewError
    index, receipt = _object(index_bytes), _object(receipt_bytes)
    bundle = parse_evidence_bundle(evidence)
    target = date.fromisoformat(bundle.date)
    start, end = _date_window(target)
    if (target.isoformat() != bundle.date or bundle.timezone != 'Asia/Tokyo'
            or bundle.window_start != start.isoformat().replace('+00:00', 'Z')
            or bundle.window_end != end.isoformat().replace('+00:00', 'Z')):
        raise VerifiedSourceError('source JST date invalid')
    if (type(index.get('record_version')) is not int
            or index['record_version'] != 1
            or index['evidence_sha256'] != bundle.sha256 or index['date'] != bundle.date
            or index['route_receipt_sha256'] != _sha(receipt_bytes)
            or type(index['route_count']) is not int or index['route_count'] > 2048
            or type(index['source_receipt_version']) is not int
            or index['source_receipt_version'] not in (2, 3, titles.RECEIPT_VERSION)
            or index['collection_mode'] not in ('scheduled_recent_api_reverified',
                                                'explicit_historical_reconfirmation')
            or _sha(index_bytes) != digest):
        raise VerifiedSourceError('capture index not bound to evidence')
    if _canonical(_object(evidence)) != evidence:
        raise VerifiedSourceError('noncanonical original Evidence')
    standard = {'record_version', 'date', 'evidence_sha256', 'routes',
                'collection_mode', 'publication_authorized', 'captured_at_utc'}
    version = index['source_receipt_version']
    temporal_fields = ({'temporal_policy', 'temporal_observations'}
                       if version in (3, titles.RECEIPT_VERSION) else set())
    label_fields = ({'entity_title_observations'}
                    if version == titles.RECEIPT_VERSION else set())
    if (set(receipt) != standard | temporal_fields | label_fields
            or type(receipt['record_version']) is not int
            or receipt['record_version'] != version or receipt['date'] != bundle.date
            or receipt['evidence_sha256'] != bundle.sha256
            or receipt['collection_mode'] != index['collection_mode']
            or receipt['captured_at_utc'] != index['captured_at_utc']
            or receipt['publication_authorized'] is not False
            or type(receipt['routes']) is not list
            or len(receipt['routes']) != index['route_count']):
        raise VerifiedSourceError('route receipt not source-bound')
    observed = _stamp(receipt['captured_at_utc'])
    if (index['collection_mode'] == 'scheduled_recent_api_reverified'
            and target not in (observed.astimezone(ZoneInfo('Asia/Tokyo')).date(),
                               observed.astimezone(ZoneInfo('Asia/Tokyo')).date() - timedelta(days=1))):
        raise VerifiedSourceError('scheduled capture day invalid')
    if len(bundle.events) > 50000:
        raise VerifiedSourceError('source event count exceeds bound')
    if version in (3, titles.RECEIPT_VERSION):
        if receipt['temporal_policy'] != TEMPORAL_POLICY:
            raise VerifiedSourceError('temporal policy mismatch')
        originals = _originals_from_v3(receipt['temporal_observations'], bundle,
                                       start, end)
    else:
        originals = list(bundle.events)
        if any('temporal_observation' in event for event in originals):
            raise VerifiedSourceError('v2 cannot attest temporal projection')
    for original in originals:
        _original_event(original, start, end)
    by_id = {}
    orphan = 0
    for event in bundle.events:
        when = _stamp(event.get('occurred_at'))
        if not start <= when < end:
            raise VerifiedSourceError('cross-day source event')
        eid = event['evidence_id']
        if eid in by_id or type(eid) is not str or not SHA.fullmatch(eid):
            raise VerifiedSourceError('duplicate or invalid source event ID')
        if event['kind'] == 'default_branch_commit':
            orphan += 1
        try:
            _event_key(event)
        except DailyEntityPreviewError as exc:
            raise VerifiedSourceError('event entity attribution invalid') from exc
        by_id[eid] = event
    routes = receipt['routes']
    ids = [row.get('evidence_id') if type(row) is dict else None for row in routes]
    if (ids != sorted(ids, key=str) or len(set(ids)) != len(ids)
            or set(ids) != {row['evidence_id'] for row in bundle.events
                                if row['kind'] in REQUIRED}):
        raise VerifiedSourceError('C1a route census incomplete')
    for row in routes:
        if type(row) is not dict or set(row) != {'evidence_id', 'kind', 'repository',
                'entity', 'number', 'source_id', 'route', 'verification_route', 'remote_id'}:
            raise VerifiedSourceError('invalid route witness')
        event = by_id[row['evidence_id']]
        kind, repo, number, source_id = (event[k] for k in
                                         ('kind', 'repository', 'number', 'source_id'))
        owner, name = repo.split('/')
        prefix = f'/repos/{owner}/{name}'
        if kind == 'issue_comment':
            route, linked, entity = (f'{prefix}/issues/comments/{source_id}',
                                     f'{prefix}/issues/{number}', 'issue')
        elif kind == 'pull_request_comment':
            route, linked, entity = (f'{prefix}/issues/comments/{source_id}',
                                     f'{prefix}/issues/{number}', 'pull_request')
        elif kind == 'pull_request_review_comment':
            route, linked, entity = (f'{prefix}/pulls/comments/{source_id}',
                                     f'{prefix}/pulls/{number}', 'pull_request')
        else:
            route, linked, entity = (f'{prefix}/pulls/{number}/commits',
                                     f'{prefix}/pulls/{number}', 'pull_request')
        if (not REPO.fullmatch(repo) or row['kind'] != kind or row['repository'] != repo
                or type(number) is not int or number < 1
                or type(row['number']) is not int or row['number'] != number
                or row['source_id'] != source_id
                or (row['route'], row['verification_route'], row['entity']) !=
                (route, linked, entity) or type(row['remote_id']) is not str
                or not row['remote_id']
                or (kind in {'pull_request_commit', 'pull_request_review_comment'}
                    and row['remote_id'] != source_id)
                or (kind != 'pull_request_commit' and
                    not re.fullmatch(r'[1-9][0-9]{0,18}', str(source_id)))
                or (kind in {'issue_comment', 'pull_request_comment'} and
                    not re.fullmatch(r'[1-9][0-9]{0,18}', row['remote_id']))):
            raise VerifiedSourceError('forged route attribution')
    extra_titles = ()
    if version == titles.RECEIPT_VERSION:
        try:
            extra_titles = titles.validate_records(
                receipt['entity_title_observations'], bundle.events,
                captured_at_utc=receipt['captured_at_utc'])
        except titles.TitleObservationError:
            raise VerifiedSourceError('invalid current-label title evidence') from None
    return VerifiedSource(
        digest, bundle, len(routes), orphan, extra_titles,
        _TITLE_OBSERVATION_GATE if version == titles.RECEIPT_VERSION else None,
        _title_binding(digest, bundle.sha256, extra_titles)
        if version == titles.RECEIPT_VERSION else None,
    )


def read_verified_source(capture_sha256: str) -> VerifiedSource:
    """Reopen exact indexed CAS, never accept caller event bytes or a fake empty day."""
    try:
        index, evidence, receipt = _capture(capture_sha256)
        return _verify(index, evidence, receipt, capture_sha256)
    except VerifiedSourceError:
        raise
    except (OSError, KeyError, TypeError, ValueError, UnicodeError, OverflowError,
            LookupError, DailyCASAdmissionError) as exc:
        raise VerifiedSourceError('trusted collector CAS/C1a source unavailable') from exc
