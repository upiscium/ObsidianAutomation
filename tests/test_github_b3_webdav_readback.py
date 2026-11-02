"""Existing real CAS Writer results + fresh read-only WebDAV GET correlation.

All network I/O here is an in-memory test transport. The source module does
not invoke PUT, start a job, persist proof or grant a WebUI action.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import replace

import pytest

from obsidian_automation.core_promotion_transport import HTTPResponse
from obsidian_automation.github_b3_attempts import B3AttemptJournal
from obsidian_automation.github_b3_readback_candidate import RunContext
from obsidian_automation.github_b3_webdav_readback import (
    MAX_CONTENT_BYTES,
    ReadbackUnavailable,
    VERSION,
    observe_b3_writer_pair,
)
from obsidian_automation.github_project_overview import (
    OverviewItem,
    apply_project_overview,
    make_overview_proposal,
)
from obsidian_automation.github_project_status_mutation import (
    apply_project_status,
    parse_watcher_proposal,
)

BASE_URL = "https://nextcloud.example/remote.php/dav/files/writer/ObsidianVault"
PROJECT_PATH = "10-Project/Terreate/Terreate.md"
STATUS_PATH = "10-Project/Terreate/Status.md"
REPOSITORY = "upiscium/Terreate"


def _watcher_proposal():
    return parse_watcher_proposal(json.dumps({
        "change": True,
        "current_status": "running",
        "event": "project-status-observation",
        "latest_commit_at": "2026-08-13T04:11:38Z",
        "latest_commit_sha": "0d022ed5140b497b5d3be886408822ecbd981dd3",
        "observed_at": "2026-09-17T03:39:13Z",
        "open_issues": [203],
        "open_prs": [42],
        "pending": True,
        "project": PROJECT_PATH,
        "proposed_status": "planning",
        "reason": "no commit within 7 days",
        "repository": REPOSITORY,
    }, ensure_ascii=False).encode())


def _overview_proposal():
    return make_overview_proposal(
        project_path=PROJECT_PATH,
        repository=REPOSITORY,
        issues=[OverviewItem(203, "Test issue")],
        pull_requests=[OverviewItem(42, "Test PR")],
    )


def _project(status="running"):
    return (
        "---\n"
        "type: project\n"
        "workspace:\n"
        f"status: {status}\n"
        "priority:\n"
        f"github_repo: {REPOSITORY}\n"
        "github_watch: true\n"
        "---\n"
        "# Project Summary\n"
        "keep user notes\n"
    ).encode()


class WebDAV:
    """In-memory HTTP transport with a conditional real-module PUT contract."""

    def __init__(self):
        self.project = _project()
        self.overview = None
        self.etags = {"project": '"p1"', "overview": '"s1"'}
        self.calls = []
        self.fail_get = None
        self.bad_body = None
        self.raise_on_get = False

    def __call__(
        self, *, method, target_url, username, password, headers,
        body, timeout, response_limit,
    ):
        assert username == "writer" and password == "secret"
        assert target_url.startswith(BASE_URL + "/")
        assert timeout > 0
        self.calls.append((method, target_url, body))
        stage = (
            "project" if target_url.endswith("/" + PROJECT_PATH)
            else "overview" if target_url.endswith("/" + STATUS_PATH)
            else None
        )
        if stage is None:
            raise AssertionError("unexpected target")
        if method == "GET":
            if self.raise_on_get:
                raise TimeoutError("hidden URL and token must not escape")
            if self.fail_get == stage:
                return HTTPResponse(503, b"unavailable", None)
            data = self.project if stage == "project" else self.overview
            if self.bad_body is not None and stage == self.bad_body[0]:
                data = self.bad_body[1]
            return HTTPResponse(
                200 if data is not None else 404,
                data if data is not None else b"",
                self.etags[stage] if data is not None else None,
            )
        if method == "PUT":
            h = headers or {}
            data = self.project if stage == "project" else self.overview
            if data is None:
                if h.get("If-None-Match") != "*":
                    return HTTPResponse(412, b"", None)
            elif h.get("If-Match") != self.etags[stage]:
                return HTTPResponse(412, b"", self.etags[stage])
            assert body is not None and type(body) is bytes
            if stage == "project":
                self.project = body
            else:
                self.overview = body
            self.etags[stage] = '"version2"'
            return HTTPResponse(204, b"", self.etags[stage])
        raise AssertionError("must be GET or conditional PUT")


@pytest.fixture
def setup_pair():
    remote = WebDAV()
    status_proposal = _watcher_proposal()
    overview_proposal = _overview_proposal()
    began = time.time() - 10
    status_result = apply_project_status(
        status_proposal, base_url=BASE_URL, username="writer", password="secret",
        transport=remote,
    )
    overview_result = apply_project_overview(
        overview_proposal, base_url=BASE_URL, username="writer", password="secret",
        transport=remote,
    )
    context = RunContext(
        "a" * 32, "b" * 32, "c" * 32, began, state="running",
    )
    return {
        "context": context,
        "status_proposal": status_proposal,
        "overview_proposal": overview_proposal,
        "status_result": status_result,
        "overview_result": overview_result,
        "remote": remote,
    }


def _verify(inputs, **changes):
    params = {key: value for key, value in inputs.items() if key != "remote"}
    return observe_b3_writer_pair(
        **(params | changes),
        base_url=BASE_URL, registered_base_url=BASE_URL,
        username="writer", password="secret",
        transport=inputs["remote"],
        now=time.time(),
    )


def test_reuses_existing_real_cas_writer_results_with_two_new_gets(setup_pair):
    p = setup_pair
    assert p["status_result"].outcome == "applied"
    assert p["overview_result"].outcome == "created"
    before = len(p["remote"].calls)
    got = _verify(p)
    assert got["schema_version"] == VERSION
    assert got["writer_readback"] == "two_fresh_gets_match_receipt_hashes"
    assert got["candidate_outcomes"] == {"status": "applied", "overview": "created"}
    assert got["candidate_changed"] is True
    assert got["last_verified_pipeline_success"] is None
    assert got["overall_health"] == "unknown"
    assert got["active_incident"] == "unknown"
    assert len(p["remote"].calls) == before + 2
    assert [call[0] for call in p["remote"].calls[before:]] == ["GET", "GET"]
    assert {call[1].removeprefix(BASE_URL + "/") for call in p["remote"].calls[before:]} == {
        PROJECT_PATH, STATUS_PATH,
    }
    assert all(call[2] is None for call in p["remote"].calls[before:])
    assert len(json.dumps(got)) < 500


def test_readback_is_read_only_even_when_writers_were_already_applied(setup_pair):
    p = setup_pair
    prev_project, prev_overview = p["remote"].project, p["remote"].overview
    before_calls = len(p["remote"].calls)
    _verify(p)
    _verify(p)
    assert p["remote"].project == prev_project
    assert p["remote"].overview == prev_overview
    assert all(call[0] == "GET" for call in p["remote"].calls[before_calls:])


def test_noop_outcomes_are_fresh_digest_matched_but_not_pipeline_success(setup_pair):
    p = setup_pair
    old_status = p["status_proposal"]
    old_overview = p["overview_proposal"]
    p["status_result"] = apply_project_status(
        old_status, base_url=BASE_URL, username="writer", password="secret",
        transport=p["remote"],
    )
    p["overview_result"] = apply_project_overview(
        old_overview, base_url=BASE_URL, username="writer", password="secret",
        transport=p["remote"],
    )
    assert p["status_result"].outcome == "already_desired"
    assert p["overview_result"].outcome == "already_desired"
    x = _verify(p)
    assert x["candidate_changed"] is False
    assert x["last_verified_pipeline_success"] is None


@pytest.mark.parametrize("stage", ("project", "overview"))
def test_owner_changes_after_writer_do_not_count_as_verified_readback(setup_pair, stage):
    p = setup_pair
    if stage == "project":
        p["remote"].project += b"\nHuman edited after writer\n"
    else:
        p["remote"].overview += b"\nHuman notes change\n"
    original = (p["remote"].project, p["remote"].overview)
    with pytest.raises(ReadbackUnavailable, match="remote_readback_diverged"):
        _verify(p)
    assert (p["remote"].project, p["remote"].overview) == original
    assert p["remote"].calls[-1][0] == "GET"


@pytest.mark.parametrize("stage", ("project", "overview"))
def test_http_unavailable_fails_closed_without_exposing_response(setup_pair, stage):
    p = setup_pair
    p["remote"].fail_get = stage
    with pytest.raises(ReadbackUnavailable, match="remote_readback_unavailable"):
        _verify(p)


def test_network_exception_is_sanitized_without_retry_or_put(setup_pair):
    p = setup_pair
    p["remote"].raise_on_get = True
    calls_before = len(p["remote"].calls)
    with pytest.raises(ReadbackUnavailable) as e:
        _verify(p)
    assert e.value.reason == "remote_readback_unavailable"
    assert "token" not in str(e.value) and "https" not in str(e.value)
    assert len(p["remote"].calls) == calls_before + 1


@pytest.mark.parametrize("bad", (bytearray(b"bad"), b"x" * (MAX_CONTENT_BYTES + 1), b""))
def test_bad_or_oversized_webdav_bytes_fail_closed(setup_pair, bad):
    p = setup_pair
    p["remote"].bad_body = ("project", bad)
    with pytest.raises(ReadbackUnavailable, match="remote_readback_unavailable"):
        _verify(p)


@pytest.mark.parametrize("part,field,override", (
    ("status_proposal", "sha256", "0" * 64),
    ("status_proposal", "project_path", "10-Project/Other/Other.md"),
    ("overview_proposal", "project_path", "10-Project/Other/Other.md"),
    ("overview_proposal", "repository", "upiscium/Other"),
    ("status_proposal", "canonical_bytes", b"not JSON"),
    ("overview_proposal", "canonical_bytes", b"{}"),
    ("overview_proposal", "canonical_bytes", b"x" * (MAX_CONTENT_BYTES + 1)),
))
def test_forged_or_mismatched_proposal_refused_before_http(setup_pair, part, field, override):
    p = setup_pair
    before = len(p["remote"].calls)
    forged = replace(p[part], **{field: override})
    with pytest.raises(ReadbackUnavailable, match="invalid_proposal"):
        _verify(p, **{part: forged})
    assert len(p["remote"].calls) == before


@pytest.mark.parametrize("part,field,override", (
    ("status_result", "after_content_sha256", "f" * 64),
    ("status_result", "proposal_sha256", "f" * 64),
    ("status_result", "project_path", "10-Project/Other/Other.md"),
    ("status_result", "desired_status", "done"),
    ("overview_result", "proposal_sha256", "f" * 64),
    ("overview_result", "status_path", "10-Project/Other/Status.md"),
    ("overview_result", "repository", "upiscium/Other"),
    ("overview_result", "before_content_sha256", "bad"),
))
def test_forged_writer_result_cannot_authorize_other_scope(setup_pair, part, field, override):
    p = setup_pair
    before = len(p["remote"].calls)
    forged = replace(p[part], **{field: override})
    if field == "after_content_sha256":
        expected = "remote_readback_diverged"
    else:
        expected = "invalid_writer_receipt"
    with pytest.raises(ReadbackUnavailable, match=expected):
        _verify(p, **{part: forged})
    if expected == "invalid_writer_receipt":
        assert len(p["remote"].calls) == before


@pytest.mark.parametrize("change", (
    {"base_url": "http://nextcloud.example/writer"},
    {"base_url": "https://token@nextcloud.example/writer"},
    {"base_url": "https://nextcloud.example/writer?path=other"},
    {"username": ""},
    {"password": ""},
    {"timeout": float("nan")},
    {"timeout": True},
    {"timeout": 121},
    {"now": float("inf")},
))
def test_bad_connection_rejected_before_network(setup_pair, change):
    p = setup_pair
    before = len(p["remote"].calls)
    params = {k: v for k, v in p.items() if k != "remote"}
    params.update({
        "base_url": BASE_URL, "registered_base_url": BASE_URL,
        "username": "writer", "password": "secret",
        "transport": p["remote"], "now": time.time(),
    })
    params.update(change)
    with pytest.raises(ReadbackUnavailable):
        observe_b3_writer_pair(**params)
    assert len(p["remote"].calls) == before


@pytest.mark.parametrize("bad", (
    {"run_id": "not-hex"},
    {"binding_ref": "x" * 32},
    {"expected_revision": "G" * 32},
    {"state": "queued"},
    {"started_at": float("nan")},
))
def test_bad_run_context_is_refused_before_network(setup_pair, bad):
    p = setup_pair
    before = len(p["remote"].calls)
    with pytest.raises(ReadbackUnavailable, match="invalid_context"):
        _verify(p, context=replace(p["context"], **bad))
    assert len(p["remote"].calls) == before


def test_status_and_overview_times_must_follow_admission(setup_pair):
    p = setup_pair
    forged = replace(
        p["status_result"], completed_at="2000-01-01T00:00:00Z",
    )
    with pytest.raises(ReadbackUnavailable, match="invalid_writer_receipt"):
        _verify(p, status_result=forged)


def test_private_temporary_v2_run_context_remains_unmodified(tmp_path, setup_pair):
    p = setup_pair
    root = tmp_path / "b3-private"
    root.mkdir(mode=0o700)
    with B3AttemptJournal(
        root, clock=lambda: p["context"].started_at, initialize=True,
    ) as journal:
        _, entry = journal.admit("a" * 32, "b" * 32, "c" * 32)
        journal.start(entry["run_id"])
        ctx = RunContext(
            entry["run_id"], "b" * 32, "c" * 32,
            journal.lookup(entry["run_id"])["started_at"],
        )
        observation = _verify(p, context=ctx)
        assert observation["writer_readback"] == "two_fresh_gets_match_receipt_hashes"
        assert observation["last_verified_pipeline_success"] is None
        assert journal.lookup(entry["run_id"])["state"] == "running"
        assert journal.snapshot()["last_verified_pipeline_success"] is None



class _SpoofEq:
    def __eq__(self, _other):
        return True

    def __ne__(self, _other):
        return False


class _ExplosiveCompare:
    def __ne__(self, _other):
        raise AssertionError("custom comparator was called")


@pytest.mark.parametrize("part,field", (
    ("status_result", "proposal_sha256"),
    ("status_result", "project_path"),
    ("status_result", "repository"),
    ("status_result", "desired_status"),
    ("overview_result", "status_path"),
    ("overview_result", "repository"),
))
def test_custom_identity_comparisons_cannot_spoof_writer_receipts(
    setup_pair, part, field,
):
    p = setup_pair
    original = len(p["remote"].calls)
    for value in (_SpoofEq(), _ExplosiveCompare()):
        with pytest.raises(ReadbackUnavailable, match="invalid_writer_receipt"):
            _verify(p, **{part: replace(p[part], **{field: value})})
    assert len(p["remote"].calls) == original


@pytest.mark.parametrize("part,field", (
    ("status_proposal", "project_path"),
    ("status_proposal", "repository"),
    ("status_proposal", "expected_status"),
    ("overview_proposal", "project_path"),
    ("overview_proposal", "repository"),
))
def test_custom_proposal_fields_never_redirect_observation(
    setup_pair, part, field,
):
    p = setup_pair
    original = len(p["remote"].calls)
    for value in (_SpoofEq(), _ExplosiveCompare()):
        with pytest.raises(ReadbackUnavailable, match="invalid_proposal"):
            _verify(p, **{part: replace(p[part], **{field: value})})
    assert len(p["remote"].calls) == original


def test_default_transport_is_real_request_entrypoint_when_attached(
    monkeypatch, setup_pair,
):
    from obsidian_automation import github_b3_webdav_readback as reader

    p = setup_pair
    p["remote"].calls.clear()
    monkeypatch.setattr(reader, "_real_http_request", p["remote"])
    values = {name: value for name, value in p.items() if name != "remote"}
    output = observe_b3_writer_pair(
        **values, base_url=BASE_URL, registered_base_url=BASE_URL,
        username="writer", password="secret", now=time.time(),
    )
    assert output["writer_readback"] == "two_fresh_gets_match_receipt_hashes"
    assert [call[0] for call in p["remote"].calls] == ["GET", "GET"]


@pytest.mark.parametrize("field,invalid", (
    ("completed_at", "nonsense"),
    ("completed_at", "2026-01-01"),
    ("completed_at", True),
    ("before_content_sha256", True),
    ("after_content_sha256", _SpoofEq()),
))
def test_malformed_private_result_fields_fail_before_http(
    setup_pair, field, invalid,
):
    p = setup_pair
    n = len(p["remote"].calls)
    with pytest.raises(ReadbackUnavailable, match="invalid_writer_receipt"):
        _verify(p, status_result=replace(p["status_result"], **{field: invalid}))
    assert len(p["remote"].calls) == n



def test_gigantic_builtin_int_clock_is_fixed_invalid_context_not_overflow(setup_pair):
    p = setup_pair
    before = len(p["remote"].calls)
    values = {k: v for k, v in p.items() if k != "remote"}
    with pytest.raises(ReadbackUnavailable, match="invalid_context"):
        observe_b3_writer_pair(
            **values, base_url=BASE_URL, registered_base_url=BASE_URL,
            username="writer", password="secret",
            transport=p["remote"], now=10 ** 1000,
        )
    assert len(p["remote"].calls) == before


def test_gigantic_builtin_int_timeout_is_fixed_invalid_connection_not_overflow(setup_pair):
    p = setup_pair
    before = len(p["remote"].calls)
    values = {k: v for k, v in p.items() if k != "remote"}
    with pytest.raises(ReadbackUnavailable, match="invalid_connection"):
        observe_b3_writer_pair(
            **values, base_url=BASE_URL, registered_base_url=BASE_URL,
            username="writer", password="secret", timeout=10 ** 1000,
            transport=p["remote"], now=time.time(),
        )
    assert len(p["remote"].calls) == before


def test_first_get_failure_never_calls_second_target(setup_pair):
    p = setup_pair
    before = len(p["remote"].calls)
    p["remote"].fail_get = "project"
    with pytest.raises(ReadbackUnavailable, match="remote_readback_unavailable"):
        _verify(p)
    assert len(p["remote"].calls) == before + 1
    assert p["remote"].calls[-1][1].endswith("/" + PROJECT_PATH)



def test_unregistered_https_origin_refused_before_sending_credentials(setup_pair):
    p = setup_pair
    old_calls = len(p["remote"].calls)
    values = {k: v for k, v in p.items() if k != "remote"}
    with pytest.raises(ReadbackUnavailable, match="unapproved_connection"):
        observe_b3_writer_pair(
            **values,
            base_url="https://unapproved.example/remote.php/dav/files/other/ObsidianVault",
            registered_base_url=BASE_URL,
            username="writer",
            password="secret",
            transport=p["remote"],
            now=time.time(),
        )
    assert len(p["remote"].calls) == old_calls


def test_final_candidate_validation_error_is_sanitized(
    setup_pair, monkeypatch,
):
    from obsidian_automation import github_b3_webdav_readback as module
    from obsidian_automation.github_b3_readback_candidate import InvalidCandidate

    original = module.evaluate_b3_readbacks

    def simulate_corrupted_final_validation(context, evidence, *, now):
        if evidence:
            raise InvalidCandidate("private payload text should not escape")
        return original(context, evidence, now=now)

    monkeypatch.setattr(module, "evaluate_b3_readbacks", simulate_corrupted_final_validation)
    p = setup_pair
    values = {k: v for k, v in p.items() if k != "remote"}
    with pytest.raises(ReadbackUnavailable, match="inconsistent_writer_receipts") as err:
        observe_b3_writer_pair(
            **values,
            base_url=BASE_URL,
            registered_base_url=BASE_URL,
            username="writer",
            password="secret",
            transport=p["remote"],
            now=time.time(),
        )
    assert "private payload text" not in str(err.value)
