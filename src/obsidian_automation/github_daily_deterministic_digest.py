"""Conservative, source-typed Japanese Issue/PR progress; no model or Writer."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date
import hashlib
import html
import json
import re

from .github_daily_activity import _date_window
from .github_daily_issue_pr import _event_key, _is_bot, _require_event_time, DailyEntityPreviewError
from .github_daily_summary import EvidenceBundle
from .github_daily_verified_source import (
    read_verified_source, VerifiedSource, VerifiedSourceError,
    admitted_title_observations,
)

SHA = re.compile(r'[0-9a-f]{64}\Z')
CONVENTIONAL = re.compile(r'(feat|fix|test|refactor|perf|docs|chore|style)(?:\([^\r\n)]*\))?!?:', re.I)
ROUTINE = re.compile(r'\b(?:readme|documentation|docs?|format(?:ting)?|whitespace|typos?|snapshot formatting)\b', re.I)
UNCERTAIN = re.compile(
    r'\b(?:planned?|pending|not yet|no pass|will|would|todo|anticipated|'
    r'not completed|not performed|not run|not executed|'
    r'without\s+(?:running|executing)(?:\s+(?:the|a))?'
    r'(?:\s+(?:gpu|full|acceptance)){0,2}\s+test|'
    r'(?:(?:the|a)\s+)?(?:(?:gpu|full|acceptance)\s+){0,2}'
    r'test\s+was\s+never\s+(?:run|executed|performed)|'
    r'no test executed)\b|予定|未実施|結果待ち|テストを実施せず',
    re.I,
)
RESULT = re.compile(r'\b(?:pass(?:ed)?|failed|failure|completed|succeeded)\b|成功|失敗|完了|検証済み', re.I)
MARKDOWN = re.compile(r'([\\`*_{}\[\]()#!|~])')
TRANSITIONS = {
    'issue_created': 'Issueが作成されました。',
    'issue_closed': 'Issueがクローズされました。',
    'issue_reopened': 'Issueが再オープンされました。',
    'pull_request_created': 'PRが作成されました。',
    'pull_request_closed': 'PRがクローズされました（マージは未確認）。',
    'pull_request_reopened': 'PRが再オープンされました。',
    'pull_request_ready_for_review': 'PRがレビュー可能になりました。',
    'pull_request_converted_to_draft': 'PRがDraftに戻されました。',
}
COMMENTS = {'issue_comment', 'pull_request_comment', 'pull_request_review_comment',
            'pull_request_review'}
# Only entity-level GitHub records are allowed to supply display titles.
# Commit messages/comments are never accepted as Issue/PR titles.
TITLE_KINDS = frozenset({
    'issue_created', 'issue_snapshot', 'issue_closed', 'issue_reopened',
    'pull_request_created', 'pull_request_snapshot', 'pull_request_closed',
    'pull_request_reopened', 'pull_request_merged',
    'pull_request_ready_for_review', 'pull_request_converted_to_draft',
})
# Only exact courtesy replies: never suppress an approval or progress sentence
# merely because it includes a token such as 'thanks' or 'looks good'.
COURTESY_ONLY = {
    'next', 'continue', 'ack', 'acknowledged', 'ok', 'okay', 'lgtm',
    'thanks', 'thanks that looks good', 'thanks looks good', 'thanks for the update',
    'thank you', 'thank you for the update',
    'looks good', 'looks good to me', 'sounds good', 'got it',
    '了解', '承知しました', '確認しました', '👍',
}
VERSION = 'github-daily-deterministic-digest-v3'


class DeterministicDigestError(ValueError):
    """A bounded, date-scoped and source-bound digest cannot be established."""


def _inline(value: str, limit: int = 110) -> str:
    clean = ' '.join(''.join(ch if ch.isprintable() else ' ' for ch in value).split())
    if len(clean) > limit:
        clean = clean[:limit] + '…'
    return html.escape(MARKDOWN.sub(r'\\\1', clean), quote=True).replace('%', '&#37;')


def _text(row: dict, name: str) -> str:
    value = row.get(name)
    if value is None:
        return ''
    if (type(value) is not dict or type(value.get('text')) is not str
            or type(value.get('truncated')) is not bool):
        raise DeterministicDigestError('invalid original text envelope')
    return '' if value['truncated'] else value['text']


def _day_mutable_admissible(row: dict, verified_capture: bool) -> bool:
    # Legacy v2 receipts prove original event/identity, but do not attest
    # when current GitHub text or review state was last edited. Preserve the
    # original CAS, but do not render potentially next-day mutable claims.
    if not verified_capture:
        return True  # Pure test-only projection; never a published CAS result.
    temporal = row.get('temporal_observation')
    return (type(temporal) is dict
            and temporal.get('status') == 'day_snapshot_not_event_instant')


def _comment(row: dict) -> tuple[str, ...]:
    if _is_bot(row):
        return ()
    raw = row.get('body')
    if type(raw) is dict and raw.get('truncated') is True:
        return ('長文コメントが記録されました。',)
    body = _text(row, 'body').strip()
    normalized = ' '.join(re.sub(r'[,.!！。、，]', ' ', body.casefold()).split())
    if not normalized or normalized in COURTESY_ONLY:
        return ()
    if len(body) < 12 and not any(token in body.casefold() for token in
                                  ('gpu', 'pass', '検証', '修正', 'fix', '承認')):
        return ()
    if len(body.encode()) > 32768:
        return ('長文コメントが記録されました。',)
    lower = body.casefold()
    sentences = []
    actual = bool(RESULT.search(body)) and not UNCERTAIN.search(body)
    if actual and ('gpu' in lower or 'cuda' in lower) and any(
        token in lower for token in ('test', 'pass', 'acceptance', '検証', 'smoke')
    ):
        sentences.append('GPU検証に関する結果が報告されました。')
    elif actual and any(token in lower for token in ('test', 'テスト', '検証')):
        sentences.append('テスト・検証に関する結果が報告されました。')
    if any(token in lower for token in ('approved', 'authorized', 'approval',
                                        'migration', 'rollback', '承認', '方針', '移行')):
        sentences.append('方針・承認に関する更新が記録されました。')
    return tuple(sentences or ('進捗コメントが記録されました。',))


def _commit(row: dict) -> str | None:
    message = _text(row, 'message').splitlines()
    if not message:
        return None
    subject = message[0].strip()
    match = CONVENTIONAL.match(subject)
    category = match.group(1).lower() if match else 'unknown'
    if category in ('docs', 'chore', 'style'):
        return None
    if category in ('test', 'unknown') and ROUTINE.search(subject):
        return None
    if category == 'unknown' and len(subject) < 12:
        return None
    return category


@dataclass(frozen=True)
class DeterministicDigest:
    date: str
    capture_sha256: str | None
    evidence_sha256: str
    items: tuple[dict, ...]
    original_event_count: int
    unattributed_count: int
    excluded_count: int

    def render(self) -> str:
        """Repo → typed status → linked title; no shallow boilerplate phrases.

        The Writer supplies the enclosing # Project Progress H1. These four
        headings are intentionally presentation-only, sourced from existing
        typed event status; original phrases/evidence remain in the JSON
        receipt for future source-grounded summary evaluation.
        """
        if not self.items:
            return '進展のあったIssue・PRはありません。\n'
        headings = (
            ('closed_issue', 'Closed Issues'),
            ('updated_issue', 'Updated Issues'),
            ('merged_pr', 'Merged PRs'),
            ('updated_pr', 'Updated PRs'),
        )
        grouped: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
        for item in self.items:
            entity = item['entity']
            typ = entity['entity_type']
            status = item['status']
            if typ == 'issue':
                kind = 'closed_issue' if status == 'issue_closed' else 'updated_issue'
            elif typ == 'pull_request':
                kind = 'merged_pr' if status == 'merged' else 'updated_pr'
            else:
                raise DeterministicDigestError('unexpected Daily entity type')
            grouped[entity['repository']][kind].append(item)

        lines = []
        for repository in sorted(grouped):
            if lines:
                lines.append('')
            lines.append('## ' + _inline(repository, 160))
            for kind, heading in headings:
                entries = grouped[repository].get(kind, ())
                if not entries:
                    continue
                lines.append('### ' + heading)
                for item in sorted(entries, key=lambda x: x['entity']['number']):
                    entity = item['entity']
                    number = entity['number']
                    label = 'PR' if entity['entity_type'] == 'pull_request' else 'Issue'
                    route = 'pull' if label == 'PR' else 'issues'
                    url = f'https://github.com/{repository}/{route}/{number}'
                    # This is deliberately a replaceable *presentation slot*:
                    # later use a grounded summary instead of source title.
                    # Missing source titles must not be invented from an event
                    # comment or mistaken commit subject.
                    title = item['title']
                    display = _inline(title, 180) if title else 'タイトル未取得'
                    lines.append(f'- [{label} #{number}]({url}): {display}')
        body = '\n'.join(lines) + '\n'
        if len(body.encode()) > 2 * 1024 * 1024:
            raise DeterministicDigestError('render byte limit exceeded')
        return body

    def report(self) -> dict:
        return {'record_version': VERSION, 'stage': 'deterministic_preview',
                'date': self.date, 'source_capture_sha256': self.capture_sha256,
                'source_evidence_sha256': self.evidence_sha256,
                'verified_cas_source': self.capture_sha256 is not None,
                'model_used': False, 'publication_authorized': False,
                'needs_user_quality_approval': False,
                'original_event_count': self.original_event_count,
                'unattributed_default_branch_commit_count': self.unattributed_count,
                'excluded_entity_count': self.excluded_count,
                'rendered_markdown_sha256': hashlib.sha256(self.render().encode()).hexdigest(),
                'items': list(self.items)}

    @property
    def canonical_bytes(self) -> bytes:
        data = (json.dumps(self.report(), ensure_ascii=False, sort_keys=True,
                           separators=(',', ':'), allow_nan=False) + '\n').encode()
        if len(data) > 4 * 1024 * 1024:
            raise DeterministicDigestError('receipt byte limit exceeded')
        return data

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes).hexdigest()


def _build(
    bundle: EvidenceBundle, source_capture_sha256: str | None,
) -> DeterministicDigest:
    if type(bundle) is not EvidenceBundle or len(bundle.events) > 50000:
        raise DeterministicDigestError('invalid original evidence count')
    if source_capture_sha256 is not None and (type(source_capture_sha256) is not str
                                               or not SHA.fullmatch(source_capture_sha256)):
        raise DeterministicDigestError('invalid capture reference')
    try:
        target = date.fromisoformat(bundle.date)
        start, end = _date_window(target)
        if (target.isoformat() != bundle.date or bundle.timezone != 'Asia/Tokyo'
                or bundle.window_start != start.isoformat().replace('+00:00', 'Z')
                or bundle.window_end != end.isoformat().replace('+00:00', 'Z')):
            raise DeterministicDigestError('wrong JST source window')
        grouped = defaultdict(list)
        unattributed = 0
        for row in bundle.events:
            _require_event_time(row.get('occurred_at'), start, end)
            key = _event_key(row)
            if key is None:
                unattributed += 1
            else:
                grouped[key].append(row)
    except (TypeError, ValueError, DailyEntityPreviewError) as exc:
        raise DeterministicDigestError('invalid dated source event/identity') from exc
    if len(grouped) > 10000:
        raise DeterministicDigestError('entity count exceeded')
    items = []
    excluded = 0
    for key, rows in sorted(grouped.items()):
        rows = sorted(rows, key=lambda row: (row['occurred_at'], row['evidence_id']))
        commits = [(kind, row) for row in rows if row['kind'] == 'pull_request_commit'
                   and not _is_bot(row) if (kind := _commit(row)) is not None]
        comments = [(phrase, row) for row in rows if row['kind'] in COMMENTS
                    and _day_mutable_admissible(row, source_capture_sha256 is not None)
                    for phrase in _comment(row)]
        reviews = [row for row in rows if row['kind'] == 'pull_request_review'
                   and _day_mutable_admissible(row, source_capture_sha256 is not None)
                   and row.get('state') in ('APPROVED', 'CHANGES_REQUESTED') and not _is_bot(row)]
        merges = [row for row in rows if row['kind'] == 'pull_request_merged']
        transitions = [row for row in rows if row['kind'] in TRANSITIONS
                       and not (_is_bot(row) and row['kind'] in ('issue_created',
                                                                 'pull_request_created'))]
        if not (commits or comments or reviews or merges or transitions):
            excluded += 1
            continue
        if (not commits and not comments and not reviews and not merges
                and transitions and all(row['kind'] == 'pull_request_created' for row in transitions)
                and any(row['kind'] == 'pull_request_commit' for row in rows)):
            excluded += 1
            continue
        phrases, ids = [], []
        def add(phrase, relevant):
            if phrase in phrases or len(phrases) >= 3:
                return
            phrases.append(phrase)
            for record in relevant:
                if record['evidence_id'] not in ids:
                    ids.append(record['evidence_id'])
        if commits:
            kinds = {kind for kind, _ in commits}
            phrase = ({frozenset({'test'}): '事前検証を含むテストコードの変更が記録されました。',
                       frozenset({'feat'}): '実装コミットが記録されました。',
                       frozenset({'fix'}): '修正コミットが記録されました。'}
                      .get(frozenset(kinds), 'コミットが記録されました（変更内容はGitHubで確認）。'))
            add(phrase, [row for _, row in commits])
        chosen = {phrase: row for phrase, row in comments}
        for phrase in ('GPU検証に関する結果が報告されました。',
                       'テスト・検証に関する結果が報告されました。',
                       '方針・承認に関する更新が記録されました。',
                       '長文コメントが記録されました。', '進捗コメントが記録されました。'):
            if phrase in chosen and len(phrases) < 3 - bool(merges):
                if phrase != '進捗コメントが記録されました。' or not phrases:
                    add(phrase, [chosen[phrase]])
        if not merges:
            if reviews:
                add('レビューで承認されました。' if reviews[-1]['state'] == 'APPROVED'
                    else 'レビューで変更要求がありました。', [reviews[-1]])
            if transitions and (not phrases or transitions[-1]['kind'] not in
                                ('issue_created', 'pull_request_created')):
                add(TRANSITIONS[transitions[-1]['kind']], [transitions[-1]])
        if merges:
            phrases = phrases[:2]
            add('当日にPRがマージされました。', [merges[-1]])
        if not phrases:
            add('更新が記録されました（詳細はGitHub）。', [rows[-1]])
        # Titles are display labels, not assertions about when a title was
        # edited. Use only source-bound nontruncated GitHub title envelopes.
        # A recorded commit subject is NOT necessarily this PR's title.
        candidates = [_text(row, 'title').strip() for row in rows
                      if row['kind'] in TITLE_KINDS and row.get('title') is not None]
        title = next((name for name in reversed(candidates) if name), '')
        items.append({'entity': {'repository': key.repository,
                                 'entity_type': key.entity_type, 'number': key.number},
                      'title': title,
                      'title_source': 'source_event_envelope' if title else 'missing',
                      'title_observed_at_utc': None,
                      'status': 'merged' if merges else
                      (transitions[-1]['kind'] if transitions else 'updated'),
                      'sentences': phrases, 'evidence_ids': ids,
                      'source_event_count': len(rows)})
    return DeterministicDigest(bundle.date, source_capture_sha256, bundle.sha256,
                               tuple(items), len(bundle.events), unattributed, excluded)


def _build_admitted_source(source: VerifiedSource) -> DeterministicDigest:
    """Apply supplemental titles ONLY from the actual admitted CAS source.

    The pure _build(bundle, sha) cannot accept arbitrary mutable title
    records: they must be obtained through read_verified_source or the
    nonroot Standard-CAS _verify gate, which mints an admission marker after
    validating the exact capture-index → route-receipt → Evidence chain.
    """
    try:
        supplement = admitted_title_observations(source)
    except VerifiedSourceError as exc:
        raise DeterministicDigestError('title observations not admitted from CAS') from exc
    digest = _build(source.bundle, source.capture_sha256)
    if digest.unattributed_count != source.unattributed_count:
        raise DeterministicDigestError('original source census incomplete')
    if not supplement:
        return digest
    indexed = {}
    for observation in supplement:
        key = (observation['repository'], observation['entity_type'],
               observation['number'])
        if key in indexed:
            raise DeterministicDigestError('duplicate admitted title observation')
        indexed[key] = observation
    final = []
    for item in digest.items:
        key = (item['entity']['repository'], item['entity']['entity_type'],
               item['entity']['number'])
        obs = indexed.get(key)
        if obs is None:
            final.append(item)
            continue
        if item['title']:
            raise DeterministicDigestError('admitted title contradicts source envelope')
        final.append({
            **item, 'title': obs['title'],
            'title_source': 'collection_observation',
            'title_observed_at_utc': obs['observed_at_utc'],
        })
    # A title record for an excluded/no-progress entity is legitimate
    # source metadata, not an instruction to show a suppressed entry.
    return replace(digest, items=tuple(final))


def build_digest_from_verified_capture(capture_sha256: str) -> DeterministicDigest:
    try:
        source = read_verified_source(capture_sha256)
    except VerifiedSourceError as exc:
        raise DeterministicDigestError('trusted CAS/C1a source not admitted') from exc
    if source.capture_sha256 != capture_sha256:
        raise DeterministicDigestError('verified capture changed identity')
    return _build_admitted_source(source)


def build_digest_from_bundle(bundle: EvidenceBundle) -> DeterministicDigest:
    """Pure unverified test projection; never accepts a claimed capture identity."""
    return _build(bundle, None)
