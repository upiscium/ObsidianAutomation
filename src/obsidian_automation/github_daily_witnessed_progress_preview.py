"""C3: private, selective Japanese progress preview; never a Writer input.

Grounding support is not materiality. Caller decisions are explicitly advisory,
not authenticated human approvals. No model/caller prose is rendered as a fact.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import html
import json
import re

from . import github_daily_witnessed_entity_rollup as roll
from . import github_daily_route_cas as cas
from .github_daily_issue_pr import (
    DailyEntityPreviewError, EntityKey, _event_key, _require_event_time, _structural_signal,
    _routine_comment, _title_for,
)
from .github_daily_summary import GitHubDailySummaryError, parse_evidence_bundle
from .artifact_lifecycle import ArtifactLifecycleError

VERSION = 3
STAGE = "github_daily_witnessed_progress_preview_v3"
MAX_DECISIONS = 4096
MAX_PREVIEW_BYTES = 4 * 1024 * 1024
MAX_MARKDOWN_BYTES = 2 * 1024 * 1024
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_COMMENT_KINDS = frozenset({"issue_comment", "pull_request_comment", "pull_request_review_comment"})
# Commits alone can be cosmetic; require a separate materiality decision.
_LIFECYCLE = frozenset({
    "issue_created", "issue_closed", "issue_reopened", "pull_request_created",
    "pull_request_closed", "pull_request_reopened", "pull_request_merged",
    "pull_request_ready_for_review", "pull_request_converted_to_draft",
})
_STATE_KINDS = _LIFECYCLE - {"pull_request_ready_for_review", "pull_request_converted_to_draft"}


class DailyProgressPreviewError(ValueError):
    """Source, selection or presentation cannot be safely reconstructed."""


def _canonical(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _digest(value: object) -> str:
    if type(value) is not str or not _DIGEST.fullmatch(value):
        raise DailyProgressPreviewError("invalid selection digest")
    return value


@dataclass(frozen=True)
class MaterialityDecision:
    """Advisory classification bound to EXACT full rollup and candidate bytes.

    This record does not prove that a human reviewed anything. An eventual
    approved caller must authenticate human/inference provenance separately.
    """
    rollup_sha256: str
    candidate_sha256: str
    classification: str  # progress | routine


@dataclass(frozen=True)
class WitnessedProgressPreview:
    canonical_bytes: bytes
    markdown: str

    @property
    def sha256(self) -> str:
        return _sha(self.canonical_bytes)

    def report(self) -> dict[str, object]:
        return json.loads(self.canonical_bytes)


def _decisions(records: tuple[MaterialityDecision, ...]) -> tuple[tuple[str, str, str], ...]:
    if type(records) is not tuple or len(records) > MAX_DECISIONS:
        raise DailyProgressPreviewError("invalid bounded materiality decisions")
    owned, seen = [], set()
    for record in records:
        if type(record) is not MaterialityDecision:
            raise DailyProgressPreviewError("invalid materiality record type")
        base, cid, choice = record.rollup_sha256, record.candidate_sha256, record.classification
        _digest(base); _digest(cid)
        if type(choice) is not str or choice not in {"progress", "routine"}:
            raise DailyProgressPreviewError("invalid materiality classification")
        if cid in seen:
            raise DailyProgressPreviewError("duplicate materiality decision")
        seen.add(cid)
        owned.append((base, cid, choice))
    return tuple(owned)


def _routine(event: dict) -> bool:
    if event["kind"] not in _COMMENT_KINDS:
        return False
    body = event.get("body")
    # An absent/truncated excerpt is not evidence that the full source is routine.
    if type(body) is not dict or body.get("truncated") is not False or type(body.get("text")) is not str:
        return False
    return _routine_comment(event)


def _status(events: list[dict]) -> tuple[str, tuple[str, ...]]:
    changes = [e for e in events if e["kind"] in _LIFECYCLE or (
        e["kind"] == "pull_request_review" and _structural_signal(e))]
    if not changes:
        return "", ()
    merges = [e for e in changes if e["kind"] == "pull_request_merged"]
    if merges:
        at = max(e["occurred_at"] for e in merges)
        contradictory = any(e["kind"] in _STATE_KINDS - {"pull_request_merged"}
                            and e["occurred_at"] >= at for e in changes)
        text = ("当日にマージの記録があります（後続・同時刻の状態は要確認）。"
                if contradictory else "PRがマージされました。")
    else:
        at = max(e["occurred_at"] for e in changes)
        latest = [e for e in changes if e["occurred_at"] == at]
        labels = {_structural_signal(e) for e in latest}
        text = labels.pop() if len(labels) == 1 else "状態遷移の記録があります（同時刻の最終状態は要確認）。"
    return text, tuple(e["evidence_id"] for e in changes)


def _escape(text: str) -> str:
    # Source titles are labels, not Markdown/HTML/Obsidian instructions.
    clean = " ".join("".join(c if c.isprintable() else " " for c in text).split())
    clean = clean[:180] + ("…" if len(clean) > 180 else "")
    clean = html.escape(clean, quote=True)
    return re.sub(r"([\\`*_{}\[\]()#!|~])", r"\\\1", clean)


def build_witnessed_progress_preview(
    capture_sha256: str,
    partial_replies: tuple[roll.PartialReply, ...],
    grounding_replies: tuple[roll.GroundReply, ...],
    materiality_decisions: tuple[MaterialityDecision, ...] = (),
    *, max_context_bytes: int = roll.c1a.MAX_CONTEXT_BYTES,
) -> WitnessedProgressPreview:
    """Rebuild rollup, read originals once more and safely select private rows.

    No API accepts an already-normalized rollup as a credential. Decisions are
    snapshotted before CAS access. Source read amplification is limited by
    C2b's quotas plus ONE bounded CAS triplet read for titles/lifecycle facts.
    """
    _digest(capture_sha256)
    decisions = _decisions(materiality_decisions)
    try:
        output = roll.build_witnessed_entity_rollup(
            capture_sha256, partial_replies, grounding_replies,
            max_context_bytes=max_context_bytes,
        )
        frozen = output.to_json_bytes()
        rollup_sha = _sha(frozen)
        data = json.loads(frozen)
        # All below are fresh local decoded structures, never caller-owned maps.
        stored = cas.read_verified_capture(capture_sha256)
        bundle = parse_evidence_bundle(stored.evidence_bytes)
        if (stored.index_sha256 != capture_sha256
                or stored.collector_storage_attested is not True
                or stored.daily_publication_authorized is not False
                or bundle.sha256 != data["evidence_sha256"]
                or data["capture_sha256"] != capture_sha256
                or stored.capture_index.route_receipt_sha256 != data["route_receipt_sha256"]
                or bundle.date != data["date"]
                or stored.capture_index.collection_mode != data["collection_mode"]
                or data["publication_authorized"] is not False):
            raise DailyProgressPreviewError("rollup and original source differ")
        start = datetime.fromisoformat(bundle.date).replace(tzinfo=timezone(timedelta(hours=9)))
        stop = start + timedelta(days=1)
        originals = {}
        for event in bundle.events:
            _require_event_time(event["occurred_at"], start, stop)
            eid = event["evidence_id"]
            if eid in originals:
                raise DailyProgressPreviewError("duplicate original source")
            originals[eid] = event
        candidates = {c["candidate_sha256"]: c for row in data["entities"] for c in row["candidates"]}
        chosen = {}
        for base, cid, choice in decisions:
            if base != rollup_sha or cid not in candidates:
                raise DailyProgressPreviewError("stale or foreign materiality decision")
            candidate = candidates[cid]
            if candidate["verdict"] != "supported":
                raise DailyProgressPreviewError("materiality requires a supported candidate")
            if choice == "progress" and all(_routine(originals[eid]) for eid in candidate["evidence_ids"]):
                raise DailyProgressPreviewError("routine-only source cannot become progress")
            chosen[cid] = choice
        selected, diagnostics, accounted = [], [], set(data["unattributed_default_branch_commit_ids"])
        for row in data["entities"]:
            identity = row["entity"]
            key = EntityKey(identity["repository"], identity["entity_type"], identity["number"])
            ids = row["source_evidence_ids"]
            if len(set(ids)) != len(ids) or accounted.intersection(ids):
                raise DailyProgressPreviewError("source assigned to multiple entities")
            events = [originals[eid] for eid in ids]
            if any(_event_key(e) != key for e in events):
                raise DailyProgressPreviewError("source entity mismatch")
            accounted.update(ids)
            text, transitions = _status(events)
            routine = {e["evidence_id"] for e in events if _routine(e)}
            progress = [c["candidate_sha256"] for c in row["candidates"]
                        if chosen.get(c["candidate_sha256"]) == "progress"]
            review_candidates = [c["candidate_sha256"] for c in row["candidates"]
                                 if c["candidate_sha256"] not in chosen
                                 and any(eid not in routine for eid in c["evidence_ids"])]
            pending = (set(row["uncited_evidence_ids"]) | set(row["unprocessed_evidence_ids"])) - routine - set(transitions)
            for c in row["candidates"]:
                if c["candidate_sha256"] in review_candidates:
                    pending.update(set(c["evidence_ids"]) - routine)
            included = bool(transitions or progress)
            if progress:
                text += f"実質的進展として選択された候補が{len(progress)}件あります（内容・重要度は未承認）。"
            if included:
                selected.append({"entity": identity, "url": key.github_url(),
                    "title": _title_for(events, key), "summary_ja": text,
                    "selection_basis": "source_transition" if transitions else "advisory_materiality",
                    "transition_evidence_ids": list(transitions), "selected_candidate_sha256s": progress})
            diagnostics.append({"entity": identity, "selected": included,
                "reason": "selected" if included else "pending_review" if pending else "no_selected_progress",
                "source_evidence_ids": ids, "routine_evidence_ids": [eid for eid in ids if eid in routine],
                "pending_evidence_ids": [eid for eid in ids if eid in pending],
                "pending_candidate_sha256s": review_candidates,
                "response_set_complete": row["response_set_complete"],
                "candidate_counts": row["candidate_counts"],
                "coverage": {name: row[name] for name in ("supported_evidence_ids", "unsupported_evidence_ids",
                    "unresolved_evidence_ids", "uncited_evidence_ids", "unprocessed_evidence_ids")}})
        if accounted != set(originals):
            raise DailyProgressPreviewError("preview source accounting is incomplete")
        pending_count = sum(len(d["pending_evidence_ids"]) for d in diagnostics)
        lines = [f"# Project Progress — {bundle.date}", "", "> 非公開プレビュー・未承認。公開や本番適用の許可ではありません。", ""]
        repository = None
        for item in selected:
            entity = item["entity"]
            if repository != entity["repository"]:
                repository = entity["repository"]
                lines.extend([f"## {_escape(repository)}", ""])
            kind = "PR" if entity["entity_type"] == "pull_request" else "Issue"
            label = f"{kind} #{entity['number']}"
            if item["title"] != label:
                label += f" — {_escape(item['title'])}"
            lines.extend([f"- [{label}]({item['url']})", f"  {item['summary_ja']}", ""])
        if not selected:
            lines.extend(["採用された進展項目はありません。" if not pending_count else
                          "進展の有無を確定できません。未確認の証拠があります。", ""])
        if pending_count:
            lines.extend([f"確認待ちの元イベント: {pending_count}件。詳細は非公開診断に保持しています。", ""])
        lines.append("重要度・日本語要約・取りこぼしの確認は未承認です。")
        markdown = "\n".join(lines) + "\n"
        if len(markdown.encode()) > MAX_MARKDOWN_BYTES:
            raise DailyProgressPreviewError("preview Markdown exceeds byte budget")
        report = {name: data[name] for name in ("capture_sha256", "route_receipt_sha256", "evidence_sha256",
            "closure_sha256", "date", "captured_at_utc", "collection_mode", "original_event_count",
            "response_set_complete", "unattributed_default_branch_commit_ids")}
        report.update(record_version=VERSION, stage=STAGE, rollup_sha256=rollup_sha,
            selected_entities=selected, entity_diagnostics=diagnostics,
            selected_repository_count=len({x["entity"]["repository"] for x in selected}),
            pending_evidence_count=pending_count,
            decisions_sha256=_sha(_canonical(sorted(decisions))), markdown_sha256=_sha(markdown.encode()),
            publication_authorized=False, model_verdict_is_advisory=True,
            materiality_provenance_verified=False, requires_human_quality_review=True,
            semantic_coverage_review_required=True)
        raw = _canonical(report)
        if len(raw) > MAX_PREVIEW_BYTES:
            raise DailyProgressPreviewError("preview report exceeds byte budget")
        return WitnessedProgressPreview(raw, markdown)
    except DailyProgressPreviewError:
        raise
    except (roll.DailyWitnessedRollupError, ArtifactLifecycleError, GitHubDailySummaryError, DailyEntityPreviewError,
            ValueError, TypeError, AttributeError, KeyError, IndexError, OverflowError, RecursionError):
        raise DailyProgressPreviewError("trusted progress preview unavailable") from None
