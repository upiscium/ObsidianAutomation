"""Read-only, operator-annotated source-role audit for individual GitHub Daily narratives.

Neither a model Grounding verdict nor a supplied human/operator annotation is an
attested source fact. This module never authorizes publication or changes a draft.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

VERSION = 2
STAGE = "github_daily_source_role_audit_v2"
MAX_PREVIEW_BYTES = 4 * 1024 * 1024
MAX_SOURCE_REVIEW_BYTES = 2 * 1024 * 1024
MAX_AUDIT_BYTES = 128 * 1024
MAX_ANNOTATIONS = 12
MAX_ANCHORS = 16
CLAIM_KINDS = frozenset({"performed_action", "example_report", "unresolved_status", "other"})
SOURCE_ROLES = frozenset({"direct_action", "quoted_example", "proposed", "negated", "background"})
ASSESSMENTS = frozenset({"entailed", "not_entailed", "uncertain"})


class SourceRoleAuditError(ValueError):
    """Malformed, unbound or incomplete audit inputs cannot be used."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _read(data: bytes, limit: int, name: str) -> dict:
    if type(data) is not bytes or not 0 < len(data) <= limit:
        raise SourceRoleAuditError("invalid bounded " + name)
    try:
        value = json.loads(data)
        if type(value) is not dict or _canonical(value) != data:
            raise ValueError("not canonical")
    except (UnicodeError, ValueError, TypeError, OverflowError, RecursionError):
        raise SourceRoleAuditError("invalid canonical " + name) from None
    return value


def _identifier(entity):
    if type(entity) is not dict or set(entity) != {"repository", "entity_type", "number"}:
        raise SourceRoleAuditError("invalid audit entity")
    repo, kind, number = entity["repository"], entity["entity_type"], entity["number"]
    if (type(repo) is not str or not repo or kind not in ("issue", "pull_request")
            or type(number) is not int or number <= 0):
        raise SourceRoleAuditError("invalid audit entity")
    return repo, kind, number


def _annotation(value):
    if type(value) is not dict or set(value) != {"sentence_index", "claim_start", "claim_end", "claim_kind", "assessment", "anchors"}:
        raise SourceRoleAuditError("invalid exact audit annotation")
    index = value["sentence_index"]
    start, end = value["claim_start"], value["claim_end"]
    anchors = value["anchors"]
    if (type(index) is not int or index < 0
            or type(start) is not int or type(end) is not int or not 0 <= start < end
            or type(value["claim_kind"]) is not str or value["claim_kind"] not in CLAIM_KINDS
            or type(value["assessment"]) is not str or value["assessment"] not in ASSESSMENTS
            or type(anchors) is not list or not 1 <= len(anchors) <= MAX_ANCHORS):
        raise SourceRoleAuditError("invalid bounded audit annotation")
    return index, start, end, anchors


@dataclass(frozen=True)
class SourceRoleAudit:
    canonical_bytes: bytes

    @property
    def sha256(self) -> str:
        return _sha(self.canonical_bytes)

    def report(self) -> dict:
        return json.loads(self.canonical_bytes)


def audit_individual_source_roles(source_review_bytes: bytes, preview_bytes: bytes,
                                  target_entity: tuple[str, str, int],
                                  annotations: list[dict] | tuple[dict, ...] = ()) -> SourceRoleAudit:
    """Bind operator annotation offsets to C5 original fields and C4 cited sentences.

    Source authority belongs to the caller's already-verified C5 capture; these
    self-consistent bytes are NOT independent evidence of GitHub authenticity.
    Anchors are user-assigned interpretations, not parser/model attestations.
    All semantic assessments remain advisory, even when they say 'entailed'.
    """
    source = _read(source_review_bytes, MAX_SOURCE_REVIEW_BYTES, "source review")
    preview = _read(preview_bytes, MAX_PREVIEW_BYTES, "narrative preview")
    if (type(source.get("record_version")) is not int or source["record_version"] != 1
            or source.get("stage") != "c5-literal-source-review-v1"
            or source.get("preview_sha256") != _sha(preview_bytes)
            or source.get("source_context_set_sha256") != preview.get("source_context_set_sha256")
            or source.get("capture_sha256") != preview.get("capture_sha256")
            or source.get("publication_authorized") is not False
            or preview.get("publication_authorized") is not False):
        raise SourceRoleAuditError("unbound source-review provenance")
    if (type(target_entity) is not tuple or len(target_entity) != 3
            or type(target_entity[0]) is not str or type(target_entity[1]) is not str
            or type(target_entity[2]) is not int
            or not target_entity[0] or target_entity[1] not in ("issue", "pull_request")
            or target_entity[2] <= 0):
        raise SourceRoleAuditError("invalid selected entity")
    try:
        src_rows = [r for r in source["entities"] if _identifier(r["entity"]) == target_entity]
        preview_rows = [r for r in preview["entities"] if _identifier(r["entity"]) == target_entity]
        if len(src_rows) != 1 or len(preview_rows) != 1:
            raise SourceRoleAuditError("ambiguous or unrequested audit entity")
        sr, pr = src_rows[0], preview_rows[0]
        if sr["context_sha256"] != pr["context_sha256"]:
            raise SourceRoleAuditError("source context identity mismatch")
        sentences = pr["sentences"]
        if type(sentences) is not list or len(sentences) > 3:
            raise SourceRoleAuditError("invalid selected narrative")
        # Validate every sentence BEFORE label offsets or audit iteration. The
        # private preview is canonical JSON, not an independent type guarantee.
        # Genuine C4 may leave a sentence's Grounding status unresolved.
        for sentence in sentences:
            if (type(sentence) is not dict
                    or type(sentence.get("text")) is not str
                    or not 0 < len(sentence["text"]) <= 360
                    or len(sentence["text"].encode("utf-8")) > 2048
                    or type(sentence.get("verdict")) is not str
                    or sentence["verdict"] not in ("supported", "unsupported", "unresolved")
                    or type(sentence.get("evidence_ids")) is not list
                    or not 1 <= len(sentence["evidence_ids"]) <= 16
                    or any(type(eid) is not str or len(eid) != 64
                           or any(c not in "0123456789abcdef" for c in eid)
                           for eid in sentence["evidence_ids"])
                    or len(set(sentence["evidence_ids"])) != len(sentence["evidence_ids"])):
                raise SourceRoleAuditError("invalid typed narrative sentence")
        field_index = {}
        for field in sr["fields"]:
            ref, name, text = field["source_ref"], field["field"], field["text"]
            if (type(ref) is not int or ref < 0 or name not in ("body", "title", "message")
                    or type(text) is not str or field["text_sha256"] != _sha(text.encode("utf-8"))
                    or type(field["evidence_id"]) is not str
                    or len(field["evidence_id"]) != 64):
                raise SourceRoleAuditError("invalid source field identity")
            key = ref, name
            if key in field_index:
                raise SourceRoleAuditError("duplicate source field")
            field_index[key] = field
    except (KeyError, IndexError, TypeError, AttributeError):
        raise SourceRoleAuditError("malformed source review or preview") from None

    if type(annotations) not in (list, tuple) or len(annotations) > MAX_ANNOTATIONS:
        raise SourceRoleAuditError("too many audit annotations")
    labels = {}
    for a in annotations:
        index, claim_start, claim_end, anchors = _annotation(a)
        if index >= len(sentences) or claim_end > len(sentences[index]["text"]):
            raise SourceRoleAuditError("out-of-range claim span")
        if any(i == index and claim_start < e and s < claim_end for i, s, e in labels):
            raise SourceRoleAuditError("overlapping or duplicate claim span")
        sentence = sentences[index]
        if sentence.get("verdict") != "supported" or type(sentence.get("evidence_ids")) is not list:
            raise SourceRoleAuditError("only cited displayed sentences can be audited")
        seen_anchors, retained = set(), []
        for anchor in anchors:
            if type(anchor) is not dict or set(anchor) != {"source_ref", "field", "start", "end", "role"}:
                raise SourceRoleAuditError("invalid exact source-span anchor")
            ref, name, start, end, role = (anchor[k] for k in ("source_ref", "field", "start", "end", "role"))
            if (type(ref) is not int or ref < 0 or type(name) is not str
                    or type(start) is not int or type(end) is not int
                    or type(role) is not str or role not in SOURCE_ROLES):
                raise SourceRoleAuditError("invalid span coordinates or role")
            field = field_index.get((ref, name))
            if (field is None or field["evidence_id"] not in sentence["evidence_ids"]
                    or not 0 <= start < end <= len(field["text"])):
                raise SourceRoleAuditError("source span not within cited evidence")
            key = ref, name, start, end
            if key in seen_anchors:
                raise SourceRoleAuditError("duplicate source span")
            seen_anchors.add(key)
            retained.append({"source_ref": ref, "field": name, "start": start,
                             "end": end, "role": role,
                             "source_text_sha256": field["text_sha256"]})
        labels[(index, claim_start, claim_end)] = (a, retained)

    results = []
    supported_sentence_count = 0
    for i, sentence in enumerate(sentences):
        if sentence.get("verdict") != "supported":
            continue
        if type(sentence.get("text")) is not str:
            raise SourceRoleAuditError("invalid narrative sentence")
        supported_sentence_count += 1
        sentence_sha = _sha(sentence["text"].encode("utf-8"))
        atoms = [(s, e, value) for (idx, s, e), value in labels.items() if idx == i]
        if not atoms:
            results.append({"sentence_index": i, "sentence_sha256": sentence_sha,
                "claim_start": None, "claim_end": None, "claim_sha256": None,
                "model_grounding_verdict": "supported", "source_scope_state": "unreviewed",
                "operator_assessment": None, "claim_kind": None, "anchors": []})
            continue
        for start, end, (label, retained) in sorted(atoms):
            roles = {a["role"] for a in retained}
            performed = label["claim_kind"] == "performed_action"
            if performed and "direct_action" not in roles:
                scope_state = "quoted_example_only" if roles == {"quoted_example"} else "performed_action_not_established_by_anchors"
            else:
                scope_state = "no_scope_conflict_detected_not_verified"
            results.append({"sentence_index": i, "sentence_sha256": sentence_sha,
                "claim_start": start, "claim_end": end,
                "claim_sha256": _sha(sentence["text"][start:end].encode("utf-8")),
                "model_grounding_verdict": "supported", "source_scope_state": scope_state,
                "operator_assessment": label["assessment"],
                "claim_kind": label["claim_kind"], "anchors": retained})
    result = {"record_version": VERSION, "stage": STAGE, "entity": dict(zip(
        ("repository", "entity_type", "number"), target_entity)),
        "capture_sha256": source["capture_sha256"],
        "source_review_sha256": _sha(source_review_bytes),
        "preview_sha256": _sha(preview_bytes),
        "source_context_sha256": sr["context_sha256"], "results": results,
        "supported_sentence_count": supported_sentence_count,
        "annotated_sentence_count": len({index for index, _, _ in labels}),
        "annotated_claim_count": len(labels),
        "all_atomic_claims_labeled_verified": False,
        "scope_conflict_count": sum(r["source_scope_state"] in
            ("quoted_example_only", "performed_action_not_established_by_anchors") for r in results),
        "annotations_independently_attested": False,
        "semantic_coverage_verified": False, "factuality_verified": False,
        "requires_human_quality_review": True, "publication_authorized": False}
    raw = _canonical(result)
    if len(raw) > MAX_AUDIT_BYTES:
        raise SourceRoleAuditError("source-role audit exceeds byte limit")
    return SourceRoleAudit(raw)
