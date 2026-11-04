"""Bounded real B3 Job orchestration with in-memory WebDAV fixtures only.

No live Nextcloud, credentials, Vault, systemd, WebUI, or old-LXC worker is
accessed. Existing production CAS Writers run against the fake transport.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from obsidian_automation.core_promotion_transport import HTTPResponse
from obsidian_automation.github_b3_attempts import B3AttemptJournal, Busy
from obsidian_automation.github_b3_job import (
    B3JobConfig,
    B3RunRequest,
    JobNotAvailable,
    run_b3_writer_once,
)
from obsidian_automation.github_project_overview import (
    OverviewItem,
    make_overview_proposal,
)
from obsidian_automation.github_project_status_mutation import (
    parse_watcher_proposal,
)

URL = "https://nextcloud.example/remote.php/dav/files/writer/ObsidianVault"
PROJECT = "10-Project/Terreate/Terreate.md"
OVERVIEW = "10-Project/Terreate/Status.md"
REPO = "upiscium/Terreate"
BINDING, REVISION, REQUEST = "b" * 32, "c" * 32, "a" * 32


def _proposal():
    return parse_watcher_proposal(json.dumps({
        "change": True, "current_status": "running",
        "event": "project-status-observation",
        "latest_commit_at": "2026-08-13T04:11:38Z",
        "latest_commit_sha": "0d022ed5140b497b5d3be886408822ecbd981dd3",
        "observed_at": "2026-09-17T03:39:13Z",
        "open_issues": [203], "open_prs": [42],
        "pending": True, "project": PROJECT, "proposed_status": "planning",
        "reason": "no commit within 7 days", "repository": REPO,
    }).encode())


def _overview():
    return make_overview_proposal(
        project_path=PROJECT, repository=REPO,
        issues=[OverviewItem(203, "A synthetic issue")],
        pull_requests=[OverviewItem(42, "A synthetic PR")],
    )


def _project(status="running"):
    return (
        "---\n"
        "type: project\n"
        "workspace:\n"
        f"status: {status}\n"
        "priority:\n"
        f"github_repo: {REPO}\n"
        "github_watch: true\n"
        "---\n"
        "# Project Summary\n"
        "KEEP HUMAN NOTES\n"
    ).encode()


class SimulatedCrash(BaseException):
    """Artificial worker death, not a safe retry or a terminal response."""


class FakeWebDAV:
    def __init__(self):
        self.project = _project()
        self.overview = None
        self.etags = {PROJECT: '"p1"', OVERVIEW: '"s1"'}
        self.calls: list[tuple[str, str]] = []
        self.conflict = None
        self.crash_on_put = False
        self.diverge_after_writer = False
        self._puts = 0

    def __call__(self, *, method, target_url, username, password,
                 headers, body, timeout, response_limit):
        assert username == "writer" and password == "private-test-secret"
        assert target_url.startswith(URL + "/")
        assert timeout > 0 and response_limit > 0
        path = target_url.removeprefix(URL + "/")
        assert path in (PROJECT, OVERVIEW)
        self.calls.append((method, path))
        data = self.project if path == PROJECT else self.overview

        if method == "GET":
            return HTTPResponse(
                200 if data is not None else 404,
                data if data is not None else b"",
                self.etags[path] if data is not None else None,
            )
        if method != "PUT":
            raise AssertionError("only Writer conditional PUT and GET")
        if self.crash_on_put:
            raise SimulatedCrash()
        if self.conflict == path:
            return HTTPResponse(412, b"", self.etags[path])
        expected = headers.get("If-Match") if data is not None else headers.get("If-None-Match")
        if expected != (self.etags[path] if data is not None else "*"):
            return HTTPResponse(412, b"", self.etags[path])
        assert type(body) is bytes
        if path == PROJECT:
            self.project = body
        else:
            self.overview = body
        self._puts += 1
        self.etags[path] = f'"v{self._puts + 1}"'
        if self.diverge_after_writer and path == OVERVIEW:
            self.project += b"\nHuman edit after the first Writer\n"
        return HTTPResponse(204, b"", self.etags[path])


class Guard:
    """Required, trusted single-Writer guard rehearsal."""

    def __init__(self):
        self.entered = 0
        self.active = False

    @contextmanager
    def __call__(self):
        assert not self.active, "concurrent Writer entered"
        self.entered += 1
        self.active = True
        try:
            yield
        finally:
            self.active = False


@pytest.fixture
def setup_job(tmp_path: Path):
    root = tmp_path / "private-b3"
    root.mkdir(mode=0o700)
    config = B3JobConfig(
        binding_ref=BINDING, revision=REVISION,
        project_path=PROJECT, repository=REPO,
        registered_base_url=URL, username="writer",
        password="private-test-secret",
    )
    return {
        "root": root, "config": config,
        "request": B3RunRequest(REQUEST, BINDING, REVISION),
        "status_proposal": _proposal(), "overview_proposal": _overview(),
        "transport": FakeWebDAV(), "guard": Guard(),
    }


def _invoke(journal, case, **updates):
    params = {
        "config": case["config"],
        "request": case["request"],
        "status_proposal": case["status_proposal"],
        "overview_proposal": case["overview_proposal"],
        "transport": case["transport"],
        "writer_guard": case["guard"],
    }
    return run_b3_writer_once(journal, **(params | updates))


def test_connected_source_cas_writers_then_two_extra_gets_and_single_db_proof(setup_job):
    case = setup_job
    with B3AttemptJournal(case["root"], initialize=True) as j:
        result = _invoke(j, case)
        assert result["decision"] == "accepted"
        assert result["writer_readback"] == "two_fresh_gets_match_receipt_hashes"
        assert result["receipt"]["state"] == "completed"
        assert result["receipt"]["terminal_code"] == "writer_readback_observed"
        assert result["last_verified_pipeline_success"] is None
        assert j.snapshot()["last_writer_readback"] == result["receipt"]
        assert j.snapshot()["overall_health"] == "unknown"
        assert j.snapshot()["last_verified_pipeline_success"] is None
        proof = j.writer_proof(result["receipt"]["run_id"])
        assert proof["status_proposal_sha256"] == case["status_proposal"].sha256
        assert proof["overview_proposal_sha256"] == case["overview_proposal"].sha256
        assert len(proof["status_after_sha256"]) == 64
        assert len(proof["overview_after_sha256"]) == 64
        assert "KEEP HUMAN NOTES" in case["transport"].project.decode()
        assert [verb for verb, _ in case["transport"].calls].count("PUT") == 2
        assert [verb for verb, _ in case["transport"].calls[-2:]] == ["GET", "GET"]
        assert case["guard"].entered == 1

        original_calls = len(case["transport"].calls)
        replay = _invoke(j, case)
        assert replay["decision"] == "replayed"
        assert replay["receipt"]["run_id"] == result["receipt"]["run_id"]
        assert replay["writer_readback"] is None
        assert len(case["transport"].calls) == original_calls
        assert case["guard"].entered == 1
    with B3AttemptJournal(case["root"], initialize=False) as reopened:
        assert reopened.snapshot()["last_writer_readback"]["run_id"] == (
            result["receipt"]["run_id"]
        )
        assert reopened.writer_proof(result["receipt"]["run_id"]) == proof


def test_later_new_id_can_finish_as_noop_without_replaying_first(setup_job):
    case = setup_job
    with B3AttemptJournal(case["root"], initialize=True) as j:
        first = _invoke(j, case)
        second = _invoke(
            j, case,
            request=B3RunRequest("d" * 32, BINDING, REVISION),
        )
        assert first["receipt"]["run_id"] != second["receipt"]["run_id"]
        assert second["receipt"]["terminal_code"] == "writer_readback_observed"
        assert j.snapshot()["last_writer_readback"]["run_id"] == second["receipt"]["run_id"]
        assert case["guard"].entered == 2
        assert case["transport"]._puts == 2
        assert j.snapshot()["last_verified_pipeline_success"] is None


@pytest.mark.parametrize("input_change,reason", (
    ({"request": B3RunRequest(REQUEST, "d" * 32, REVISION)}, "stale_or_unbound_request"),
    ({"request": B3RunRequest(REQUEST, BINDING, "f" * 32)}, "stale_or_unbound_request"),
    ({"config": None}, "invalid_configuration"),
))
def test_malformed_configuration_or_unbound_run_refuses_before_admission(
    setup_job, input_change, reason,
):
    case = setup_job
    with B3AttemptJournal(case["root"], initialize=True) as j:
        before = len(case["transport"].calls)
        with pytest.raises(JobNotAvailable, match=reason):
            _invoke(j, case, **input_change)
        assert j.snapshot()["last_attempt"] is None
        assert len(case["transport"].calls) == before


def test_status_cas_conflict_remains_unknown_with_no_overview_write(setup_job):
    case = setup_job
    case["transport"].conflict = PROJECT
    with B3AttemptJournal(case["root"], initialize=True) as j:
        with pytest.raises(JobNotAvailable, match="writer_effect_unknown"):
            _invoke(j, case)
        assert j.snapshot()["last_unconfirmed"]["terminal_code"] == "effect_unknown"
        assert j.snapshot()["last_verified_pipeline_success"] is None
        assert not any(path == OVERVIEW and method == "PUT"
                       for method, path in case["transport"].calls)
        with pytest.raises(Busy, match="unconfirmed_effect"):
            _invoke(j, case, request=B3RunRequest("e" * 32, BINDING, REVISION))


def test_overview_cas_conflict_after_status_changed_is_not_auto_retried(setup_job):
    case = setup_job
    case["transport"].conflict = OVERVIEW
    with B3AttemptJournal(case["root"], initialize=True) as j:
        with pytest.raises(JobNotAvailable, match="writer_effect_unknown"):
            _invoke(j, case)
        assert b"status: planning" in case["transport"].project
        assert j.snapshot()["last_attempt"]["state"] == "unconfirmed"
        assert j.snapshot()["last_writer_readback"] is None


def test_human_edit_after_writer_keeps_attempt_unconfirmed(setup_job):
    case = setup_job
    case["transport"].diverge_after_writer = True
    with B3AttemptJournal(case["root"], initialize=True) as j:
        with pytest.raises(JobNotAvailable, match="writer_effect_unknown"):
            _invoke(j, case)
        assert b"Human edit after the first Writer" in case["transport"].project
        assert j.snapshot()["last_attempt"]["state"] == "unconfirmed"
        assert j.snapshot()["last_verified_pipeline_success"] is None
        assert j.writer_proof(j.snapshot()["last_attempt"]["run_id"]) is None


def test_interrupted_worker_after_persisted_start_is_never_blindly_replayed(setup_job):
    case = setup_job
    case["transport"].crash_on_put = True
    with pytest.raises(SimulatedCrash):
        with B3AttemptJournal(case["root"], initialize=True) as j:
            _invoke(j, case)
    with B3AttemptJournal(case["root"], initialize=False) as j:
        assert j.snapshot()["last_unconfirmed"]["terminal_code"] == "interrupted_unknown"
        with pytest.raises(Busy, match="unconfirmed_effect"):
            _invoke(j, case, request=B3RunRequest("e" * 32, BINDING, REVISION))
        assert j.snapshot()["last_verified_pipeline_success"] is None


def test_config_origin_and_guard_not_in_request_surface(setup_job):
    case = setup_job
    assert set(B3RunRequest.__dataclass_fields__) == {
        "request_id", "binding_ref", "expected_revision", "trigger", "retry_of",
    }
    with B3AttemptJournal(case["root"], initialize=True) as j:
        bad = replace(case["config"], registered_base_url="http://evil.example")
        with pytest.raises(JobNotAvailable, match="invalid_configuration"):
            _invoke(j, case, config=bad)
        with pytest.raises(JobNotAvailable, match="writer_guard_unavailable"):
            _invoke(j, case, writer_guard=None)
        assert j.snapshot()["last_attempt"] is None
        assert len(case["transport"].calls) == 0


def test_no_public_values_include_credentials_or_private_writer_hashes(setup_job):
    case = setup_job
    with B3AttemptJournal(case["root"], initialize=True) as j:
        output = _invoke(j, case)
        rendered = json.dumps(output)
        assert "private-test-secret" not in rendered
        assert URL not in rendered
        assert PROJECT not in rendered
        assert REPO not in rendered
        assert case["config"].binding_ref not in rendered
        assert case["config"].revision not in rendered
        assert case["status_proposal"].sha256 not in rendered
        assert case["overview_proposal"].sha256 not in rendered
        assert j.snapshot()["last_verified_pipeline_success"] is None



@pytest.mark.parametrize("bad,expected", (
    ({"registered_base_url": "http://evil.example/ObsidianVault"}, "invalid_configuration"),
    ({"timeout": 10 ** 1000}, "invalid_configuration"),
    ({"timeout": float("nan")}, "invalid_configuration"),
    ({"password": ""}, "invalid_configuration"),
    ({"repository": "upiscium/Other"}, "unbound_proposals"),
    ({"project_path": "10-Project/Other/Other.md"}, "unbound_proposals"),
))
def test_config_binding_and_url_rejected_before_any_writer_effect(
    setup_job, bad, expected,
):
    case = setup_job
    with B3AttemptJournal(case["root"], initialize=True) as j:
        with pytest.raises(JobNotAvailable, match=expected):
            _invoke(j, case, config=replace(case["config"], **bad))
        assert len(case["transport"].calls) == 0
        assert j.snapshot()["last_attempt"] is None


@pytest.mark.parametrize("bad", (
    {"trigger": "arbitrary_shell"},
    {"retry_of": "not-run"},
    {"request_id": "X" * 32},
))
def test_invalid_job_request_has_no_admission_or_transport(setup_job, bad):
    case = setup_job
    with B3AttemptJournal(case["root"], initialize=True) as j:
        request = replace(case["request"], **bad)
        with pytest.raises(JobNotAvailable, match="invalid_request"):
            _invoke(j, case, request=request)
        assert j.snapshot()["last_attempt"] is None
        assert case["transport"].calls == []


def test_bad_canonical_proposals_have_no_admission(setup_job):
    case = setup_job
    with B3AttemptJournal(case["root"], initialize=True) as j:
        with pytest.raises(JobNotAvailable, match="invalid_proposals"):
            _invoke(
                j, case,
                status_proposal=replace(
                    case["status_proposal"], canonical_bytes=b"bad JSON",
                ),
            )
        assert j.snapshot()["last_attempt"] is None


def test_guard_rejection_before_start_never_leads_to_writer_effect(setup_job):
    case = setup_job

    @contextmanager
    def rejected():
        raise RuntimeError("guard closed")
        yield

    with B3AttemptJournal(case["root"], initialize=True) as j:
        with pytest.raises(JobNotAvailable, match="writer_not_started"):
            _invoke(j, case, writer_guard=rejected)
        abandoned = j.snapshot()["last_attempt"]
        assert abandoned["state"] == "completed"
        assert abandoned["terminal_code"] == "abandoned_before_start"
        assert abandoned["started_at"] is None
        assert case["transport"].calls == []
        # Same request ID stays replay-only: it cannot silently run later.
        replay = _invoke(j, case)
        assert replay["decision"] == "replayed"
        assert replay["receipt"] == abandoned
        assert case["guard"].entered == 0
        assert case["transport"].calls == []
        # A new explicit request can proceed without restarting the service.
        next_request = B3RunRequest("d" * 32, BINDING, REVISION)
        result = _invoke(j, case, request=next_request)
        assert result["decision"] == "accepted"
        assert result["writer_readback"] == "two_fresh_gets_match_receipt_hashes"
        assert case["guard"].entered == 1
        assert case["transport"]._puts == 2
    with B3AttemptJournal(case["root"], initialize=False) as j:
        assert j.lookup(abandoned["run_id"]) == abandoned
        assert j.snapshot()["last_attempt"]["terminal_code"] == "writer_readback_observed"


def test_invalid_guard_context_without_any_start_is_abandoned(setup_job):
    case = setup_job
    with B3AttemptJournal(case["root"], initialize=True) as j:
        with pytest.raises(JobNotAvailable, match="writer_not_started"):
            _invoke(j, case, writer_guard=lambda: None)
        assert j.snapshot()["last_attempt"]["terminal_code"] == "abandoned_before_start"
        assert case["transport"].calls == []


def test_baseexception_during_guard_acquisition_is_recovered_only_on_restart(
    setup_job,
):
    case = setup_job

    @contextmanager
    def interrupted():
        raise SimulatedCrash()
        yield

    with B3AttemptJournal(case["root"], initialize=True) as j:
        with pytest.raises(SimulatedCrash):
            _invoke(j, case, writer_guard=interrupted)
        assert j.snapshot()["last_attempt"]["state"] == "queued"
        assert case["transport"].calls == []
    with B3AttemptJournal(case["root"], initialize=False) as j:
        assert j.snapshot()["last_attempt"]["terminal_code"] == "abandoned_before_start"


def test_server_configuration_never_exposes_password_in_object_repr(setup_job):
    case = setup_job
    assert "private-test-secret" not in repr(case["config"])
    assert "private-test-secret" not in json.dumps(
        {"version": "github-b3-producer-job/v1"},
    )



def test_guard_release_failure_revokes_committed_proof_and_blocks_retry(
    setup_job,
):
    case = setup_job

    @contextmanager
    def guard_release_broken():
        yield
        raise RuntimeError("shared guard release failed")

    with B3AttemptJournal(case["root"], initialize=True) as j:
        with pytest.raises(JobNotAvailable, match="writer_effect_unknown"):
            _invoke(j, case, writer_guard=guard_release_broken)
        held = j.snapshot()["last_unconfirmed"]
        assert held["state"] == "unconfirmed"
        assert held["terminal_code"] == "effect_unknown"
        assert j.snapshot()["last_writer_readback"] is None
        assert j.writer_proof(held["run_id"]) is None
        assert case["transport"]._puts == 2
        assert j.snapshot()["last_verified_pipeline_success"] is None
        with pytest.raises(Busy, match="unconfirmed_effect"):
            _invoke(
                j, case,
                request=B3RunRequest("e" * 32, BINDING, REVISION),
            )
    with B3AttemptJournal(case["root"], initialize=False) as j:
        assert j.snapshot()["last_unconfirmed"]["terminal_code"] == "effect_unknown"
        assert j.snapshot()["last_writer_readback"] is None



def test_baseexception_on_writer_guard_exit_never_publishes_proof(
    setup_job,
):
    case = setup_job

    @contextmanager
    def keyboard_interrupt_during_release():
        yield
        raise KeyboardInterrupt()

    with B3AttemptJournal(case["root"], initialize=True) as j:
        with pytest.raises(KeyboardInterrupt):
            _invoke(j, case, writer_guard=keyboard_interrupt_during_release)
        running = j.snapshot()["last_attempt"]
        assert running["state"] == "running"
        assert j.writer_proof(running["run_id"]) is None
        assert j.snapshot()["last_writer_readback"] is None

    with B3AttemptJournal(case["root"], initialize=False) as j:
        assert j.snapshot()["last_unconfirmed"]["terminal_code"] == (
            "interrupted_unknown"
        )
        assert j.snapshot()["last_writer_readback"] is None
        with pytest.raises(Busy, match="unconfirmed_effect"):
            _invoke(
                j, case,
                request=B3RunRequest("f" * 32, BINDING, REVISION),
            )
