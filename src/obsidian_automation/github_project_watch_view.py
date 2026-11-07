"""Pure, read-only B3 Project watch list projection for Issue #534.

Inputs must come from a future trusted read-only vault discovery and watcher
observation adapter. This module does not read a Vault, GitHub or SQLite; it
does not attest source authority, start a Watcher, or authorize any Writer.

Configured github_watch on each Project and effective global scheduling are
DISTINCT. A claimed global Off is not proven effective while a legacy LXC
can independently run old GitHub timer/Watcher paths.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone

_SCHEMA = "b3-project-watch-list/v1"
_PATH = re.compile(r"10-Project/(?:[^/\\\x00-\x1f]+/)*[^/\\\x00-\x1f]+\.md\Z")
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_PROJECT_STATES = frozenset({
    "planning", "running", "stopped", "stable", "done", "cancelled",
})
_UNWATCHED = frozenset({"stopped", "done", "cancelled"})
_MAX_ROWS = 256
_MAX_SOURCE_AGE = 86400


class ProjectWatchProjectionUnavailable(ValueError):
    """Fixed nonsecret error; raw source rows never cross into UI errors."""

    def __init__(self) -> None:
        super().__init__("project_watch_projection_unavailable")


@dataclass(frozen=True, slots=True)
class ProjectWatchConfig:
    """Sanitized source note header, not full user-authored Markdown."""

    project_path: str
    status: str | None
    github_watch: bool | None
    repository: str | None


@dataclass(frozen=True, slots=True)
class ProjectWatchObservation:
    """One historical per-Project Watcher observation, not a Writer receipt."""

    project_path: str
    repository: str
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class GlobalWatchStatus:
    """Claim from the trusted scheduler observer, not a public toggle value.

    A configured desired Off is not evidence of *effective* Off unless the
    scheduler source is authoritative AND every legacy producer is excluded.
    """

    desired_enabled: bool | None
    authoritative_runtime: bool
    legacy_watchers_excluded: bool


def _now_utc(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is None:
        raise ProjectWatchProjectionUnavailable()
    try:
        if value.utcoffset() is None:
            raise ValueError("naive timestamp")
        return value.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        raise ProjectWatchProjectionUnavailable() from None


def _project_path(value: object) -> bool:
    return (
        type(value) is str
        and 15 <= len(value) <= 500
        and _PATH.fullmatch(value) is not None
        and all(part not in (".", "..") for part in value.split("/"))
        # Surrogates from a malformed/untrusted filename cannot be hashed
        # with strict UTF-8; reject without leaking their raw path.
        and all(unicodedata.category(ch) != "Cs" for ch in value)
    )


def _repository(value: object) -> bool:
    return (
        type(value) is str
        and 3 <= len(value) <= 180
        and _REPO.fullmatch(value) is not None
        and all(part not in (".", "..") for part in value.split("/"))
    )


def _safe_name(path: str) -> str:
    name = path.rsplit("/", 1)[1][:-3]
    # User-controlled note filenames appear in a browser list. Replace
    # controls, direction overrides, and line/paragraph separators so a
    # name cannot visually reorder neighboring UI rows or status labels.
    safe = "".join(
        "?" if unicodedata.category(ch).startswith("C")
        or unicodedata.category(ch) in ("Zl", "Zp") else ch
        for ch in name
    ).strip()
    return safe[:120] if safe else "Unnamed Project"


def project_watch_projection(
    projects: tuple[ProjectWatchConfig, ...],
    observations: tuple[ProjectWatchObservation, ...],
    global_status: GlobalWatchStatus,
    *,
    now: datetime,
    stale_after_seconds: int = 7200,
) -> dict[str, object]:
    """Project watch *observation* UI DTO; cannot claim verified Writer effects.

    Disabled Projects must remain visible. Historical rows may only be
    attributed when exact project path and repository are still identical.
    Unknown/legacy-unfenced global control can never claim paused globally.
    """
    instant = _now_utc(now)
    if (
        type(projects) is not tuple
        or type(observations) is not tuple
        or len(projects) > _MAX_ROWS or len(observations) > _MAX_ROWS
        or type(global_status) is not GlobalWatchStatus
        or type(global_status.desired_enabled) not in (bool, type(None))
        or type(global_status.authoritative_runtime) is not bool
        or type(global_status.legacy_watchers_excluded) is not bool
        or type(stale_after_seconds) is not int
        or not 60 <= stale_after_seconds <= _MAX_SOURCE_AGE
    ):
        raise ProjectWatchProjectionUnavailable()
    if any(
        type(p) is not ProjectWatchConfig or not _project_path(p.project_path)
        or (p.status is not None and type(p.status) is not str)
        or type(p.github_watch) not in (bool, type(None))
        or (p.repository is not None and type(p.repository) is not str)
        for p in projects
    ):
        raise ProjectWatchProjectionUnavailable()
    if len({p.project_path for p in projects}) != len(projects):
        raise ProjectWatchProjectionUnavailable()
    if any(type(o) is not ProjectWatchObservation for o in observations):
        raise ProjectWatchProjectionUnavailable()
    # Historical rows are indexed by Project path only; never join by repo
    # alone or retain the previous binding when a user changes github_repo.
    seen: set[str] = set()
    lookup: dict[str, ProjectWatchObservation] = {}
    for observation in observations:
        if (
            not _project_path(observation.project_path)
            or not _repository(observation.repository)
            or observation.project_path in seen
        ):
            raise ProjectWatchProjectionUnavailable()
        # Validate all history records even if their Project is currently
        # disabled or absent. An ignored stale binding is not an excuse to
        # accept malformed timestamps from the durable observation store.
        _now_utc(observation.observed_at)
        seen.add(observation.project_path)
        lookup[observation.project_path] = observation
    authoritative = (
        global_status.authoritative_runtime
        and global_status.legacy_watchers_excluded
        and global_status.desired_enabled is not None
    )
    rows: list[dict[str, object]] = []
    for project in sorted(projects, key=lambda p: p.project_path):
        repo = project.repository
        watch = project.github_watch
        observed_at = None
        if watch is False:
            state = "disabled_by_project"
        elif watch is None:
            state = "configuration_unknown"
        elif not _repository(repo):
            state = "misconfigured"
            repo = None
        elif project.status not in _PROJECT_STATES:
            state = "misconfigured_status"
        elif project.status in _UNWATCHED:
            state = "ineligible_status"
        elif not authoritative:
            state = "global_control_unverified"
        elif global_status.desired_enabled is False:
            state = "paused_globally"
        else:
            prior = lookup.get(project.project_path)
            if prior is None:
                state = "not_observed"
            elif prior.repository != repo:
                state = "binding_changed"
            else:
                sample = _now_utc(prior.observed_at)
                seconds = (instant - sample).total_seconds()
                if seconds < 0:
                    state = "observation_invalid"
                elif seconds > stale_after_seconds:
                    state, observed_at = "stale", sample.isoformat()
                else:
                    state, observed_at = "recently_observed", sample.isoformat()
        rows.append({
            "projectId": hashlib.sha256(
                project.project_path.encode("utf-8")
            ).hexdigest()[:24],
            "name": _safe_name(project.project_path),
            "repository": repo if _repository(repo) else None,
            "projectStatus": (
                project.status if project.status in _PROJECT_STATES else None
            ),
            "githubWatchConfigured": watch,
            "monitoringState": state,
            "lastWatcherObservedAt": observed_at,
            # Never imply canonical Writer verification from GitHub read.
            "lastVerifiedVaultWrite": None,
        })
    return {
        "schemaVersion": _SCHEMA,
        # Projection/render timestamp only: NOT the SourceOwner's evidence
        # collection time and NOT a verified Vault write timestamp.
        "projectedAt": instant.isoformat(),
        "globalDesiredEnabled": global_status.desired_enabled,
        "globalControlVerified": authoritative,
        "projectCount": len(rows),
        "projects": tuple(rows),
    }
