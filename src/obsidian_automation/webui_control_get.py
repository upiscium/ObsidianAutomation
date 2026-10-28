"""Read-only v1 Catalog assembly from explicitly attached, existing sources.

A source object's status() is the only operation permitted here. Never build a
Job or a provider, and never call submit/run/tick/update or initialize a journal.
The output from a legacy job is a *status observation*, not effect evidence.
"""
from __future__ import annotations

import time

from .webui_control_read import compose_read_catalog
from .webui_legacy_read import project_legacy_read

_OPTIONAL = frozenset(("b1", "b3", "b5"))


def _existing_status(job: object | None) -> object:
    if job is None:
        return None
    try:
        value = job.status()
        # None from an *attached* owner is a failed observation, never proof
        # that the owner was absent from this server instance.
        return False if value is None else value
    except Exception:
        # A failing attached adapter is unavailable, NOT absent or healthy.
        return False


def read_control_catalog(*, snapshot_job: object | None,
                         daily_job: object | None,
                         observed_at: float | None = None,
                         optional_snapshots: object = None) -> dict:
    """Return only the five v1 read/mock slots; no new control authority.

    Optional B1/B3/B5 sources must be *already materialized* DTOs. No method
    on an optional object is ever invoked. The source map cannot override
    live B2/B4 observations. An invalid map fails closed for optional slots.
    """
    snapshot = _existing_status(snapshot_job)
    daily = _existing_status(daily_job)
    # Sample AFTER the already-attached status reads so a just-created run
    # cannot falsely be future-dated by the request's observation time.
    if observed_at is None:
        observed_at = time.time()
    # Strict v1 composer validates observed_at, status types and nested fields.
    legacy = project_legacy_read(snapshot, daily, observed_at)
    sources: dict[str, object] = dict(legacy)
    if optional_snapshots is not None:
        if (type(optional_snapshots) is dict and
                all(type(k) is str and k in _OPTIONAL for k in optional_snapshots)):
            for key, value in optional_snapshots.items():
                sources[key] = value
        else:
            sources.update({key: False for key in _OPTIONAL})
    return compose_read_catalog(sources, observed_at)
