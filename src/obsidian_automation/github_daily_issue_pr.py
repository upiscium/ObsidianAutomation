"""Issue/PR-scoped *preview* of verified GitHub Daily evidence (#324).

This versioned read model never writes to a Vault, Daily CAS, production
schedule, or immutable pipeline root. Existing v1 projections remain intact.
Publication needs a separate semantic-materiality gate (#283/#290).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Mapping
from urllib.parse import unquote, urlsplit

from .artifact_lifecycle import (
    ArtifactLifecycleError, _canonical_json_bytes, sha256_bytes,
)
from .github_daily_activity import _date_window
from .github_daily_progress import (
    DailyProgressError,
    GroundedSummaryView,
    _claim_id,
    _escape_markdown_text,
    parse_grounded_summary,
)
from .github_daily_summary import (
    CLAIM_KINDS,
    EvidenceBundle,
    parse_evidence_bundle,
)

PREVIEW_VERSION = 2
PREVIEW_STAGE = "github_daily_issue_pr_entity_preview"
MAX_EVIDENCE_BYTES = 32 * 1024 * 1024
MAX_SUMMARY_BYTES = 4 * 1024 * 1024
MAX_EVENTS = 50000
MAX_ENTITIES = 10000
MAX_CLAIMS = 4096
MAX_CLAIMS_PER_ENTITY = 40
MAX_SUMMARY_ITEM_BYTES = 32 * 1024
MAX_RENDER_BYTES = 2 * 1024 * 1024
MAX_PREVIEW_JSON_BYTES = 4 * 1024 * 1024
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ISSUE_EVENTS = frozenset({
    "issue_created", "issue_snapshot", "issue_closed", "issue_reopened",
    "issue_comment",
})
_PR_EVENTS = frozenset({
    "pull_request_created", "pull_request_snapshot", "pull_request_closed",
    "pull_request_reopened", "pull_request_merged",
    "pull_request_ready_for_review", "pull_request_converted_to_draft",
    "pull_request_comment", "pull_request_review",
    "pull_request_review_comment", "pull_request_commit",
})
_STRUCTURAL_PROGRESS = {
    "issue_created": "Issueが作成されました。",
    "issue_closed": "Issueがクローズされました。",
    "issue_reopened": "Issueが再オープンされました。",
    "pull_request_created": "PRが作成されました。",
    "pull_request_closed": "PRがクローズされました（マージ未確認）。",
    "pull_request_reopened": "PRが再オープンされました。",
    "pull_request_merged": "PRがマージされました。",
    "pull_request_ready_for_review": "PRがレビュー可能になりました。",
    "pull_request_converted_to_draft": "PRがDraftに戻されました。",
    "pull_request_commit": "PRにコミットが追加されました。",
}
_GROUNDED_PROGRESS = frozenset({
    "issue_comment", "pull_request_comment", "pull_request_review",
    "pull_request_review_comment",
})


class DailyEntityPreviewError(DailyProgressError):
    """Reject ambiguous or unbound source identity without publication."""


@dataclass(frozen=True, order=True)
class EntityKey:
    repository: str
    entity_type: str
    number: int

    def github_url(self) -> str:
        segment = "pull" if self.entity_type == "pull_request" else "issues"
        return f"https://github.com/{self.repository}/{segment}/{self.number}"


@dataclass(frozen=True)
class EntityProgressItem:
    key: EntityKey
    title: str
    status: str
    summary: str
    evidence_ids: tuple[str, ...]
    claim_ids: tuple[str, ...]

    def payload(self) -> dict[str, object]:
        return {
            "repository": self.key.repository,
            "entity_type": self.key.entity_type,
            "number": self.key.number,
            "url": self.key.github_url(),
            "title": self.title,
            "status": self.status,
            "summary": self.summary,
            "evidence_ids": list(self.evidence_ids),
            "grounded_claim_ids": list(self.claim_ids),
        }


@dataclass(frozen=True)
class DailyEntityPreview:
    target_date: str
    evidence_bundle_sha256: str
    grounded_summary_sha256: str
    items: tuple[EntityProgressItem, ...]
    ambiguous_claim_ids: tuple[str, ...]
    excluded_no_progress_count: int
    unattributed_events_count: int
    unresolved_candidate_keys: tuple[EntityKey, ...]

    @property
    def requires_review(self) -> bool:
        # Even with zero ambiguous claims, deciding whether routine comments
        # omit material progress is not a deterministic judgment (#290).
        return True

    def to_json_bytes(self) -> bytes:
        data = _canonical_json_bytes({
            "record_version": PREVIEW_VERSION,
            "stage": PREVIEW_STAGE,
            "date": self.target_date,
            "evidence_bundle_sha256": self.evidence_bundle_sha256,
            "grounded_summary_sha256": self.grounded_summary_sha256,
            "items": [item.payload() for item in self.items],
            "ambiguous_claim_ids": list(self.ambiguous_claim_ids),
            "excluded_no_progress_count": self.excluded_no_progress_count,
            "unattributed_events_count": self.unattributed_events_count,
            "unresolved_candidate_keys": [
                {
                    "repository": key.repository,
                    "entity_type": key.entity_type,
                    "number": key.number,
                } for key in self.unresolved_candidate_keys
            ],
            "requires_human_quality_review": self.requires_review,
            "publication_authorized": False,
        })
        if len(data) > MAX_PREVIEW_JSON_BYTES:
            raise DailyEntityPreviewError("bounded preview JSON exceeds size limit")
        return data


def _require_event_time(value: object, start: datetime, end: datetime) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise DailyEntityPreviewError("event timestamp must be canonical UTC")
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DailyEntityPreviewError("event timestamp is malformed") from exc
    if timestamp.tzinfo != timezone.utc or not start <= timestamp < end:
        raise DailyEntityPreviewError("event is outside target JST date")
    return timestamp


def _validate_evidence_url(event: Mapping[str, object], key: EntityKey) -> None:
    raw = event.get("url")
    if not isinstance(raw, str):
        raise DailyEntityPreviewError("event has no GitHub URL")
    parsed = urlsplit(raw)
    try:
        safe_port = parsed.port in (None, 443)
    except ValueError as exc:
        raise DailyEntityPreviewError("event URL port is malformed") from exc
    if (
        parsed.scheme != "https" or parsed.hostname != "github.com"
        or parsed.username is not None or parsed.password is not None
        or parsed.query or not safe_port
    ):
        raise DailyEntityPreviewError("event URL is not canonical GitHub HTTPS")
    raw_parts = [part for part in parsed.path.split("/") if part]
    parts = [unquote(part) for part in raw_parts]
    if len(parts) != 4 or any(part in {".", "..", ""} or "/" in part for part in parts):
        raise DailyEntityPreviewError("entity evidence URL path is not canonical")
    owner, repo = key.repository.split("/")
    if (parts[0].casefold(), parts[1].casefold()) != (
        owner.casefold(), repo.casefold()
    ):
        raise DailyEntityPreviewError("entity evidence URL repository mismatch")
    kind = event["kind"]
    if kind == "pull_request_commit":
        # Pull-commits API returns commit URLs rather than /pull/N URLs.
        if parts[2] == "commit" and len(parts) == 4:
            return
    permitted = (
        {"issues"} if key.entity_type == "issue"
        else {"pull", "issues"} if kind == "pull_request_comment" else {"pull"}
    )
    if parts[2] not in permitted or parts[3] != str(key.number):
        raise DailyEntityPreviewError("entity evidence URL target mismatch")


def _event_key(event: Mapping[str, object]) -> EntityKey | None:
    kind = event.get("kind")
    entity = event.get("entity_type")
    number = event.get("number")
    repository = event.get("repository")
    if not isinstance(repository, str) or _REPOSITORY.fullmatch(repository) is None:
        raise DailyEntityPreviewError("event repository identity is invalid")
    if kind == "default_branch_commit":
        if entity != "commit" or number is not None:
            raise DailyEntityPreviewError("unattributed commit fields are invalid")
        return None
    if kind in _ISSUE_EVENTS:
        entity_type = "issue"
        expected = "issue_comment" if kind == "issue_comment" else "issue"
    elif kind in _PR_EVENTS:
        entity_type = "pull_request"
        expected = "commit" if kind == "pull_request_commit" else (
            kind if kind in {
                "pull_request_comment", "pull_request_review",
                "pull_request_review_comment",
            } else "pull_request"
        )
    else:
        raise DailyEntityPreviewError("unexpected event kind or entity type")
    if entity != expected or type(number) is not int or number < 1:
        raise DailyEntityPreviewError("event kind/entity/number mismatch")
    key = EntityKey(repository, entity_type, number)
    _validate_evidence_url(event, key)
    return key


def _is_bot(event: Mapping[str, object]) -> bool:
    actor = event.get("actor")
    return isinstance(actor, str) and (
        actor.casefold().endswith("[bot]")
        or actor.casefold() in {"github-actions", "dependabot", "renovate"}
    )


_ROUTINE_COMMENTS = frozenset({
    "next", "continue", "ack", "acknowledged", "ok", "lgtm",
    "thanks", "thank you", "了解", "確認しました", "承知しました",
    "ありがとう", "👍", "+1",
})


def _routine_comment(event: Mapping[str, object]) -> bool:
    raw = event.get("body")
    if not isinstance(raw, dict) or not isinstance(raw.get("text"), str):
        return True
    normalized = raw["text"].strip().strip("。.!！　 ").casefold()
    return not normalized or normalized in _ROUTINE_COMMENTS


def _structural_signal(event: Mapping[str, object]) -> str:
    kind = event["kind"]
    if kind in _STRUCTURAL_PROGRESS:
        return _STRUCTURAL_PROGRESS[str(kind)]
    if kind == "pull_request_review" and not _is_bot(event):
        state = event.get("state")
        if isinstance(state, str):
            if state.casefold() == "approved":
                return "PRレビューで承認されました。"
            if state.casefold() == "changes_requested":
                return "PRレビューで変更要求を受けました。"
    return ""


_UNVERIFIED_MERGE = re.compile(
    r"(?:マージ(?:済み|されました|完了)|(?<![A-Za-z])(?:was )?merged(?![A-Za-z]))",
    re.IGNORECASE,
)
_UNVERIFIED_DEPLOY = re.compile(
    r"(?:デプロイ(?:済み|完了)|本番反映(?:済み|完了)|(?<![A-Za-z])deployed(?![A-Za-z]))",
    re.IGNORECASE,
)

_CROSS_ENTITY_MENTION = re.compile(r"(?<![A-Za-z0-9_])#([1-9][0-9]*)")
_UNVERIFIED_LINK = re.compile(r"https?://", re.IGNORECASE)

def _title_for(events: list[Mapping[str, object]], key: EntityKey) -> str:
    titles: list[tuple[str, str]] = []
    for event in events:
        if event.get("kind") not in {
            "issue_created", "issue_snapshot",
            "pull_request_created", "pull_request_snapshot",
        }:
            continue
        title = event.get("title")
        if title is None:
            continue
        if not isinstance(title, dict) or not isinstance(title.get("text"), str):
            raise DailyEntityPreviewError("issue/PR title is invalid")
        text = " ".join(title["text"].split())
        if not text or len(text.encode("utf-8")) > 2048:
            raise DailyEntityPreviewError("issue/PR title is missing or oversized")
        titles.append((str(event["occurred_at"]), text))
    if titles:
        return sorted(titles)[-1][1]
    return f"{'PR' if key.entity_type == 'pull_request' else 'Issue'} #{key.number}"


def _status_for(events: list[Mapping[str, object]]) -> tuple[str, str]:
    kinds = {str(event["kind"]) for event in events}
    if "pull_request_merged" in kinds:
        return "merged", _STRUCTURAL_PROGRESS["pull_request_merged"]
    changed = [event for event in events if _structural_signal(event)]
    if not changed:
        return "progress", ""
    selected = sorted(
        changed,
        key=lambda event: (str(event["occurred_at"]), str(event["evidence_id"])),
    )[-1]
    return str(selected["kind"]), _structural_signal(selected)


def _valid_claim(
    claim: object,
    bundle: EvidenceBundle,
    events_by_id: Mapping[str, Mapping[str, object]],
    keys_by_id: Mapping[str, EntityKey | None],
) -> tuple[EntityKey | None, tuple[str, ...], bool]:
    from .github_daily_summary import SummaryClaim
    if not isinstance(claim, SummaryClaim):
        raise DailyEntityPreviewError("unsupported grounded claim")
    if claim.kind not in CLAIM_KINDS or claim.repository not in bundle.repositories:
        raise DailyEntityPreviewError("grounded claim kind/repository mismatch")
    if (
        not isinstance(claim.summary, str)
        or not claim.summary.strip() or "\n" in claim.summary
        or len(claim.summary.encode("utf-8")) > 8192
        or not 1 <= len(claim.evidence_ids) <= 8
        or len(set(claim.evidence_ids)) != len(claim.evidence_ids)
        or _claim_id(
            kind=claim.kind, repository=claim.repository,
            summary=claim.summary, evidence_ids=claim.evidence_ids,
        ) != claim.claim_id
    ):
        raise DailyEntityPreviewError("grounded claim identity/text mismatch")
    targets: set[EntityKey] = set()
    qualifying = False
    for evidence_id in claim.evidence_ids:
        event = events_by_id.get(evidence_id)
        if event is None or event["repository"] != claim.repository:
            raise DailyEntityPreviewError("claim has unbound/cross-repo evidence")
        key = keys_by_id.get(evidence_id)
        if key is not None:
            targets.add(key)
        if _structural_signal(event):
            qualifying = True
        if (
            event["kind"] in _GROUNDED_PROGRESS
            and not _is_bot(event)
            and not _routine_comment(event)
        ):
            qualifying = True
    if len(targets) > 1 or (len(targets) == 1 and any(
        keys_by_id[item] is None for item in claim.evidence_ids
    )):
        return None, claim.evidence_ids, True
    return (next(iter(targets)) if targets else None), claim.evidence_ids, qualifying


def build_daily_entity_preview(
    *, bundle: EvidenceBundle, summary: GroundedSummaryView,
) -> DailyEntityPreview:
    if bundle.sha256 != summary.evidence_bundle_sha256:
        raise DailyEntityPreviewError("summary does not bind exact Evidence")
    if len(bundle.events) > MAX_EVENTS or len(summary.claims) > MAX_CLAIMS:
        raise DailyEntityPreviewError("Daily entity preview capacity exceeded")
    try:
        target = date.fromisoformat(bundle.date)
    except ValueError as exc:
        raise DailyEntityPreviewError("invalid Daily date") from exc
    if target.isoformat() != bundle.date or bundle.timezone != "Asia/Tokyo":
        raise DailyEntityPreviewError("Daily date/timezone is not canonical")
    start, end = _date_window(target)
    if (
        bundle.window_start != start.isoformat().replace("+00:00", "Z")
        or bundle.window_end != end.isoformat().replace("+00:00", "Z")
    ):
        raise DailyEntityPreviewError("Daily evidence window is not the JST date")

    entity_events: dict[EntityKey, list[Mapping[str, object]]] = defaultdict(list)
    events_by_id = bundle.events_by_id
    if len(events_by_id) != len(bundle.events):
        raise DailyEntityPreviewError("duplicate evidence IDs are not allowed")
    keys_by_id: dict[str, EntityKey | None] = {}
    excluded_unattributed = 0
    for event in bundle.events:
        _require_event_time(event.get("occurred_at"), start, end)
        key = _event_key(event)
        evidence_id = event.get("evidence_id")
        if not isinstance(evidence_id, str) or not _SHA256.fullmatch(evidence_id):
            raise DailyEntityPreviewError("evidence ID is invalid")
        keys_by_id[evidence_id] = key
        if key is None:
            excluded_unattributed += 1
            continue
        entity_events[key].append(event)
    if len(entity_events) > MAX_ENTITIES:
        raise DailyEntityPreviewError("Daily has too many Issue/PR entities")

    entity_claims: dict[EntityKey, list[object]] = defaultdict(list)
    ambiguous: list[str] = []
    for claim in summary.claims:
        key, _, qualifies = _valid_claim(claim, bundle, events_by_id, keys_by_id)
        if key is None:
            # A genuine multi-target claim cannot be assigned by guessing,
            # unlike a default-branch commit with no entity identity.
            if any(
                keys_by_id[eid] is not None
                for eid in claim.evidence_ids
            ):
                ambiguous.append(claim.claim_id)
            continue
        if qualifies:
            entity_claims[key].append(claim)

    items: list[EntityProgressItem] = []
    unresolved: list[EntityKey] = []
    excluded_no_progress = 0
    for key, events in entity_events.items():
        claims = entity_claims.get(key, [])
        lifecycle = [event for event in events if _structural_signal(event)]
        if not claims and not lifecycle:
            # An uncited human substantive message is potentially important:
            # avoid certifying inactivity solely because a model omitted it.
            if any(
                event["kind"] in _GROUNDED_PROGRESS
                and not _is_bot(event)
                and not _routine_comment(event)
                for event in events
            ):
                unresolved.append(key)
            excluded_no_progress += 1
            continue
        if len(claims) > MAX_CLAIMS_PER_ENTITY:
            raise DailyEntityPreviewError("single entity progress exceeds claim cap")
        state, state_phrase = _status_for(events)
        for claim in claims:
            if _UNVERIFIED_DEPLOY.search(claim.summary):
                raise DailyEntityPreviewError(
                    "GitHub evidence cannot prove live deployment"
                )
            if state != "merged" and _UNVERIFIED_MERGE.search(claim.summary):
                raise DailyEntityPreviewError(
                    "model claim asserts merge without verified merge event"
                )
            if _UNVERIFIED_LINK.search(claim.summary):
                raise DailyEntityPreviewError(
                    "model-provided links are not entity-bound evidence"
                )
            if any(
                int(number) != key.number
                for number in _CROSS_ENTITY_MENTION.findall(claim.summary)
            ):
                raise DailyEntityPreviewError(
                    "model claim refers to another unverified Issue/PR"
                )
        # Original supported claim text is retained; no model or arbitrary
        # renderer may invent a merge decision or an Issue/PR identifier.
        parts: list[str] = []
        if state_phrase:
            parts.append(state_phrase)
        seen: set[str] = set()
        for claim in sorted(
            claims, key=lambda c: (min(
                str(bundle.events_by_id[eid]["occurred_at"])
                for eid in c.evidence_ids
            ), c.claim_id),
        ):
            if claim.summary not in seen:
                parts.append(claim.summary)
                seen.add(claim.summary)
        summary_text = " ".join(parts).strip()
        if not summary_text or len(summary_text.encode("utf-8")) > MAX_SUMMARY_ITEM_BYTES:
            raise DailyEntityPreviewError("entity summary is empty or oversized")
        cited = {
            eid for claim in claims for eid in claim.evidence_ids
        } | {
            str(event["evidence_id"]) for event in lifecycle
        }
        items.append(EntityProgressItem(
            key=key, title=_title_for(events, key),
            status=state, summary=summary_text,
            evidence_ids=tuple(sorted(cited)),
            claim_ids=tuple(sorted(claim.claim_id for claim in claims)),
        ))

    items.sort(key=lambda item: (
        item.key.repository.casefold(), item.key.repository,
        item.key.entity_type, item.key.number,
    ))
    return DailyEntityPreview(
        target_date=bundle.date, evidence_bundle_sha256=bundle.sha256,
        grounded_summary_sha256=summary.sha256, items=tuple(items),
        ambiguous_claim_ids=tuple(sorted(ambiguous)),
        excluded_no_progress_count=excluded_no_progress,
        unattributed_events_count=excluded_unattributed,
        unresolved_candidate_keys=tuple(sorted(unresolved)),
    )


def render_daily_entity_preview(preview: DailyEntityPreview) -> str:
    """Never usable as an authorized writer projection by itself."""
    if not preview.items:
        return "進展のあったIssue・PRはありません。\n"
    lines: list[str] = []
    current_repo: str | None = None
    for item in preview.items:
        if item.key.repository != current_repo:
            if lines:
                lines.append("")
            current_repo = item.key.repository
            lines.append(f"## {_escape_markdown_text(current_repo)}")
        prefix = "PR" if item.key.entity_type == "pull_request" else "Issue"
        title = _escape_markdown_text(item.title)
        detail = _escape_markdown_text(item.summary)
        lines.append(
            f"- [{prefix} #{item.key.number} — {title}]({item.key.github_url()})"
            f" — {detail}"
        )
    result = "\n".join(lines).rstrip() + "\n"
    if len(result.encode("utf-8")) > MAX_RENDER_BYTES:
        raise DailyEntityPreviewError("rendered preview exceeds size limit")
    return result


def _read_artifact(path: Path, *, max_bytes: int, suffix: str) -> bytes:
    if not path.is_absolute() or not path.name.endswith(suffix):
        raise DailyEntityPreviewError("local artifact path/suffix is invalid")
    digest = path.name[:-len(suffix)]
    if _SHA256.fullmatch(digest) is None:
        raise DailyEntityPreviewError("artifact filename must bind SHA-256")
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise DailyEntityPreviewError("cannot open bounded artifact") from exc
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_size < 1 or info.st_size > max_bytes
        ):
            raise DailyEntityPreviewError("artifact inode or size is invalid")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            chunks.append(chunk)
        data = b"".join(chunks)
        if len(data) != info.st_size or sha256_bytes(data) != digest:
            raise DailyEntityPreviewError("artifact bytes do not match identity")
        return data
    finally:
        os.close(fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only Issue/PR Daily v2 preview; never publish"
    )
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--summary", required=True, type=Path)
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    args = parser.parse_args(argv)
    try:
        evidence = _read_artifact(
            args.evidence, max_bytes=MAX_EVIDENCE_BYTES,
            suffix=".github-daily-evidence.json",
        )
        bundle = parse_evidence_bundle(evidence)
        grounded = _read_artifact(
            args.summary, max_bytes=MAX_SUMMARY_BYTES,
            suffix=".github-daily-grounded-summary.json",
        )
        summary = parse_grounded_summary(grounded, bundle=bundle)
        preview = build_daily_entity_preview(bundle=bundle, summary=summary)
        if args.format == "json":
            sys.stdout.buffer.write(preview.to_json_bytes())
        else:
            sys.stdout.write(render_daily_entity_preview(preview))
        print(
            f"PREVIEW_ONLY date={preview.target_date} items={len(preview.items)} "
            f"ambiguous={len(preview.ambiguous_claim_ids)} "
            f"unresolved_candidates={len(preview.unresolved_candidate_keys)} "
            f"excluded_no_progress={preview.excluded_no_progress_count} "
            "publication_authorized=false",
            file=sys.stderr,
        )
        return 3 if (
            preview.ambiguous_claim_ids or preview.unresolved_candidate_keys
        ) else 0
    except (ArtifactLifecycleError, DailyProgressError, OSError, ValueError, TypeError) as exc:
        print(f"Daily entity preview blocked: {type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
