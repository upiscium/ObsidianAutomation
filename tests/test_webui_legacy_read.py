"""Offline B2/B4 legacy statuses: fixed, non-effectful v1 read views."""
from __future__ import annotations

from copy import deepcopy
import json

import pytest

from obsidian_automation.webui_legacy_read import project_legacy_read
from obsidian_automation.webui_control_read import compose_read_catalog

AT = 100.0
RID = "a" * 32
REQ = "b" * 32


def snapshot(*, configured=True, enabled=False, due=None, last=None):
    return {
        "version": 1, "job": "vault_snapshot",
        "settings": {"version": 1, "configured": configured, "enabled": enabled,
                     "interval_seconds": 300, "next_due": due,
                     "source_name": "nextcloud", "destination_name": "gitea",
                     "gitea_write": "unverified"},
        "last_run": last,
    }


def daily(*, configured=True, enabled=False, due=None, last=None):
    return {
        "version": 1, "job": "github_daily_preview", "publication_enabled": False,
        "settings": {"configured": configured, "enabled": enabled,
                     "interval_seconds": 3600, "next_due": due},
        "last_run": last,
    }


def snapshot_run(status="running", *, created=20, completed=None):
    return {"run_id": RID, "request_id": REQ, "trigger": "manual",
            "status": status, "created": created, "completed": completed,
            "stage": "running" if status == "running" else
                     "queued" if status == "queued" else "verify",
            "remote_ref": "f" * 40}


def daily_run(status="running", *, created=20, completed=None):
    return {"run_id": RID, "request_id": REQ, "trigger": "manual",
            "status": status, "created": created, "completed": completed,
            "date_jst": "2026-10-28", "capture_sha256": "c" * 64,
            "digest_sha256": "d" * 64, "original_event_count": 2,
            "rendered_entity_count": 1,
            "publication_outcome": "applied" if status == "published" else None}


def full_catalog(s=None, d=None):
    projections = project_legacy_read(s, d, AT)
    return compose_read_catalog(projections, AT)["workflows"]


def test_absent_services_are_independent_and_never_executable():
    pair = project_legacy_read(None, None, AT)
    assert tuple(pair) == ("b2", "b4")
    assert pair["b2"]["integration"] == "not_attached"
    assert pair["b4"]["integration"] == "not_attached"
    assert [w["id"] for w in full_catalog()] == ["b1", "b2", "b3", "b4", "b5"]
    for value in pair.values():
        assert value["availableActions"] == []
        assert set(value["actionReasons"]) == {"run_once", "set_schedule"}
        assert not value["lastVerifiedOutcome"]
        assert value["execution"]["state"] == "unknown"


def test_configured_and_enabled_schedules_are_truthful_but_not_authority():
    pair = project_legacy_read(snapshot(enabled=True, due=3700),
                               daily(enabled=True, due=3750), AT)
    for identifier, interval, due in (("b2", 300, 3700), ("b4", 3600, 3750)):
        item = pair[identifier]
        assert item["integration"] == "pending"
        assert item["adapterMode"] == "read_only"
        assert item["configuration"] == {"state": "configured", "reason": None}
        assert item["schedule"] == {
            "state": "enabled", "intervalSeconds": interval,
            "nextDueAt": due, "reason": None}
        assert item["revision"] == "0" * 32
        assert item["actionReasons"] == {"run_once": "read_only",
                                          "set_schedule": "read_only"}
        assert item["availableActions"] == []
    assert full_catalog(snapshot(), daily())[1]["integration"] == "pending"


def test_missing_configuration_disabled_schedule_and_unknown_due():
    pair = project_legacy_read(snapshot(configured=False),
                               daily(enabled=True, due=None), AT)
    assert pair["b2"]["configuration"]["state"] == "missing"
    assert pair["b2"]["schedule"]["state"] == "disabled"
    assert pair["b4"]["schedule"]["state"] == "enabled"
    assert pair["b4"]["schedule"]["nextDueAt"] is None
    assert pair["b4"]["schedule"]["reason"] == "due_unavailable"


@pytest.mark.parametrize("bad", [
    lambda s: s.update(version=True),
    lambda s: s.update(job="other"),
    lambda s: s.update(private_secret="abc"),
    lambda s: s["settings"].update(enabled=1),
    lambda s: s["settings"].update(interval_seconds=59),
    lambda s: s["settings"].update(next_due=float("nan")),
    lambda s: s["settings"].update(next_due=1),
    lambda s: s["settings"].update(gitea_write="/private/secret"),
    lambda s: s["settings"].update(source_name="local-vault"),
    lambda s: s["settings"].update(enabled=True, configured=False),
    lambda s: s["settings"].update(enabled=True, next_due=float("inf")),
])
def test_malformed_snapshot_fails_closed_independent_of_daily(bad):
    value = snapshot()
    bad(value)
    a = project_legacy_read(value, daily(), AT)
    assert a["b2"]["integration"] == "unavailable"
    assert a["b2"]["lastAttempt"] is None
    assert a["b2"]["availableActions"] == []
    assert a["b4"]["integration"] == "pending"
    assert "private_secret" not in json.dumps(a)


@pytest.mark.parametrize("bad", [
    lambda s: s.update(version=True),
    lambda s: s.update(publication_enabled=1),
    lambda s: s.update(path="/private"),
    lambda s: s["settings"].update(interval_seconds=3599),
    lambda s: s["settings"].update(enabled=True, configured=False),
    lambda s: s["settings"].update(next_due=-1),
    lambda s: s["settings"].update(next_due="http://secret/"),
])
def test_malformed_daily_fails_closed_independent_of_snapshot(bad):
    value = daily()
    bad(value)
    result = project_legacy_read(snapshot(), value, AT)
    assert result["b4"]["integration"] == "unavailable"
    assert result["b2"]["integration"] == "pending"
    assert result["b4"]["availableActions"] == []
    assert "http://secret" not in json.dumps(result)


@pytest.mark.parametrize("status", ["queued", "running"])
@pytest.mark.parametrize("identity", ["b2", "b4"])
def test_active_attempt_has_opaque_id_and_no_fabricated_start(identity, status):
    run = snapshot_run(status) if identity == "b2" else daily_run(status)
    data = project_legacy_read(snapshot(last=run) if identity == "b2" else None,
                               daily(last=run) if identity == "b4" else None, AT)[identity]
    assert data["integration"] == "pending"
    assert data["execution"] == {"state": status, "requestId": REQ, "startedAt": None}
    assert data["lastAttemptAt"] == 20
    assert data["lastAttempt"]["state"] == status
    assert data["lastAttempt"]["outcome"] is None
    assert data["lastAttempt"]["effect"]["state"] == "unknown"
    assert data["lastVerifiedOutcome"] is None
    assert data["availableActions"] == []


@pytest.mark.parametrize("status,outcome", [
    ("saved", "unknown"), ("no_changes", "unknown"),
    ("failed", "failure"), ("unconfirmed", "unknown"),
])
def test_snapshot_terminal_never_claims_remote_ref_verified(status, outcome):
    run = snapshot_run(status, completed=50)
    pair = project_legacy_read(snapshot(last=run), daily(), AT)
    b2 = pair["b2"]
    assert b2["integration"] == "pending"
    assert b2["execution"]["state"] == "unknown"
    assert b2["lastAttempt"]["outcome"] == outcome
    assert b2["lastAttempt"]["completedAt"] == 50
    assert b2["lastAttempt"]["effect"] == {
        "kind": "snapshot_ref", "state": "unknown", "changed": None}
    assert b2["lastVerifiedOutcome"] is None
    assert "f" * 40 not in json.dumps(pair)


@pytest.mark.parametrize("status,kind,outcome", [
    ("preview_ready", "daily_preview", "unknown"),
    ("published", "daily_vault", "unknown"),
    ("publish_failed", "daily_vault", "failure"),
    ("failed", "daily_preview", "failure"),
    ("unconfirmed", "daily_preview", "unknown"),
])
def test_daily_terminal_never_promotes_preview_or_publication(status, kind, outcome):
    run = daily_run(status, completed=51)
    pair = project_legacy_read(snapshot(), daily(last=run), AT)
    view = pair["b4"]
    assert view["integration"] == "pending"
    assert view["lastAttempt"]["outcome"] == outcome
    assert view["lastAttempt"]["effect"] == {
        "kind": kind, "state": "unknown", "changed": None}
    assert view["lastVerifiedOutcome"] is None
    raw = json.dumps(pair)
    assert "c" * 64 not in raw and "d" * 64 not in raw and "2026-10-28" not in raw


@pytest.mark.parametrize("bad", [
    lambda r: r.update(request_id="../secret"),
    lambda r: r.update(status="custom"),
    lambda r: r.update(created=True),
    lambda r: r.update(created=101),
    lambda r: r.update(completed=-1),
    lambda r: r.update(stage="/private"),
    lambda r: r.update(remote_ref="private-token"),
    lambda r: r.update(unexpected=0),
])
def test_invalid_snapshot_run_downgrades_only_snapshot(bad):
    r = snapshot_run("saved", completed=45)
    bad(r)
    result = project_legacy_read(snapshot(last=r), daily(), AT)
    assert result["b2"]["integration"] == "unavailable"
    assert result["b2"]["lastAttempt"] is None
    assert result["b4"]["integration"] == "pending"


@pytest.mark.parametrize("bad", [
    lambda r: r.update(publication_outcome="secret"),
    lambda r: r.update(date_jst="../../secret"),
    lambda r: r.update(capture_sha256="token"),
    lambda r: r.update(digest_sha256=float("nan")),
    lambda r: r.update(original_event_count=True),
    lambda r: r.update(completed=101),
    lambda r: r.update(status="password"),
    lambda r: r.update(extra="private"),
])
def test_invalid_daily_run_downgrades_only_daily(bad):
    r = daily_run("published", completed=42)
    bad(r)
    view = project_legacy_read(snapshot(), daily(last=r), AT)
    assert view["b4"]["integration"] == "unavailable"
    assert view["b2"]["integration"] == "pending"


def test_pure_input_unchanged_and_output_detached():
    src, day = snapshot(last=snapshot_run("saved", completed=45)), daily(
        last=daily_run("preview_ready", completed=40))
    original = deepcopy((src, day))
    output = project_legacy_read(src, day, AT)
    assert (src, day) == original
    output["b2"]["actionReasons"]["run_once"] = "altered"
    output["b4"]["lastAttempt"]["requestId"] = "f" * 32
    assert (src, day) == original
    assert project_legacy_read(src, day, AT)["b2"]["actionReasons"]["run_once"] == "read_only"


def test_custom_object_is_not_invoked_and_invalid_time_rejected():
    class Trap:
        def __getattr__(self, _):
            raise AssertionError("must not touch")
        def __iter__(self):
            raise AssertionError("must not iterate")
        def __eq__(self, _):
            raise AssertionError("must not compare")
    result = project_legacy_read(Trap(), daily(), AT)
    assert result["b2"]["integration"] == "unavailable"
    assert result["b4"]["integration"] == "pending"
    # A plain outer dict can contain malicious nested objects. Check types
    # before comparing values so no untrusted __eq__/__str__ hooks execute.
    for field in ("source_name", "destination_name", "gitea_write"):
        invalid = snapshot()
        invalid["settings"][field] = Trap()
        assert project_legacy_read(invalid, daily(), AT)["b2"]["integration"] == "unavailable"
    for at in (-1, True, float("inf"), float("nan"), "tomorrow"):
        with pytest.raises(ValueError):
            project_legacy_read(None, None, at)
