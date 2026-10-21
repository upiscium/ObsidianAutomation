"""Opt-in, source-bounded related Issue/PR later-evidence quality warning.

A dated comment describing unfinished work may be superseded by later,
related Issue/PR observations, but same model name != same validation stage,
and a healthy service != proof of a completed GA cutover. This optional
diagnostic returns candidates for human adjudication, never new C5 claims.
"""
from __future__ import annotations

from datetime import datetime
import re

VERSION = "github-daily-related-later-progress-candidate-v1"
MAX_RELATED_CONTEXTS = 8
MAX_CITED_SENTENCES = 8
MAX_EVENT_TEXT_BYTES = 32768
_NOT_DONE = re.compile(
    r"(?:未実施|未完了|まだ.{0,34}(?:完了|実行|推論).{0,20}(?:していない|されていない|しておらず|していません)|"
    r"(?:まだ|実際の).{0,50}(?:推論|稼働|実行).{0,55}(?:未|していない|されていない|まだ.{0,24}(?:しておらず|していない|していません))|"
    r"not (?:yet )?(?:completed|run|tested|ready|deployed)|"
    r"no actual .{0,50}(?:inference|run|test))", re.IGNORECASE,
)
_POSSIBLE_LATER = re.compile(
    r"(?:\b(?:active|healthy|ready|passed|pass|completed|succeeded|"
    r"operational)\b|(?:稼働|完了|成功|実動作|実GPU))",
    re.IGNORECASE,
)
_TECH_ID = re.compile(r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z._-]{1,24}[0-9]"
                      r"(?:[A-Za-z0-9_.-]{0,23})", re.IGNORECASE)
_TERM_IGNORE = frozenset({"ga", "ci", "e2e", "sha256", "cuda12"})
_LINK = re.compile(r"(?<![\w])#\s*([1-9][0-9]{0,8})(?![0-9])")


class RelatedStatusAuditError(ValueError):
    """Invalid cross-entity source cannot become a verified status claim."""


def _time(value):
    if type(value) is not str or len(value) > 60:
        return None
    try:
        stamp=datetime.fromisoformat(value.replace("Z","+00:00"))
        return stamp if stamp.tzinfo is not None else None
    except ValueError:
        return None


def _key(value):
    if (type(value) is not dict
            or value.get("stage")!="github_daily_narrative_context_v4"
            or value.get("publication_authorized") is not False
            or type(value.get("capture_sha256")) is not str
            or re.fullmatch(r"[0-9a-f]{64}",value["capture_sha256"]) is None
            or type(value.get("entity")) is not dict
            or type(value.get("sources")) is not list
            or type(value.get("date")) is not str
            or not 1 <= len(value["sources"]) <= 256):
        raise RelatedStatusAuditError("invalid source context")
    entity=value["entity"]
    if (type(entity.get("repository")) is not str
            or type(entity.get("entity_type")) is not str
            or type(entity.get("number")) is not int
            or entity["number"]<1):
        raise RelatedStatusAuditError("invalid source identity")
    return (entity["repository"],entity["entity_type"],entity["number"])


def _event_text(event):
    chunks=[]
    total=0
    for field in ("title","message","body"):
        value=event.get(field)
        if (type(value) is not dict or type(value.get("text")) is not str
                or value.get("truncated") is not False):
            continue
        buf=value["text"].encode("utf-8")
        if total+len(buf)>MAX_EVENT_TEXT_BYTES:
            continue
        total+=len(buf)
        chunks.append(value["text"])
    return "\n".join(chunks)


def _tech_names(value):
    return {term.casefold() for term in _TECH_ID.findall(value)
            if term.casefold() not in _TERM_IGNORE}


def _link_to_issue(related, target_id):
    number=target_id[2]
    for row in related["sources"]:
        event=row["original_event"]
        if (event.get("kind")=="pull_request_commit"
                or event.get("kind","").endswith("_comment")):
            if str(number) in _LINK.findall(_event_text(event)):
                return row["source_ref"]
    return None


def inspect_related_later_progress(target, sentences, related_contexts=()):
    """Return review-only later linked candidates, never a contradiction verdict.

    Candidates must be explicitly provided, in the same captured JST day,
    the same repository and linked by an original source event reference.
    Later timestamps are event timestamps, NOT proof that current mutable
    comment text was historically identical at that instant.
    """
    key=_key(target)
    if (type(related_contexts) not in (list,tuple)
            or len(related_contexts)>MAX_RELATED_CONTEXTS
            or type(sentences) is not list
            or len(sentences)>MAX_CITED_SENTENCES):
        raise RelatedStatusAuditError("unbounded related status audit")
    if key[1]!="issue":
        return _report(())
    for index,entry in enumerate(target["sources"]):
        if (type(entry) is not dict or entry.get("source_ref")!=index
                or type(entry.get("original_event")) is not dict):
            raise RelatedStatusAuditError("source order not bound")
    earlier=[]
    for item in sentences:
        if (type(item) is not dict or type(item.get("text")) is not str
                or len(item["text"])>720
                or type(item.get("source_refs")) is not list):
            raise RelatedStatusAuditError("invalid model claim")
        cited=item["source_refs"]
        if (not cited or any(type(ref) is not int
                             or not 0 <= ref < len(target["sources"]) for ref in cited)):
            raise RelatedStatusAuditError("unbound model source references")
        if not _NOT_DONE.search(item["text"]):
            continue
        stamps=[_time(target["sources"][ref]["original_event"].get("occurred_at"))
                for ref in cited]
        if any(t is None for t in stamps):
            continue
        named=_tech_names(item["text"])
        if named:
            earlier.append((max(stamps), named, sorted(set(cited))))
    if not earlier:
        return _report(())
    results=[]
    seen=set()
    for related in related_contexts:
        other=_key(related)
        if (other[0]!=key[0] or other[1]!="pull_request"
                or related["date"]!=target["date"]
                or related["capture_sha256"]!=target["capture_sha256"]
                or other==key):
            continue
        for index,row in enumerate(related["sources"]):
            if (type(row) is not dict or row.get("source_ref")!=index
                    or type(row.get("original_event")) is not dict):
                raise RelatedStatusAuditError("related source order not bound")
        link_ref=_link_to_issue(related,key)
        if link_ref is None:
            continue
        for newer_ref,row in enumerate(related["sources"]):
            event=row["original_event"]
            if (event.get("kind") not in ("pull_request_comment",
                                         "pull_request_review_comment",
                                         "pull_request_commit")):
                continue
            stamp=_time(event.get("occurred_at"))
            if stamp is None:
                continue
            text=_event_text(event)
            if not text or not _POSSIBLE_LATER.search(text):
                continue
            shared=_tech_names(text)
            for older_at,names,old_refs in earlier:
                common=names & shared
                if stamp<=older_at or not common:
                    continue
                identifier=(other,newer_ref,tuple(old_refs),tuple(sorted(common)))
                if identifier in seen:
                    continue
                seen.add(identifier)
                results.append({
                    "related_entity":{"repository":other[0],
                                      "entity_type":other[1],
                                      "number":other[2]},
                    "original_link_ref":link_ref,
                    "later_source_ref":newer_ref,
                    "older_target_model_source_refs":old_refs,
                    "shared_technical_identifiers":sorted(common),
                    "source_event_after_cited_reference":True,
                    "not_proof_of_actual_runtime_or_merged_state":True,
                })
    return _report(results)


def _report(rows):
    result=sorted(rows,key=lambda x:(
        x["related_entity"]["repository"],x["related_entity"]["number"],
        x["later_source_ref"],x["older_target_model_source_refs"]))
    return {
        "version":VERSION,
        "status":"human_recheck_required" if result else "no_linked_candidate_detected",
        "later_related_progress_candidates":result,
        "model_claim_contradiction_certified":False,
        "site_or_cutover_attested":False,
        "c4_original_source_deleted":False,
        "publication_authorized":False,
    }
