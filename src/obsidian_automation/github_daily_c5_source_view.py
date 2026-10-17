"""Non-authoritative C5 model-facing projection of the most recent source events.

The complete C4 Context stays unchanged and CAS-bound. This pure, bounded
wire-only view retains original per-entity source_ref indices so the original
C4 normalizer and C5 Grounding can reopen all original source events.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

VERSION = "c5-recent-whole-source-view-v2"
MAX_SHOWN = 2
MAX_WIRE_BYTES = 16 * 1024
SNAPSHOT_KINDS = frozenset(("issue_snapshot", "pull_request_snapshot"))
CREATED_KINDS = frozenset(("issue_created", "pull_request_created"))
COMMIT_KIND = "pull_request_commit"
CONTENTS = ("title", "body", "message")
# Weak editorial *presentation* hints only. They are literal lines from
# source, not an inference of truth or authorisation.
LIMITATION_MARKERS = (
    "no live", "no production", "no real", "not installed",
    "not merged", "not authorised", "not authorized", "not available",
    "unavailable", "disabled", "uninstalled", "no cutover",
    "run once", "cannot execute", "can_execute=false", "not permission",
    "still blocked", "remains open", "未導入", "未インストール",
    "未マージ", "未反映", "未稼働", "利用不可", "無効", "未完了",
)
MAX_LITERAL_LIMITATION_LINE = 768
TEMPORAL_STATUSES = frozenset((
    "day_snapshot_not_event_instant",
    "content_addressed_commit_not_push_time",
    "event_transition_only",
    "withheld_unknown_modification_time",
    "withheld_outside_target_day",
))


class C5RecentSourceViewError(ValueError):
    """Reject malformed or unrepresentable C4 model-view inputs."""


def _json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


@dataclass(frozen=True)
class RecentSourceView:
    canonical_bytes: bytes
    shown_source_refs: tuple[int, ...]
    omitted_source_refs: tuple[int, ...]
    primary_source_ref: int
    source_event_count: int

    def report(self) -> dict[str, object]:
        return json.loads(self.canonical_bytes)


def _explicit_limitation_quotes(events: list[dict]) -> list[dict]:
    """Quote source lines verbatim, never create a derived factual verdict."""
    found = []
    for item in reversed(events):
        src = item["original_event"]
        for field in ("body", "message", "title"):
            excerpt = src.get(field)
            if excerpt is None:
                continue
            lines = excerpt["text"].splitlines()
            for lineno in range(len(lines) - 1, -1, -1):
                line = lines[lineno]
                if (0 < len(line.strip()) <= MAX_LITERAL_LIMITATION_LINE
                        and any(token in line.lower() for token in LIMITATION_MARKERS)):
                    found.append({"source_ref": item["source_ref"],
                                  "field": field,
                                  "line_index": lineno,
                                  "verbatim_line": line,
                                  "source_truncated": excerpt["truncated"]})
                    if len(found) >= 2:
                        return found
    return found


def _recent_source_refs(sources: list, eligible: list[int], kind: str) -> list[int]:
    """Prefer a PR's latest real code/created milestone + latest status note.

    This is a deterministic source presentation heuristic, not a judgment of
    importance. The original evidence set stays whole and pending refs remain
    explicit; C5 post-edit Grounding is unchanged.
    """
    if kind != "pull_request":
        return eligible[-MAX_SHOWN:]
    comments = [i for i in eligible
                if sources[i]["original_event"]["kind"] in (
                    "pull_request_comment", "pull_request_review_comment",
                    "pull_request_review")]
    implementations = []
    for i in eligible:
        e = sources[i]["original_event"]
        k = e["kind"]
        if k == "pull_request_created" and e.get("body") is not None:
            implementations.append(i)
        elif k == COMMIT_KIND:
            message = e.get("message")
            words = message.get("text", "").lower().lstrip() if type(message) is dict else ""
            if words.startswith(("feat:", "feat(", "fix:", "fix(",
                                 "perf:", "perf(", "security:", "security(")):
                implementations.append(i)
    if not implementations:
        implementations = [i for i in eligible
                           if sources[i]["original_event"]["kind"] == COMMIT_KIND]
    if implementations and comments:
        selected = sorted({implementations[-1], comments[-1]})
        if len(selected) == MAX_SHOWN:
            return selected
    return eligible[-MAX_SHOWN:]


def build_recent_c5_source_view(context: dict) -> RecentSourceView:
    """Select at most two recent, whole **projected** original events.

    Snapshot bodies and empty created shells do not push actual recorded
    updates out of the model's tiny window. If a commit is followed by only
    a review/status comment, the last commit remains the main progress ref.
    Other omitted original refs remain explicitly pending. No text is cut.
    """
    if type(context) is not dict or context.get("stage") != "github_daily_narrative_context_v4":
        raise C5RecentSourceViewError("not a verified C4 Context shape")
    sources = context.get("sources")
    if type(sources) is not list or not 1 <= len(sources) <= 256:
        raise C5RecentSourceViewError("invalid C4 source count")
    if type(context.get("entity")) is not dict or type(context.get("date")) is not str:
        raise C5RecentSourceViewError("invalid C4 target")
    eligible: list[int] = []
    for i, item in enumerate(sources):
        if (type(item) is not dict or item.get("source_ref") != i
                or type(item.get("original_event")) is not dict):
            raise C5RecentSourceViewError("unbound original source index")
        e = item["original_event"]
        kind = e.get("kind")
        if type(item["source_ref"]) is not int or type(kind) is not str:
            raise C5RecentSourceViewError("invalid original source kind or index")
        if kind in SNAPSHOT_KINDS:
            continue
        if kind in CREATED_KINDS and all(e.get(k) is None for k in CONTENTS):
            continue
        if all(e.get(k) is None for k in CONTENTS) and not kind.endswith(
                ("_closed", "_reopened", "_merged")):
            continue
        eligible.append(i)
    if not eligible:
        # A structural-only source can still be represented as pending and C3
        # independently renders lifecycle status. Never certify no progress.
        eligible = [len(sources) - 1]
    chosen = _recent_source_refs(sources, eligible, context["entity"].get("entity_type"))
    while chosen:
        visible_events = []
        for i in chosen:
            event = sources[i]["original_event"]
            excerpts = {}
            for field in CONTENTS:
                value = event.get(field)
                if value is not None and (type(value) is not dict
                        or type(value.get("text")) is not str
                        or type(value.get("truncated")) is not bool):
                    raise C5RecentSourceViewError("invalid original excerpt")
                excerpts[field] = None if value is None else {
                    "text": value["text"], "truncated": value["truncated"]}
            temporal = event.get("temporal_observation")
            if temporal is None:
                status = "legacy_unverified"
                withheld: list[str] = []
            else:
                if (type(temporal) is not dict
                        or temporal.get("policy") != "github-day-snapshot-v1"
                        or temporal.get("status") not in TEMPORAL_STATUSES
                        or temporal.get("historical_instant_verified") is not False
                        or type(temporal.get("withheld_fields")) is not list
                        or any(type(v) is not str for v in temporal["withheld_fields"])):
                    raise C5RecentSourceViewError("unverified temporal source")
                status = temporal["status"]
                withheld = list(temporal["withheld_fields"])
                if any(event.get(k) is not None for k in withheld):
                    raise C5RecentSourceViewError("outside-day withheld source was restored")
            visible_events.append({"source_ref": i, "original_event": {
                "kind": event["kind"],
                "occurred_at": event["occurred_at"],
                "actor": event.get("actor"),
                "draft": event.get("draft"),
                "state": event.get("state"),
                "temporal_source_status": status,
                "historical_instant_verified": False,
                "withheld_fields": withheld,
                **excerpts,
            }})
        # Source order/metadata are hints, not a semantic-importance verdict.
        implementations = [i for i in chosen if sources[i]["original_event"]["kind"]
                           in (COMMIT_KIND, "pull_request_created")]
        primary = implementations[-1] if implementations else chosen[-1]
        chosen_set = set(chosen)
        data = {
            "view_version": VERSION,
            "entity": context["entity"],
            "date": context["date"],
            "source_event_count": len(sources),
            "shown_source_refs": chosen,
            "omitted_source_refs": [i for i in range(len(sources)) if i not in chosen_set],
            "primary_progress_source_ref": primary,
            "sources": visible_events,
            "explicit_limitation_quotes": _explicit_limitation_quotes(visible_events),
            "limitation_quotes_are_source_text_only": True,
            "publication_authorized": False,
            "historical_instant_verified": False,
            "semantic_coverage_verified": False,
        }
        body = _json(data)
        if len(body) <= MAX_WIRE_BYTES:
            return RecentSourceView(body, tuple(chosen),
                                    tuple(data["omitted_source_refs"]),
                                    primary, len(sources))
        chosen = chosen[1:]
    raise C5RecentSourceViewError("latest full source cannot fit private view")
