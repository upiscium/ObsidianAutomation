"""Source-typed Japanese Daily digest without an LLM or human approval.

A private, read-only MVI built from exact GitHub C4/CAS originals. Neither a
textual comment, a model reply nor a commit timestamp is a merge/GA grant.
This module produces an auditable informational preview, never publication.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import html
import json
import re
from typing import Mapping

from . import github_daily_issue_pr as entity
from . import github_daily_route_cas as cas
from . import github_daily_witnessed_entity_context as c1a
from .github_daily_summary import EvidenceBundle, parse_evidence_bundle

VERSION = "github-daily-deterministic-digest-v1"
MAX_EVENTS = 50000
MAX_ENTITIES = 10000
MAX_RENDER_BYTES = 2 * 1024 * 1024
MAX_RECORD_BYTES = 4 * 1024 * 1024
_SHA = re.compile(r"^[a-f0-9]{64}$")
_COMMIT = re.compile(r"^(feat|fix|test|refactor|perf|docs|chore|style)(?:\([^\r\n)]*\))?!?:", re.I)
_ROUTINE_COMMIT = re.compile(
    r"(?:\b(?:whitespace|formatting|typo|generated snapshot|snapshot formatting)\b|"
    r"(?:空白|書式|整形|誤字))", re.I)
_ROUTINE_MESSAGE = re.compile(r"^(?:next|continue|ack|acknowledged|ok|lgtm|thanks|"
                              r"thank you|了解|確認しました|承知しました|ありがとう|👍)$", re.I)
# A mention of a planned/pending result is not an achieved PASS. Ambiguous
# mixed passages intentionally degrade to a plain source-comment mention.
_RESULT_UNCERTAIN = re.compile(
    r"(?:\b(?:plan(?:ned)?|pending|not yet|not performed|not completed|"
    r"no (?:pass|result)|without (?:a )?pass|will|would|todo|to be tested)\b|"
    r"(?:予定|計画|未実施|未完了|結果待ち|これから|まだ.{0,16}完了していない))",
    re.I,
)
_RESULT_POSITIVE = re.compile(
    r"(?:\b(?:passed|pass(?:ed)?|success(?:ful(?:ly)?)?|completed|"
    r"finished|succeeded|failed|failure)\b|"
    r"(?:成功|失敗|合格|不合格|完了|検証済み|実施済み|PASS))",
    re.I,
)
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]()#!|~])")
_KIND_LABEL = {"feat":"実装", "fix":"修正", "test":"テスト", "refactor":"整理",
               "perf":"性能関連", "docs":"文書", "chore":"保守", "style":"書式"}
_TRANSITIONS = {
    "issue_created":"Issueが作成されました。",
    "issue_closed":"Issueがクローズされました。",
    "issue_reopened":"Issueが再オープンされました。",
    "pull_request_created":"PRが作成されました。",
    "pull_request_closed":"PRがクローズされました（マージは未確認）。",
    "pull_request_reopened":"PRが再オープンされました。",
    "pull_request_ready_for_review":"PRがレビュー可能になりました。",
    "pull_request_converted_to_draft":"PRがDraftに戻されました。",
}


class DeterministicDigestError(ValueError):
    """Cannot safely extract a bounded, time-scoped evidence-only digest."""


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False)+"\n").encode()


def _safe_inline(value: str, limit: int = 100) -> str:
    clean = " ".join("".join(c if c.isprintable() else " " for c in value).split())
    clean = clean[:limit] + ("…" if len(clean)>limit else "")
    return html.escape(_MARKDOWN_SPECIAL.sub(r"\\\1", clean), quote=True).replace("%","&#37;")


def _source_text(event: Mapping[str, object], field: str) -> str:
    value = event.get(field)
    if value is None:
        return ""
    if (type(value) is not dict or type(value.get("text")) is not str
            or type(value.get("truncated")) is not bool):
        raise DeterministicDigestError("invalid original text envelope")
    # A truncated source is never evidence of a complete statement.
    return "" if value["truncated"] else value["text"]


def _human_comment(event: Mapping[str, object]) -> str:
    actor=event.get("actor")
    if entity._is_bot(event) or type(actor) not in (str,type(None)):
        return ""
    raw=event.get("body")
    text=_source_text(event,"body").strip()
    if type(raw) is dict and raw.get("truncated") is True:
        # The short head is proof only that an original long comment exists,
        # NOT sufficient evidence that an apparent "PASS" is the final state.
        return "長文の進捗コメントの抜粋が記録されました。"
    if not text or _ROUTINE_MESSAGE.fullmatch(text.strip(" 。.!！　")):
        return ""
    # Accept concise, *specific* original reports such as "GPU PASS";
    # disallow generic one-word chatter, which is not a progress milestone.
    low=text.casefold()
    if len(text)<12 and not any(word in low for word in (
            "gpu","pass","検証","修正","fix","承認","稼働","移行")):
        return ""
    if len(text.encode("utf-8"))>32768:
        return "長文の進捗コメントの抜粋が記録されました。"
    return text


def _commit_type(event: Mapping[str, object]) -> str | None:
    text=_source_text(event,"message").splitlines()
    if not text:
        return None
    subject=text[0].strip()
    match=_COMMIT.match(subject)
    category=match.group(1).casefold() if match else "unknown"
    if category in {"docs","chore","style"}:
        return None
    if category=="test" and _ROUTINE_COMMIT.search(subject):
        return None
    if category=="unknown":
        if (len(subject)<12 or _ROUTINE_COMMIT.search(subject)
                or re.search(r"\b(?:readme|documentation|docs?|format|"
                             r"whitespace|typos?)\b",subject,re.I)):
            return None
    return category


def _human_event_signals(text: str) -> tuple[str, ...]:
    low=text.casefold()
    # Both concrete test and a separate approval decision can be reported in
    # one GitHub comment. Distinguish their TOPICS, but never claim that the
    # current host was directly inspected or an approved cutover occurred.
    signals=[]
    actual_result=bool(_RESULT_POSITIVE.search(text)) and not bool(
        _RESULT_UNCERTAIN.search(text))
    if actual_result and ("gpu" in low or "cuda" in low or "実gpu" in low) and any(
            token in low for token in ("検証","acceptance","test","pass","smoke")):
        signals.append("GPU検証に関する結果が報告されました。")
    elif actual_result and ("test" in low or "テスト" in low or "検証" in low):
        signals.append("テスト・検証に関する結果が報告されました。")
    if any(x in low for x in ("approved","authorized","approval","migration","rollback",
                              "方針","承認","許可","移行")):
        signals.append("方針・承認に関する更新が記録されました。")
    return tuple(signals) if signals else ("進捗コメントが記録されました。",)


def _date_window(bundle: EvidenceBundle):
    try:
        target=date.fromisoformat(bundle.date)
    except (TypeError,ValueError) as exc:
        raise DeterministicDigestError("invalid JST source date") from exc
    if target.isoformat()!=bundle.date or bundle.timezone!="Asia/Tokyo":
        raise DeterministicDigestError("noncanonical source day")
    start,end=entity._date_window(target)
    if (bundle.window_start!=start.isoformat().replace("+00:00","Z")
            or bundle.window_end!=end.isoformat().replace("+00:00","Z")):
        raise DeterministicDigestError("original JST window does not match date")
    return start,end


def _event_time(event: Mapping[str,object], start: datetime, end: datetime):
    try:
        return entity._require_event_time(event.get("occurred_at"),start,end)
    except entity.DailyEntityPreviewError as exc:
        raise DeterministicDigestError("original event outside JST window") from exc


@dataclass(frozen=True)
class DeterministicDigest:
    date: str
    capture_sha256: str | None
    evidence_bundle_sha256: str
    items: tuple[dict, ...]
    original_event_count: int
    unattributed_commit_count: int
    excluded_entity_count: int
    verified_cas_source: bool

    def render(self) -> str:
        if not self.items:
            return "進展のあったIssue・PRはありません。\n"
        lines=[]
        repo=None
        for item in self.items:
            current=item["entity"]["repository"]
            if current!=repo:
                if lines:lines.append("")
                lines.append("## "+_safe_inline(current,160))
                repo=current
            key=item["entity"]
            number=key["number"]
            label="PR" if key["entity_type"]=="pull_request" else "Issue"
            url=f"https://github.com/{current}/{'pull' if label=='PR' else 'issues'}/{number}"
            base_label=f"{label} #{number}"
            title=item["title"]
            display=base_label if title==base_label else (
                base_label+" — "+_safe_inline(title))
            phrases=" ".join(item["sentences"])
            lines.append(f"- [{display}]({url}) — {phrases}")
        result="\n".join(lines)+"\n"
        if len(result.encode("utf-8"))>MAX_RENDER_BYTES:
            raise DeterministicDigestError("Daily render exceeds bounded size")
        return result

    def report(self) -> dict:
        data={
            "record_version":VERSION,
            "stage":"deterministic_preview",
            "date":self.date,
            "source_capture_sha256":self.capture_sha256,
            "source_evidence_sha256":self.evidence_bundle_sha256,
            "verified_cas_source":self.verified_cas_source,
            "model_used":False,
            "needs_user_quality_approval":False,
            "publication_authorized":False,
            "original_event_count":self.original_event_count,
            "unattributed_default_branch_commit_count":self.unattributed_commit_count,
            "excluded_entity_count":self.excluded_entity_count,
            "items":list(self.items),
        }
        body=self.render().encode()
        data["rendered_markdown_sha256"]=_sha(body)
        return data

    @property
    def canonical_bytes(self) -> bytes:
        data=_canonical(self.report())
        if len(data)>MAX_RECORD_BYTES:
            raise DeterministicDigestError("Daily preview record exceeds budget")
        return data

    @property
    def sha256(self) -> str:
        return _sha(self.canonical_bytes)


def build_digest_from_bundle(bundle: EvidenceBundle, *,
                             source_capture_sha256: str | None = None,
                             verified_cas_source: bool = False) -> DeterministicDigest:
    """Pure, bounded derivation. Real use must supply verified original CAS."""
    if type(bundle) is not EvidenceBundle:
        raise DeterministicDigestError("expected canonical original Evidence bundle")
    if len(bundle.events)>MAX_EVENTS:
        raise DeterministicDigestError("original daily event count over limit")
    if verified_cas_source != (source_capture_sha256 is not None):
        raise DeterministicDigestError("verified source identity is ambiguous")
    if source_capture_sha256 is not None and not _SHA.fullmatch(source_capture_sha256):
        raise DeterministicDigestError("invalid CAS source identity")
    start,end=_date_window(bundle)
    grouped=defaultdict(list)
    unattributed=0
    for raw in bundle.events:
        _event_time(raw,start,end)
        try:
            key=entity._event_key(raw)
        except entity.DailyEntityPreviewError as exc:
            raise DeterministicDigestError("original Issue/PR identity is invalid") from exc
        if key is None:
            unattributed+=1
            continue
        grouped[key].append(raw)
    if len(grouped)>MAX_ENTITIES:
        raise DeterministicDigestError("too many Issue/PR identities")
    items=[]
    excluded=0
    for key,records in sorted(grouped.items()):
        events=sorted(records,key=lambda row:(row["occurred_at"],row["evidence_id"]))
        commits=[]
        ignored_commits=[]
        for row in events:
            if row["kind"]!="pull_request_commit" or entity._is_bot(row):
                continue
            category=_commit_type(row)
            if category is not None:commits.append((category,row))
            else:ignored_commits.append(row)
        substantive=[]
        for row in events:
            if row["kind"] not in ("issue_comment","pull_request_comment",
                                  "pull_request_review_comment","pull_request_review"):
                continue
            text=_human_comment(row)
            if text:
                substantive.extend((category,row)
                    for category in _human_event_signals(text))
        reviews=[row for row in events if row["kind"]=="pull_request_review"
                 and not entity._is_bot(row)
                 and row.get("state") in ("APPROVED","CHANGES_REQUESTED")]
        merges=[row for row in events if row["kind"]=="pull_request_merged"]
        transitions=[row for row in events if row["kind"] in _TRANSITIONS
                     and not (entity._is_bot(row) and row["kind"] in (
                         "pull_request_created","issue_created"))]
        if not (commits or substantive or reviews or merges or transitions):
            excluded+=1
            continue
        if (ignored_commits and not commits and not substantive and not reviews
                and not merges and transitions
                and all(row["kind"]=="pull_request_created" for row in transitions)):
            # Opening a PR only to adjust formatting/fixtures is not a
            # substantive Daily achievement; its evidence remains intact.
            excluded+=1
            continue
        phrases=[]
        ids=[]
        def add(phrase, rows):
            if phrase in phrases or len(phrases)>=3:return
            phrases.append(phrase)
            for row in rows:
                eid=row["evidence_id"]
                if eid not in ids:ids.append(eid)
        if commits:
            categories=set(x[0] for x in commits)
            if categories=={"test"}:
                label="事前検証を含むテストコードの変更が記録されました。"
            elif categories=={"feat"}:
                label="実装コミットが記録されました。"
            elif categories=={"fix"}:
                label="修正コミットが記録されました。"
            elif "feat" in categories or "fix" in categories:
                label="実装・修正に関するコミットが記録されました。"
            elif categories=={"unknown"}:
                label="コミットが記録されました（変更内容はGitHubで確認）。"
            else:
                label="コミットが記録されました（変更内容はGitHubで確認）。"
            add(label,[r for _,r in commits])
        # Two distinct source-reported categories may help reconstruct an
        # experiment/decision, but never promote the report into a true GA state.
        selected={}
        for phrase,row in substantive:
            selected[phrase]=row
        if substantive:
            ordered=["GPU検証に関する結果が報告されました。",
                     "テスト・検証に関する結果が報告されました。",
                     "方針・承認に関する更新が記録されました。",
                     "進捗コメントが記録されました。"]
            # Preserve at most two types while leaving room for verified merge.
            reserved=1 if merges else 0
            for label in ordered:
                if label=="進捗コメントが記録されました。" and phrases:
                    # Generic chat should not consume a third line after
                    # concrete GPU/test/decision updates were already found.
                    continue
                if label in selected and len(phrases)<3-reserved:
                    add(label,[selected[label]])
        if not merges:
            if reviews and len(phrases)<3:
                state=reviews[-1]["state"]
                add("レビューで承認されました。" if state=="APPROVED"
                    else "レビューで変更要求がありました。",[reviews[-1]])
            if transitions and len(phrases)<3:
                latest=transitions[-1]
                if (not phrases or latest["kind"] not in (
                        "pull_request_created","issue_created")):
                    add(_TRANSITIONS[latest["kind"]],[latest])
        if merges:
            # Merge takes precedence over earlier Draft/ready/closed. Never
            # infer that a merged source PR was deployed to running GA.
            if len(phrases)>=3:
                phrases=phrases[:2]
            add("当日にPRがマージされました。",[merges[-1]])
            status="merged"
        else:
            status=transitions[-1]["kind"] if transitions else "updated"
        if not phrases:
            # A verified meaningful event must not disappear for lack of
            # extractable prose. This is the canonical low-information fallback.
            add("更新が記録されました（詳細はGitHub）。",[events[-1]])
        try:
            title=entity._title_for(events,key)
        except entity.DailyEntityPreviewError as exc:
            raise DeterministicDigestError("untrusted Issue/PR title") from exc
        default=f"{'PR' if key.entity_type=='pull_request' else 'Issue'} #{key.number}"
        if title==default and commits:
            # Preserve the literal Git commit's first line as a useful label
            # if GitHub withheld a mutable PR title for the historical day.
            title=_source_text(commits[0][1],"message").splitlines()[0].strip()[:110]
        items.append({
            "entity":{"repository":key.repository,"entity_type":key.entity_type,
                      "number":key.number},
            "title":title,"status":status,
            "sentences":phrases,
            "evidence_ids":ids,
            "source_event_count":len(events),
        })
    return DeterministicDigest(
        date=bundle.date, capture_sha256=source_capture_sha256,
        evidence_bundle_sha256=bundle.sha256, items=tuple(items),
        original_event_count=len(bundle.events),
        unattributed_commit_count=unattributed,
        excluded_entity_count=excluded,
        verified_cas_source=verified_cas_source)


def build_digest_from_verified_capture(capture_sha256: str) -> DeterministicDigest:
    """Reopen exact C4/CAS and reject any unknown/unaccounted original event."""
    if type(capture_sha256) is not str or not _SHA.fullmatch(capture_sha256):
        raise DeterministicDigestError("invalid capture identity")
    try:
        source=c1a.build_witnessed_entity_contexts(capture_sha256)
        stored=cas.read_verified_capture(capture_sha256)
        bundle=parse_evidence_bundle(stored.evidence_bytes)
    except (ValueError,TypeError,OSError) as exc:
        raise DeterministicDigestError("source CAS/C1a not admitted") from exc
    ids=[part.original["evidence_id"] for ctx in source.contexts
         for part in ctx.events]
    original_ids=[record["evidence_id"] for record in bundle.events]
    defaults=[record["evidence_id"] for record in bundle.events
              if record["kind"]=="default_branch_commit"]
    if (source.capture_sha256!=capture_sha256 or
        stored.index_sha256!=capture_sha256 or
        stored.collector_storage_attested is not True or
        stored.daily_publication_authorized is not False or
        source.evidence_sha256!=bundle.sha256 or
        source.date!=bundle.date or
        len(ids)!=len(set(ids)) or len(defaults)!=len(set(defaults)) or
        set(ids)&set(defaults) or set(ids)|set(defaults)!=set(original_ids) or
        source.original_event_count!=len(original_ids) or
        source.unattributed_default_commit_count!=len(defaults)):
        raise DeterministicDigestError("original CAS source census is incomplete")
    return build_digest_from_bundle(
        bundle,source_capture_sha256=capture_sha256,verified_cas_source=True)
