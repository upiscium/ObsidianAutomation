"""Optional, read-only cross-event consistency annotations for ONE GitHub Issue/PR.

Only verified C5 source-review and preview bytes are appropriate as inputs.
This module checks citations, SHA and Unicode spans. It DOES NOT infer whether
a conflict is true, recover GitHub edit history or authorize publication.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import re

from . import github_daily_source_role_audit as role


VERSION = 1
STAGE = "github_daily_entity_consistency_v1"
PERIOD_POLICY = "github-day-snapshot-v1"
MAX_FIELDS = 256
MAX_EVENTS = 128
MAX_CANDIDATES = 24
MAX_OUTPUT_BYTES = 128 * 1024
MAX_SENTENCES = 3
_RELATIONS = frozenset(("possible_contradiction", "possible_supersession", "consistent", "uncertain"))
_TEMPORAL_STATUSES = frozenset((
    "day_snapshot_not_event_instant",
    "content_addressed_commit_not_push_time",
    "event_transition_only",
    "withheld_outside_target_day",
    "withheld_unknown_modification_time",
))
_HEX = re.compile(r"[0-9a-f]{64}\Z")


class EntityConsistencyError(ValueError):
    """Invalid exact private evidence or advisory claim span."""


@dataclass(frozen=True)
class EntityConsistencyReport:
    canonical_bytes: bytes

    @property
    def sha256(self):
        return hashlib.sha256(self.canonical_bytes).hexdigest()

    def report(self):
        return json.loads(self.canonical_bytes)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _hex(x):
    return type(x) is str and _HEX.fullmatch(x) is not None


def _identity(value):
    if (type(value) is not dict
            or set(value) != {"repository", "entity_type", "number"}
            or type(value["repository"]) is not str or not value["repository"]
            or value["entity_type"] not in ("issue", "pull_request")
            or type(value["number"]) is not int or value["number"] <= 0):
        raise EntityConsistencyError("invalid source entity")
    return value["repository"], value["entity_type"], value["number"]


def _span(value, fields, *, cited_ids=None):
    """Typed Unicode-code-point slice bound to one exact captured source field."""
    if type(value) is not dict or set(value) != {"source_ref", "field", "start", "end"}:
        raise EntityConsistencyError("invalid exact source span")
    ref, name, start, end = (value[k] for k in ("source_ref", "field", "start", "end"))
    if (type(ref) is not int or ref < 0 or type(name) is not str
            or type(start) is not int or type(end) is not int):
        raise EntityConsistencyError("invalid source span types")
    record = fields.get((ref, name))
    if (record is None or not 0 <= start < end <= len(record["text"])
            or cited_ids is not None and record["evidence_id"] not in cited_ids):
        raise EntityConsistencyError("span is outside permitted original source")
    return record, {
        "source_ref": ref, "field": name,
        "evidence_id": record["evidence_id"],
        "field_sha256": record["text_sha256"],
        "start": start, "end": end,
        "span_sha256": _sha(record["text"][start:end].encode("utf-8")),
    }


def _source_fields(source_row):
    raw = source_row.get("fields")
    if type(raw) is not list or len(raw) > MAX_FIELDS:
        raise EntityConsistencyError("invalid source-field budget")
    fields, event_ids = {}, {}
    for f in raw:
        if type(f) is not dict:
            raise EntityConsistencyError("invalid captured field")
        ref, field, evidence_id, value = (f.get(k) for k in ("source_ref", "field", "evidence_id", "text"))
        if (type(ref) is not int or ref < 0 or ref >= MAX_EVENTS
                or field not in ("body", "title", "message") or type(field) is not str
                or not _hex(evidence_id) or type(value) is not str
                or not _hex(f.get("text_sha256"))
                or f["text_sha256"] != _sha(value.encode("utf-8"))):
            raise EntityConsistencyError("unbound captured source field")
        original = f.get("original_value", value)
        if type(original) is dict:
            if original.get("text") != value:
                raise EntityConsistencyError("source field disagrees with original value")
        elif type(original) is str:
            if original != value:
                raise EntityConsistencyError("source field original differs")
        else:
            raise EntityConsistencyError("invalid source original value")
        key = (ref, field)
        if key in fields or ref in event_ids and event_ids[ref] != evidence_id:
            raise EntityConsistencyError("duplicate/foreign source field")
        fields[key] = f
        event_ids[ref] = evidence_id
    count = source_row.get("source_event_count")
    if count is not None and (type(count) is not int or not 0 <= count <= MAX_EVENTS
                              or any(r >= count for r in event_ids)):
        raise EntityConsistencyError("invalid event count in review")
    return fields, event_ids


def _stamp(raw):
    if type(raw) is not str:
        raise EntityConsistencyError("missing or invalid source observation time")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            raise ValueError("naive time")
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError, TypeError):
        raise EntityConsistencyError("invalid source observation timestamp") from None


def _period_notes(source_row, fields, event_ids, target_date):
    """Check #358's day-level source markers; never infer instant/history."""
    raw = source_row.get("temporal_observations")
    if raw is None:
        return False
    if (type(target_date) is not str):
        raise EntityConsistencyError("missing target date for period policy")
    try:
        day = date.fromisoformat(target_date)
        if day.isoformat() != target_date:
            raise ValueError("noncanonical day")
    except (TypeError, ValueError):
        raise EntityConsistencyError("invalid selected day") from None
    jst = timezone(timedelta(hours=9))
    if type(raw) is not list or not 1 <= len(raw) <= MAX_EVENTS:
        raise EntityConsistencyError("invalid temporal observation count")
    count = source_row.get("source_event_count")
    if type(count) is not int or len(raw) != count:
        raise EntityConsistencyError("incomplete period observations")
    notes = {}
    for record in raw:
        if (type(record) is not dict
                or set(record) != {"source_ref", "evidence_id", "observation"}
                or type(record["source_ref"]) is not int
                or not 0 <= record["source_ref"] < len(raw)
                or not _hex(record["evidence_id"])):
            raise EntityConsistencyError("invalid temporal event note")
        ref = record["source_ref"]
        if ref in notes:
            raise EntityConsistencyError("duplicate temporal event note")
        info = record["observation"]
        if (type(info) is not dict
                or info.get("policy") != PERIOD_POLICY
                or info.get("status") not in _TEMPORAL_STATUSES
                or info.get("historical_instant_verified") is not False
                or not _hex(info.get("original_event_sha256"))
                or type(info.get("withheld_fields")) is not list
                or len(set(info["withheld_fields"])) != len(info["withheld_fields"])
                or any(type(k) is not str or k not in ("title", "body", "message", "state", "draft")
                       for k in info["withheld_fields"])):
            raise EntityConsistencyError("invalid day-period note")
        observed = _stamp(info.get("observed_at_utc"))
        updated = (_stamp(info["resource_updated_at"])
                   if info.get("resource_updated_at") is not None else None)
        if updated is not None and updated > observed:
            raise EntityConsistencyError("resource update is later than observation")
        scope = info["status"]
        if scope == "day_snapshot_not_event_instant":
            if updated is None or updated.astimezone(jst).date() != day:
                raise EntityConsistencyError("day snapshot timestamp not in selected day")
        elif scope == "withheld_outside_target_day":
            if updated is None or updated.astimezone(jst).date() == day:
                raise EntityConsistencyError("out-of-day status does not match update time")
        elif scope == "withheld_unknown_modification_time" and updated is not None:
            raise EntityConsistencyError("unknown-update status contradicted")
        notes[ref] = record
        if ref in event_ids and event_ids[ref] != record["evidence_id"]:
            raise EntityConsistencyError("period evidence identity mismatch")
    if set(notes) != set(range(count)):
        raise EntityConsistencyError("missing period event note")
    for (ref, field), _ in fields.items():
        info = notes[ref]["observation"]
        if field in info["withheld_fields"]:
            raise EntityConsistencyError("withheld field leaked into C5 audit")
        if info["status"] not in ("day_snapshot_not_event_instant",
                                 "content_addressed_commit_not_push_time"):
            raise EntityConsistencyError("out-of-period source field admitted")
    return True


def _sentences(preview_row):
    arr = preview_row.get("sentences")
    if type(arr) is not list or len(arr) > MAX_SENTENCES:
        raise EntityConsistencyError("invalid bounded preview sentences")
    for s in arr:
        if (type(s) is not dict
                or type(s.get("text")) is not str or not 0 < len(s["text"]) <= 360
                or len(s["text"].encode("utf-8")) > 2048
                or type(s.get("verdict")) is not str
                or s["verdict"] not in ("supported", "unsupported", "unresolved")
                or type(s.get("evidence_ids")) is not list
                or not 1 <= len(s["evidence_ids"]) <= 16
                or any(not _hex(eid) for eid in s["evidence_ids"])
                or len(set(s["evidence_ids"])) != len(s["evidence_ids"])):
            raise EntityConsistencyError("invalid preview sentence source binding")
    return arr



def audit_entity_consistency(source_review_bytes, preview_bytes, target_entity,
                             candidates=(), *, require_day_policy=True):
    """Validate private C5 source/event spans for operator/model conflict candidates.

    require_day_policy=True refuses legacy source reviews lacking #358 metadata.
    False is useful for clearly labelled historical/offline diagnostics only.
    The caller MUST verify the source's actual Collector/CAS authenticity.
    """
    if type(require_day_policy) is not bool:
        raise EntityConsistencyError("invalid day-policy gate")
    try:
        source = role._read(source_review_bytes, role.MAX_SOURCE_REVIEW_BYTES, "source review")
        preview = role._read(preview_bytes, role.MAX_PREVIEW_BYTES, "preview")
    except role.SourceRoleAuditError:
        raise EntityConsistencyError("invalid canonical private artifact") from None
    if (source.get("record_version") != 1 or type(source.get("record_version")) is not int
            or source.get("stage") != "c5-literal-source-review-v1"
            or source.get("publication_authorized") is not False
            or preview.get("publication_authorized") is not False
            or source.get("preview_sha256") != _sha(preview_bytes)
            or source.get("source_context_set_sha256") != preview.get("source_context_set_sha256")
            or source.get("capture_sha256") != preview.get("capture_sha256")
            or not _hex(source.get("capture_sha256"))
            or not _hex(source.get("source_context_set_sha256"))):
        raise EntityConsistencyError("source review and private preview are not bound")
    if (type(target_entity) is not tuple or len(target_entity) != 3
            or type(target_entity[0]) is not str or not target_entity[0]
            or target_entity[1] not in ("issue", "pull_request")
            or type(target_entity[2]) is not int or target_entity[2] <= 0):
        raise EntityConsistencyError("invalid target entity")
    if (type(source.get("entities")) is not list or
            type(preview.get("entities")) is not list):
        raise EntityConsistencyError("missing entity sets")
    try:
        matches = [x for x in source["entities"] if _identity(x["entity"]) == target_entity]
        targets = [x for x in preview["entities"] if _identity(x["entity"]) == target_entity]
        if len(matches) != 1 or len(targets) != 1:
            raise EntityConsistencyError("target not uniquely present in source and preview")
        source_row, preview_row = matches[0], targets[0]
        if (not _hex(source_row.get("context_sha256"))
                or source_row["context_sha256"] != preview_row.get("context_sha256")):
            raise EntityConsistencyError("target context changed")
        fields, event_ids = _source_fields(source_row)
        day_valid = _period_notes(source_row, fields, event_ids, preview.get("date"))
        sentences = _sentences(preview_row)
    except (KeyError, TypeError, AttributeError, IndexError):
        raise EntityConsistencyError("malformed entity evidence binding") from None
    if require_day_policy and not day_valid:
        raise EntityConsistencyError("day policy not established; legacy snapshot excluded")

    if type(candidates) not in (tuple, list) or len(candidates) > MAX_CANDIDATES:
        raise EntityConsistencyError("invalid bounded consistency candidates")
    checked = []
    spans = set()
    for entry in candidates:
        if (type(entry) is not dict or set(entry) !=
                {"sentence_index", "claim_start", "claim_end", "support", "counter", "relation"}):
            raise EntityConsistencyError("invalid exact consistency candidate")
        i, start, end, relation = (entry[k] for k in
            ("sentence_index", "claim_start", "claim_end", "relation"))
        if (type(i) is not int or not 0 <= i < len(sentences)
                or sentences[i]["verdict"] != "supported"
                or type(start) is not int or type(end) is not int
                or not 0 <= start < end <= len(sentences[i]["text"])
                or type(relation) is not str or relation not in _RELATIONS):
            raise EntityConsistencyError("invalid displayed claim or relation")
        if any(k == i and start < other_end and other_start < end
               for k, other_start, other_end in spans):
            raise EntityConsistencyError("overlapping advisory claim")
        spans.add((i, start, end))
        support_field, support = _span(entry["support"], fields,
                                       cited_ids=sentences[i]["evidence_ids"])
        counter_field, counter = _span(entry["counter"], fields)
        if support_field["evidence_id"] == counter_field["evidence_id"]:
            raise EntityConsistencyError("counter must come from another event")
        if relation in ("possible_contradiction", "possible_supersession"):
            scope_state = "potential_cross_event_contradiction"
        elif relation == "consistent":
            scope_state = "no_advisory_conflict_not_verified"
        else:
            scope_state = "uncertain_cross_event_relation"
        checked.append({
            "sentence_index": i,
            "sentence_sha256": _sha(sentences[i]["text"].encode("utf-8")),
            "claim_start": start, "claim_end": end,
            "claim_sha256": _sha(sentences[i]["text"][start:end].encode("utf-8")),
            "model_grounding_verdict": "supported",
            "support_scope": "cited_original_span_verified_not_truth",
            "cited_support": support,
            "counter_scope": "same_entity_different_original_event",
            "potential_counter": counter,
            "advisory_relation": relation,
            "consistency_state": scope_state,
            "historical_order_state": "historical_order_unverified",
        })
    checked.sort(key=lambda x: (x["sentence_index"], x["claim_start"], x["claim_end"]))
    for i, s in enumerate(sentences):
        if s["verdict"] == "supported" and not any(
                r["sentence_index"] == i for r in checked):
            checked.append({
                "sentence_index": i,
                "sentence_sha256": _sha(s["text"].encode("utf-8")),
                "claim_start": None, "claim_end": None, "claim_sha256": None,
                "model_grounding_verdict": "supported",
                "support_scope": "unreviewed", "cited_support": None,
                "counter_scope": "unreviewed", "potential_counter": None,
                "advisory_relation": None, "consistency_state": "unreviewed",
                "historical_order_state": "historical_order_unverified",
            })
    checked.sort(key=lambda x: (x["sentence_index"], x["claim_start"] is None,
                                 -1 if x["claim_start"] is None else x["claim_start"]))
    out = {
        "record_version": VERSION, "stage": STAGE,
        "entity": dict(zip(("repository", "entity_type", "number"), target_entity)),
        "capture_sha256": source["capture_sha256"],
        "source_review_sha256": _sha(source_review_bytes),
        "preview_sha256": _sha(preview_bytes),
        "context_sha256": source_row["context_sha256"],
        "source_period_policy": PERIOD_POLICY if day_valid else "legacy_unverified",
        "day_snapshot_provenance_present": day_valid,
        "historical_instant_verified": False,
        "supported_sentence_count": sum(s["verdict"] == "supported" for s in sentences),
        "annotated_claim_count": len(candidates),
        "potential_conflict_count": sum(
            r["consistency_state"] == "potential_cross_event_contradiction" for r in checked),
        "results": checked,
        "advisory_relations_independently_attested": False,
        "all_atomic_claims_reviewed": False,
        "factuality_verified": False,
        "semantic_coverage_verified": False,
        "requires_human_quality_review": True,
        "publication_authorized": False,
    }
    raw = role._canonical(out)
    if len(raw) > MAX_OUTPUT_BYTES:
        raise EntityConsistencyError("bounded consistency report too large")
    return EntityConsistencyReport(raw)
