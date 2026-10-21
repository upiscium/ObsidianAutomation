"""Private, nonpublishing Daily v2 milestone-label evaluation.

Consumes explicit per-source and per-sentence human-reviewed annotations.
Never infers importance from Grounding=SUPPORTED or source-ref overlap alone.
No network access, model calls, Collector/Writer or deployment.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
import re
from typing import Any

VERSION = "github-daily-human-milestone-eval-v1"
MAX_MILESTONES = 512
MAX_ENTITIES = 128
MAX_SENTENCES = 2048
_SHA = re.compile(r"^[a-f0-9]{64}$")
_ROUTES = frozenset({"baseline", "feature_first"})
_MATERIALITY = frozenset({"material", "routine", "uncertain"})
_SUPPORT = frozenset({"supported", "unsupported", "uncertain"})
_REVIEW_STATUS = frozenset({"adjudicated", "pending"})
_ENTITY_TYPES = frozenset({"issue", "pull_request"})


class MilestoneEvaluationError(ValueError):
    """Invalid annotations cannot imply an authoritative quality verdict."""


def _require(condition: bool, explanation: str) -> None:
    if not condition:
        raise MilestoneEvaluationError(explanation)


def _sha(value: object, name: str) -> None:
    _require(type(value) is str and bool(_SHA.fullmatch(value)), f"invalid {name}")


def _entity(obj: object, name: str) -> tuple[str, str, int]:
    _require(type(obj) is dict and set(obj) == {
        "repository", "entity_type", "number"}, f"invalid {name}")
    repo, kind, number = obj["repository"], obj["entity_type"], obj["number"]
    _require(type(repo) is str and 1 <= len(repo) <= 256
             and repo.count("/") == 1 and not any(c.isspace() for c in repo),
             f"invalid {name} repository")
    _require(type(kind) is str and kind in _ENTITY_TYPES
             and type(number) is int and 0 < number <= 10**9,
             f"invalid {name} identity")
    return repo, kind, number


def _refs(raw: object, *, max_count: int, name: str) -> tuple[int, ...]:
    _require(type(raw) is list and 0 < len(raw) <= 256,
             f"{name} requires refs")
    _require(all(type(v) is int and 0 <= v < max_count for v in raw),
             f"{name} refs outside original C4")
    _require(len(set(raw)) == len(raw), f"{name} duplicate refs")
    return tuple(sorted(raw))


def evaluate_annotations(payload: object) -> dict[str, Any]:
    """Evaluate only complete, explicit annotations.

    The caller remains responsible for separately verifying that a claimed
    human reviewer is actually independent and that sentence/evidence hashes
    match the private C4 and model-receipt artifacts. Even successful scores
    NEVER confer Writer/Timer or publication authorization.
    """
    _require(type(payload) is dict and set(payload) == {
        "record_version", "source_capture_sha256", "source_date",
        "review", "entity_inventory", "milestones", "route_results"},
        "invalid milestone evaluation payload fields")
    _require(payload["record_version"] == VERSION, "evaluation schema mismatch")
    _sha(payload["source_capture_sha256"], "capture sha")
    date = payload["source_date"]
    _require(type(date) is str and re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) is not None,
             "invalid source date")
    review = payload["review"]
    _require(type(review) is dict and set(review) == {
        "reviewer_type", "reviewer_id", "reviewed_at", "inventory_complete"},
        "invalid reviewer provenance")
    reviewer_type = review["reviewer_type"]
    _require(reviewer_type in ("human", "model_provisional"),
             "unrecognized reviewer type")
    _require(type(review["reviewer_id"]) is str
             and 2 <= len(review["reviewer_id"]) <= 100,
             "invalid reviewer identity")
    _require(type(review["reviewed_at"]) is str
             and len(review["reviewed_at"]) <= 50, "invalid review timestamp")
    _require(type(review["inventory_complete"]) is bool,
             "inventory completeness is not boolean")
    entities = payload["entity_inventory"]
    _require(type(entities) is list and 0 < len(entities) <= MAX_ENTITIES,
             "unbounded entity inventory")
    inventory: dict[tuple[str, str, int], int] = {}
    for entry in entities:
        _require(type(entry) is dict and set(entry) == {
            "entity", "source_ref_count", "applicable_routes"},
            "malformed entity inventory")
        entity = _entity(entry["entity"], "inventory entity")
        count = entry["source_ref_count"]
        routes = entry["applicable_routes"]
        _require(type(count) is int and 1 <= count <= 256, "bad source ref count")
        _require(type(routes) is list
                 and all(type(route) is str and route in _ROUTES for route in routes)
                 and len(set(routes)) == len(routes)
                 and "baseline" in routes, "invalid applicability; baseline is mandatory")
        _require(entity not in inventory, "duplicate entity inventory")
        inventory[entity] = {"count":count,"routes":set(routes)}

    labels = payload["milestones"]
    _require(type(labels) is list and len(labels) <= MAX_MILESTONES,
             "unbounded milestone inventory")
    item_index = {}
    incomplete = []
    material_ids: set[str] = set()
    routine_ids: set[str] = set()
    for item in labels:
        _require(type(item) is dict and set(item) == {
            "milestone_id", "entity", "source_refs", "source_support",
            "materiality", "review_status", "reason"},
            "invalid milestone label fields")
        mid = item["milestone_id"]
        _require(type(mid) is str and re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", mid),
                 "invalid milestone id")
        _require(mid not in item_index, "duplicate milestone id")
        entity = _entity(item["entity"], "milestone entity")
        _require(entity in inventory, "milestone entity is not inventoried")
        refs = _refs(item["source_refs"], max_count=inventory[entity]["count"],
                     name=mid)
        _require(type(item["source_support"]) is str
                 and type(item["materiality"]) is str
                 and type(item["review_status"]) is str
                 and item["source_support"] in _SUPPORT
                 and item["materiality"] in _MATERIALITY
                 and item["review_status"] in _REVIEW_STATUS,
                 "invalid milestone verdict")
        _require(type(item["reason"]) is str and 1 <= len(item["reason"]) <= 2000,
                 "missing milestone review rationale")
        item_index[mid] = {"entity":entity, "source_refs":set(refs), "label":item}
        if item["review_status"] != "adjudicated" or (
                item["source_support"] == "uncertain"
                or item["materiality"] == "uncertain"):
            incomplete.append(mid)
        if item["source_support"] == "supported":
            if item["materiality"] == "material":
                material_ids.add(mid)
            if item["materiality"] == "routine":
                routine_ids.add(mid)

    results = payload["route_results"]
    _require(type(results) is list and len(results) <= MAX_ENTITIES*len(_ROUTES),
             "unbounded result inventory")
    per_route: dict[str, dict[tuple[str, str, int], list[dict]]] = defaultdict(dict)
    total_sentences = 0
    for result in results:
        _require(type(result) is dict and set(result) == {
            "route", "entity", "preview_sha256", "sentences"},
            "invalid source-result fields")
        route = result["route"]
        _require(type(route) is str and route in _ROUTES,
                 "unexpected C5 route")
        entity = _entity(result["entity"], "result entity")
        _require(entity in inventory and entity not in per_route[route]
                 and route in inventory[entity]["routes"],
                 "duplicated, inapplicable or out-of-inventory route")
        _sha(result["preview_sha256"], "preview sha")
        sentences = result["sentences"]
        _require(type(sentences) is list and len(sentences) <= 16, "unbounded sentences")
        total_sentences += len(sentences)
        _require(total_sentences <= MAX_SENTENCES, "aggregate sentences unbounded")
        visited = set()
        for sentence in sentences:
            _require(type(sentence) is dict and set(sentence) == {
                "sentence_sha256", "source_refs", "grounding",
                "review_status", "semantic_verdict", "covered_milestone_ids"},
                "invalid sentence audit")
            sid = sentence["sentence_sha256"]
            _sha(sid, "sentence sha")
            _require(sid not in visited, "duplicate sentence hash in result")
            visited.add(sid)
            refs = set(_refs(sentence["source_refs"],
                             max_count=inventory[entity]["count"], name="sentence"))
            _require(sentence["grounding"] in ("supported", "unsupported"),
                     "missing post-edit Grounding")
            _require(sentence["review_status"] in _REVIEW_STATUS,
                     "invalid sentence review status")
            _require(sentence["semantic_verdict"] in (
                "material", "routine", "unsupported", "uncertain"),
                "invalid semantic sentence assessment")
            covers = sentence["covered_milestone_ids"]
            _require(type(covers) is list and len(covers) <= 16
                     and all(type(mid) is str for mid in covers)
                     and len(covers) == len(set(covers)),
                     "unbounded/duplicate milestones per sentence")
            for mid in covers:
                _require(type(mid) is str and mid in item_index,
                         "unknown milestone in sentence")
                info = item_index[mid]
                _require(info["entity"] == entity,
                         "cross-Issue/PR coverage attribution")
                _require(bool(refs & info["source_refs"]),
                         "coverage not bound to any labelled original source ref")
                _require(mid in material_ids,
                         "routine/unsupported milestone cannot be counted as covered")
            if sentence["review_status"] != "adjudicated" or (
                sentence["semantic_verdict"] == "uncertain"):
                incomplete.append(f"{route}:{entity}:{sid[:12]}")
            if covers:
                _require(sentence["grounding"] == "supported"
                         and sentence["semantic_verdict"] == "material",
                         "a rejected/routine sentence cannot cover a milestone")
            if (sentence["semantic_verdict"] == "material"
                    and sentence["review_status"] == "adjudicated"):
                _require(bool(covers), "material sentence has no labelled milestone")
            if sentence["grounding"] == "unsupported":
                # Before adjudication the semantic verdict can be pending;
                # Grounding already excludes this sentence from the
                # post-edit candidate, so it cannot cover a milestone.
                _require(not covers and (
                    sentence["semantic_verdict"] == "unsupported"
                    or (sentence["review_status"] == "pending"
                        and sentence["semantic_verdict"] == "uncertain")),
                    "unsupported post-edit sentence cannot score material")
        per_route[route][entity] = sentences

    all_routes = sorted(_ROUTES)
    for route in all_routes:
        expected_entities = {
            entity for entity, attrs in inventory.items()
            if route in attrs["routes"]
        }
        if set(per_route[route]) != expected_entities:
            incomplete.append(f"{route}:incomplete_route_entity_inventory")

    # Always provide useful diagnostic accounting. A score is NOT a
    # defensible measurement when any labels, reviewed sentences, or
    # required entity routes are absent.
    scores = {}
    for route in all_routes:
        sentence_rows = [s for rows in per_route[route].values() for s in rows]
        published = [s for s in sentence_rows if s["grounding"] == "supported"]
        material_sentences = [s for s in published if s["semantic_verdict"] == "material"]
        routine_sentences = [s for s in published if s["semantic_verdict"] == "routine"]
        unknown_sentences = [s for s in published if s["semantic_verdict"] == "uncertain"]
        covered = {mid for s in material_sentences for mid in s["covered_milestone_ids"]}
        # A material verdict is never a substitute for explicit milestone
        # matching; avoid counting uncovered content as recall.
        eligible_ids = {
            mid for mid in material_ids
            if route in inventory[item_index[mid]["entity"]]["routes"]
        }
        missing = eligible_ids - covered
        scores[route] = {
            "reviewed_source_entities": len(per_route[route]),
            "raw_editor_sentences": len(sentence_rows),
            "post_grounding_supported_sentences": len(published),
            "post_grounding_unsupported_sentences": len(sentence_rows)-len(published),
            "matched_material_milestones": len(covered),
            "reviewed_material_milestones": len(eligible_ids),
            "missed_material_milestone_ids": sorted(missing),
            "supported_routine_overinclusion_count": len(routine_sentences),
            "unadjudicated_supported_sentence_count": len(unknown_sentences),
            "post_grounding_material_precision": (
                len(material_sentences)/len(published) if published else None),
            "source_milestone_recall": (
                len(covered)/len(eligible_ids) if eligible_ids else None),
            "material_f1": None,
            "numerators_describe_only_adjudicated_items": not incomplete,
        }
        p = scores[route]["post_grounding_material_precision"]
        r = scores[route]["source_milestone_recall"]
        if p is not None and r is not None:
            scores[route]["material_f1"] = 2*p*r/(p+r) if (p+r)>0 else 0.

    human_self_reported = (reviewer_type == "human")
    complete = bool(review["inventory_complete"]) and not incomplete
    eligible = human_self_reported and complete
    return {
        "record_version":VERSION,"capture_sha256":payload["source_capture_sha256"],
        "source_date":date,
        "status":("ADJUDICATED_INPUT_SCORED_NOT_PRODUCTION_CERTIFIED"
                  if eligible else "NOT_HUMAN_ADJUDICATED_OR_INCOMPLETE"),
        "scores_per_route":scores,
        "reviewer_type_self_reported":reviewer_type,
        "human_label_completeness_self_reported":bool(review["inventory_complete"]),
        "annotation_complete":complete,
        "missing_or_uncertain_annotations":sorted(set(incomplete)),
        "source_milestone_label_count":len(labels),
        "candidate_material_label_count":len(material_ids),
        "routine_negative_label_count":len(routine_ids),
        "human_reviewer_independence_verified_by_tool":False,
        "semantic_acceptance_automatically_granted":False,
        "publication_authorized":False,
        "source_deleted":False,
        "writer_timer_integration_authorized":False,
    }


def evaluate_bytes(data: bytes) -> bytes:
    _require(type(data) is bytes and 0 < len(data) <= 4*1024*1024,
             "invalid bounded evaluation bytes")
    try:
        doc = json.loads(data)
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise MilestoneEvaluationError("invalid evaluation JSON") from exc
    return (json.dumps(evaluate_annotations(doc), ensure_ascii=False,
                       sort_keys=True, separators=(",", ":"))+"\n").encode()


def main() -> None:
    """Offline CLI: emit a non-authoritative, provenance-explicit JSON score."""
    import argparse
    from pathlib import Path
    import sys

    parser = argparse.ArgumentParser(
        description="Offline, nonpublishing Daily milestone annotation evaluator.")
    parser.add_argument("annotation_json", help="private review annotation JSON")
    args = parser.parse_args()
    path = Path(args.annotation_json)
    if path.is_symlink() or not path.is_file():
        parser.error("annotation file must be a regular non-symlink file")
    try:
        raw = path.read_bytes()
        result = evaluate_bytes(raw)
    except (OSError, MilestoneEvaluationError) as exc:
        parser.error(str(exc))
    sys.stdout.buffer.write(result)


if __name__ == "__main__":
    main()
