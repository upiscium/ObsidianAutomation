"""Sanitized read-only AI orchestration incidents for Management Dashboard.

Canonical status is derived from the latest generation of each durable job. A
Validation reject is a normal candidate outcome; only operational blockage and
exhausted retries are incidents. This module never reads Vault projections,
controls jobs, or exposes model output, paths, credentials, or raw exceptions.
"""
from __future__ import annotations

from pathlib import Path

from .pre_review_status import ALL_STATES, PreReviewStatus, build_status, parse_status

REJECTION_STATE = "deterministic_reject"
ACTIONABLE_STATES = (
    ("blocked", "blocked_generations"),
    ("retry_exhausted", "retry_exhausted_generations"),
)
TRANSIENT_STATE = "retryable_failure"


def unavailable_pipeline(reason: str = "pipeline_not_configured") -> dict[str, object]:
    if reason not in {"pipeline_not_configured", "pipeline_observation_unavailable"}:
        reason = "pipeline_observation_unavailable"
    return {
        "source": "pre_review_orchestration",
        "health": "unknown",
        "reason_codes": [reason],
        "observed_at": None,
        "review_pending": None,
        "normal_rejections": None,
        "transient_failures": None,
        "incidents": [],
    }


def summarize_pipeline(status: PreReviewStatus) -> dict[str, object]:
    """Convert a verified durable scheduler snapshot to a fixed public schema.

    A missing/invalid snapshot raises to the server, which returns an UNKNOWN
    projection. Never trust a free-form status reason or database field as UI text.
    """
    if not isinstance(status, PreReviewStatus):
        raise ValueError("invalid pipeline status type")
    # The existing schema validator enforces counts, field types and authority.
    validated = parse_status(status.to_json_bytes())
    counts = validated.states
    if set(counts) != set(ALL_STATES):
        raise ValueError("unknown scheduler states")
    actionable = sum(counts[key] for key, _ in ACTIONABLE_STATES)
    transient = counts[TRANSIENT_STATE]
    expected = "CRITICAL" if actionable else ("WARNING" if transient else "OK")
    if validated.pipeline_health != expected:
        raise ValueError("inconsistent pipeline health")
    incidents = [
        {
            "code": code,
            "severity": "critical",
            "affected": counts[state],
            "active": True,
        }
        for state, code in ACTIONABLE_STATES
        if counts[state] > 0
    ]
    reasons = [item["code"] for item in incidents]
    if transient:
        reasons.append("retryable_failures")
    return {
        "source": "pre_review_orchestration",
        # "healthy" means only the durable pre-review scheduler reports no
        # classified faults; it does NOT assert provider/Nextcloud/systemd health.
        "health": "failed" if actionable else ("retrying" if transient else "no_known_fault"),
        "reason_codes": reasons,
        "observed_at": validated.generated_at,
        "review_pending": counts["awaiting_human_review"],
        "normal_rejections": counts[REJECTION_STATE],
        "transient_failures": transient,
        "incidents": incidents,
    }


def collect_pipeline(ai_root: Path) -> dict[str, object]:
    """Read authoritative local SQLite metadata through existing read-only API.

    A web request cannot select ai_root. It is configured by the local operator
    at server startup; missing ACL/DB and malformed state yield UNKNOWN above.
    """
    return summarize_pipeline(build_status(ai_root))
