"""Bounded private cross-event consistency challenge for ONE GitHub Issue/PR.

The C4 per-sentence Grounding verdict refers to *cited* events. This
independent diagnostic includes the other captured events of that same
entity and asks whether they qualify/contradict the displayed prose.
Model judgments and source-provided timestamps remain advisory, never
a release decision. There are no model/Collector/Writer calls here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re

STAGE = "github_daily_entity_consistency_challenge_v1"
RESULT_STAGE = "github_daily_entity_consistency_advisory_v1"
POLICY = "github-day-snapshot-v1"
MAX_SOURCE_REVIEW_BYTES = 2 * 1024 * 1024
MAX_PREVIEW_BYTES = 4 * 1024 * 1024
MAX_PACKET_BYTES = 128 * 1024
MAX_REPLY_BYTES = 8 * 1024
MAX_EVENTS = 32
MAX_FIELDS = 96
MAX_SENTENCES = 3
SHA = re.compile(r"[a-f0-9]{64}\Z")
JST = timezone(timedelta(hours=9))
STATUSES = frozenset(("day_snapshot_not_event_instant",
    "content_addressed_commit_not_push_time", "event_transition_only",
    "withheld_outside_target_day", "withheld_unknown_modification_time"))
VERDICTS = frozenset(("potential_conflict", "possible_omission",
    "no_conflict_observed", "uncertain"))


class EntityConsistencyError(ValueError):
    """Unbound input or unadmitted advisory fails without rewriting prose."""


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json(value) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False,
                       separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _object(data, limit, what, *, canonical=True):
    if type(data) is not bytes or not 0 < len(data) <= limit:
        raise EntityConsistencyError("invalid bounded " + what)
    def distinct_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    try:
        value = json.loads(data, object_pairs_hook=distinct_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if type(value) is not dict or canonical and _json(value) != data:
            raise ValueError()
    except (ValueError, TypeError, OverflowError, UnicodeError, RecursionError):
        raise EntityConsistencyError("invalid JSON " + what) from None
    return value


def _digest(s):
    return type(s) is str and SHA.fullmatch(s) is not None


def _entity(entity):
    if (type(entity) is not dict
            or set(entity) != {"repository", "entity_type", "number"}
            or type(entity["repository"]) is not str
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", entity["repository"])
            or entity["entity_type"] not in ("issue", "pull_request")
            or type(entity["number"]) is not int or entity["number"] <= 0):
        raise EntityConsistencyError("invalid exact entity identity")
    return entity["repository"], entity["entity_type"], entity["number"]


def _time(s):
    if type(s) is not str:
        raise EntityConsistencyError("invalid event timestamp")
    try:
        obj = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if obj.tzinfo is None:
            raise ValueError()
        return obj.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise EntityConsistencyError("invalid event timestamp") from None


@dataclass(frozen=True)
class EntityConsistencyPacket:
    canonical_bytes: bytes

    @property
    def sha256(self):
        return _sha(self.canonical_bytes)

    def report(self):
        return json.loads(self.canonical_bytes)


@dataclass(frozen=True)
class EntityConsistencyAdvisory:
    canonical_bytes: bytes

    @property
    def sha256(self):
        return _sha(self.canonical_bytes)

    def report(self):
        return json.loads(self.canonical_bytes)


def build_entity_consistency_packet(source_review_bytes: bytes, preview_bytes: bytes,
                                    target_entity: tuple[str, str, int], *,
                                    legacy_offline_diagnostic: bool = False):
    """Create a private same-entity challenge without a new CAS/model read.

    Strict mode requires the explicit #357 day-policy metadata for EVERY
    event. The legacy switch only permits diagnostics and is unmistakably
    marked unverified. It must never authorize date-valid conclusions.
    """
    if type(legacy_offline_diagnostic) is not bool:
        raise EntityConsistencyError("invalid temporal policy switch")
    source = _object(source_review_bytes, MAX_SOURCE_REVIEW_BYTES, "source review")
    preview = _object(preview_bytes, MAX_PREVIEW_BYTES, "narrative preview")
    if (type(source.get("record_version")) is not int or source["record_version"] != 1
            or source.get("stage") != "c5-literal-source-review-v1"
            or type(preview.get("record_version")) is not int
            or preview["record_version"] != 4
            or preview.get("stage") != "github_daily_narrative_private_preview_v4"
            or source.get("publication_authorized") is not False
            or preview.get("publication_authorized") is not False
            or source.get("preview_sha256") != _sha(preview_bytes)
            or source.get("source_context_set_sha256") != preview.get("source_context_set_sha256")
            or source.get("capture_sha256") != preview.get("capture_sha256")
            or type(source.get("entities")) is not list
            or type(preview.get("entities")) is not list):
        raise EntityConsistencyError("source review/preview provenance mismatch")
    if (type(target_entity) is not tuple or len(target_entity) != 3
            or type(target_entity[0]) is not str or type(target_entity[1]) is not str
            or type(target_entity[2]) is not int or target_entity[2] <= 0):
        raise EntityConsistencyError("invalid selected entity")
    def matching(rows):
        selected = []
        for row in rows:
            if type(row) is not dict or type(row.get("entity")) is not dict:
                raise EntityConsistencyError("malformed entity container")
            if _entity(row["entity"]) == target_entity:
                selected.append(row)
        return selected
    sr = matching(source["entities"])
    pr = matching(preview["entities"])
    if len(sr) != 1 or len(pr) != 1:
        raise EntityConsistencyError("ambiguous/unrequested selected entity")
    sr, pr = sr[0], pr[0]
    if (sr.get("context_sha256") != pr.get("context_sha256")
            or not _digest(sr.get("context_sha256"))
            or type(sr.get("source_event_count")) is not int
            or not 1 <= sr["source_event_count"] <= MAX_EVENTS
            or type(sr.get("fields")) is not list
            or len(sr["fields"]) > MAX_FIELDS
            or type(pr.get("sentences")) is not list
            or len(pr["sentences"]) > MAX_SENTENCES):
        raise EntityConsistencyError("selected entity context structure invalid")
    if type(preview.get("date")) is not str:
        raise EntityConsistencyError("target date absent")
    try:
        date = datetime.strptime(preview["date"], "%Y-%m-%d").replace(tzinfo=JST)
    except ValueError:
        raise EntityConsistencyError("invalid target date") from None
    lower, upper = date.astimezone(timezone.utc), (date + timedelta(days=1)).astimezone(timezone.utc)
    notes = sr.get("temporal_observations")
    if notes is None:
        if not legacy_offline_diagnostic:
            raise EntityConsistencyError("period-safe event observations required")
        legacy = True
        note_map = {}
    else:
        legacy = False
        if (type(notes) is not list or len(notes) != sr["source_event_count"]
                or len(notes) > MAX_EVENTS):
            raise EntityConsistencyError("incomplete temporal source observations")
        note_map = {}
        for item in notes:
            if type(item) is not dict or set(item) != {"source_ref", "evidence_id", "observation"}:
                raise EntityConsistencyError("invalid source observation item")
            ref, eid, obs = item["source_ref"], item["evidence_id"], item["observation"]
            if (type(ref) is not int or not 0 <= ref < sr["source_event_count"]
                    or ref in note_map or not _digest(eid) or type(obs) is not dict
                    or obs.get("policy") != POLICY or obs.get("status") not in STATUSES
                    or obs.get("historical_instant_verified") is not False
                    or type(obs.get("withheld_fields")) is not list
                    or not _digest(obs.get("original_event_sha256"))):
                raise EntityConsistencyError("invalid day-policy identity/scope")
            _time(obs.get("observed_at_utc"))
            if obs.get("resource_updated_at") is not None:
                _time(obs["resource_updated_at"])
            if obs["status"] == "day_snapshot_not_event_instant":
                u = _time(obs["resource_updated_at"])
                if not lower <= u < upper:
                    raise EntityConsistencyError("mutable field update outside target day")
            if type(obs["withheld_fields"]) is not list or any(
                    type(n) is not str or n not in ("title", "body", "message", "draft", "state")
                    for n in obs["withheld_fields"]):
                raise EntityConsistencyError("invalid withheld field declaration")
            note_map[ref] = (eid, obs)
        if (len(note_map) != sr["source_event_count"]
                or len({eid for eid, _ in note_map.values()}) != len(note_map)):
            raise EntityConsistencyError("period metadata missing or duplicate events")
    field_ids, source_rows, event_ids = set(), [], {}
    for row in sr["fields"]:
        if type(row) is not dict:
            raise EntityConsistencyError("invalid source field")
        ref, eid, name, text = (row.get(k) for k in ("source_ref", "evidence_id", "field", "text"))
        if (type(ref) is not int or not 0 <= ref < sr["source_event_count"]
                or not _digest(eid) or name not in ("title", "body", "message")
                or type(text) is not str or not 0 < len(text.encode("utf-8")) <= 65536
                or row.get("text_sha256") != _sha(text.encode("utf-8"))
                or type(row.get("kind")) is not str
                or type(row.get("occurred_at")) is not str):
            raise EntityConsistencyError("invalid source field identity/text")
        when = _time(row["occurred_at"])
        if not lower <= when < upper:
            raise EntityConsistencyError("source event outside target day")
        if not legacy:
            _, observation = note_map[ref]
            observed = _time(observation["observed_at_utc"])
            if observed < when or (observation["resource_updated_at"] is not None
                    and _time(observation["resource_updated_at"]) > observed):
                raise EntityConsistencyError("source observation predates event or update")
        if (ref, name) in field_ids or ref in event_ids and event_ids[ref] != eid:
            raise EntityConsistencyError("duplicate/mixed source event field")
        field_ids.add((ref, name)); event_ids[ref] = eid
        if not legacy:
            expected, obs = note_map[ref]
            if expected != eid or name in obs["withheld_fields"]:
                raise EntityConsistencyError("projected source field was withheld")
            if obs["status"] not in ("day_snapshot_not_event_instant",
                                      "content_addressed_commit_not_push_time"):
                raise EntityConsistencyError("non-day-valid mutable source field")
            commit_kind = row["kind"] in ("pull_request_commit", "default_branch_commit")
            if (obs["status"] == "content_addressed_commit_not_push_time"
                    and (not commit_kind or name != "message")
                    or obs["status"] == "day_snapshot_not_event_instant" and commit_kind):
                raise EntityConsistencyError("event kind and temporal scope disagree")
            scope = obs["status"]
        else:
            scope = "legacy_current_API_snapshot_unverified"
        source_rows.append({"source_ref": ref, "field": name, "evidence_id": eid,
            "event_kind": row["kind"], "occurred_at": row["occurred_at"],
            "text": text, "text_sha256": row["text_sha256"], "temporal_scope": scope})
    if not legacy:
        for ref, (eid, obs) in note_map.items():
            if ref in event_ids and event_ids[ref] != eid:
                raise EntityConsistencyError("event observation and original field mismatch")
            event_ids.setdefault(ref, eid)
    if len(event_ids) != len(set(event_ids.values())):
        raise EntityConsistencyError("duplicate source evidence across events")
    results = []
    for i, s in enumerate(pr["sentences"]):
        if (type(s) is not dict or type(s.get("verdict")) is not str
                or s["verdict"] not in ("supported", "unsupported", "unresolved")
                or type(s.get("text")) is not str or not 0 < len(s["text"]) <= 360
                or type(s.get("evidence_ids")) is not list
                or not 1 <= len(s["evidence_ids"]) <= 16
                or any(not _digest(eid) for eid in s["evidence_ids"])
                or len(set(s["evidence_ids"])) != len(s["evidence_ids"])
                or any(eid not in event_ids.values() for eid in s["evidence_ids"])):
            raise EntityConsistencyError("invalid displayed sentence source identity")
        if s["verdict"] != "supported":
            continue
        cited = [ref for ref, eid in event_ids.items() if eid in s["evidence_ids"]]
        results.append({"sentence_index": i, "sentence": s["text"],
            "sentence_sha256": _sha(s["text"].encode("utf-8")),
            "previous_model_verdict": "supported", "cited_source_refs": sorted(cited)})
    missing_text_refs = sorted(set(range(sr["source_event_count"])) -
                               {row["source_ref"] for row in source_rows})
    payload = {"record_version": 1, "stage": STAGE, "entity": dict(zip(
        ("repository", "entity_type", "number"), target_entity)),
        "date": preview["date"], "capture_sha256": source["capture_sha256"],
        "source_review_sha256": _sha(source_review_bytes),
        "preview_sha256": _sha(preview_bytes),
        "source_context_sha256": sr["context_sha256"],
        "temporal_source_mode": "legacy_unverified_offline_diagnostic" if legacy else POLICY,
        "historical_instant_verified": False, "sources": source_rows,
        "source_event_count": sr["source_event_count"],
        "source_refs_with_no_retained_text": missing_text_refs,
        "sentences": results, "publication_authorized": False,
        "requires_human_quality_review": True,
        "semantic_coverage_verified": False, "factuality_verified": False}
    raw = _json(payload)
    if len(raw) > MAX_PACKET_BYTES:
        raise EntityConsistencyError("complete entity challenge exceeds byte cap")
    return EntityConsistencyPacket(raw)


def admit_entity_consistency_reply(packet_bytes: bytes, reply_bytes: bytes):
    """Validate model-only cross-event concerns, never change C4 support.

    Even 'no_conflict_observed' cannot certify that a fact is true or that
    material information was retained; this is an advisory review cue.
    """
    packet = _object(packet_bytes, MAX_PACKET_BYTES, "challenge")
    if (packet.get("stage") != STAGE or packet.get("publication_authorized") is not False
            or type(packet.get("sources")) is not list
            or type(packet.get("sentences")) is not list):
        raise EntityConsistencyError("unbound consistency packet")
    # The packet is created by the builder above; a hash alone is not a
    # signed source credential. Validate the exact expected model envelope.
    reply = _object(reply_bytes, MAX_REPLY_BYTES, "consistency reply", canonical=False)
    if set(reply) != {"packet_sha256", "checks"} or reply["packet_sha256"] != _sha(packet_bytes):
        raise EntityConsistencyError("foreign or incomplete consistency reply")
    checks = reply["checks"]
    if type(checks) is not list or len(checks) != len(packet["sentences"]):
        raise EntityConsistencyError("missing or duplicated individual checks")
    admitted = []
    field_refs = {r["source_ref"] for r in packet["sources"]}
    for expect, item in zip(packet["sentences"], checks):
        if (type(item) is not dict
                or set(item) != {"sentence_index", "status", "challenger_refs", "reason"}
                or type(item["sentence_index"]) is not int
                or item["sentence_index"] != expect["sentence_index"]
                or type(item["status"]) is not str or item["status"] not in VERDICTS
                or type(item["challenger_refs"]) is not list
                or len(item["challenger_refs"]) > MAX_EVENTS
                or any(type(r) is not int or r not in field_refs
                       or r in expect["cited_source_refs"] for r in item["challenger_refs"])
                or len(set(item["challenger_refs"])) != len(item["challenger_refs"])
                or type(item["reason"]) is not str
                or not 1 <= len(item["reason"]) <= 240
                or "\n" in item["reason"] or "\r" in item["reason"]):
            raise EntityConsistencyError("invalid typed advisory check")
        if item["status"] in ("potential_conflict", "possible_omission"):
            if not item["challenger_refs"]:
                raise EntityConsistencyError("challenge lacks independent source event")
        elif item["status"] == "no_conflict_observed" and item["challenger_refs"]:
            raise EntityConsistencyError("no-conflict status cannot carry challengers")
        admitted.append({"sentence_index": expect["sentence_index"],
            "sentence_sha256": expect["sentence_sha256"],
            "model_consistency_status": item["status"],
            "challenger_refs": item["challenger_refs"],
            "model_reason_sha256": _sha(item["reason"].encode("utf-8")),
            "requires_human_quality_review": True})
    result = {"record_version": 1, "stage": RESULT_STAGE,
        "entity": packet["entity"], "packet_sha256": _sha(packet_bytes),
        "source_review_sha256": packet["source_review_sha256"],
        "preview_sha256": packet["preview_sha256"],
        "temporal_source_mode": packet["temporal_source_mode"],
        "checks": admitted, "model_consistency_is_advisory": True,
        "human_quality_review_completed": False,
        "historical_instant_verified": False,
        "factuality_verified": False, "semantic_coverage_verified": False,
        "publication_authorized": False}
    return EntityConsistencyAdvisory(_json(result))
