"""Exact-line, non-destructive d1-3B source subfragments for private C5 shadow.

Boundaries are original line boundaries, not model-generated or semantically
certified sentences. Crucially, don't break a line to fit the token window:
oversized lines remain pending, including any negation and caveats.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

from . import github_daily_d1_priority_shadow as d1

VERSION = "daily-d1-original-line-shadow-v1"
MAX_LINES_TOTAL = 10000
MAX_LINE_CHARS = 520  # Not an assumption about tokens; TSUMGI enforces 128-token cap.


def build_line_priority_plan(plan: d1.PriorityPlan) -> d1.PriorityPlan:
    """Project C4-bound d1 paragraph plan into exact lines; retain original CAS."""
    if type(plan) is not d1.PriorityPlan:
        raise d1.D1PriorityShadowError("invalid d1 C4 source plan")
    original = plan.report()
    if (original.get("stage") != "daily_c5_d1_priority_plan"
            or original.get("record_version") != d1.VERSION
            or original.get("model_selected") != d1.MODEL
            or original.get("model_question_version") != d1.QUESTION_VERSION
            or original.get("publication_authorized") is not False
            or original.get("source_evidence_deleted") is not False
            or type(original.get("paragraphs")) is not list):
        raise d1.D1PriorityShadowError("unverified parent plan")
    old_spans = original["paragraphs"]
    segments = []
    seen = set()
    for old in old_spans:
        if (type(old) is not dict or type(old.get("start")) is not int
                or type(old.get("end")) is not int
                or type(old.get("text")) is not str
                or old["end"] - old["start"] != len(old["text"])
                or d1._sha(old["text"].encode("utf-8")) != old.get("text_sha256")):
            raise d1.D1PriorityShadowError("invalid original CAS-bound span")
        original_text = old["text"]
        cursor = old["start"]
        substrings = original_text.splitlines(keepends=True)
        if not substrings and original_text:
            substrings = [original_text]
        assert "".join(substrings) == original_text
        for text in substrings:
            start = cursor
            end = start + len(text)
            cursor = end
            if not text.strip():
                continue
            piece = dict(old)
            piece["id"] = (
                f"{old['entity']['repository']}:{old['entity']['entity_type']}:"
                f"{old['entity']['number']}:ref{old['source_ref']}:"
                f"{old['field']}:{start}-{end}")
            piece["start"] = start
            piece["end"] = end
            piece["text"] = text
            piece["text_sha256"] = d1._sha(text.encode("utf-8"))
            piece["line_original_parent_id"] = old["id"]
            piece["line_boundary_version"] = VERSION
            piece["line_exceeds_safe_char_hint"] = len(text) > MAX_LINE_CHARS
            if piece["id"] in seen:
                raise d1.D1PriorityShadowError("duplicate original line")
            seen.add(piece["id"])
            segments.append(piece)
            if len(segments) > MAX_LINES_TOTAL:
                raise d1.D1PriorityShadowError("source line budget exceeded")
        if cursor != old["end"]:
            raise d1.D1PriorityShadowError("unreconciled line boundaries")
    new = dict(original)
    new["paragraphs"] = segments
    new["paragraph_count"] = len(segments)
    new["source_line_view_version"] = VERSION
    new["original_paragraph_plan_sha256"] = plan.sha256
    new["source_evidence_deleted"] = False
    new["semantic_coverage_verified"] = False
    new["publication_authorized"] = False
    raw=d1._json(new)
    if len(raw)>d1.MAX_PLAN_BYTES:
        raise d1.D1PriorityShadowError("private line plan byte budget exceeded")
    return d1.PriorityPlan(raw)
