"""Collector-owned, fresh-route Issue/PR provenance for Daily v2 (#328).

Nonwired preview primitive: never writes the canonical v1 Evidence, vault,
Nextcloud, or GitHub. A hash binds data; it is NOT a cryptographic signature
or authorization to promote a model output. Only a trusted collector process
performing a fresh API re-verification may issue this witness.
"""
from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
from dataclasses import dataclass, replace
from datetime import date
from typing import Mapping

from . import github_daily_activity as activity
from . import github_project_watcher as watcher
from .github_daily_summary import parse_evidence_bundle


class DailySourceRouteError(ValueError):
    """Identity or route could not be independently verified; fail closed."""


RECORD_VERSION = 2
MAX_OBSERVED_ROUTES = 2048
MAX_PAGES_PER_PR = 32
REQUIRES_WITNESS = frozenset({
    "issue_comment", "pull_request_comment",
    "pull_request_commit", "pull_request_review_comment",
})
_SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")
_ID_RE = re.compile(r"^[1-9][0-9]{0,18}$")


@dataclass(frozen=True)
class FreshRouteCollection:
    evidence: activity.DailyEvidenceBundle
    receipt_bytes: bytes
    receipt_sha256: str
    route_count: int


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _json(value: Mapping[str, object]) -> bytes:
    return activity._canonical_json_bytes(value)


def _api_prefix(api: watcher.GitHubClient) -> str:
    base = getattr(api, "api_base", "https://api.github.com")
    parts = urllib.parse.urlsplit(base)
    if (
        parts.scheme != "https" or not parts.netloc or parts.username
        or parts.password or parts.query or parts.fragment
    ):
        raise DailySourceRouteError("invalid trusted GitHub API base")
    return base.rstrip("/")


def _require_api_url(raw: object, *, api: watcher.GitHubClient, route: str) -> None:
    if not isinstance(raw, str):
        raise DailySourceRouteError("missing collector API resource link")
    actual = urllib.parse.urlsplit(raw)
    expected = urllib.parse.urlsplit(_api_prefix(api) + route)
    if (
        actual.scheme != expected.scheme or actual.netloc != expected.netloc
        or actual.username or actual.password or actual.query or actual.fragment
        or urllib.parse.unquote(actual.path) != urllib.parse.unquote(expected.path)
    ):
        raise DailySourceRouteError("GitHub API resource identity mismatch")


def _number(raw: object, *, label: str) -> int:
    if type(raw) is not int or raw < 1:
        raise DailySourceRouteError(f"invalid {label} identity")
    return raw


def _source_id(event: activity.EvidenceEvent) -> int:
    if not _ID_RE.fullmatch(event.source_id):
        raise DailySourceRouteError("source event has no authenticated numeric ID")
    return int(event.source_id)


def _comment_equal(row: Mapping[str, object], event: activity.EvidenceEvent, *, limit: int) -> None:
    if (
        row.get("html_url") != event.url
        or activity._format_timestamp(
            activity._timestamp(row.get("created_at"), label="collector comment timestamp")
        ) != event.occurred_at
        or activity._actor(row) != event.actor
    ):
        raise DailySourceRouteError("collector comment metadata changed during recheck")
    fresh = activity._bounded_text(row.get("body"), limit=limit, label="verified comment body")
    if fresh != event.body:
        raise DailySourceRouteError("collector comment body changed during recheck")


def _public_comment_url(
    event: activity.EvidenceEvent,
    *,
    comment_id: int,
    is_review: bool = False,
) -> None:
    """Require canonical GitHub URL attached to the exact comment/entity."""
    repository = event.repository
    number = event.number
    if number is None:
        raise DailySourceRouteError("comment has no originating target")
    candidates = (
        (f"https://github.com/{repository}/pull/{number}#discussion_r{comment_id}",)
        if is_review else (
            f"https://github.com/{repository}/issues/{number}#issuecomment-{comment_id}",
            f"https://github.com/{repository}/pull/{number}#issuecomment-{comment_id}",
        )
    )
    if event.url not in candidates:
        raise DailySourceRouteError("public comment URL does not bind the original entity")


def _correct_comment(event: activity.EvidenceEvent, *, is_pr: bool) -> activity.EvidenceEvent:
    kind = "pull_request_comment" if is_pr else "issue_comment"
    if event.kind == kind:
        return event
    raw = event.to_json()
    raw.pop("evidence_id")
    raw["kind"] = kind
    raw["entity_type"] = kind
    # The new v1-compatible bundle is generated only for this *fresh* run.
    # No historical v1 CAS file may be modified/relabelled in place.
    return replace(
        event, kind=kind, entity_type=kind,
        evidence_id=_sha(_json(raw)),
    )


def _route_record(
    event: activity.EvidenceEvent,
    *,
    route: str,
    entity: str,
    remote_id: str,
    verification_route: str,
) -> dict[str, object]:
    return {
        "evidence_id": event.evidence_id,
        "kind": event.kind,
        "repository": event.repository,
        "entity": entity,
        "number": event.number,
        "source_id": event.source_id,
        "route": route,
        "verification_route": verification_route,
        "remote_id": remote_id,
    }


def _pr_commits(
    api: watcher.GitHubClient,
    path: str,
) -> list[dict[str, object]]:
    """Bounded source API pagination; never trust a caller-supplied URL."""
    rows: list[dict[str, object]] = []
    for page in range(1, MAX_PAGES_PER_PR + 1):
        response = api._request_json(f"{path}?per_page=100&page={page}")
        if not isinstance(response, list) or any(not isinstance(item, dict) for item in response):
            raise DailySourceRouteError("PR commits source response is malformed")
        rows.extend(response)
        if len(response) < 100:
            return rows
    raise DailySourceRouteError("PR commit source exceeded bounded pagination")


def _validate_record(raw: object, event: activity.EvidenceEvent) -> None:
    if not isinstance(raw, dict) or set(raw) != {
        "evidence_id", "kind", "repository", "entity", "number",
        "source_id", "route", "verification_route", "remote_id",
    }:
        raise DailySourceRouteError("source route witness has invalid schema")
    if raw["evidence_id"] != event.evidence_id or raw["kind"] != event.kind:
        raise DailySourceRouteError("route witness not bound to original evidence")
    if raw["repository"] != event.repository or raw["source_id"] != event.source_id:
        raise DailySourceRouteError("route witness source identity mismatch")
    if raw["number"] != event.number or type(raw["number"]) is not int or event.number is None or event.number < 1:
        raise DailySourceRouteError("route witness entity number mismatch")
    required_entity_types = {
        "issue_comment": "issue_comment",
        "pull_request_comment": "pull_request_comment",
        "pull_request_review_comment": "pull_request_review_comment",
        "pull_request_commit": "commit",
    }
    if event.entity_type != required_entity_types.get(event.kind):
        raise DailySourceRouteError("original event kind/entity_type contradiction")
    if not isinstance(raw["remote_id"], str) or not raw["remote_id"]:
        raise DailySourceRouteError("missing verified source object identifier")
    owner, name = event.repository.split("/", 1)
    repo_route = f"/repos/{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(name, safe='')}"
    number = event.number
    if event.kind in {"issue_comment", "pull_request_comment"}:
        expected = (
            f"{repo_route}/issues/comments/{event.source_id}",
            f"{repo_route}/issues/{number}",
            "pull_request" if event.kind == "pull_request_comment" else "issue",
        )
        if not _ID_RE.fullmatch(event.source_id) or not _ID_RE.fullmatch(raw["remote_id"]):
            raise DailySourceRouteError("invalid issue comment/issue original source IDs")
    elif event.kind == "pull_request_review_comment":
        expected = (
            f"{repo_route}/pulls/comments/{event.source_id}",
            f"{repo_route}/pulls/{number}",
            "pull_request",
        )
        if not _ID_RE.fullmatch(event.source_id) or raw["remote_id"] != event.source_id:
            raise DailySourceRouteError("review comment ID mismatch")
    elif event.kind == "pull_request_commit":
        expected = (
            f"{repo_route}/pulls/{number}/commits",
            f"{repo_route}/pulls/{number}",
            "pull_request",
        )
        if not _SHA_RE.fullmatch(event.source_id) or raw["remote_id"] != event.source_id:
            raise DailySourceRouteError("PR commit identity mismatch")
    else:
        raise DailySourceRouteError("unexpected route witness kind")
    if (raw["route"], raw["verification_route"], raw["entity"]) != expected:
        raise DailySourceRouteError("source route not the trusted entity endpoint")


def parse_verified_routes(
    receipt_bytes: bytes,
    evidence_bytes: bytes,
) -> tuple[dict[str, object], ...]:
    """Reattest exact receipt fields against original immutable Evidence.

    Only a trusted collector-owned location/provenance makes this evidence.
    Neither JSON nor SHA alone constitutes a signature or external authority.
    """
    if len(receipt_bytes) > 4 * 1024 * 1024 or len(evidence_bytes) > 32 * 1024 * 1024:
        raise DailySourceRouteError("route receipt or evidence exceeds verification bound")
    bundle = parse_evidence_bundle(evidence_bytes)
    try:
        raw = json.loads(receipt_bytes)
    except (ValueError, TypeError, UnicodeError) as exc:
        raise DailySourceRouteError("invalid source receipt JSON") from exc
    if (
        not isinstance(raw, dict)
        or set(raw) != {"record_version", "date", "evidence_sha256", "routes", "collection_mode", "publication_authorized"}
        or type(raw["record_version"]) is not int or raw["record_version"] != RECORD_VERSION
        or raw["collection_mode"] != "fresh_api_reverified"
        or raw["publication_authorized"] is not False
        or raw["date"] != bundle.date
        or raw["evidence_sha256"] != bundle.sha256
        or not isinstance(raw["routes"], list)
        or len(raw["routes"]) > MAX_OBSERVED_ROUTES
    ):
        raise DailySourceRouteError("route receipt version/evidence binding mismatch")
    if _json(raw) != receipt_bytes:
        raise DailySourceRouteError("noncanonical source receipt")
    expected_events = {
        str(e["evidence_id"]): e
        for e in bundle.events if e["kind"] in REQUIRES_WITNESS
    }
    if len(expected_events) != sum(e["kind"] in REQUIRES_WITNESS for e in bundle.events):
        raise DailySourceRouteError("duplicate required source event")
    if len(raw["routes"]) != len(expected_events):
        raise DailySourceRouteError("missing or unexpected source route attestations")
    seen: set[str] = set()
    # Use a narrow metadata object; evidence parser has already verified each
    # source event ID against its exact original source bytes.
    for record in raw["routes"]:
        if not isinstance(record, dict):
            raise DailySourceRouteError("invalid route entry")
        event_id = record.get("evidence_id")
        if not isinstance(event_id, str) or event_id not in expected_events or event_id in seen:
            raise DailySourceRouteError("unknown or repeated source event witness")
        event_row = expected_events[event_id]
        event = activity.EvidenceEvent(
            evidence_id=event_id, kind=event_row["kind"], repository=event_row["repository"],
            occurred_at=event_row["occurred_at"], url=event_row["url"],
            actor=event_row["actor"], entity_type=event_row["entity_type"],
            number=event_row["number"], source_id=event_row["source_id"], sha=event_row["sha"],
            state=event_row["state"], draft=event_row["draft"],
            title=None, body=None, message=None,
        )
        _validate_record(record, event)
        seen.add(event_id)
    if seen != set(expected_events):
        raise DailySourceRouteError("unwitnessed original issue/PR events")
    return tuple(raw["routes"])


def collect_fresh_verified_routes(
    config: watcher.WatcherConfig,
    *,
    target_date: date,
    client: watcher.GitHubClient,
) -> FreshRouteCollection:
    """Fresh GitHub API collection + re-verification in one invocation.

    Never use this for silent historical ref repair or old v1 ref promotion.
    A failed read/rate-limit/changed comment/changed PR route fails closed; no
    original Evidence or local state is persisted by this function.
    """
    original = activity.collect_daily_evidence(config, target_date=target_date, client=client)
    records: list[dict[str, object]] = []
    corrected: list[activity.EvidenceEvent] = []
    seen_comment_targets: dict[tuple[str, int], tuple[str, int]] = {}
    pr_cache: dict[tuple[str, int], dict[str, dict[str, object]]] = {}
    for event in original.events:
        if event.kind not in REQUIRES_WITNESS:
            corrected.append(event)
            continue
        if len(records) >= MAX_OBSERVED_ROUTES:
            raise DailySourceRouteError("too many fresh route observations")
        repository = event.repository
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise DailySourceRouteError("malformed collector repository")
        if type(event.number) is not int or event.number < 1:
            raise DailySourceRouteError("source event lacks an entity number")
        owner, name = repository.split("/", 1)
        base = f"/repos/{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(name, safe='')}"
        number = event.number
        if event.kind in {"issue_comment", "pull_request_comment"}:
            comment_id = _source_id(event)
            route = f"{base}/issues/comments/{comment_id}"
            comment = client._request_json(route)
            if not isinstance(comment, dict) or _number(comment.get("id"), label="issue comment ID") != comment_id:
                raise DailySourceRouteError("issue comment resource changed")
            _comment_equal(comment, event, limit=activity.COMMENT_MAX_BYTES)
            _public_comment_url(event, comment_id=comment_id)
            verify = f"{base}/issues/{number}"
            _require_api_url(comment.get("issue_url"), api=client, route=verify)
            key = (repository, number)
            if key not in seen_comment_targets:
                if len(seen_comment_targets) >= MAX_OBSERVED_ROUTES:
                    raise DailySourceRouteError("too many GitHub issue classification sources")
                issue = client._request_json(verify)
                if not isinstance(issue, dict) or _number(issue.get("number"), label="issue number") != number:
                    raise DailySourceRouteError("issue classification source changed")
                issue_id = _number(issue.get("id"), label="GitHub issue record ID")
                link = issue.get("pull_request")
                if link is not None:
                    if not isinstance(link, dict):
                        raise DailySourceRouteError("invalid PR classification response")
                    _require_api_url(
                        link.get("url"), api=client,
                        route=f"{base}/pulls/{number}",
                    )
                seen_comment_targets[key] = ("pull_request" if link is not None else "issue", issue_id)
            entity, issue_id = seen_comment_targets[key]
            replacement = _correct_comment(event, is_pr=(entity == "pull_request"))
            corrected.append(replacement)
            records.append(_route_record(
                replacement, route=route, entity=entity,
                verification_route=verify, remote_id=str(issue_id),
            ))
        elif event.kind == "pull_request_review_comment":
            comment_id = _source_id(event)
            route = f"{base}/pulls/comments/{comment_id}"
            comment = client._request_json(route)
            if not isinstance(comment, dict) or _number(comment.get("id"), label="review comment ID") != comment_id:
                raise DailySourceRouteError("review comment source changed")
            _comment_equal(comment, event, limit=activity.COMMENT_MAX_BYTES)
            _public_comment_url(event, comment_id=comment_id, is_review=True)
            verify = f"{base}/pulls/{number}"
            _require_api_url(comment.get("pull_request_url"), api=client, route=verify)
            corrected.append(event)
            records.append(_route_record(
                event, route=route, entity="pull_request",
                verification_route=verify, remote_id=event.source_id,
            ))
        else:
            assert event.kind == "pull_request_commit"
            if not _SHA_RE.fullmatch(event.source_id) or event.sha != event.source_id:
                raise DailySourceRouteError("invalid original PR commit SHA")
            if event.url != f"https://github.com/{repository}/commit/{event.sha}":
                raise DailySourceRouteError("PR commit has noncanonical GitHub URL")
            route = f"{base}/pulls/{number}/commits"
            key = (repository, number)
            if key not in pr_cache:
                commits = _pr_commits(client, route)
                hashes: dict[str, dict[str, object]] = {}
                for row in commits:
                    sha = row.get("sha")
                    if not isinstance(sha, str) or not _SHA_RE.fullmatch(sha):
                        raise DailySourceRouteError("invalid commit SHA in PR source route")
                    if row.get("html_url") != f"https://github.com/{repository}/commit/{sha}":
                        raise DailySourceRouteError("PR source commit URL mismatch")
                    if sha in hashes:
                        raise DailySourceRouteError("duplicate commit in source API route")
                    hashes[sha] = row
                pr_cache[key] = hashes
            if event.source_id not in pr_cache[key]:
                raise DailySourceRouteError("PR commit has no fresh membership proof")
            # The SHA implies immutable Git content, but also check that the
            # listed resource corresponds to the original normalized comment,
            # actor, timestamp and bounded GitHub message in this source run.
            source = pr_cache[key][event.source_id]
            if not isinstance(source.get("commit"), dict):
                raise DailySourceRouteError("PR commit content resource changed")
            when = watcher.GitHubClient._timestamp_from_commit_payload(source)
            if (
                when is None or activity._format_timestamp(when) != event.occurred_at
                or activity._actor(source) != event.actor
                or activity._bounded_text(
                    source["commit"].get("message"),
                    limit=activity.COMMIT_MESSAGE_MAX_BYTES,
                    label="verified PR commit message",
                ) != event.message
            ):
                raise DailySourceRouteError("PR commit content disagrees with original evidence")
            corrected.append(event)
            records.append(_route_record(
                event, route=route, entity="pull_request",
                verification_route=f"{base}/pulls/{number}", remote_id=event.source_id,
            ))
    updated = activity.make_daily_evidence_bundle(
        target_date=target_date, projects=original.projects, events=corrected,
    )
    records.sort(key=lambda item: item["evidence_id"])
    payload = {
        "record_version": RECORD_VERSION,
        "date": updated.target_date,
        "evidence_sha256": updated.sha256,
        "collection_mode": "fresh_api_reverified",
        "publication_authorized": False,
        "routes": records,
    }
    receipt = _json(payload)
    # Validate our own freshly produced read-only artifact before returning.
    validated = parse_verified_routes(receipt, updated.canonical_bytes)
    assert len(validated) == len(records)
    return FreshRouteCollection(
        evidence=updated, receipt_bytes=receipt,
        receipt_sha256=_sha(receipt), route_count=len(records),
    )
