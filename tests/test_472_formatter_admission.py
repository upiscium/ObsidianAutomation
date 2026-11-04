"""#472 source-only R1: scheduled Writer admission and safe status vocabulary.

All cases use disposable local plans / Python fakes. No credentials or network.
"""
from __future__ import annotations

import threading
import time
import uuid

import pytest

from obsidian_automation.vault_formatter import create_plan
from obsidian_automation.vault_format_job import FormatterJob


def prepared_plan(tmp_path, *, count=1):
    root = tmp_path / "synthetic-vault"
    target = root / "98-System" / "one.md"
    target.parent.mkdir(parents=True)
    raw = b"# Fixture\n\nfirst paragraph\n  \nsecond paragraph\n"
    target.write_bytes(raw)
    if count == 2:
        (target.parent / "two.md").write_bytes(raw)
    assert count in (1, 2)
    plan = create_plan(root)
    assert plan["counts"] == {"would_change": count}
    return plan


def writer_receipt(plan, state, codes):
    """Construct the actual Writer's metadata envelope without remote effects."""
    entries = sorted((x for x in plan["results"] if x["status"] == "would_change"),
                     key=lambda x: x["path"])
    assert len(entries) >= len(codes)
    changed = any(code in {"applied", "recovered"} for code in codes)
    return {
        "status": state, "plan_sha256": plan["plan_sha256"],
        "operation_id": "e" * 32, "receipt_sha256": "f" * 64,
        "vault_modified": (None if state == "indeterminate" else
                           False if state == "blocked" else changed),
        "outcomes": [
            {"path": item["path"],
             "before_sha256": item["before_sha256"],
             "after_sha256": item["after_sha256"],
             "result": code}
            for item, code in zip(entries, codes)
        ],
    }


def make_job(tmp_path, plan, *, runner=None, writer=None, generation=None):
    epoch = generation if generation is not None else ["a" * 64]
    job = FormatterJob(
        tmp_path / "formatter",
        runner or (lambda: plan),
        configured=lambda: epoch[0] is not None,
        source_generation=lambda: epoch[0],
        writer=writer,
    )
    return job, epoch


def scheduled(job):
    outcome, receipt = job.submit(uuid.uuid4().hex, trigger="scheduled")
    assert outcome == "accepted" and receipt
    return receipt["run_id"]


def finish(job, run_id):
    assert job._worker is not None
    job._worker.join(5)
    assert not job._worker.is_alive()
    record = job.run(run_id)
    assert record is not None
    return record


@pytest.mark.parametrize("writer_state, codes, status, reason", [
    ("blocked", ["source_changed"], "conflict", "writer_conflict"),
    ("blocked", ["cas_conflict"], "conflict", "writer_conflict"),
    ("blocked", ["weak_or_missing_etag"], "failed", "writer_preflight_or_remote_failed"),
    ("blocked", ["remote_unavailable"], "failed", "writer_preflight_or_remote_failed"),
    ("blocked", ["remote_missing_or_rejected"], "failed", "writer_preflight_or_remote_failed"),
    ("blocked", ["formatter_policy_mismatch"], "failed", "writer_preflight_or_remote_failed"),
    ("blocked", ["write_rejected"], "failed", "writer_preflight_or_remote_failed"),
    ("blocked", ["ready", "source_changed"], "conflict", "writer_conflict"),
    ("partial", ["applied", "cas_conflict"], "conflict", "writer_partial_conflict"),
    ("complete", ["applied"], "applied", None),
    ("complete", ["recovered"], "applied", None),
    ("complete", ["already_desired"], "no_changes", None),
    ("indeterminate", ["indeterminate"], "indeterminate", "remote_effect_unknown"),
])
def test_writer_fixed_outcomes_are_not_mislabeled(tmp_path, writer_state, codes, status, reason):
    plan = prepared_plan(tmp_path, count=len(codes))
    calls = []

    def writer(candidate):
        calls.append(candidate["plan_sha256"])
        return writer_receipt(candidate, writer_state, codes)

    job, _ = make_job(tmp_path, plan, writer=writer)
    with job:
        job.update({"mode": "auto-apply", "interval_seconds": 300})
        record = finish(job, scheduled(job))
        assert record["status"] == status
        assert record["reason_code"] == reason
        assert record["writer_outcome"] == writer_state
        assert calls == [plan["plan_sha256"]]


@pytest.mark.parametrize("writer_state, codes", [
    ("complete", ["cas_conflict"]),
    ("complete", ["source_changed"]),
    ("blocked", ["applied"]),
    ("blocked", ["ready"]),
    ("partial", ["cas_conflict"]),
    ("indeterminate", ["applied"]),
    ("blocked", ["invented-private-path"]),
])
def test_writer_malformed_receipt_is_not_reported_as_success(tmp_path, writer_state, codes):
    plan = prepared_plan(tmp_path, count=len(codes))
    writer = lambda candidate: writer_receipt(candidate, writer_state, codes)
    job, _ = make_job(tmp_path, plan, writer=writer)
    with job:
        job.update({"mode": "auto-apply", "interval_seconds": 300})
        record = finish(job, scheduled(job))
        assert record["status"] == "indeterminate"
        assert record["reason_code"] == "writer_result_invalid"
        assert record["writer_outcome"] is None


@pytest.mark.parametrize("revocation", [
    "off", "off_then_on", "dry_run", "interval_change",
    "connection_saved", "source_generation_changed",
])
def test_revoked_admission_during_long_scan_has_zero_writer_calls(tmp_path, revocation):
    plan = prepared_plan(tmp_path)
    started, release = threading.Event(), threading.Event()
    calls = []

    def scan():
        started.set()
        assert release.wait(5)
        return plan

    def writer(candidate):
        calls.append(candidate["plan_sha256"])
        return writer_receipt(candidate, "complete", ["applied"])

    job, generation = make_job(tmp_path, plan, runner=scan, writer=writer)
    with job:
        job.update({"mode": "auto-apply", "interval_seconds": 300})
        run_id = scheduled(job)
        assert started.wait(3)
        try:
            if revocation in ("off", "off_then_on"):
                job.update({"mode": "off", "interval_seconds": 300})
            if revocation == "off_then_on":
                job.update({"mode": "auto-apply", "interval_seconds": 300})
            elif revocation == "dry_run":
                job.update({"mode": "dry-run", "interval_seconds": 300})
            elif revocation == "interval_change":
                job.update({"mode": "auto-apply", "interval_seconds": 600})
            elif revocation == "connection_saved":
                job.connections_saved()
            elif revocation == "source_generation_changed":
                generation[0] = "b" * 64
        finally:
            release.set()
        record = finish(job, run_id)
        assert not calls
        assert record["status"] == "failed"
        assert record["reason_code"] == "writer_admission_revoked"
        # A canceled run is not silently resubmitted.
        assert record["run_mode"] == "auto-apply"
        assert job.db.execute("SELECT count(*) FROM runs").fetchone()[0] == 1


def test_off_acknowledged_only_after_inflight_writer_returns(tmp_path):
    plan = prepared_plan(tmp_path)
    active, release, off_returned = threading.Event(), threading.Event(), threading.Event()
    calls = []
    errors = []

    def writer(candidate):
        active.set()
        assert release.wait(5)
        calls.append(candidate["plan_sha256"])
        return writer_receipt(candidate, "complete", ["applied"])

    job, _ = make_job(tmp_path, plan, writer=writer)
    with job:
        job.update({"mode": "auto-apply", "interval_seconds": 300})
        run_id = scheduled(job)
        assert active.wait(3)

        def stop():
            try:
                job.update({"mode": "off", "interval_seconds": 300})
            except Exception as exc:
                errors.append(type(exc).__name__)
            finally:
                off_returned.set()

        stopper = threading.Thread(target=stop)
        stopper.start()
        try:
            assert not off_returned.wait(0.1)
            # Read-only status must remain reachable during Writer I/O.
            assert job.status()["last_run"]["status"] == "running"
        finally:
            release.set()
            stopper.join(4)
        assert off_returned.is_set() and not errors
        assert job.settings()["mode"] == "off"
        assert finish(job, run_id)["status"] == "applied"
        assert calls == [plan["plan_sha256"]]
        assert job.submit(uuid.uuid4().hex, "scheduled")[0] == "disabled"


def test_manual_run_remains_readonly_with_writer_present(tmp_path):
    plan = prepared_plan(tmp_path)
    calls = []
    job, _ = make_job(tmp_path, plan, writer=lambda _: calls.append(True))
    with job:
        job.update({"mode": "auto-apply", "interval_seconds": 300})
        outcome, receipt = job.submit(uuid.uuid4().hex)
        assert outcome == "accepted"
        record = finish(job, receipt["run_id"])
        assert record["run_mode"] == "dry-run"
        assert record["status"] == "would_change"
        assert calls == []


@pytest.mark.parametrize("mutation", [
    "missing_operation", "invalid_operation", "missing_receipt", "invalid_receipt",
    "missing_path", "wrong_path", "wrong_before_sha", "wrong_after_sha",
    "wrong_plan", "missing_vault_modified", "vault_modified_false",
    "vault_modified_integer", "empty_outcomes", "nonstring_code",
])
def test_writer_incomplete_or_forged_receipt_cannot_report_applied(tmp_path, mutation):
    plan = prepared_plan(tmp_path)
    response = writer_receipt(plan, "complete", ["applied"])
    if mutation == "missing_operation":
        del response["operation_id"]
    elif mutation == "invalid_operation":
        response["operation_id"] = "not-an-operation-id"
    elif mutation == "missing_receipt":
        del response["receipt_sha256"]
    elif mutation == "invalid_receipt":
        response["receipt_sha256"] = "g" * 64
    elif mutation == "missing_path":
        del response["outcomes"][0]["path"]
    elif mutation == "wrong_path":
        response["outcomes"][0]["path"] = "98-System/unapproved.md"
    elif mutation == "wrong_before_sha":
        response["outcomes"][0]["before_sha256"] = "0" * 64
    elif mutation == "wrong_after_sha":
        response["outcomes"][0]["after_sha256"] = "0" * 64
    elif mutation == "wrong_plan":
        response["plan_sha256"] = "0" * 64
    elif mutation == "missing_vault_modified":
        del response["vault_modified"]
    elif mutation == "vault_modified_false":
        response["vault_modified"] = False
    elif mutation == "vault_modified_integer":
        response["vault_modified"] = 1
    elif mutation == "empty_outcomes":
        response["outcomes"] = []
    elif mutation == "nonstring_code":
        response["outcomes"][0]["result"] = 1

    job, _ = make_job(tmp_path, plan, writer=lambda _: response)
    with job:
        job.update({"mode": "auto-apply", "interval_seconds": 300})
        row = finish(job, scheduled(job))
        assert row["status"] == "indeterminate"
        assert row["reason_code"] == "writer_result_invalid"
        assert row["writer_outcome"] is None


def test_duplicate_outcome_path_does_not_claim_complete(tmp_path):
    plan = prepared_plan(tmp_path, count=2)
    response = writer_receipt(plan, "complete", ["applied", "already_desired"])
    response["outcomes"][1]["path"] = response["outcomes"][0]["path"]
    job, _ = make_job(tmp_path, plan, writer=lambda _: response)
    with job:
        job.update({"mode": "auto-apply", "interval_seconds": 300})
        row = finish(job, scheduled(job))
        assert row["status"] == "indeterminate"
        assert row["reason_code"] == "writer_result_invalid"


def test_job_real_cas_writer_receipt_then_already_desired_in_memory(tmp_path):
    """Exercise the actual CAS writer through the Job, not a summary stub."""
    from obsidian_automation.core_promotion_transport import HTTPResponse
    from obsidian_automation.vault_format_writer import apply_plan

    plan = prepared_plan(tmp_path)
    entry = next(item for item in plan["results"] if item["status"] == "would_change")
    before = (tmp_path / "synthetic-vault" / entry["path"]).read_bytes()
    remote = {"body": before, "etag": '"v0"', "puts": 0, "gets": 0}
    base = "https://synthetic.invalid/remote.php/dav/files/user/ObsidianVault"
    receipts = tmp_path / "writer-receipts"
    receipts.mkdir(mode=0o700)
    operations = iter(["a" * 32, "b" * 32])

    def dav(*, method, target_url, username, password, headers, body,
            timeout, response_limit):
        assert target_url.startswith(base + "/98-System/")
        assert username == "user" and password == "SYNTHETIC"
        if method == "GET":
            remote["gets"] += 1
            return HTTPResponse(status=200, body=remote["body"], etag=remote["etag"])
        assert method == "PUT"
        remote["puts"] += 1
        assert headers.get("If-Match") == remote["etag"]
        assert type(body) is bytes
        remote["body"] = body
        remote["etag"] = '"v1"'
        return HTTPResponse(status=204, body=b"", etag=remote["etag"])

    def writer(candidate):
        return apply_plan(candidate, selected_paths=(entry["path"],),
                          enabled=True, cas_server_verified=True,
                          receipt_dir=receipts, base_url=base,
                          username="user", password="SYNTHETIC",
                          transport=dav, operation_id=next(operations))

    job, _ = make_job(tmp_path, plan, writer=writer)
    with job:
        job.update({"mode": "auto-apply", "interval_seconds": 300})
        first = finish(job, scheduled(job))
        assert first["status"] == "applied"
        assert first["writer_outcome"] == "complete"
        assert remote["puts"] == 1
        second = finish(job, scheduled(job))
        assert second["status"] == "no_changes"
        assert second["writer_outcome"] == "complete"
        assert remote["puts"] == 1
        assert remote["gets"] >= 3
        assert len(list(receipts.glob("*.vault-formatter-receipt.json"))) >= 5
        assert len(list(receipts.glob("operation-*.marker"))) == 2


@pytest.mark.parametrize("remote_state, expected_status, expected_reason", [
    ("source_changed", "conflict", "writer_conflict"),
    ("weak_etag", "failed", "writer_preflight_or_remote_failed"),
])
def test_job_maps_actual_writer_preflight_refusal_without_put(
        tmp_path, remote_state, expected_status, expected_reason):
    from obsidian_automation.core_promotion_transport import HTTPResponse
    from obsidian_automation.vault_format_writer import apply_plan

    plan = prepared_plan(tmp_path)
    entry = next(item for item in plan["results"] if item["status"] == "would_change")
    before = (tmp_path / "synthetic-vault" / entry["path"]).read_bytes()
    calls = []

    def dav(*, method, target_url, username, password, headers, body,
            timeout, response_limit):
        calls.append(method)
        assert method == "GET"
        if remote_state == "source_changed":
            return HTTPResponse(status=200, body=b"Human edit, do not overwrite\n",
                                etag='"v0"')
        return HTTPResponse(status=200, body=before, etag='W/"weak"')

    receipts = tmp_path / "private-receipts"
    receipts.mkdir(mode=0o700)
    def writer(candidate):
        return apply_plan(candidate, selected_paths=(entry["path"],),
                          enabled=True, cas_server_verified=True,
                          receipt_dir=receipts,
                          base_url="https://synthetic.invalid/remote.php/dav/files/user/ObsidianVault",
                          username="user", password="SYNTHETIC",
                          transport=dav, operation_id="a" * 32)

    job, _ = make_job(tmp_path, plan, writer=writer)
    with job:
        job.update({"mode": "auto-apply", "interval_seconds": 300})
        row = finish(job, scheduled(job))
        assert row["status"] == expected_status
        assert row["reason_code"] == expected_reason
        assert row["writer_outcome"] == "blocked"
        assert calls == ["GET"]



@pytest.mark.parametrize("codes, status, reason", [
    (["applied", "write_rejected"], "failed", "writer_partial_write_rejected"),
    (["applied", "cas_conflict"], "conflict", "writer_partial_conflict"),
])
def test_partial_write_refusal_is_not_cas_conflict(tmp_path, codes, status, reason):
    plan = prepared_plan(tmp_path, count=2)
    reply = writer_receipt(plan, "partial", codes)
    job, _ = make_job(tmp_path, plan, writer=lambda _: reply)
    with job:
        job.update({"mode": "auto-apply", "interval_seconds": 300})
        result = finish(job, scheduled(job))
        assert result["status"] == status
        assert result["reason_code"] == reason
        # Some files were already written; retain the partial marker.
        assert result["writer_outcome"] == "partial"


def test_complete_receipt_for_subset_is_not_full_plan_success(tmp_path):
    plan = prepared_plan(tmp_path, count=2)
    only_first = writer_receipt(plan, "complete", ["applied"])
    job, _ = make_job(tmp_path, plan, writer=lambda _: only_first)
    with job:
        job.update({"mode": "auto-apply", "interval_seconds": 300})
        result = finish(job, scheduled(job))
        assert result["status"] == "indeterminate"
        assert result["reason_code"] == "writer_result_invalid"
        assert result["writer_outcome"] is None


def test_connection_save_acknowledgement_waits_for_active_writer(tmp_path):
    plan = prepared_plan(tmp_path)
    started, release, callback_completed = (
        threading.Event(), threading.Event(), threading.Event()
    )
    errors: list[str] = []

    def writer(candidate):
        started.set()
        assert release.wait(5)
        return writer_receipt(candidate, "complete", ["applied"])

    job, _ = make_job(tmp_path, plan, writer=writer)
    with job:
        job.update({"mode": "auto-apply", "interval_seconds": 300})
        run_id = scheduled(job)
        assert started.wait(3)

        def rotate():
            try:
                job.connections_saved()
            except Exception as exc:
                errors.append(type(exc).__name__)
            finally:
                callback_completed.set()

        thread = threading.Thread(target=rotate)
        thread.start()
        try:
            assert not callback_completed.wait(0.12)
            # Running scan/Writer receipt and read-only status remain visible.
            assert job.status()["last_run"]["status"] == "running"
        finally:
            release.set()
            thread.join(4)
        assert callback_completed.is_set() and not errors
        assert job.settings()["mode"] == "off"
        assert finish(job, run_id)["status"] == "applied"
        assert job.submit(uuid.uuid4().hex, "scheduled")[0] == "disabled"


def test_dashboard_nested_gate_and_formatter_lock_reentrant_for_credentials(tmp_path):
    plan = prepared_plan(tmp_path)
    job, _ = make_job(tmp_path, plan, writer=lambda _: None)
    with job:
        job.update({"mode": "auto-apply", "interval_seconds": 300})
        # Dashboard holds the Writer gate before Formatter._lock, then calls
        # connections_saved in the same thread. Must not self-deadlock.
        with job._write_gate, job._lock:
            job.connections_saved()
        assert job.settings()["mode"] == "off"


def test_actual_guarded_writer_selected_subset_is_not_complete_job_plan(tmp_path):
    from obsidian_automation.core_promotion_transport import HTTPResponse
    from obsidian_automation.vault_format_writer import apply_plan

    plan = prepared_plan(tmp_path, count=2)
    entries = sorted(
        (row for row in plan["results"] if row["status"] == "would_change"),
        key=lambda row: row["path"]
    )
    selected = entries[0]["path"]
    initial = (tmp_path / "synthetic-vault" / selected).read_bytes()
    state = {"body": initial, "etag": '"v0"', "puts": 0}

    def dav(*, method, target_url, username, password, headers, body,
            timeout, response_limit):
        assert target_url.endswith("/" + selected)
        assert username == "user" and password == "SYNTHETIC"
        if method == "GET":
            return HTTPResponse(200, state["body"], state["etag"])
        assert method == "PUT"
        assert headers.get("If-Match") == state["etag"]
        state["puts"] += 1
        state["body"] = body
        state["etag"] = '"v1"'
        return HTTPResponse(204, b"", state["etag"])

    receipts = tmp_path / "private-receipts"
    receipts.mkdir(mode=0o700)
    def writer(candidate):
        return apply_plan(
            candidate, selected_paths=(selected,), enabled=True,
            cas_server_verified=True, receipt_dir=receipts,
            base_url="https://synthetic.invalid/remote.php/dav/files/user/ObsidianVault",
            username="user", password="SYNTHETIC", transport=dav,
            operation_id="c" * 32
        )

    job, _ = make_job(tmp_path, plan, writer=writer)
    with job:
        job.update({"mode": "auto-apply", "interval_seconds": 300})
        result = finish(job, scheduled(job))
        assert state["puts"] == 1
        assert result["status"] == "indeterminate"
        assert result["reason_code"] == "writer_result_invalid"
        assert result["writer_outcome"] is None



def test_actual_writer_partial_permission_denial_is_failed_not_conflict(tmp_path):
    """First CAS applied, second provider PUT 403: preserve partial effects."""
    from obsidian_automation.core_promotion_transport import HTTPResponse
    from obsidian_automation.vault_format_writer import apply_plan

    plan = prepared_plan(tmp_path, count=2)
    selected = sorted(
        row["path"] for row in plan["results"]
        if row["status"] == "would_change"
    )
    remote = {
        path: (tmp_path / "synthetic-vault" / path).read_bytes()
        for path in selected
    }
    etags = {path: '"v0"' for path in selected}
    calls = []

    def dav(*, method, target_url, username, password, headers, body,
            timeout, response_limit):
        assert username == "user" and password == "SYNTHETIC"
        path = next((x for x in selected if target_url.endswith("/" + x)), None)
        assert path in remote
        calls.append((method, path))
        if method == "GET":
            return HTTPResponse(200, remote[path], etags[path])
        assert method == "PUT"
        assert headers.get("If-Match") == etags[path]
        if path == selected[1]:
            return HTTPResponse(403, b"", etags[path])
        remote[path] = body
        etags[path] = '"v1"'
        return HTTPResponse(204, b"", etags[path])

    original_second = remote[selected[1]]
    receipts = tmp_path / "writer-receipts"
    receipts.mkdir(mode=0o700)

    def writer(candidate):
        return apply_plan(
            candidate, selected_paths=selected,
            enabled=True, cas_server_verified=True,
            receipt_dir=receipts,
            base_url="https://synthetic.invalid/remote.php/dav/files/user/ObsidianVault",
            username="user", password="SYNTHETIC",
            transport=dav, operation_id="d" * 32
        )

    job, _ = make_job(tmp_path, plan, writer=writer)
    with job:
        job.update({"mode": "auto-apply", "interval_seconds": 300})
        result = finish(job, scheduled(job))
        assert result["status"] == "failed"
        assert result["reason_code"] == "writer_partial_write_rejected"
        assert result["writer_outcome"] == "partial"
        assert remote[selected[1]] == original_second
        assert remote[selected[0]] != (
            tmp_path / "synthetic-vault" / selected[0]
        ).read_bytes()
        assert [x for x in calls if x[0] == "PUT"] == [
            ("PUT", selected[0]), ("PUT", selected[1])
        ]
        # Never issue a third PUT to try to recover from a known 403.
