"""Bounded, source-identity-bound current GitHub title observations (#444).

An observed title is a *label at collection time*, NEVER a dated claim that
the Issue/PR title existed when a previous-JST-day event occurred. Data is
carried in new v4 source-route receipts, not injected into original events.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable

from . import github_daily_activity as activity
from .github_daily_issue_pr import _event_key, EntityKey, DailyEntityPreviewError

RECEIPT_VERSION = 4
MAX_OBSERVATIONS = 128  # bounded optional API cost; missing labels degrade safely
MAX_TITLE_BYTES = activity.TITLE_MAX_BYTES
MAX_ELAPSED = timedelta(hours=2)
# Only canonical GitHub entity-level lifecycle/snapshot events supply titles.
ENTITY_TITLE_KINDS = frozenset({
    'issue_created', 'issue_snapshot', 'issue_closed', 'issue_reopened',
    'pull_request_created', 'pull_request_snapshot', 'pull_request_closed',
    'pull_request_reopened', 'pull_request_merged',
    'pull_request_ready_for_review', 'pull_request_converted_to_draft',
})


class TitleObservationError(ValueError):
    """A title observation has no bounded, dated, typed source identity."""


def title_in_original(row: dict) -> bool:
    envelope = row.get('title')
    return (row.get('kind') in ENTITY_TITLE_KINDS and
            type(envelope) is dict and envelope.get('truncated') is False and
            type(envelope.get('text')) is str and bool(envelope['text'].strip()))


def missing_title_keys(events: Iterable[dict]) -> tuple[EntityKey, ...]:
    """Scope optional lookups to real entities without admitted source titles."""
    seen = set()
    with_titles = set()
    try:
        for event in events:
            key = _event_key(event)
            if key is None:
                continue
            seen.add(key)
            if title_in_original(event):
                with_titles.add(key)
    except (DailyEntityPreviewError, ValueError, TypeError) as exc:
        raise TitleObservationError('invalid title target entity') from exc
    return tuple(sorted(seen - with_titles))


def _stamp(value: object) -> datetime:
    if type(value) is not str or not value.endswith('Z'):
        raise TitleObservationError('title observation UTC timestamp invalid')
    try:
        actual = activity._timestamp(value, label='title observation time')
        if activity._format_timestamp(actual) != value:
            raise TitleObservationError('noncanonical title observation time')
    except (ValueError, TypeError, OverflowError, activity.GitHubDailyActivityError):
        raise TitleObservationError('title observation UTC timestamp invalid') from None
    return actual


def validate_records(records, events, *, captured_at_utc: str) -> tuple[dict, ...]:
    """Validate v4 receipt title records against the immutable dated Evidence."""
    if type(records) is not list or len(records) > MAX_OBSERVATIONS:
        raise TitleObservationError('title observation count invalid')
    captured = _stamp(captured_at_utc)
    missing = set(missing_title_keys(events))
    order = []
    validated = []
    keys = {'repository', 'entity_type', 'number', 'route',
            'observed_at_utc', 'title'}
    for row in records:
        if type(row) is not dict or set(row) != keys:
            raise TitleObservationError('title observation schema mismatch')
        repo, typ, num = row['repository'], row['entity_type'], row['number']
        if type(repo) is not str or type(typ) is not str or type(num) is not int:
            raise TitleObservationError('title observation identity type mismatch')
        key = EntityKey(repo, typ, num)
        if key not in missing:
            raise TitleObservationError('unexpected title observation entity')
        owner, name = repo.split('/')
        expected = f'/repos/{owner}/{name}/issues/{num}'
        if row['route'] != expected:
            raise TitleObservationError('title observation route not canonical')
        title = row['title']
        if (type(title) is not str or not title.strip() or title != title.strip()
                or len(title.encode('utf-8')) > MAX_TITLE_BYTES):
            raise TitleObservationError('title observation text not bounded')
        observed = _stamp(row['observed_at_utc'])
        if not captured <= observed <= captured + MAX_ELAPSED:
            raise TitleObservationError('title observation outside capture interval')
        order.append(key)
        validated.append(row)
    if order != sorted(set(order)):
        raise TitleObservationError('duplicate or unsorted title observations')
    return tuple(validated)
