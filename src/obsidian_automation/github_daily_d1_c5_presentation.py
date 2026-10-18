"""d1-3B shadow annotations for original-source C5 private model presentation.

The current C5 source-ref set is unchanged and FULL original evidence remains
the only authority. Score-based hints never certify relevance, completeness,
unimportance, push date, or source deletion. This component is pure and opt-in.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math

from . import github_daily_d1_priority_shadow as d1
from . import github_daily_c5_source_view as recent

VERSION = "c5-d1-advisory-original-source-v2"
MAX_HINTS = 3
MAX_SUPPLEMENTAL_SOURCE_REFS = 1
MAX_MODEL_VIEW_BYTES = recent.MAX_WIRE_BYTES


class D1C5PresentationError(ValueError):
    """Fail closed if original CAS/span/model-binding identity is inconsistent."""


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


@dataclass(frozen=True)
class AdvisoryC5View:
    canonical_bytes: bytes
    shown_source_refs: tuple[int, ...]
    plan_sha256: str
    shadow_sha256: str
    hint_count: int
    original_source_event_count: int
    supplemental_source_refs: tuple[int, ...] = ()

    def report(self) -> dict:
        return json.loads(self.canonical_bytes)


def _validate_bindings(context: dict, plan: d1.PriorityPlan,
                       shadow: d1.ShadowResult) -> tuple[dict, dict, list]:
    if type(context) is not dict or type(plan) is not d1.PriorityPlan or type(shadow) is not d1.ShadowResult:
        raise D1C5PresentationError("invalid original source or private shadow type")
    p = plan.report()
    q = shadow.report()
    if (p.get("record_version") != d1.VERSION or
            p.get("stage") != "daily_c5_d1_priority_plan" or
            p.get("model_selected") != d1.MODEL or
            p.get("model_question_version") != d1.QUESTION_VERSION or
            p.get("publication_authorized") is not False or
            p.get("source_evidence_deleted") is not False or
            context.get("capture_sha256") != p.get("source_capture_sha256") or
            q.get("record_version") != d1.VERSION or
            q.get("stage") != "daily_c5_d1_priority_shadow" or
            q.get("model_primary") != d1.MODEL or
            q.get("question_version") != d1.QUESTION_VERSION or
            q.get("source_plan_sha256") != plan.sha256 or
            q.get("source_capture_sha256") != p["source_capture_sha256"] or
            q.get("publication_authorized") is not False or
            q.get("source_evidence_deleted") is not False or
            q.get("may_filter_C5_source") is not False or
            q.get("score_calibrated") is not False or
            type(q.get("observations")) is not list or
            type(p.get("paragraphs")) is not list):
        raise D1C5PresentationError("C4/C5 or model advisory binding incompatible")
    # Only data inside the original selected entity are relevant to this
    # model input. The immutable original C4 context is the verification root.
    entity = context.get("entity")
    if type(entity) is not dict or type(context.get("sources")) is not list:
        raise D1C5PresentationError("C4 model input missing source identity")
    relevant = [
        s for s in p["paragraphs"]
        if type(s) is dict and s.get("entity") == entity
    ]
    valid_ids = {entry.get("id"): entry for entry in relevant}
    if len(valid_ids) != len(relevant):
        raise D1C5PresentationError("duplicated source-bound paragraph identity")
    for entry in relevant:
        ref = entry.get("source_ref")
        field = entry.get("field")
        if (type(ref) is not int or not 0 <= ref < len(context["sources"]) or
                field not in ("title", "body", "message") or
                entry.get("source_cas_sha256") != p["source_capture_sha256"]):
            raise D1C5PresentationError("advisory fragment outside original source")
        item = context["sources"][ref]
        if type(item) is not dict:
            raise D1C5PresentationError("original C4 source item invalid")
        event = item.get("original_event")
        if (type(item.get("source_ref")) is not int or item["source_ref"] != ref
                or type(event) is not dict
                or entry.get("original_evidence_id") != event.get("evidence_id")
                or entry.get("original_kind") != event.get("kind")
                or entry.get("draft") != event.get("draft")
                or entry.get("date") != context.get("date")):
            raise D1C5PresentationError("original source event changed")
        content = event.get(field)
        if type(content) is not dict or type(content.get("text")) is not str:
            raise D1C5PresentationError("original source field no longer visible")
        text = content["text"]
        start, end = entry.get("start"), entry.get("end")
        if (type(start) is not int or type(end) is not int
                or not 0 <= start < end <= len(text)
                or entry.get("text") != text[start:end]
                or entry.get("field_text_sha256") != _sha(text.encode("utf-8"))
                or entry.get("text_sha256") != _sha(text[start:end].encode("utf-8"))
                or entry.get("field_truncated_at_collection") != content.get("truncated")):
            raise D1C5PresentationError("original source span changed")
        temporal = event.get("temporal_observation")
        recorded_status = ("legacy_unverified" if temporal is None
                           else temporal.get("status") if type(temporal) is dict
                           else None)
        if entry.get("source_temporal_status") != recorded_status:
            raise D1C5PresentationError("original temporal source status changed")
        if event.get("kind") == "pull_request_commit" and entry.get("commit_sha") != event.get("sha"):
            raise D1C5PresentationError("commit SHA provenance changed")
    if (type(q.get("segment_count")) is not int
            or q["segment_count"] != p.get("paragraph_count")
            or type(q.get("requested_segment_count")) is not int
            or q["requested_segment_count"] != len(q["observations"])):
        raise D1C5PresentationError("shadow observation count inconsistent")
    all_ids = {entry.get("id") for entry in p["paragraphs"] if type(entry) is dict}
    if (len(all_ids) != len(p["paragraphs"])
            or len(q["observations"]) > d1.MAX_SHADOW_CALLS
            or type(q.get("summary")) is not dict
            or set(q["summary"]) != {"observed", "abstained", "pending"}
            or any(type(v) is not int or v < 0 for v in q["summary"].values())
            or sum(q["summary"].values()) != len(q["observations"])):
        raise D1C5PresentationError("shadow summary or source-id inventory invalid")
    seen = set()
    observed_states = {"observed": 0, "abstained": 0, "pending": 0}
    for observation in q["observations"]:
        if (type(observation) is not dict or type(observation.get("segment_id")) is not str
                or observation["segment_id"] in seen
                or observation["segment_id"] not in all_ids):
            raise D1C5PresentationError("invalid repeated model observation")
        seen.add(observation["segment_id"])
        # The entire shadow result was constructed by a source-CAS bound
        # run; out-of-entity observations remain perfectly legal.
        if type(observation.get("status")) is not str or observation["status"] not in (
                "observed", "abstained", "pending"):
            raise D1C5PresentationError("invalid model observation state")
        observed_states[observation["status"]] += 1
        if observation["status"] == "observed":
            scores = observation.get("scores")
            if (observation.get("profile_revision") != d1.PINNED_D1_PROFILE_REVISION
                    or observation.get("observed_decision_semantics_id") != d1.PINNED_D1_SEMANTICS_ID
                    or observation.get("observed_deployment_revision") != d1.PINNED_D1_DEPLOYMENT_REVISION
                    or observation.get("observed_serving_contract_revision") != d1.PINNED_D1_SERVING_CONTRACT_REVISION
                    or observation.get("score_calibrated") is not False
                    or type(scores) is not dict or set(scores) != set(d1.CATEGORIES)
                    or any(
                        (type(value) is int and value not in (0, 1))
                        or (type(value) is float and
                            (not math.isfinite(value) or not 0 <= value <= 1))
                        or type(value) not in (int, float)
                        for value in scores.values())
                    or abs(sum(scores.values()) - 1.0) > 1e-4
                    or observation.get("recommendation") not in d1.CATEGORIES
                    or scores[observation["recommendation"]] < max(scores.values()) - 1e-8):
                raise D1C5PresentationError("unbound model choice-set recommendation")
        elif observation.get("recommendation") is not None:
            raise D1C5PresentationError("abstained/pending model has recommendation")
        if observation.get("publication_authorized") is not False or observation.get("source_deleted") is not False:
            raise D1C5PresentationError("model outcome claims unexpected authority")
    if observed_states != q["summary"]:
        raise D1C5PresentationError("shadow observation count mismatch")
    return p, q, relevant


def build_private_advisory_c5_view(
        context: dict, plan: d1.PriorityPlan,
        shadow: d1.ShadowResult) -> AdvisoryC5View:
    """Preserve both original recent refs; optionally add one verbatim fragment.

    d1 cannot remove a baseline source or fabricate extra original refs. The
    added source_ref is legal only for the shown exact original text fragment,
    and original full C4 Grounding remains authoritative. Without an observed
    and source-matched MUST label, byte-exact fallback to recent C5 is used.
    """
    base = recent.build_recent_c5_source_view(context)
    _, outcomes, relevant = _validate_bindings(context, plan, shadow)
    byid = {e["id"]: e for e in relevant}
    shown = set(base.shown_source_refs)
    candidates = []
    for o in outcomes["observations"]:
        item = byid.get(o["segment_id"])
        if item is None:
            continue
        if o["status"] != "observed" or o["recommendation"] != "must_include":
            continue
        # It is a literal original source span, never a standalone assertion.
        candidates.append({
            "source_ref": item["source_ref"],
            "field": item["field"],
            "start": item["start"],
            "end": item["end"],
            "verbatim_original_excerpt": item["text"],
            "observed_label": "must_include",
            "importance_certified": False,
            "source_authority": "original_C4_only",
        })
    candidates.sort(key=lambda v: (-v["source_ref"],
                                   {"body": 0, "message": 1, "title": 2}.get(v["field"], 3),
                                   v["start"]))
    unique = []
    occupied = set()
    for candidate in candidates:
        key = (candidate["source_ref"],candidate["field"],candidate["start"],candidate["end"])
        if key not in occupied:
            occupied.add(key)
            unique.append(candidate)
    document = base.report()
    chosen = []
    new_source_refs = set()
    for candidate in unique:
        if len(chosen) >= MAX_HINTS:
            break
        source_ref = candidate["source_ref"]
        supplementary = source_ref not in shown
        if supplementary and (len(new_source_refs) >= MAX_SUPPLEMENTAL_SOURCE_REFS
                              and source_ref not in new_source_refs):
            continue
        proposed = new_source_refs | ({source_ref} if supplementary else set())
        allowed = tuple(sorted(shown | proposed))
        obj = dict(document)
        obj["advisory_priority_version"] = VERSION
        obj["advisory_priority_hints"] = chosen + [candidate]
        obj["advisory_model_unverified"] = True
        obj["advisory_has_no_selection_authority"] = True
        obj["advisory_original_plan_sha256"] = plan.sha256
        obj["advisory_observation_sha256"] = _sha(shadow.canonical_bytes)
        obj["baseline_recent_source_refs"] = list(base.shown_source_refs)
        obj["supplemental_excerpt_source_refs"] = sorted(proposed)
        obj["shown_source_refs"] = list(allowed)
        obj["omitted_source_refs"] = [i for i in range(base.source_event_count)
                                      if i not in allowed]
        obj["supplementary_source_is_verbatim_excerpt_only"] = True
        wire = _json(obj)
        if len(wire) <= MAX_MODEL_VIEW_BYTES:
            chosen.append(candidate)
            new_source_refs = proposed
    if not chosen:
        return AdvisoryC5View(
            base.canonical_bytes, base.shown_source_refs,
            plan.sha256, _sha(shadow.canonical_bytes), 0,
            base.source_event_count)
    allowed = tuple(sorted(shown | new_source_refs))
    document["advisory_priority_version"] = VERSION
    document["advisory_priority_hints"] = chosen
    document["advisory_model_unverified"] = True
    document["advisory_has_no_selection_authority"] = True
    document["advisory_original_plan_sha256"] = plan.sha256
    document["advisory_observation_sha256"] = _sha(shadow.canonical_bytes)
    document["baseline_recent_source_refs"] = list(base.shown_source_refs)
    document["supplemental_excerpt_source_refs"] = sorted(new_source_refs)
    document["shown_source_refs"] = list(allowed)
    document["omitted_source_refs"] = [i for i in range(base.source_event_count)
                                      if i not in allowed]
    document["supplementary_source_is_verbatim_excerpt_only"] = True
    wire = _json(document)
    if len(wire) > MAX_MODEL_VIEW_BYTES:
        raise D1C5PresentationError("model priority view exceeds source budget")
    return AdvisoryC5View(
        wire, allowed, plan.sha256,
        _sha(shadow.canonical_bytes), len(chosen),
        base.source_event_count, tuple(sorted(new_source_refs)))
