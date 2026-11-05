"""R3B trusted-source B3 port; disposable SQLite and fake WebDAV only.

A typed claim is never proof that the old LXC is isolated in production.
No router, old/new real host, credentials, systemd or network are exercised.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import replace

import pytest

from obsidian_automation.core_promotion_transport import HTTPResponse
from obsidian_automation.github_b3_attempts import B3AttemptJournal, Busy
from obsidian_automation.github_b3_job import B3JobConfig, JobNotAvailable
from obsidian_automation.github_b3_trusted_port import (
    B3CutoverObservation,
    B3ExecutionUnavailable,
    B3RegisteredInputs,
    B3TrustedJobPort,
)
from obsidian_automation.github_project_overview import (
    OverviewItem,
    make_overview_proposal,
)
from obsidian_automation.github_project_status_mutation import (
    parse_watcher_proposal,
)
from obsidian_automation.webui_b3_adapter import ProjectSyncRequest


BASE = "https://nextcloud.example/remote.php/dav/files/owner/ObsidianVault"
PROJECT = "10-Project/Terreate/Terreate.md"
OVERVIEW = "10-Project/Terreate/Status.md"
REPO = "upiscium/Terreate"
BINDING = "a" * 32
REV = "b" * 32
RECORD = "c" * 32
REQUEST = "d" * 32


def status_proposal():
    return parse_watcher_proposal(json.dumps({
        "change": True, "current_status": "running",
        "event": "project-status-observation",
        "latest_commit_at": "2026-08-13T04:11:38Z",
        "latest_commit_sha": "0d022ed5140b497b5d3be886408822ecbd981dd3",
        "observed_at": "2026-09-17T03:39:13Z",
        "open_issues": [203], "open_prs": [42],
        "pending": True, "project": PROJECT,
        "proposed_status": "planning",
        "reason": "no commit within 7 days", "repository": REPO,
    }).encode())


def overview_proposal():
    return make_overview_proposal(
        project_path=PROJECT, repository=REPO,
        issues=[OverviewItem(203, "Synthetic issue")],
        pull_requests=[OverviewItem(42, "Synthetic PR")],
    )


class MemoryDav:
    def __init__(self):
        self.project = (
            "---\ntype: project\nworkspace:\nstatus: running\npriority:\n"
            f"github_repo: {REPO}\ngithub_watch: true\n---\n"
            "# Human notes\nPRESERVE OWNER TEXT\n"
        ).encode()
        self.overview = None
        self.etags = {PROJECT: '"a1"', OVERVIEW: '"b1"'}
        self.calls = []
        self.reject = None
        self.put_count = 0

    def __call__(
        self, *, method, target_url, username, password,
        headers, body, timeout, response_limit,
    ):
        assert target_url.startswith(BASE + "/")
        assert username == "writer" and password == "synthetic-secret"
        assert timeout > 0 and response_limit > 0
        path = target_url[len(BASE) + 1:]
        assert path in (PROJECT, OVERVIEW)
        self.calls.append((method, path))
        current = self.project if path == PROJECT else self.overview
        if method == "GET":
            return HTTPResponse(
                200 if current is not None else 404,
                current if current is not None else b"",
                self.etags[path] if current is not None else None,
            )
        assert method == "PUT"
        if path == self.reject:
            return HTTPResponse(412, b"", self.etags[path])
        expected = headers.get(
            "If-Match" if current is not None else "If-None-Match",
        )
        assert expected == (self.etags[path] if current is not None else "*")
        assert type(body) is bytes
        if path == PROJECT:
            self.project = body
        else:
            self.overview = body
        self.put_count += 1
        self.etags[path] = f'"new{self.put_count}"'
        return HTTPResponse(204, b"", self.etags[path])


class Guard:
    def __init__(self, before_entry=None):
        self.entered = 0
        self.active = False
        self.before_entry = before_entry

    @contextmanager
    def __call__(self):
        assert not self.active
        self.entered += 1
        self.active = True
        try:
            if self.before_entry is not None:
                self.before_entry()
            yield
        finally:
            self.active = False


class Sources:
    def __init__(self):
        self.config = B3JobConfig(
            binding_ref=BINDING, revision=REV,
            project_path=PROJECT, repository=REPO,
            registered_base_url=BASE, username="writer",
            password="synthetic-secret",
        )
        self.cutover = B3CutoverObservation(
            record_id=RECORD, binding_ref=BINDING, revision=REV,
            operator_approved=False,
            old_writer_exclusion="unverified",
        )
        self.cutover_calls = 0
        self.registered_calls = 0

    def evidence(self):
        self.cutover_calls += 1
        return self.cutover

    def registered(self):
        self.registered_calls += 1
        return B3RegisteredInputs(
            self.config, status_proposal(), overview_proposal(),
        )


@pytest.fixture
def context(tmp_path):
    state = tmp_path / "b3"
    state.mkdir(mode=0o700)
    sources = Sources()
    dav = MemoryDav()
    guard = Guard()
    with B3AttemptJournal(state, initialize=True) as journal:
        port = B3TrustedJobPort(
            journal, registered_inputs=sources.registered,
            cutover_observation=sources.evidence,
            shared_writer_guard=guard,
        )
        yield sources, dav, guard, journal, port


def req(request_id=REQUEST, *, binding=BINDING, revision=REV):
    return ProjectSyncRequest(request_id, revision, binding)


def authorize(sources, *, mode="host_isolated"):
    sources.cutover = replace(
        sources.cutover, operator_approved=True,
        old_writer_exclusion=mode,
    )


@pytest.mark.parametrize("bad", [
    None, object(), {}, "wrong", 123, True,
])
def test_only_exact_public_request_type_is_validated_before_observation(
    context, bad,
):
    sources, dav, guard, journal, port = context
    with pytest.raises(B3ExecutionUnavailable, match="invalid_request"):
        port.sync_project(bad, transport=dav)
    assert sources.cutover_calls == sources.registered_calls == 0
    assert journal.snapshot()["last_attempt"] is None
    assert guard.entered == 0 and dav.calls == []


@pytest.mark.parametrize("change", [
    {"operator_approved": True},
    {"old_writer_exclusion": "host_isolated"},
    {"old_writer_exclusion": "credential_revoked"},
    {"operator_approved": 1, "old_writer_exclusion": "host_isolated"},
    {"operator_approved": True, "old_writer_exclusion": "unknown"},
    {"operator_approved": True, "old_writer_exclusion": "invalid"},
    {"operator_approved": True, "old_writer_exclusion": "host_isolated",
     "record_id": "z" * 32},
    {"operator_approved": True, "old_writer_exclusion": "host_isolated",
     "binding_ref": "e" * 32},
    {"operator_approved": True, "old_writer_exclusion": "host_isolated",
     "revision": "e" * 32},
])
def test_incomplete_or_unbound_cutover_has_zero_admission(context, change):
    sources, dav, guard, journal, port = context
    sources.cutover = replace(sources.cutover, **change)
    with pytest.raises(B3ExecutionUnavailable, match="cutover_unverified"):
        port.sync_project(req(), transport=dav)
    assert sources.registered_calls == 0
    assert journal.snapshot()["last_attempt"] is None
    assert guard.entered == 0 and dav.calls == []


@pytest.mark.parametrize("mode", ["host_isolated", "credential_revoked"])
def test_approved_source_runs_actual_cas_writers_once_with_fake_dav(
    context, mode,
):
    sources, dav, guard, journal, port = context
    authorize(sources, mode=mode)
    first = port.sync_project(req(), transport=dav)
    assert first["decision"] == "accepted"
    assert first["writer_readback"] == "two_fresh_gets_match_receipt_hashes"
    assert first["last_verified_pipeline_success"] is None
    assert first["receipt"]["state"] == "completed"
    assert first["receipt"]["terminal_code"] == "writer_readback_observed"
    assert journal.snapshot()["last_verified_pipeline_success"] is None
    assert guard.entered == 1
    assert sources.cutover_calls == 2  # before config + under guard
    assert dav.put_count == 2
    assert [method for method, _ in dav.calls[-2:]] == ["GET", "GET"]
    assert b"PRESERVE OWNER TEXT" in dav.project

    calls = len(dav.calls)
    second = port.sync_project(req(), transport=dav)
    assert second["decision"] == "replayed"
    assert second["receipt"]["run_id"] == first["receipt"]["run_id"]
    assert guard.entered == 1
    assert len(dav.calls) == calls
    assert "synthetic-secret" not in json.dumps(first)
    assert BASE not in json.dumps(first)
    assert PROJECT not in json.dumps(first)


def test_revocation_between_preflight_and_guard_is_denied_before_effect(context):
    sources, dav, _, journal, _ = context
    authorize(sources)
    def revoke():
        sources.cutover = replace(
            sources.cutover, old_writer_exclusion="unverified",
        )
    guard = Guard(before_entry=revoke)
    port = B3TrustedJobPort(
        journal, registered_inputs=sources.registered,
        cutover_observation=sources.evidence,
        shared_writer_guard=guard,
    )
    with pytest.raises(JobNotAvailable, match="writer_not_started"):
        port.sync_project(req(), transport=dav)
    abandoned = journal.snapshot()["last_attempt"]
    assert abandoned["state"] == "completed"
    assert abandoned["terminal_code"] == "abandoned_before_start"
    assert journal.snapshot()["last_unconfirmed"] is None
    assert guard.entered == 1
    assert dav.calls == []
    authorize(sources)
    assert port.sync_project(req(), transport=dav)["decision"] == "replayed"
    assert dav.calls == []
    guard.before_entry = None  # trusted owner restored cutover authority
    port.sync_project(req("f" * 32), transport=dav)
    assert dav.put_count == 2


def test_same_claim_with_new_record_id_is_refused_inside_guard(context):
    sources, dav, _, journal, _ = context
    authorize(sources)
    guard = Guard(before_entry=lambda: setattr(
        sources, "cutover",
        replace(sources.cutover, record_id="e" * 32),
    ))
    port = B3TrustedJobPort(
        journal, registered_inputs=sources.registered,
        cutover_observation=sources.evidence,
        shared_writer_guard=guard,
    )
    with pytest.raises(JobNotAvailable, match="writer_not_started"):
        port.sync_project(req(), transport=dav)
    assert journal.snapshot()["last_attempt"]["terminal_code"] == "abandoned_before_start"
    assert dav.calls == []


@pytest.mark.parametrize("change", [
    {"binding_ref": "f" * 32},
    {"revision": "f" * 32},
    {"registered_base_url": "http://invalid.example"},
])
def test_server_binding_drift_cannot_admit_any_writer(context, change):
    sources, dav, guard, journal, port = context
    authorize(sources)
    sources.config = replace(sources.config, **change)
    if "registered_base_url" in change:
        with pytest.raises(JobNotAvailable, match="invalid_configuration"):
            port.sync_project(req(), transport=dav)
    else:
        with pytest.raises(
            B3ExecutionUnavailable, match="registered_inputs_unavailable",
        ):
            port.sync_project(req(), transport=dav)
    assert journal.snapshot()["last_attempt"] is None
    assert guard.entered == 0 and dav.calls == []


def test_status_cas_conflict_held_unconfirmed_and_never_blind_retry(context):
    sources, dav, guard, journal, port = context
    authorize(sources)
    dav.reject = PROJECT
    with pytest.raises(JobNotAvailable, match="writer_effect_unknown"):
        port.sync_project(req(), transport=dav)
    assert journal.snapshot()["last_unconfirmed"]["terminal_code"] == "effect_unknown"
    assert journal.snapshot()["last_verified_pipeline_success"] is None
    assert b"PRESERVE OWNER TEXT" in dav.project
    original_calls = len(dav.calls)
    with pytest.raises(Busy, match="unconfirmed_effect"):
        port.sync_project(req("f" * 32), transport=dav)
    assert len(dav.calls) == original_calls


def test_external_probe_failure_does_not_leak_exception_or_open_journal(context):
    sources, dav, guard, journal, _ = context
    def broken_cutover():
        raise ValueError("sensitive legacy host path and credential")
    port = B3TrustedJobPort(
        journal, registered_inputs=sources.registered,
        cutover_observation=broken_cutover,
        shared_writer_guard=guard,
    )
    with pytest.raises(B3ExecutionUnavailable, match="cutover_unavailable") as exc:
        port.sync_project(req(), transport=dav)
    assert "sensitive" not in str(exc.value)
    assert journal.snapshot()["last_attempt"] is None
    assert dav.calls == []


def test_no_router_or_scheduler_is_registered_and_inert_constructor(context):
    sources, dav, guard, journal, port = context
    assert not hasattr(port, "set_schedule")
    assert not hasattr(port, "tick")
    assert not hasattr(port, "observe")
    assert journal.snapshot()["last_attempt"] is None
    assert sources.cutover_calls == 0
    assert sources.registered_calls == 0
    assert guard.entered == 0
    assert dav.calls == []


def test_reject_bad_port_construction(context):
    sources, dav, guard, journal, _ = context
    with pytest.raises(TypeError, match="invalid_b3_journal"):
        B3TrustedJobPort(
            None, registered_inputs=sources.registered,
            cutover_observation=sources.evidence, shared_writer_guard=guard,
        )
    for bad in (None, 1, object()):
        with pytest.raises(TypeError, match="invalid_b3_execution_sources"):
            B3TrustedJobPort(
                journal, registered_inputs=bad,
                cutover_observation=sources.evidence, shared_writer_guard=guard,
            )


def test_real_execution_port_cannot_be_registered_as_webui_read_adapter(context):
    from obsidian_automation.webui_optional_read import (
        validate_optional_read_ports,
    )

    sources, dav, guard, journal, port = context
    with pytest.raises(TypeError, match="invalid_workflow_control_read_ports"):
        validate_optional_read_ports({"b3": port})
    assert sources.cutover_calls == sources.registered_calls == 0
    assert guard.entered == 0 and dav.calls == []


def test_private_registered_source_error_is_fixed_and_does_not_admit(context):
    sources, dav, guard, journal, _ = context
    authorize(sources)

    def unavailable_source():
        raise RuntimeError("private-config-file /etc/secrets/password")

    port = B3TrustedJobPort(
        journal, registered_inputs=unavailable_source,
        cutover_observation=sources.evidence,
        shared_writer_guard=guard,
    )
    with pytest.raises(
        B3ExecutionUnavailable, match="registered_inputs_unavailable",
    ) as exc:
        port.sync_project(req(), transport=dav)
    assert "/etc" not in str(exc.value)
    assert journal.snapshot()["last_attempt"] is None
    assert guard.entered == 0 and dav.calls == []


def test_forged_cutover_type_does_not_grant_execution(context):
    sources, dav, guard, journal, _ = context
    authorize(sources)
    port = B3TrustedJobPort(
        journal, registered_inputs=sources.registered,
        cutover_observation=lambda: {
            "record_id": RECORD, "binding_ref": BINDING, "revision": REV,
            "operator_approved": True, "old_writer_exclusion": "host_isolated",
        },
        shared_writer_guard=guard,
    )
    with pytest.raises(B3ExecutionUnavailable, match="cutover_unverified"):
        port.sync_project(req(), transport=dav)
    assert journal.snapshot()["last_attempt"] is None
    assert sources.registered_calls == 0 and dav.calls == []


def test_wrong_project_is_never_selected_from_public_request(context):
    sources, dav, guard, journal, port = context
    authorize(sources)
    with pytest.raises(B3ExecutionUnavailable, match="cutover_unverified"):
        port.sync_project(req(binding="e" * 32), transport=dav)
    with pytest.raises(B3ExecutionUnavailable, match="cutover_unverified"):
        port.sync_project(req(revision="e" * 32), transport=dav)
    assert journal.snapshot()["last_attempt"] is None
    assert sources.registered_calls == 0 and dav.calls == []
