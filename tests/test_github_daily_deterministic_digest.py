"""Independent offline expected outcomes; no provider, host CAS or production paths."""
from __future__ import annotations

from dataclasses import replace
from datetime import date
import hashlib
import json

import pytest

from obsidian_automation.github_daily_activity import (
    EvidenceEvent, ProjectBinding, _bounded_text, _canonical_json_bytes,
    make_daily_evidence_bundle,
)
from obsidian_automation.github_daily_summary import parse_evidence_bundle
from obsidian_automation import github_daily_deterministic_digest as digest
from obsidian_automation import github_daily_deterministic_preview as cli
from obsidian_automation import github_daily_verified_source as source

DAY = date(2026, 10, 18)
CAPTURE = 'a' * 64


def event(kind, number=1, *, repo='Example/Alpha', minute=1, body=None,
          message=None, actor='human', title=None, state=None):
    sha = 'a' * 40 if kind.endswith('commit') else None
    entity = ('commit' if kind.endswith('commit') else
              kind if kind in ('issue_comment', 'pull_request_comment',
                               'pull_request_review', 'pull_request_review_comment') else
              'pull_request' if kind.startswith('pull_request') else 'issue')
    path = ('commit/' + sha if kind.endswith('commit') else
            ('pull' if kind.startswith('pull_request') else 'issues') + '/' + str(number))
    fragment = ('#discussion_r' if kind == 'pull_request_review_comment' else
                '#issuecomment-' if kind in ('issue_comment', 'pull_request_comment') else '')
    if fragment:
        path += fragment + str(1000 + minute)
    fields = dict(kind=kind, repository=repo,
                  occurred_at=f'2026-10-17T15:{minute:02}:00Z',
                  url=f'https://github.com/{repo}/{path}', actor=actor,
                  entity_type=entity, number=number, source_id=str(1000 + minute)
                  if sha is None else sha, sha=sha, state=state, draft=None,
                  title=_bounded_text(title, limit=2048, label='title') if title else None,
                  body=_bounded_text(body, limit=8192, label='body') if body else None,
                  message=_bounded_text(message, limit=8192, label='message') if message else None)
    eid = hashlib.sha256(_canonical_json_bytes({k: v.to_json() if hasattr(v, 'to_json')
                                                 else v for k, v in fields.items()})).hexdigest()
    return EvidenceEvent(eid, **fields)


def bundle(*events):
    repos = {item.repository for item in events} or {'Example/Alpha'}
    projects = [ProjectBinding('10-Project/' + repo.replace('/', '-') + '.md', repo)
                for repo in repos]
    original = make_daily_evidence_bundle(target_date=DAY, projects=projects, events=events)
    return parse_evidence_bundle(original.canonical_bytes)


def _sentences(value):
    """Preserved typed source diagnostic; no longer shown as Daily filler."""
    return '\n'.join(phrase for item in value.items for phrase in item['sentences'])


def test_zero_one_and_two_repos_with_typed_merge_and_orphan():
    assert digest.build_digest_from_bundle(bundle()).render() == '進展のあったIssue・PRはありません。\n'
    merged = event('pull_request_merged', 17, minute=10)
    previous = event('pull_request_created', 17, minute=1, title='First draft')
    second = event('issue_comment', 5, repo='Example/Beta', minute=12,
                   body='GPU acceptance PASS confirmed in reported test.')
    orphan = replace(event('default_branch_commit', None, minute=13), number=None)
    result = digest.build_digest_from_bundle(bundle(previous, merged, second, orphan))
    assert len(result.items) == 2
    assert result.unattributed_count == 1
    assert result.render().count('PR #17') == 1
    assert '当日にPRがマージされました。' in _sentences(result)
    assert 'GPU検証に関する結果が報告されました。' in _sentences(result)
    assert 'https://github.com/Example/Beta/issues/5' in result.render()
    assert result.report()['verified_cas_source'] is False
    assert result.report()['model_used'] is False


@pytest.mark.parametrize('subject,visible', [
    ('test: add WAL recovery preflight', True),
    ('fix(runtime): prevent duplicate writes', True),
    ('feat: add docs search API', True),
    ('refactor: index README references in search API', True),
    ('Update README documentation', False),
    ('docs: document search API', False),
    ('test: format snapshot whitespace', False),
])
def test_commit_only_semantics(subject, visible):
    result = digest.build_digest_from_bundle(bundle(event('pull_request_commit', 31,
                                                      message=subject)))
    assert bool(result.items) is visible
    assert 'マージされました' not in _sentences(result)


def test_material_feat_with_docs_word_is_linked_without_fake_merge():
    result = digest.build_digest_from_bundle(bundle(event('pull_request_commit', 83,
                                                       message='feat: add docs search API')))
    assert len(result.items) == 1
    assert result.items[0]['status'] == 'updated'
    assert result.items[0]['sentences'] == ['実装コミットが記録されました。']
    assert 'https://github.com/Example/Alpha/pull/83' in result.render()
    assert '当日にPRがマージされました。' not in _sentences(result)


def test_routine_only_pr_creation_stays_excluded():
    result = digest.build_digest_from_bundle(bundle(
        event('pull_request_created', 84, title='Format-only fixtures'),
        event('pull_request_commit', 84, minute=2,
              message='test: format snapshot whitespace')))
    assert result.items == ()


@pytest.mark.parametrize('repo', ['Example/.', 'Example/..', './Alpha'])
def test_dot_only_repository_segments_block_entire_day(repo):
    # The original Bundle parser accepts these syntactically, so the
    # deterministic source identity gate must reject instead of saying quiet day.
    invalid = bundle(event('issue_comment', 9, repo=repo,
                           body='Acceptance test completed with results.'))
    with pytest.raises(digest.DeterministicDigestError, match='identity'):
        digest.build_digest_from_bundle(invalid)


def test_embedded_period_repository_name_remains_canonical():
    valid = digest.build_digest_from_bundle(bundle(event('issue_comment', 9,
        repo='Example/.github', body='Acceptance test completed with results.')))
    assert len(valid.items) == 1
    assert 'https://github.com/Example/.github/issues/9' in valid.render()


@pytest.mark.parametrize('alias', [
    'https://github.com/Example/%2e/issues/9',
    'https://github.com/Example/Alpha/../issues/9',
    'https://github.com/Example/%41lpha/issues/9',
])
def test_noncanonical_public_route_cannot_replace_source_identity(alias):
    original = bundle(event('issue_comment', 9,
                            body='Acceptance test completed with results.'))
    forged = replace(original, events=({**original.events[0], 'url': alias},))
    with pytest.raises(digest.DeterministicDigestError, match='identity'):
        digest.build_digest_from_bundle(forged)


def test_bot_and_uncertain_snapshot_are_not_progress():
    result = digest.build_digest_from_bundle(bundle(
        event('issue_snapshot', 2, title='Snapshot changed?'),
        event('issue_comment', 2, minute=4, body='Next'),
        event('pull_request_commit', 3, minute=8, actor='ci[bot]',
              message='feat: ship code')))
    assert result.items == ()
    assert result.original_event_count == 3


def test_planned_is_not_positive_but_actual_failure_is_reported():
    planned = digest.build_digest_from_bundle(bundle(event('issue_comment', 7,
        body='GPU acceptance test planned; no PASS yet.')))
    assert 'GPU検証に関する結果' not in _sentences(planned)
    failed = digest.build_digest_from_bundle(bundle(event('issue_comment', 7,
        body='GPU acceptance test failed on the proposed system.')))
    assert 'GPU検証に関する結果が報告されました。' in _sentences(failed)
    assert '本番稼働' not in _sentences(failed)


@pytest.mark.parametrize('body', [
    'GPU acceptance test would have passed, but was not performed.',
    'GPU test PASS anticipated but not run.',
    'GPU acceptance PASS predicted, not executed.',
])
def test_hypothetical_gpu_result_is_only_an_ordinary_source_comment(body):
    result = digest.build_digest_from_bundle(bundle(event('issue_comment', 72,
                                                        body=body)))
    assert len(result.items) == 1
    assert result.items[0]['sentences'] == ['進捗コメントが記録されました。']
    assert 'GPU検証に関する結果が報告されました。' not in _sentences(result)


def test_actual_gpu_pass_remains_reported_not_independently_verified():
    result = digest.build_digest_from_bundle(bundle(event('issue_comment', 72,
        body='GPU acceptance PASS confirmed in a completed test.')))
    assert result.items[0]['sentences'] == ['GPU検証に関する結果が報告されました。']
    assert '本番稼働' not in _sentences(result)


@pytest.mark.parametrize('body', [
    'GPU acceptance PASS without running the test.',
    'GPU acceptance PASS without executing the test.',
    'GPU acceptance PASS without executing the GPU acceptance test.',
    'GPU acceptance PASS without running the full acceptance test.',
    'GPU acceptance PASS claimed; test was never run.',
    'GPU acceptance PASS; the acceptance test was never executed.',
    'GPU acceptance PASS; the GPU test was never performed.',
    'GPU acceptance PASS claimed; no test executed.',
    'GPU検証PASSと記載、テストを実施せず。',
])
def test_explicit_nonexecution_does_not_become_reported_gpu_result(body):
    result = digest.build_digest_from_bundle(bundle(event('issue_comment', 73,
                                                        body=body)))
    assert len(result.items) == 1
    assert result.items[0]['sentences'] == ['進捗コメントが記録されました。']
    assert 'GPU検証に関する結果が報告されました。' not in _sentences(result)
    assert 'https://github.com/Example/Alpha/issues/73' in result.render()


@pytest.mark.parametrize('body', [
    'GPU acceptance PASS confirmed in a completed test.',
    'GPU acceptance test failed on the proposed system.',
])
def test_executed_gpu_result_remains_a_source_report(body):
    result = digest.build_digest_from_bundle(bundle(event('issue_comment', 73,
                                                        body=body)))
    assert len(result.items) == 1
    assert result.items[0]['sentences'] == ['GPU検証に関する結果が報告されました。']
    assert 'https://github.com/Example/Alpha/issues/73' in result.render()
    assert '本番稼働' not in _sentences(result)


def test_invalid_cross_day_and_forged_identity_fail_closed():
    original = bundle(event('pull_request_merged', 18))
    altered = replace(original, events=({**original.events[0],
                                           'occurred_at': '2026-10-18T15:00:00Z'},))
    with pytest.raises(digest.DeterministicDigestError):
        digest.build_digest_from_bundle(altered)
    altered = replace(original, events=({**original.events[0], 'number': 19},))
    with pytest.raises(digest.DeterministicDigestError):
        digest.build_digest_from_bundle(altered)


def test_unknown_source_never_enters_cli_or_attests_empty_day(monkeypatch, capsys):
    def unavailable(_):
        raise source.VerifiedSourceError('offline')
    monkeypatch.setattr(digest, 'read_verified_source', unavailable)
    with pytest.raises(digest.DeterministicDigestError):
        digest.build_digest_from_verified_capture(CAPTURE)
    assert cli.main(['--capture-sha', CAPTURE, '--target-date', str(DAY)]) == 2
    assert '進展のあったIssue' not in capsys.readouterr().out


def test_index_receipt_census_and_forged_route_rejected():
    comment = event('issue_comment', 9, body='Acceptance test completed with results.')
    b = bundle(comment)
    receipt = {'record_version': 2, 'date': b.date, 'evidence_sha256': b.sha256,
               'routes': [], 'collection_mode': 'explicit_historical_reconfirmation',
               'captured_at_utc': '2026-10-19T00:00:00Z',
               'publication_authorized': False}
    original = make_daily_evidence_bundle(target_date=DAY,
        projects=[ProjectBinding('10-Project/Example-Alpha.md', 'Example/Alpha')], events=[comment])
    raw_receipt = _canonical_json_bytes(receipt)
    index = {'record_version': 1, 'source_receipt_version': 2, 'date': b.date,
             'evidence_sha256': b.sha256,
             'route_receipt_sha256': hashlib.sha256(raw_receipt).hexdigest(),
             'route_count': 0, 'collection_mode': receipt['collection_mode'],
             'captured_at_utc': receipt['captured_at_utc'],
             'producer_attestation_verified': False, 'publication_authorized': False}
    raw_index = _canonical_json_bytes(index)
    with pytest.raises(source.VerifiedSourceError, match='census'):
        source._verify(raw_index, original.canonical_bytes, raw_receipt,
                       hashlib.sha256(raw_index).hexdigest())
    with pytest.raises(source.VerifiedSourceError):
        source._verify(raw_index, original.canonical_bytes, raw_receipt, CAPTURE)


def test_source_route_must_match_exact_api_entity_and_index(monkeypatch):
    comment = event('issue_comment', 9, body='Acceptance test completed with results.')
    original = make_daily_evidence_bundle(target_date=DAY,
        projects=[ProjectBinding('10-Project/Example-Alpha.md', 'Example/Alpha')],
        events=[comment])
    witness = {'evidence_id': comment.evidence_id, 'kind': comment.kind,
               'repository': comment.repository, 'entity': 'issue', 'number': 9,
               'source_id': comment.source_id,
               'route': f'/repos/Example/Alpha/issues/comments/{comment.source_id}',
               'verification_route': '/repos/Example/Alpha/issues/9',
               'remote_id': '715'}
    def triplet(rows):
        receipt = {'record_version': 2, 'date': str(DAY),
                   'evidence_sha256': original.sha256, 'routes': rows,
                   'collection_mode': 'explicit_historical_reconfirmation',
                   'captured_at_utc': '2026-10-19T00:00:00Z',
                   'publication_authorized': False}
        raw_receipt = _canonical_json_bytes(receipt)
        index = {'record_version': 1, 'source_receipt_version': 2,
                 'date': str(DAY), 'evidence_sha256': original.sha256,
                 'route_receipt_sha256': hashlib.sha256(raw_receipt).hexdigest(),
                 'route_count': len(rows), 'collection_mode': receipt['collection_mode'],
                 'captured_at_utc': receipt['captured_at_utc'],
                 'producer_attestation_verified': False,
                 'publication_authorized': False}
        raw_index = _canonical_json_bytes(index)
        return raw_index, original.canonical_bytes, raw_receipt
    valid = triplet([witness])
    capture = hashlib.sha256(valid[0]).hexdigest()
    source._verify(*valid, capture)
    # In isolation the synthetic tuple is only a contract fixture. It is NOT
    # an authorized CAS fixture, so the real read gate must still be called.
    monkeypatch.setattr(source, '_capture', lambda digest: valid)
    verified = source.read_verified_source(capture)
    assert verified.witnessed_count == 1
    assert verified.unattributed_count == 0
    forged = triplet([{**witness, 'verification_route':
                       '/repos/Example/Alpha/issues/10'}])
    with pytest.raises(source.VerifiedSourceError, match='attribution'):
        source._verify(*forged, hashlib.sha256(forged[0]).hexdigest())
    with pytest.raises(source.VerifiedSourceError):
        source._verify(*valid, CAPTURE)


def test_unauthorized_role_is_rejected_before_filesystem_access(monkeypatch):
    from types import SimpleNamespace
    def lookup(name):
        uid = 801 if name == source.OWNER else 802
        return SimpleNamespace(pw_uid=uid, pw_gid=uid)
    monkeypatch.setattr(source.pwd, 'getpwnam', lookup)
    monkeypatch.setattr(source.os, 'geteuid', lambda: 999)
    with pytest.raises(source.VerifiedSourceError, match='role'):
        source._capture(CAPTURE)


def test_synthetic_acl_reader_without_mask_rights_is_denied(monkeypatch):
    from obsidian_automation import github_daily_cas_admission as acl
    entries = {(acl.USER_OBJ, None): 6, (acl.GROUP_OBJ, None): 0,
               (acl.MASK, None): 0, (acl.OTHER, None): 0,
               (acl.USER, 802): 5}
    monkeypatch.setattr(source, '_acl_from_fd', lambda fd, attr: entries)
    with pytest.raises(source.VerifiedSourceError, match='ACL'):
        source._acl(7, 801, 802, directory=False)


def test_output_symlink_is_rejected_without_writing(tmp_path, monkeypatch):
    trusted = replace(digest.build_digest_from_bundle(bundle()), capture_sha256=CAPTURE)
    monkeypatch.setattr(cli, 'build_digest_from_verified_capture', lambda sha: trusted)
    actual = tmp_path / 'private'
    actual.mkdir(mode=0o700)
    alias = tmp_path / 'alias'
    alias.symlink_to(actual, target_is_directory=True)
    assert cli.main(['--capture-sha', CAPTURE, '--target-date', str(DAY),
                     '--output-dir', str(alias)]) == 2
    assert list(actual.iterdir()) == []


@pytest.fixture
def isolated_cas(tmp_path, monkeypatch):
    """Private synthetic UID/ACL metadata; never represents live host admission."""
    from obsidian_automation import github_daily_cas_admission as acl
    import os
    from types import SimpleNamespace
    root = tmp_path / 'collector'
    root.mkdir(mode=0o750)
    for stage in ('capture', 'evidence', 'receipt'):
        (root / stage).mkdir(mode=0o750)
    monkeypatch.setattr(source, 'TRUST_ANCHOR', tmp_path)
    monkeypatch.setattr(source, 'ROOT', root)
    uid, gid = os.geteuid(), os.getegid()
    reader = uid + 100000
    def lookup(name):
        assert name in (source.OWNER, source.READER)
        return SimpleNamespace(pw_uid=uid if name == source.OWNER else reader,
                               pw_gid=gid if name == source.OWNER else gid + 100000)
    monkeypatch.setattr(source.pwd, 'getpwnam', lookup)
    allowed = {'reader': True}
    def fake_acl(fd, *, attr='system.posix_acl_access'):
        import stat
        is_dir = stat.S_ISDIR(os.fstat(fd).st_mode)
        mask = 7 if attr == 'system.posix_acl_default' else (5 if is_dir else 4)
        return {(acl.USER_OBJ, None): 7 if is_dir else 6,
                (acl.GROUP_OBJ, None): 0, (acl.MASK, None): mask,
                (acl.OTHER, None): 0,
                (acl.USER, reader): 5 if allowed['reader'] else 0}
    monkeypatch.setattr(source, '_acl_from_fd', fake_acl)
    def write(stage, digest, data):
        path = root / stage / f'{digest}.github-daily-{source.SUFFIX[stage]}.json'
        path.write_bytes(data)
        path.chmod(0o640)
        return path
    return write, allowed


def _synthetic_triplet(events, *, temporal=False):
    """Build independent fixture bytes, never use the production verifier."""
    original = make_daily_evidence_bundle(
        target_date=DAY, projects=[ProjectBinding('10-Project/Example-Alpha.md',
                                                  'Example/Alpha')], events=events)
    if temporal:
        projected, observations = [], []
        for raw in original.events:
            data = raw.to_json()
            if raw.kind == 'pull_request_merged':
                status, withheld = 'event_transition_only', []
                observation = {'created_at': None, 'updated_at': None,
                               'submitted_at': None, 'observed_at_utc':
                               '2026-10-19T00:00:00Z'}
            elif raw.kind == 'default_branch_commit':
                status, withheld = 'content_addressed_commit_not_push_time', []
                observation = {'created_at': None, 'updated_at': None,
                               'submitted_at': None, 'observed_at_utc':
                               '2026-10-19T00:00:00Z'}
            else:
                status, withheld = 'day_snapshot_not_event_instant', []
                observation = {
                    'created_at': (None if raw.kind == 'pull_request_review'
                                   else data['occurred_at']),
                    'updated_at': data['occurred_at'],
                    'submitted_at': (data['occurred_at']
                                     if raw.kind == 'pull_request_review' else None),
                    'observed_at_utc': '2026-10-19T00:00:00Z',
                }
            view = {key: value for key, value in data.items() if key != 'evidence_id'}
            view['temporal_observation'] = {
                'policy': 'github-day-snapshot-v1', 'status': status,
                'observed_at_utc': observation['observed_at_utc'],
                'resource_updated_at': observation['updated_at'],
                'original_event_sha256': data['evidence_id'],
                'withheld_fields': withheld, 'historical_instant_verified': False}
            view['evidence_id'] = hashlib.sha256(_canonical_json_bytes(view)).hexdigest()
            observations.append({'evidence_id': view['evidence_id'],
                                 'original_event': data, 'observation': observation})
            projected.append(view)
        payload = json.loads(original.canonical_bytes)
        payload['events'] = projected
        evidence = _canonical_json_bytes(payload)
        observations.sort(key=lambda row: row['evidence_id'])
    else:
        evidence = original.canonical_bytes
        projected = [e.to_json() for e in original.events]
        observations = []
    digest_evidence = hashlib.sha256(evidence).hexdigest()
    receipt = {'record_version': 3 if temporal else 2, 'date': str(DAY),
               'evidence_sha256': digest_evidence,
               'collection_mode': 'explicit_historical_reconfirmation',
               'captured_at_utc': '2026-10-19T00:00:00Z',
               'publication_authorized': False, 'routes': []}
    if temporal:
        receipt.update(temporal_policy='github-day-snapshot-v1',
                       temporal_observations=observations)
    for original_row, projected_row in zip(original.events, projected):
        if original_row.kind == 'issue_comment':
            receipt['routes'].append({
                'evidence_id': projected_row['evidence_id'],
                'kind': 'issue_comment', 'repository': original_row.repository,
                'entity': 'issue', 'number': original_row.number,
                'source_id': original_row.source_id,
                'route': f'/repos/Example/Alpha/issues/comments/{original_row.source_id}',
                'verification_route': f'/repos/Example/Alpha/issues/{original_row.number}',
                'remote_id': '8100'})
    receipt['routes'].sort(key=lambda row: row['evidence_id'])
    raw_receipt = _canonical_json_bytes(receipt)
    index = {'record_version': 1, 'source_receipt_version': receipt['record_version'],
             'date': str(DAY), 'evidence_sha256': digest_evidence,
             'route_receipt_sha256': hashlib.sha256(raw_receipt).hexdigest(),
             'route_count': len(receipt['routes']), 'collection_mode': receipt['collection_mode'],
             'captured_at_utc': receipt['captured_at_utc'],
             'producer_attestation_verified': False, 'publication_authorized': False}
    raw_index = _canonical_json_bytes(index)
    return (hashlib.sha256(raw_index).hexdigest(), raw_index, evidence, raw_receipt)


@pytest.mark.parametrize('temporal', [False, True], ids=['v2', 'v3'])
def test_exact_integer_capture_index_and_route_number(isolated_cas, temporal):
    # Independently specified Issue #1 and typed route. All variants rebind
    # their JSON and SHA-256 identities; hashing malformed JSON cannot admit it.
    write, _ = isolated_cas
    original = event('issue_comment', 1, minute=4,
                     body='Acceptance test completed with recorded results.')
    capture, index_bytes, evidence, receipt_bytes = _synthetic_triplet(
        [original], temporal=temporal)
    record = json.loads(index_bytes)
    receipt = json.loads(receipt_bytes)
    assert record['record_version'] == 1
    assert receipt['routes'][0]['number'] == 1
    write('evidence', record['evidence_sha256'], evidence)
    write('receipt', record['route_receipt_sha256'], receipt_bytes)
    write('capture', capture, index_bytes)
    admitted = digest.build_digest_from_verified_capture(capture)
    assert admitted.report()['verified_cas_source'] is True
    assert admitted.original_event_count == 1
    assert len(admitted.items) == (1 if temporal else 0)
    assert ('https://github.com/Example/Alpha/issues/1' in admitted.render()) is temporal

    def check_rejected(bad_index, bad_receipt):
        raw_receipt = _canonical_json_bytes(bad_receipt)
        bad_index['route_receipt_sha256'] = hashlib.sha256(raw_receipt).hexdigest()
        raw_index = _canonical_json_bytes(bad_index)
        identity = hashlib.sha256(raw_index).hexdigest()
        write('receipt', bad_index['route_receipt_sha256'], raw_receipt)
        write('capture', identity, raw_index)
        with pytest.raises(source.VerifiedSourceError):
            source._verify(raw_index, evidence, raw_receipt, identity)
        with pytest.raises(source.VerifiedSourceError):
            source.read_verified_source(identity)
        with pytest.raises(digest.DeterministicDigestError):
            digest.build_digest_from_verified_capture(identity)

    for malformed in (True, 1.0):
        bad_index = json.loads(index_bytes)
        bad_index['record_version'] = malformed
        check_rejected(bad_index, json.loads(receipt_bytes))
        bad_receipt = json.loads(receipt_bytes)
        bad_receipt['routes'][0]['number'] = malformed
        check_rejected(json.loads(index_bytes), bad_receipt)


@pytest.mark.parametrize('temporal', [False, True])
def test_private_synthetic_cas_reads_complete_v2_and_v3(isolated_cas, temporal):
    write, allowed = isolated_cas
    merge = event('pull_request_merged', 41, minute=2)
    comment = event('issue_comment', 9, minute=4,
                    body='Acceptance test completed with recorded results.')
    snapshot = event('issue_snapshot', 91, minute=5, title='Mutable observation')
    orphan = event('default_branch_commit', None, minute=6,
                   message='feat: orphaned default branch commit')
    capture, index, evidence, receipt = _synthetic_triplet([merge, comment, snapshot, orphan],
                                                             temporal=temporal)
    parsed_index = json.loads(index)
    paths = [write('evidence', parsed_index['evidence_sha256'], evidence),
             write('receipt', parsed_index['route_receipt_sha256'], receipt),
             write('capture', capture, index)]
    result = digest.build_digest_from_verified_capture(capture)
    assert result.capture_sha256 == capture
    assert result.report()['verified_cas_source'] is True
    assert result.original_event_count == 4
    assert result.unattributed_count == 1
    assert len(result.items) == (2 if temporal else 1)
    assert [item['status'] for item in result.items].count('merged') == 1
    assert '当日にPRがマージされました。' in _sentences(result)
    # v2 carries no updated_at attestation, so it cannot support dated claims
    # from mutable comment/review state. The original CAS remains intact.
    assert ('https://github.com/Example/Alpha/issues/9' in result.render()) is temporal
    assert ('テスト・検証に関する結果が報告されました。' in _sentences(result)) is temporal
    assert 'Mutable observation' not in result.render()
    allowed['reader'] = False
    with pytest.raises(digest.DeterministicDigestError):
        digest.build_digest_from_verified_capture(capture)
    allowed['reader'] = True
    for path in paths:
        saved = path.read_bytes()
        path.write_bytes(saved + b' ')
        with pytest.raises(digest.DeterministicDigestError):
            digest.build_digest_from_verified_capture(capture)
        path.write_bytes(saved)


@pytest.mark.parametrize('courtesy', [
    'Thanks, that looks good.', 'Looks good to me!', 'Got it.',
    'Thanks for the update.', 'Thank you.', 'Thank you for the update.',
    'Sounds good.', '承知しました。',
])
def test_courtesy_only_is_not_progress(courtesy):
    quiet = digest.build_digest_from_bundle(bundle(
        event('issue_comment', 11, body=courtesy)))
    assert quiet.items == ()
    assert quiet.render() == '進展のあったIssue・PRはありません。\n'
    substantive = digest.build_digest_from_bundle(bundle(
        event('issue_comment', 11, body='Thanks, the tests passed after the fix.')))
    assert substantive.items
    assert 'テスト・検証に関する結果が報告されました。' in _sentences(substantive)
    positive_thanks = digest.build_digest_from_bundle(bundle(
        event('issue_comment', 11, body='Thank you, the tests passed after the fix.')))
    assert 'テスト・検証に関する結果が報告されました。' in _sentences(positive_thanks)


def test_legacy_v2_keeps_immutable_merges_but_not_mutable_results():
    comment = event('issue_comment', 12,
                    body='GPU acceptance PASS confirmed in a completed test.')
    review = event('pull_request_review', 13, minute=3, state='APPROVED',
                   body='Approved after validation.')
    merge = event('pull_request_merged', 13, minute=5)
    sha, raw_index, evidence, raw_receipt = _synthetic_triplet(
        [comment, review, merge], temporal=False)
    source_ok = source._verify(raw_index, evidence, raw_receipt, sha)
    result = digest._build(source_ok.bundle, sha)
    assert result.original_event_count == 3
    assert len(result.items) == 1
    assert '当日にPRがマージされました。' in _sentences(result)
    assert 'GPU検証' not in _sentences(result)
    assert 'レビューで承認されました' not in _sentences(result)
    assert 'https://github.com/Example/Alpha/issues/12' not in result.render()


def test_verified_v3_keeps_day_scoped_reported_result_and_review():
    comment = event('issue_comment', 12,
                    body='GPU acceptance PASS confirmed in a completed test.')
    review = event('pull_request_review', 13, minute=3, state='APPROVED',
                   body='Approved after validation.')
    sha, raw_index, evidence, raw_receipt = _synthetic_triplet(
        [comment, review], temporal=True)
    source_ok = source._verify(raw_index, evidence, raw_receipt, sha)
    result = digest._build(source_ok.bundle, sha)
    assert len(result.items) == 2
    assert 'GPU検証に関する結果が報告されました。' in _sentences(result)
    assert 'レビューで承認されました。' in _sentences(result)
    assert '当日にPRがマージされました。' not in _sentences(result)


def test_v3_next_day_edited_comment_text_is_withheld():
    from obsidian_automation.github_daily_activity import _date_window
    original = event('issue_comment', 12,
                     body='GPU acceptance PASS confirmed in a completed test.').to_json()
    observation = dict(created_at=original['occurred_at'],
                       updated_at='2026-10-19T15:10:00Z', submitted_at=None,
                       observed_at_utc='2026-10-20T00:00:00Z')
    projected = source._temporal_projection(original, observation, *_date_window(DAY))
    assert projected['body'] is None
    assert projected['temporal_observation']['status'] == 'withheld_outside_target_day'


def test_self_consistent_tampered_census_entity_and_temporal_record_rejected():
    comment = event('issue_comment', 9, minute=4,
                    body='Acceptance test completed with recorded results.')
    for temporal in (False, True):
        _, raw_index, evidence, raw_receipt = _synthetic_triplet([comment],
                                                                  temporal=temporal)
        def rebind(receipt):
            receipt_bytes = _canonical_json_bytes(receipt)
            index = json.loads(raw_index)
            index['route_count'] = len(receipt['routes'])
            index['route_receipt_sha256'] = hashlib.sha256(receipt_bytes).hexdigest()
            index_bytes = _canonical_json_bytes(index)
            capture = hashlib.sha256(index_bytes).hexdigest()
            return index_bytes, evidence, receipt_bytes, capture
        receipt = json.loads(raw_receipt)
        receipt['routes'] = []
        with pytest.raises(source.VerifiedSourceError, match='census'):
            source._verify(*rebind(receipt))
        receipt = json.loads(raw_receipt)
        receipt['routes'][0]['entity'] = 'pull_request'
        with pytest.raises(source.VerifiedSourceError, match='attribution'):
            source._verify(*rebind(receipt))
        if temporal:
            receipt = json.loads(raw_receipt)
            receipt['temporal_observations'][0]['observation']['updated_at'] = (
                '2026-10-20T00:00:00Z')
            with pytest.raises(source.VerifiedSourceError, match='temporal'):
                source._verify(*rebind(receipt))
        else:
            receipt = json.loads(raw_receipt)
            receipt['temporal_policy'] = 'github-day-snapshot-v1'
            receipt['temporal_observations'] = []
            with pytest.raises(source.VerifiedSourceError):
                source._verify(*rebind(receipt))


def test_cli_private_section_only_and_no_overwrite(tmp_path, monkeypatch):
    doc = digest.build_digest_from_bundle(bundle(event('issue_created', 41,
                                                     title='New work')))
    trusted = replace(doc, capture_sha256=CAPTURE)
    monkeypatch.setattr(cli, 'build_digest_from_verified_capture', lambda sha: trusted)
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    original = (b'---\ntype: daily-review\n---\n# Project Progress\nold\n'
                b'# My Notes\nkeep this text\n')
    daily = tmp_path / 'daily.md'
    daily.write_bytes(original)
    args = ['--capture-sha', CAPTURE, '--target-date', str(DAY),
            '--output-dir', str(private), '--preview-daily', str(daily)]
    assert cli.main(args) == 0
    assert daily.read_bytes() == original
    preview = next(private.glob('*.daily-note.preview.md')).read_text()
    assert '# My Notes\nkeep this text\n' in preview
    assert 'old\n' not in preview
    assert cli.main(args) == 0  # Identical retry is idempotent.
    assert len(list(private.iterdir())) == 3


def test_cli_recovers_after_one_artifact_is_published(tmp_path, monkeypatch):
    doc = digest.build_digest_from_bundle(bundle(event('issue_created', 41)))
    trusted = replace(doc, capture_sha256=CAPTURE)
    monkeypatch.setattr(cli, 'build_digest_from_verified_capture', lambda sha: trusted)
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    daily = tmp_path / 'daily.md'
    original = (b'---\ntype: daily-review\n---\n# Project Progress\nold\n'
                b'# My Notes\nkeep this text\n')
    daily.write_bytes(original)
    args = ['--capture-sha', CAPTURE, '--target-date', str(DAY),
            '--output-dir', str(private), '--preview-daily', str(daily)]
    original_publish = cli._publish
    calls = []

    def fail_second(root, name, content):
        calls.append(name)
        if len(calls) == 2:
            raise OSError('simulated interruption')
        original_publish(root, name, content)

    monkeypatch.setattr(cli, '_publish', fail_second)
    assert cli.main(args) == 2
    assert len(list(private.iterdir())) == 1
    first = next(private.iterdir())
    contents = first.read_bytes()
    monkeypatch.setattr(cli, '_publish', original_publish)
    assert cli.main(args) == 0
    assert len(list(private.iterdir())) == 3
    assert first.read_bytes() == contents
    assert daily.read_bytes() == original
    assert cli.main(args) == 0
    assert len(list(private.iterdir())) == 3


def test_cli_atomic_artifact_write_recovers_after_link_failure(tmp_path, monkeypatch):
    doc = digest.build_digest_from_bundle(bundle(event('issue_created', 41)))
    trusted = replace(doc, capture_sha256=CAPTURE)
    monkeypatch.setattr(cli, 'build_digest_from_verified_capture', lambda sha: trusted)
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    args = ['--capture-sha', CAPTURE, '--target-date', str(DAY), '--output-dir', str(private)]
    original_link = cli.os.link

    def cannot_link(*args, **kwargs):
        raise OSError('simulated publication failure')

    monkeypatch.setattr(cli.os, 'link', cannot_link)
    assert cli.main(args) == 2
    assert list(private.iterdir()) == []  # No incomplete final or temp artifact.
    monkeypatch.setattr(cli.os, 'link', original_link)
    assert cli.main(args) == 0
    assert len(list(private.iterdir())) == 2


def test_cli_conflicting_retry_does_not_overwrite_or_publish_anything(tmp_path, monkeypatch):
    doc = digest.build_digest_from_bundle(bundle(event('issue_created', 41)))
    trusted = replace(doc, capture_sha256=CAPTURE)
    monkeypatch.setattr(cli, 'build_digest_from_verified_capture', lambda sha: trusted)
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    # A conflict in the last artifact must prevent earlier artifact creation.
    conflict = private / f'{trusted.sha256}.daily-note.preview.md'
    conflict.write_bytes(b'other-output')
    daily = tmp_path / 'daily.md'
    original = (b'---\ntype: daily-review\n---\n# Project Progress\nold\n'
                b'# My Notes\nkeep this text\n')
    daily.write_bytes(original)
    args = ['--capture-sha', CAPTURE, '--target-date', str(DAY),
            '--output-dir', str(private), '--preview-daily', str(daily)]
    assert cli.main(args) == 2
    assert list(private.iterdir()) == [conflict]
    assert conflict.read_bytes() == b'other-output'
    assert daily.read_bytes() == original


def test_cli_rejects_unsafe_output_and_wrong_day(tmp_path, monkeypatch):
    trusted = replace(digest.build_digest_from_bundle(bundle()), capture_sha256=CAPTURE)
    monkeypatch.setattr(cli, 'build_digest_from_verified_capture', lambda sha: trusted)
    public = tmp_path / 'public'
    public.mkdir(mode=0o755)
    assert cli.main(['--capture-sha', CAPTURE, '--target-date', str(DAY),
                     '--output-dir', str(public)]) == 2
    assert cli.main(['--capture-sha', CAPTURE, '--target-date', '2026-10-17']) == 2
    assert not list(public.iterdir())
