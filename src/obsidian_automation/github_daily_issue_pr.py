"""Narrow original-event Issue/PR identity checks, never inferred from prose."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Mapping
from urllib.parse import urlsplit

REPO = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z')
ISSUE = {'issue_created', 'issue_snapshot', 'issue_closed', 'issue_reopened',
         'issue_comment'}
PR = {'pull_request_created', 'pull_request_snapshot', 'pull_request_closed',
      'pull_request_reopened', 'pull_request_merged',
      'pull_request_ready_for_review', 'pull_request_converted_to_draft',
      'pull_request_comment', 'pull_request_review',
      'pull_request_review_comment', 'pull_request_commit'}


class DailyEntityPreviewError(ValueError):
    pass


@dataclass(frozen=True, order=True)
class EntityKey:
    repository: str
    entity_type: str
    number: int


def _require_event_time(value: object, start: datetime, end: datetime) -> datetime:
    if type(value) is not str or not value.endswith('Z'):
        raise DailyEntityPreviewError('event time is not canonical UTC')
    try:
        actual = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise DailyEntityPreviewError('event time invalid') from exc
    if (actual.tzinfo != timezone.utc or not start <= actual < end
            or actual.isoformat().replace('+00:00', 'Z') != value):
        raise DailyEntityPreviewError('event outside JST day')
    return actual


def _event_key(row: Mapping[str, object]) -> EntityKey | None:
    kind, repo, number = row.get('kind'), row.get('repository'), row.get('number')
    if (type(repo) is not str or not REPO.fullmatch(repo)
            or any(part in ('.', '..') for part in repo.split('/'))):
        raise DailyEntityPreviewError('invalid repository')
    if kind == 'default_branch_commit':
        if row.get('entity_type') != 'commit' or number is not None:
            raise DailyEntityPreviewError('unattributed commit changed identity')
        entity = None
    elif kind in ISSUE | PR:
        entity = 'issue' if kind in ISSUE else 'pull_request'
        expected_type = ('commit' if kind == 'pull_request_commit' else
                         kind if kind in {'issue_comment', 'pull_request_comment',
                                          'pull_request_review',
                                          'pull_request_review_comment'} else entity)
        if row.get('entity_type') != expected_type or type(number) is not int or number < 1:
            raise DailyEntityPreviewError('kind/entity/number contradiction')
    else:
        raise DailyEntityPreviewError('unknown event kind')
    sha = row.get('sha')
    suffix = ('commit/' + sha if entity is None or kind == 'pull_request_commit' else
              ('pull' if entity == 'pull_request' and kind != 'pull_request_comment'
               else 'issues') + '/' + str(number))
    # Conversation comments can use /pull/N or /issues/N. A public URL alone
    # never resolves this ambiguity: the CAS route receipt must classify it.
    allowed = {f'/{suffix}'}
    if kind == 'pull_request_comment':
        allowed.add(f'/pull/{number}')
    raw = row.get('url')
    if type(raw) is not str:
        raise DailyEntityPreviewError('event URL missing')
    parsed = urlsplit(raw)
    if (parsed.scheme != 'https' or parsed.netloc != 'github.com'
            or parsed.query or parsed.path not in {f'/{repo}{s}' for s in allowed}
            or (parsed.fragment and not (
                kind in {'issue_comment', 'pull_request_comment',
                         'pull_request_review', 'pull_request_review_comment'}))
            or (entity is None or kind == 'pull_request_commit') and
            (type(sha) is not str or not re.fullmatch(r'[a-f0-9]{40,64}', sha))):
        raise DailyEntityPreviewError('event URL does not bind entity')
    return None if entity is None else EntityKey(repo, entity, number)


def _is_bot(row: Mapping[str, object]) -> bool:
    actor = row.get('actor')
    return isinstance(actor, str) and (actor.casefold().endswith('[bot]')
        or actor.casefold() in {'github-actions', 'dependabot', 'renovate'})
