"""R3B2B1 pure request-bound producer inputs; no real host or provider."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path

import pytest

from obsidian_automation.github_b3_job import B3JobConfig, JobNotAvailable
from obsidian_automation.github_b3_request_binding import (
    B3BindingUnavailable, B3PreparedProposalPair, B3RegisteredScope,
    bind_b3_registered_inputs, trusted_b3_registered_provider,
)
from obsidian_automation.github_b3_trusted_port import (
    B3CutoverObservation, B3TrustedJobPort,
)
from obsidian_automation.github_b3_attempts import B3AttemptJournal
from obsidian_automation.github_project_overview import (
    OverviewItem, make_overview_proposal,
)
from obsidian_automation.github_project_status_mutation import (
    parse_watcher_proposal,
)
from obsidian_automation.webui_b3_adapter import ProjectSyncRequest
from test_github_b3_trusted_port import MemoryDav, Guard

BASE = "https://nextcloud.example/remote.php/dav/files/owner/ObsidianVault"
PROJECT = "10-Project/Terreate/Terreate.md"
REPO = "upiscium/Terreate"
BINDING, REVISION, REQUEST = "a"*32, "b"*32, "d"*32
GENERATION, RECORD = "e"*32, "f"*32
NOW = 1800000000.0


@pytest.fixture
def records():
    config = B3JobConfig(
        binding_ref=BINDING, revision=REVISION,
        project_path=PROJECT, repository=REPO,
        registered_base_url=BASE, username="writer",
        password="synthetic-secret",
    )
    status = parse_watcher_proposal(json.dumps({
        "change": True, "current_status": "running",
        "event": "project-status-observation",
        "latest_commit_at": "2026-08-13T04:11:38Z",
        "latest_commit_sha": "0d022ed5140b497b5d3be886408822ecbd981dd3",
        "observed_at": datetime.fromtimestamp(
            NOW - 5, timezone.utc,
        ).isoformat().replace("+00:00", "Z"),
        "open_issues": [203], "open_prs": [42],
        "pending": True, "project": PROJECT,
        "proposed_status": "planning",
        "reason": "no commit within 7 days", "repository": REPO,
    }).encode())
    overview = make_overview_proposal(
        project_path=PROJECT, repository=REPO,
        issues=[OverviewItem(203, "Synthetic issue")],
        pull_requests=[OverviewItem(42, "Synthetic PR")],
    )
    pair = B3PreparedProposalPair(
        request_id=REQUEST, binding_ref=BINDING,
        expected_revision=REVISION, source_generation=GENERATION,
        source_record_id=RECORD, prepared_at=NOW - 1,
        status_bytes=status.canonical_bytes,
        overview_bytes=overview.canonical_bytes,
        status_sha256=hashlib.sha256(status.canonical_bytes).hexdigest(),
        overview_sha256=hashlib.sha256(overview.canonical_bytes).hexdigest(),
    )
    return B3RegisteredScope(config, GENERATION), pair, ProjectSyncRequest(
        REQUEST, REVISION, BINDING,
    )


def bind(items, *, now=NOW):
    scope, pair, request = items
    return bind_b3_registered_inputs(scope, pair, request, now=now)


def test_exact_bound_proposals_return_private_registered_inputs(records):
    result = bind(records)
    assert result.config is records[0].config
    assert result.request_id == REQUEST
    assert result.status_proposal.project_path == PROJECT
    assert result.overview_proposal.project_path == PROJECT
    assert result.status_proposal.repository == REPO
    assert result.overview_proposal.repository == REPO
    assert "synthetic-secret" not in repr(result.config)
    assert result.status_proposal.sha256 == records[1].status_sha256
    assert result.overview_proposal.sha256 == records[1].overview_sha256


@pytest.mark.parametrize("field,bad", (
    ("request_id", "c"*32),
    ("request_id", 1),
    ("binding_ref", "c"*32),
    ("expected_revision", "c"*32),
    ("source_generation", "c"*32),
    ("source_record_id", "z"*32),
    ("source_record_id", None),
    ("prepared_at", NOW + 1),
    ("prepared_at", NOW - 301),
    ("prepared_at", float("nan")),
    ("prepared_at", True),
    ("prepared_at", 10**1000),
    ("status_sha256", "0"*64),
    ("overview_sha256", "0"*64),
    ("status_bytes", b'{"wrong": true}'),
    ("status_bytes", bytearray(b"fake")),
    ("overview_bytes", b'{"wrong": true}'),
    ("overview_bytes", b""),
    ("overview_bytes", b"x" * (4*1024*1024+1)),
))
def test_untrusted_or_stale_producer_record_fails_closed(records, field, bad):
    scope, pair, request = records
    with pytest.raises(B3BindingUnavailable, match="registration_unavailable"):
        bind((scope, replace(pair, **{field: bad}), request))


@pytest.mark.parametrize("bad", (
    float("nan"), float("inf"), -1, True, None, 10**1000,
    NOW - 10, NOW + 301,
))
def test_invalid_clock_and_stale_age_never_bind(records, bad):
    with pytest.raises(B3BindingUnavailable, match="registration_unavailable"):
        bind(records, now=bad)


def test_one_request_cannot_reuse_another_producer_record(records):
    scope, pair, request = records
    for altered in (
        replace(request, requestId="c"*32),
        replace(request, expectedRevision="c"*32),
        replace(request, bindingRef="c"*32),
    ):
        with pytest.raises(B3BindingUnavailable):
            bind((scope,pair,altered))


def test_stale_canonical_watcher_cannot_be_disguised_as_fresh_envelope(
    records,
):
    scope, pair, request = records
    original = json.loads(pair.status_bytes.decode("utf-8"))
    original["observed_at"] = "2026-09-17T03:39:13Z"
    raw = json.dumps(original, sort_keys=True).encode()
    forged = replace(
        pair, status_bytes=raw,
        status_sha256=hashlib.sha256(raw).hexdigest(),
    )
    with pytest.raises(B3BindingUnavailable):
        bind((scope, forged, request))


def test_registration_generation_rotation_requires_matching_new_producer(records):
    scope, pair, request = records
    scope = replace(scope, source_generation="c"*32)
    with pytest.raises(B3BindingUnavailable):
        bind((scope,pair,request))


@pytest.mark.parametrize("change", (
    {"binding_ref": "c"*32},
    {"revision": "c"*32},
    {"project_path": "10-Project/Other/Other.md"},
    {"repository": "upiscium/Other"},
))
def test_registration_mismatch_cannot_select_another_project(records, change):
    scope,pair,request=records
    config=replace(scope.config, **change)
    with pytest.raises(B3BindingUnavailable):
        bind((replace(scope,config=config),pair,request))


def test_canonical_parsers_reject_cross_project_pair_even_if_hash_matches(records):
    scope,pair,request=records
    other = make_overview_proposal(
        project_path="10-Project/Other/Other.md", repository=REPO,
        issues=[], pull_requests=[],
    )
    bad=replace(
        pair, overview_bytes=other.canonical_bytes,
        overview_sha256=hashlib.sha256(other.canonical_bytes).hexdigest(),
    )
    with pytest.raises(B3BindingUnavailable):
        bind((scope,bad,request))


def test_no_secrets_or_private_payload_in_error_or_repr(records):
    scope,pair,request=records
    forged=replace(pair,status_bytes=b"untrusted /etc/very-private-secret")
    with pytest.raises(B3BindingUnavailable) as exc:
        bind((scope,forged,request))
    assert "very-private" not in str(exc.value)
    assert "synthetic-secret" not in repr(scope)


def test_trusted_provider_rechecks_scope_and_pair_for_each_request(records):
    scope,pair,request=records
    calls=[]
    owner_scope=scope
    def read_scope():
        calls.append("scope")
        return owner_scope
    def read_pair(req):
        calls.append(("pair", req.requestId))
        return pair
    provider=trusted_b3_registered_provider(read_scope,read_pair,clock=lambda: NOW)
    a=provider(request)
    assert a.request_id==REQUEST
    assert calls==["scope",("pair",REQUEST)]
    with pytest.raises(B3BindingUnavailable):
        provider(replace(request,requestId="c"*32))


def test_broken_provider_leaks_no_private_exception(records):
    scope,pair,request=records
    def broken(_):
        raise RuntimeError("private nextcloud creds at /etc/private")
    provider=trusted_b3_registered_provider(
        lambda:scope,broken,clock=lambda:NOW,
    )
    with pytest.raises(B3BindingUnavailable) as exc:
        provider(request)
    assert "nextcloud" not in str(exc.value)
    assert "private" not in str(exc.value)


def test_reject_noncallable_registration_providers():
    with pytest.raises(TypeError,match="invalid_registered_source"):
        trusted_b3_registered_provider(None,None,clock=None)


def test_producer_record_rotation_under_guard_abandons_before_write(
    records, tmp_path: Path,
):
    scope, pair, request = records
    current_pair = pair
    def produce(_request):
        return current_pair
    provider = trusted_b3_registered_provider(
        lambda: scope, produce, clock=lambda: NOW,
    )
    def rotate():
        nonlocal current_pair
        current_pair = replace(
            current_pair, source_record_id="c" * 32,
        )
    guard = Guard(before_entry=rotate)
    dav = MemoryDav()
    state = tmp_path / "private-b3"
    state.mkdir(mode=0o700)
    with B3AttemptJournal(state, initialize=True) as journal:
        port = B3TrustedJobPort(
            journal, registered_inputs=provider,
            cutover_observation=lambda: B3CutoverObservation(
                record_id="c" * 32, binding_ref=BINDING,
                revision=REVISION, operator_approved=True,
                old_writer_exclusion="host_isolated",
            ),
            shared_writer_guard=guard,
        )
        with pytest.raises(JobNotAvailable, match="writer_not_started"):
            port.sync_project(request, transport=dav)
        assert journal.snapshot()["last_attempt"]["terminal_code"] == (
            "abandoned_before_start"
        )
        assert dav.calls == []
        assert guard.entered == 1
        # Old request must remain replay-only, even after the registered
        # generation is restored. The source alone cannot replay effects.
        current_pair = pair
        guard.before_entry = None
        result = port.sync_project(request, transport=dav)
        assert result["decision"] == "replayed"
        assert dav.calls == []


def test_registered_scope_rotation_under_guard_abandons_before_write(
    records, tmp_path: Path,
):
    scope, pair, request = records
    current_scope = scope
    provider = trusted_b3_registered_provider(
        lambda: current_scope, lambda _: pair, clock=lambda: NOW,
    )
    def rotate():
        nonlocal current_scope
        current_scope = replace(
            current_scope,
            config=replace(current_scope.config, password="different-secret"),
        )
    guard = Guard(before_entry=rotate)
    state = tmp_path / "private-b3"
    state.mkdir(mode=0o700)
    dav = MemoryDav()
    with B3AttemptJournal(state, initialize=True) as journal:
        port = B3TrustedJobPort(
            journal, registered_inputs=provider,
            cutover_observation=lambda: B3CutoverObservation(
                record_id="c" * 32, binding_ref=BINDING,
                revision=REVISION, operator_approved=True,
                old_writer_exclusion="host_isolated",
            ),
            shared_writer_guard=guard,
        )
        with pytest.raises(JobNotAvailable, match="writer_not_started"):
            port.sync_project(request, transport=dav)
        assert journal.snapshot()["last_attempt"]["terminal_code"] == (
            "abandoned_before_start"
        )
        assert dav.calls == []


def test_existing_unattached_job_can_consume_a_request_bound_pair(
    records,tmp_path:Path,
):
    scope,pair,request=records
    provider=trusted_b3_registered_provider(
        lambda:scope,lambda req:pair,clock=lambda:NOW,
    )
    dav=MemoryDav()
    guard=Guard()
    state=tmp_path/"private-b3"
    state.mkdir(mode=0o700)
    with B3AttemptJournal(state,initialize=True) as journal:
        port=B3TrustedJobPort(
            journal,registered_inputs=provider,
            cutover_observation=lambda:B3CutoverObservation(
                record_id="c"*32,binding_ref=BINDING,revision=REVISION,
                operator_approved=True,old_writer_exclusion="host_isolated",
            ),
            shared_writer_guard=guard,
        )
        # Synthetic cutover claim for a disposable fixture, NOT a real fence.
        accepted=port.sync_project(request,transport=dav)
        assert accepted["decision"]=="accepted"
        assert accepted["writer_readback"]=="two_fresh_gets_match_receipt_hashes"
        assert accepted["last_verified_pipeline_success"] is None
        assert dav.put_count == 2
        assert guard.entered == 1
        assert port.sync_project(request,transport=dav)["decision"]=="replayed"
        assert dav.put_count == 2
