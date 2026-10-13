"""Read-only, never-publishing daily Issue/PR candidate rollup (#332).

Requires *complete* typed Partial and singleton Grounding responses for every
verified entity context, rebinding all sources to the exact original Evidence.
An advisory model verdict is NOT proof of material progress. Unattributed
comments and commits remain explicit unresolved state, never 'no progress'.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Mapping

from .github_daily_entity_context import (
    MAX_CONTEXT_BATCHES, DailyEntityContextError,
    build_entity_contexts, normalize_entity_candidates,
)
from .github_daily_entity_grounding import (
    build_entity_grounding_contexts,
    normalize_entity_grounding_verdict,
)
from .github_daily_issue_pr import (
    EntityKey, _STRUCTURAL_PROGRESS, _UNVERIFIED_DEPLOY,
    _UNVERIFIED_MERGE, _UNVERIFIED_LINK, _CROSS_ENTITY_MENTION,
    _escape_markdown_text, _is_bot, _routine_comment,
)

VERSION = 2
STAGE = "github_daily_entity_grounded_rollup_preview_v2"
MAX_MODEL_CONTEXTS = MAX_CONTEXT_BATCHES
MAX_GROUND_CONTEXTS = 32768
MAX_ENTITY_CANDIDATES = 40
MAX_TOTAL_PREVIEW_BYTES = 2 * 1024 * 1024
MAX_SUMMARY_BYTES_PER_ENTITY = 32 * 1024
# Each trust-boundary parser deliberately replays the original Evidence.
# Fail closed before adversarial input causes unbounded full-bundle replays.
MAX_EVIDENCE_REPLAY_BYTES = 512 * 1024 * 1024
_SHA = re.compile(r"^[0-9a-f]{64}$")
# 'snapshot' and unclassified comments are not verifiable state changes.
_LIFECYCLE_KINDS = frozenset({
    "issue_created", "issue_closed", "issue_reopened",
    "pull_request_created", "pull_request_closed",
    "pull_request_reopened", "pull_request_merged",
    "pull_request_ready_for_review", "pull_request_converted_to_draft",
})
_POSSIBLY_MATERIAL_KINDS = frozenset({
    "issue_comment", "pull_request_comment",
    "pull_request_review", "pull_request_review_comment",
    "pull_request_commit",
})
_REVIEW_TRANSITIONS = {
    "approved": "PRレビューで承認されました。",
    "changes_requested": "PRレビューで変更要求を受けました。",
}


def _material_human_source(event: Mapping[str, object]) -> bool:
    return (
        event["kind"] in _POSSIBLY_MATERIAL_KINDS
        and not _is_bot(event)
        and (event["kind"] == "pull_request_review" or not _routine_comment(event))
    )


def _lifecycle_signal(event: Mapping[str, object]) -> str | None:
    if event["kind"] in _LIFECYCLE_KINDS:
        return str(event["kind"])
    if event["kind"] == "pull_request_review" and not _is_bot(event):
        state = event.get("state")
        if isinstance(state, str) and state.casefold() in _REVIEW_TRANSITIONS:
            return "pull_request_review:" + state.casefold()
    return None


class DailyEntityRollupError(DailyEntityContextError):
    """Incomplete or inconsistent entity rollup is never publication-ready."""


def _canonical(value: Mapping[str, object]) -> bytes:
    return (json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ) + "\n").encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _key(key: EntityKey) -> dict[str, object]:
    return {
        "repository": key.repository,
        "entity_type": key.entity_type,
        "number": key.number,
        "url": key.github_url(),
    }


def _source_sort(event: Mapping[str, object]) -> tuple[str, str]:
    return str(event["occurred_at"]), str(event["evidence_id"])


def _validated_report_inputs(
    supplied: Mapping[str, bytes], expected: set[str], *, label: str,
) -> None:
    if not isinstance(supplied, Mapping) or len(supplied) > MAX_GROUND_CONTEXTS:
        raise DailyEntityRollupError(f"{label} reply map is invalid")
    if any(type(k) is not str or _SHA.fullmatch(k) is None for k in supplied):
        raise DailyEntityRollupError(f"{label} keys must be context SHA-256")
    if set(supplied) != expected:
        raise DailyEntityRollupError(f"{label} replies missing, extra or swapped")
    if any(type(value) is not bytes for value in supplied.values()):
        raise DailyEntityRollupError(f"{label} responses must be bytes")


def _reject_unverified_status(summary: str, *, key: EntityKey, merged: bool) -> None:
    # A v2 Partial candidate still allows Unicode layout characters. Never
    # let an invisible line separator turn one candidate into Markdown blocks.
    if not summary.isprintable() or len(summary.splitlines()) != 1:
        raise DailyEntityRollupError("model progress contains nonprintable layout characters")
    if _UNVERIFIED_LINK.search(summary) or _UNVERIFIED_DEPLOY.search(summary):
        raise DailyEntityRollupError("model progress text contains unverified URL or deployment")
    if _UNVERIFIED_MERGE.search(summary) and not merged:
        raise DailyEntityRollupError("model progress text asserts unverified merge")
    if any(int(n) != key.number for n in _CROSS_ENTITY_MENTION.findall(summary)):
        raise DailyEntityRollupError("model progress text cross-refers to another entity")


@dataclass(frozen=True)
class EntityRollupItem:
    key: EntityKey
    status_event: str | None
    verified_merge_event: bool
    lifecycle_conflict: bool
    model_supported_candidates: tuple[str, ...]
    supported_candidate_sha256s: tuple[str, ...]
    supported_evidence_ids: tuple[str, ...]
    lifecycle_evidence_ids: tuple[str, ...]
    unresolved_source_ids: tuple[str, ...]
    rejected_candidate_count: int

    def payload(self) -> dict[str, object]:
        return {
            "entity": _key(self.key),
            "latest_verified_lifecycle_event": self.status_event,
            "same_day_merge_event_verified": self.verified_merge_event,
            "conflicting_later_lifecycle": self.lifecycle_conflict,
            "advisory_supported_summaries": list(self.model_supported_candidates),
            "supported_candidate_sha256s": list(self.supported_candidate_sha256s),
            "supported_evidence_ids": list(self.supported_evidence_ids),
            "lifecycle_evidence_ids": list(self.lifecycle_evidence_ids),
            "unresolved_source_ids": list(self.unresolved_source_ids),
            "unsupported_candidate_count": self.rejected_candidate_count,
            "model_materiality_verified": False,
        }


@dataclass(frozen=True)
class DailyEntityRollupPreview:
    date: str
    evidence_bundle_sha256: str
    items: tuple[EntityRollupItem, ...]
    unresolved_entity_keys: tuple[EntityKey, ...]
    unverified_issue_comments: int
    unverified_pr_comments: int
    unverified_pr_commits: int
    unassigned_default_commits: int
    processed_context_count: int
    processed_ground_count: int

    @property
    def incomplete(self) -> bool:
        return bool(
            self.unresolved_entity_keys
            or self.unverified_issue_comments
            or self.unverified_pr_comments
            or self.unverified_pr_commits
            or self.unassigned_default_commits
            or any(item.unresolved_source_ids or item.lifecycle_conflict for item in self.items)
        )

    def payload(self) -> dict[str, object]:
        return {
            "record_version": VERSION,
            "stage": STAGE,
            "date": self.date,
            "evidence_bundle_sha256": self.evidence_bundle_sha256,
            "items": [item.payload() for item in self.items],
            "unresolved_entities": [_key(key) for key in self.unresolved_entity_keys],
            "unverified_issue_comments": self.unverified_issue_comments,
            "unverified_pr_comments": self.unverified_pr_comments,
            "unverified_pr_commits": self.unverified_pr_commits,
            "unassigned_default_commits": self.unassigned_default_commits,
            "processed_partial_contexts": self.processed_context_count,
            "processed_singleton_grounding_contexts": self.processed_ground_count,
            "incomplete_source_or_materiality": self.incomplete,
            "requires_human_quality_review": True,
            "publication_authorized": False,
        }

    def to_json_bytes(self) -> bytes:
        data = _canonical(self.payload())
        if len(data) > MAX_TOTAL_PREVIEW_BYTES:
            raise DailyEntityRollupError("entity rollup report byte cap exceeded")
        return data

    @property
    def sha256(self) -> str:
        return _sha256(self.to_json_bytes())


def build_entity_rollup_preview(
    original_evidence_bytes: bytes, *,
    partial_responses_by_context_sha256: Mapping[str, bytes],
    grounding_responses_by_context_sha256: Mapping[str, bytes],
) -> DailyEntityRollupPreview:
    """Deterministically derive an unpublishable Issue/PR-only daily preview.

    No inference, GitHub API, filesystem writes, CAS writer or scheduler.
    Input response maps must be COMPLETE for the exact regenerated contexts,
    including empty candidate replies; missing inputs are never 'no progress'.
    """
    grouping = build_entity_contexts(original_evidence_bytes)
    if len(grouping.contexts) > MAX_MODEL_CONTEXTS:
        raise DailyEntityRollupError("too many source contexts")
    if len(original_evidence_bytes) * max(1, len(grouping.contexts) * 3) > MAX_EVIDENCE_REPLAY_BYTES:
        raise DailyEntityRollupError("entity Evidence replay budget exceeded")
    contexts = tuple((ctx, ctx.sha256) for ctx in grouping.contexts)
    if len({sha for _, sha in contexts}) != len(contexts):
        raise DailyEntityRollupError("duplicated source context SHA")
    _validated_report_inputs(
        partial_responses_by_context_sha256, {sha for _, sha in contexts},
        label="partial",
    )
    prepared: list[tuple[object, bytes, object, tuple[object, ...]]] = []
    expected_ground_shas: set[str] = set()
    for context, sha in contexts:
        model_bytes = partial_responses_by_context_sha256[sha]
        candidate_batch = normalize_entity_candidates(
            context, model_bytes, original_evidence_bytes=original_evidence_bytes,
        )
        ground_inputs = build_entity_grounding_contexts(
            context, model_bytes, original_evidence_bytes=original_evidence_bytes,
        )
        for ground in ground_inputs:
            if ground.sha256 in expected_ground_shas:
                raise DailyEntityRollupError("duplicated singleton Grounding SHA")
            expected_ground_shas.add(ground.sha256)
        if len(expected_ground_shas) > MAX_GROUND_CONTEXTS:
            raise DailyEntityRollupError("too many singleton Grounding inputs")
        if len(original_evidence_bytes) * (
            3 * len(contexts) + len(expected_ground_shas)
        ) > MAX_EVIDENCE_REPLAY_BYTES:
            raise DailyEntityRollupError("singleton Grounding replay budget exceeded")
        prepared.append((context, model_bytes, candidate_batch, ground_inputs))
    _validated_report_inputs(
        grounding_responses_by_context_sha256, expected_ground_shas,
        label="grounding",
    )

    grouped_events: dict[EntityKey, dict[str, Mapping[str, object]]] = {}
    grouped_supported: dict[EntityKey, list[tuple[str, str, tuple[str, ...]]]] = {}
    uncited: dict[EntityKey, set[str]] = {}
    unsupported: dict[EntityKey, int] = {}
    for context, model_bytes, batch, ground_inputs in prepared:
        key = context.key
        actual = grouped_events.setdefault(key, {})
        for event in context.events:
            eid = str(event["evidence_id"])
            if eid in actual:
                raise DailyEntityRollupError("entity original source duplicated")
            actual[eid] = event
        for eid in batch.uncited_evidence_ids:
            event = actual[eid]
            if _material_human_source(event):
                uncited.setdefault(key, set()).add(eid)
        for ground in ground_inputs:
            verdict = normalize_entity_grounding_verdict(
                ground, grounding_responses_by_context_sha256[ground.sha256],
                partial_context=context, partial_model_bytes=model_bytes,
                original_evidence_bytes=original_evidence_bytes,
            )
            if verdict.verdict == "supported":
                grouped_supported.setdefault(key, []).append((
                    ground.summary, ground.candidate_sha256, ground.evidence_ids,
                ))
            else:
                unsupported[key] = unsupported.get(key, 0) + 1
                uncited.setdefault(key, set()).update(ground.evidence_ids)

    items: list[EntityRollupItem] = []
    unresolved_keys: set[EntityKey] = set()
    for key in sorted(grouped_events):
        events = sorted(grouped_events[key].values(), key=_source_sort)
        lifecycle = [row for row in events if _lifecycle_signal(row) is not None]
        merged = key.entity_type == "pull_request" and any(
            row["kind"] == "pull_request_merged" for row in lifecycle
        )
        last_lifecycle = lifecycle[-1] if lifecycle else None
        last_status = _lifecycle_signal(last_lifecycle) if last_lifecycle else None
        merge_rows = [row for row in lifecycle if row["kind"] == "pull_request_merged"]
        after_merge_conflict = bool(
            merge_rows and last_lifecycle and
            last_lifecycle["kind"] != "pull_request_merged"
            and _source_sort(last_lifecycle) > _source_sort(merge_rows[-1])
        )
        supported = grouped_supported.get(key, [])
        if len(supported) > MAX_ENTITY_CANDIDATES:
            raise DailyEntityRollupError("too many supported entity candidates")
        seen_summaries: set[str] = set()
        texts: list[str] = []
        hashes: list[str] = []
        cited: set[str] = set()
        for summary, candidate_sha, refs in supported:
            _reject_unverified_status(summary, key=key, merged=merged and not after_merge_conflict)
            cited.update(refs)
            if summary not in seen_summaries:
                seen_summaries.add(summary)
                texts.append(summary)
                hashes.append(candidate_sha)
        unresolved_ids = set(uncited.get(key, set()))
        # If a model produced no supported candidate for a context with
        # possibly material human activity, never report that as no progress.
        if not supported:
            unresolved_ids.update(
                str(event["evidence_id"])
                for event in events
                if _material_human_source(event)
            )
        if unresolved_ids or after_merge_conflict:
            unresolved_keys.add(key)
        if not lifecycle and not supported:
            # A key with only unsupported/empty/routine candidate evidence is
            # not a certified no-change verdict; retain it as unresolved if
            # it might be meaningful.
            if unresolved_ids:
                unresolved_keys.add(key)
            continue
        lifecycle_ids = tuple(str(row["evidence_id"]) for row in lifecycle)
        if len(" ".join(texts).encode("utf-8")) > MAX_SUMMARY_BYTES_PER_ENTITY:
            raise DailyEntityRollupError("one entity summary byte cap exceeded")
        items.append(EntityRollupItem(
            key=key,
            status_event=last_status,
            verified_merge_event=merged,
            lifecycle_conflict=after_merge_conflict,
            model_supported_candidates=tuple(texts),
            supported_candidate_sha256s=tuple(hashes),
            supported_evidence_ids=tuple(sorted(cited)),
            lifecycle_evidence_ids=lifecycle_ids,
            unresolved_source_ids=tuple(sorted(unresolved_ids)),
            rejected_candidate_count=unsupported.get(key, 0),
        ))

    report = DailyEntityRollupPreview(
        date=grouping.date, evidence_bundle_sha256=grouping.evidence_bundle_sha256,
        items=tuple(items), unresolved_entity_keys=tuple(sorted(unresolved_keys)),
        unverified_issue_comments=grouping.unverified_issue_comments,
        unverified_pr_comments=grouping.unverified_pr_comments,
        unverified_pr_commits=grouping.unverified_pr_commits,
        unassigned_default_commits=grouping.excluded_default_commits,
        processed_context_count=len(contexts),
        processed_ground_count=len(expected_ground_shas),
    )
    report.to_json_bytes()
    return report


def render_entity_rollup_preview(report: DailyEntityRollupPreview) -> str:
    """Render a HUMAN-REVIEW CANDIDATE, never a Daily Writer projection."""
    if not report.items:
        return (
            "Issue/PRの進展判定は未完了です（要レビュー）。\n"
            if report.incomplete
            else "進展のあったIssue・PRは確認できませんでした（要レビュー）。\n"
        )
    lines = [
        "※ 以下は未承認の進展候補です。Dailyへの反映は禁止されています。",
        "",
    ]
    repo: str | None = None
    for item in report.items:
        if item.key.repository != repo:
            if repo is not None:
                lines.append("")
            repo = item.key.repository
            lines.append(f"## {_escape_markdown_text(repo)}")
        label = "PR" if item.key.entity_type == "pull_request" else "Issue"
        phrase = ""
        if item.status_event is not None and not item.lifecycle_conflict:
            phrase = _STRUCTURAL_PROGRESS.get(item.status_event, "")
            if item.status_event.startswith("pull_request_review:"):
                phrase = _REVIEW_TRANSITIONS.get(
                    item.status_event.removeprefix("pull_request_review:"), ""
                )
        if item.lifecycle_conflict:
            phrase = "矛盾するライフサイクル情報のため状態要確認。"
        summaries = tuple(
            "要確認の候補：" + _escape_markdown_text(text)
            for text in item.model_supported_candidates
        )
        detail = " ".join((phrase, *summaries)).strip()
        if not detail:
            detail = "進展候補あり（根拠と意味的重要性は要確認）。"
        if item.unresolved_source_ids:
            detail += " 未解決の参照イベントあり。"
        lines.append(
            f"- [{label} #{item.key.number}]({item.key.github_url()}) — {detail}"
        )
    if report.incomplete:
        lines.extend(["", "※ 未検証のGitHubイベント・候補が残っています。完了判定ではありません。"])
    body = "\n".join(lines).rstrip() + "\n"
    if len(body.encode("utf-8")) > MAX_TOTAL_PREVIEW_BYTES:
        raise DailyEntityRollupError("rendered candidate preview too large")
    return body
