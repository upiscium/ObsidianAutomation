"""Opt-in day-scoped mutable-field projection for fresh source receipts (#357).

A GitHub event timestamp is not the observation time of its current resource.
Original bounded events remain in the private receipt, never in model evidence
when their mutable fields cannot be placed within the requested JST day.
This is an API observation policy, not a historical database or a signature.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import hashlib
import json
import re

from . import github_daily_activity as activity
from . import github_project_watcher as watcher

POLICY = "github-day-snapshot-v1"
RECEIPT_VERSION = 3
MAX_OBSERVATIONS = 50000
MAX_SOURCE_ROWS = 50000
MAX_SOURCE_BYTES = 32 * 1024 * 1024
MAX_RECEIPT_BYTES = 4 * 1024 * 1024
MUTABLE_FIELDS = ("body", "draft", "message", "state", "title")
_COMMENTS = {"issue_comment": "comment", "pull_request_comment": "comment",
             "pull_request_review_comment": "review_comment"}
_OBJECTS = {"issue_created", "pull_request_created", "issue_snapshot", "pull_request_snapshot"}
_COMMITS = {"default_branch_commit", "pull_request_commit"}
_PATH = re.compile(r"^/repos/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(/.*)$")


class TemporalEvidenceError(ValueError):
    """Temporal source observation or exact projection cannot be admitted."""


def _sha(value):
    return hashlib.sha256(activity._canonical_json_bytes(value)).hexdigest()


def _stamp(value, *, optional=False):
    if value is None and optional:
        return None
    try:
        parsed = activity._timestamp(value, label="temporal source timestamp")
        canonical = activity._format_timestamp(parsed)
    except (ValueError, TypeError, OverflowError, activity.GitHubDailyActivityError,
            watcher.GitHubProjectWatcherError):
        raise TemporalEvidenceError("invalid temporal source timestamp") from None
    return canonical


def _bounded_json(value, limit, *, materialize=False):
    """Charge canonical chunks before retaining a complete aggregate encoding."""
    size, chunks = 1, []  # canonical trailing newline
    try:
        encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":"), allow_nan=False)
        for chunk in encoder.iterencode(value):
            encoded = chunk.encode("utf-8")
            size += len(encoded)
            if size > limit:
                raise TemporalEvidenceError("temporal aggregate byte limit exceeded")
            if materialize:
                chunks.append(encoded)
    except TemporalEvidenceError:
        raise
    except (TypeError, ValueError, UnicodeError):
        raise TemporalEvidenceError("temporal JSON cannot be encoded") from None
    return b"".join(chunks) + b"\n" if materialize else size


def encode_receipt(payload):
    return _bounded_json(payload, MAX_RECEIPT_BYTES, materialize=True)


class ObservationClient:
    """Forward existing bounded API calls, keeping only observation timestamps.

    Wrapped only AFTER the source collector validates the pinned real client.
    This changes neither URL destinations nor pagination nor authentication.
    """
    def __init__(self, client, clock):
        self._client, self._clock = client, clock
        self.observations = {}
        self.rows_seen = 0
        self.bytes_seen = 0

    def _charge(self, value, count):
        # Includes unobserved commit/timeline rows and classification responses,
        # not just objects stored in the timestamp map. The reused client's
        # per-endpoint page allocation still precedes this wrapper boundary.
        if self.rows_seen + count > MAX_SOURCE_ROWS:
            raise TemporalEvidenceError("temporal source row limit exceeded")
        size = _bounded_json(value, MAX_SOURCE_BYTES - self.bytes_seen)
        self.rows_seen += count
        self.bytes_seen += size

    def __getattr__(self, name):
        return getattr(self._client, name)

    def _remember(self, key, row):
        if not isinstance(row, dict):
            raise TemporalEvidenceError("invalid observed resource")
        value = {"created_at": _stamp(row.get("created_at"), optional=True),
                 "updated_at": _stamp(row.get("updated_at"), optional=True),
                 "submitted_at": _stamp(row.get("submitted_at"), optional=True)}
        previous = self.observations.get(key)
        if previous is not None and any(previous[k] != v for k, v in value.items()):
            raise TemporalEvidenceError("resource timestamps changed during route verification")
        if previous is None and len(self.observations) >= MAX_OBSERVATIONS:
            raise TemporalEvidenceError("temporal observation limit exceeded")
        value["observed_at_utc"] = activity._format_timestamp(self._clock())
        self.observations[key] = value

    def _record(self, path, values):
        match = _PATH.fullmatch(path.split("?", 1)[0])
        if match is None:
            return
        repo, suffix = match.groups()
        for row in values:
            if not isinstance(row, dict):
                raise TemporalEvidenceError("invalid observed source row")
            if suffix == "/issues":
                self._remember((repo, "object", str(row.get("number"))), row)
            elif suffix == "/issues/comments" or re.fullmatch(r"/issues/comments/[1-9][0-9]*", suffix):
                self._remember((repo, "comment", str(row.get("id"))), row)
            elif suffix == "/pulls/comments" or re.fullmatch(r"/pulls/comments/[1-9][0-9]*", suffix):
                self._remember((repo, "review_comment", str(row.get("id"))), row)
            elif re.fullmatch(r"/pulls/[1-9][0-9]*/reviews", suffix):
                self._remember((repo, "review", str(row.get("id"))), row)

    def _paged(self, path):
        rows = self._client._paged(path)
        if type(rows) is not list:
            raise TemporalEvidenceError("observed pagination is not a complete list")
        self._charge(rows, len(rows))
        self._record(path, rows)
        return rows

    def _request_json(self, path):
        value = self._client._request_json(path)
        self._charge(value, len(value) if type(value) is list else 1)
        # Only direct comment rechecks affect observations. Other endpoints
        # (classification, commit lists) do not establish mutable-field times.
        suffix = path.split("?", 1)[0]
        if re.search(r"/(issues|pulls)/comments/[1-9][0-9]*$", suffix):
            self._record(path, [value])
        return value

    def for_event(self, event):
        kind = event.kind
        if kind in _OBJECTS:
            key = (event.repository, "object", str(event.number))
        elif kind in _COMMENTS:
            key = (event.repository, _COMMENTS[kind], event.source_id)
        elif kind == "pull_request_review":
            key = (event.repository, "review", event.source_id)
        else:
            # Lifecycle embedded issue objects are not historical snapshots;
            # commits have a content identity but not a push-time guarantee.
            return {"created_at": None, "updated_at": None, "submitted_at": None,
                    "observed_at_utc": activity._format_timestamp(self._clock())}
        result = self.observations.get(key)
        if result is None:
            raise TemporalEvidenceError("missing temporal source observation")
        return dict(result)


def project_row(original, observation, *, start: datetime, end: datetime):
    """Deterministically reproduce the period-safe event from a private snapshot."""
    if (type(original) is not dict or "temporal_observation" in original
            or type(observation) is not dict
            or set(observation) != {"created_at", "updated_at", "submitted_at", "observed_at_utc"}):
        raise TemporalEvidenceError("temporal projection schema mismatch")
    event_id = original.get("evidence_id")
    if (type(event_id) is not str or re.fullmatch(r"[0-9a-f]{64}", event_id) is None
            or _sha({k: v for k, v in original.items() if k != "evidence_id"}) != event_id):
        raise TemporalEvidenceError("original temporal snapshot hash mismatch")
    stamps = {k: _stamp(v, optional=k != "observed_at_utc") for k, v in observation.items()}
    if stamps != observation:
        raise TemporalEvidenceError("noncanonical temporal observation")
    occurred = activity._timestamp(_stamp(original["occurred_at"]), label="temporal event")
    observed = activity._timestamp(stamps["observed_at_utc"], label="temporal observation")
    updated = None if stamps["updated_at"] is None else activity._timestamp(stamps["updated_at"], label="temporal update")
    if not start <= occurred < end or observed < occurred:
        raise TemporalEvidenceError("temporal event/observation window mismatch")
    kind = original["kind"]
    if kind in _COMMITS:
        status = "content_addressed_commit_not_push_time"
        keep = True
    elif kind in _OBJECTS or kind in _COMMENTS or kind == "pull_request_review":
        if kind in _OBJECTS and kind.endswith("snapshot"):
            identity_time = stamps["updated_at"]
        else:
            identity_time = stamps["submitted_at"] if kind == "pull_request_review" else stamps["created_at"]
        if identity_time != original["occurred_at"]:
            raise TemporalEvidenceError("observation does not identify original event time")
        created = stamps["submitted_at"] if kind == "pull_request_review" else stamps["created_at"]
        if updated is not None and (updated > observed or created is not None and updated < activity._timestamp(created, label="temporal creation")):
            raise TemporalEvidenceError("inconsistent resource update time")
        keep = updated is not None and start <= updated < end
        status = "day_snapshot_not_event_instant" if keep else (
            "withheld_unknown_modification_time" if updated is None else "withheld_outside_target_day")
    else:
        # A timeline action establishes its transition, not its current nested
        # issue title/state. Downstream derives transitions from event.kind.
        keep = False
        status = "event_transition_only"
    withheld = [k for k in MUTABLE_FIELDS if not keep and original.get(k) is not None]
    projected = {k: v for k, v in original.items() if k != "evidence_id"}
    for key in withheld:
        projected[key] = None
    projected["temporal_observation"] = {
        "policy": POLICY, "status": status,
        "observed_at_utc": stamps["observed_at_utc"],
        "resource_updated_at": stamps["updated_at"],
        "original_event_sha256": event_id, "withheld_fields": withheld,
        "historical_instant_verified": False,
    }
    return dict(evidence_id=_sha(projected), **projected)


def project_events(events, recorder, *, start, end):
    if type(events) not in (tuple, list) or len(events) > MAX_OBSERVATIONS:
        raise TemporalEvidenceError("temporal projected event count limit exceeded")
    projected, records, mapping = [], [], {}
    record_bytes = 2  # array delimiters; final envelope is separately bounded
    for event in events:
        original, observation = event.to_json(), recorder.for_event(event)
        row = project_row(original, observation, start=start, end=end)
        record = {"evidence_id": row["evidence_id"],
                  "observation": observation, "original_event": original}
        record_bytes += _bounded_json(record, MAX_RECEIPT_BYTES - record_bytes)
        mapping[event.evidence_id] = row["evidence_id"]
        fields = {k: None if row[k] is None else activity.TextExcerpt(**row[k])
                  for k in ("title", "body", "message")}
        projected.append(replace(event, evidence_id=row["evidence_id"],
            state=row["state"], draft=row["draft"], **fields,
            temporal_observation=row["temporal_observation"]))
        records.append(record)
    records.sort(key=lambda row: row["evidence_id"])
    return projected, records, mapping


def validate_records(records, events, *, start, end):
    if type(records) is not list or len(records) != len(events) or len(records) > MAX_OBSERVATIONS:
        raise TemporalEvidenceError("incomplete temporal receipt")
    expected = {e["evidence_id"]: e for e in events}
    seen, originals, ordered = set(), [], []
    for record in records:
        if type(record) is not dict or set(record) != {"evidence_id", "observation", "original_event"}:
            raise TemporalEvidenceError("temporal record schema mismatch")
        eid = record["evidence_id"]
        if type(eid) is not str or eid in seen or eid not in expected:
            raise TemporalEvidenceError("unknown or duplicate temporal event")
        reproduced = project_row(record["original_event"], record["observation"], start=start, end=end)
        if activity._canonical_json_bytes(reproduced) != activity._canonical_json_bytes(expected[eid]):
            raise TemporalEvidenceError("period-safe Evidence is not its exact projection")
        seen.add(eid); ordered.append(eid); originals.append(record["original_event"])
    if ordered != sorted(ordered) or seen != set(expected):
        raise TemporalEvidenceError("temporal receipt ordering or coverage mismatch")
    return originals
