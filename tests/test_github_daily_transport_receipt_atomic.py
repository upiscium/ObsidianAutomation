"""Issue #338 — fixed-schema immutable Transport Result crash/replay boundaries."""
from __future__ import annotations

import json
import os
import stat
import time
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from obsidian_automation import artifact_lifecycle
from obsidian_automation import github_daily_production as production
from obsidian_automation.github_daily_progress import (
    DailyProgressConflict,
    DailyProgressError,
    DailyProgressTransportResult,
    persist_transport_result,
    parse_transport_result,
)


def _receipt(outcome="applied"):
    return DailyProgressTransportResult(
        projection_sha256="a" * 64,
        date="2026-10-05",
        target_path="00-DailyNote/2026-10-05.md",
        evidence_bundle_sha256="b" * 64,
        grounded_summary_sha256="c" * 64,
        section_body_sha256="d" * 64,
        outcome=outcome,
        before_content_sha256="e" * 64,
        after_content_sha256="f" * 64,
        completed_at="2026-10-13T00:00:00Z",
    )


def test_replay_uses_first_valid_receipt_outcome_when_new_observation_differs(tmp_path):
    path = tmp_path / "receipt.json"
    first = _receipt()
    saved = persist_transport_result(path, first)
    second = replace(
        first, outcome="already_desired",
        before_content_sha256="f" * 64, completed_at="2026-10-13T00:01:00Z",
    )
    replayed = persist_transport_result(path, second)
    assert replayed == saved
    assert path.read_bytes() == saved
    assert parse_transport_result(replayed).outcome == "applied"
    assert len(list(tmp_path.glob(".obsidian-cas-*.tmp"))) == 0


def test_replay_mismatched_projection_desired_binding_fails_closed(tmp_path):
    path = tmp_path / "receipt.json"
    original = persist_transport_result(path, _receipt())
    for changed in (
        replace(_receipt(), after_content_sha256="d" * 64),
        replace(_receipt(), date="2026-10-06"),
        replace(_receipt(), grounded_summary_sha256="a" * 64),
    ):
        with pytest.raises(DailyProgressConflict, match="binding differs"):
            persist_transport_result(path, changed)
        assert path.read_bytes() == original


def test_existing_truncated_json_is_never_auto_repaired(tmp_path):
    path = tmp_path / "receipt.json"
    path.write_bytes(b'{"record_version":')
    with pytest.raises(DailyProgressError):
        persist_transport_result(path, _receipt())
    assert path.read_bytes() == b'{"record_version":'


@pytest.mark.parametrize("inode_kind", ["fifo", "symlink", "hardlink", "oversize"])
def test_unsafe_final_receipt_inodes_fail_closed_without_blocking(tmp_path, inode_kind):
    path = tmp_path / "receipt.json"
    if inode_kind == "fifo":
        os.mkfifo(path, 0o600)
    elif inode_kind == "symlink":
        path.symlink_to(tmp_path / "missing")
    elif inode_kind == "hardlink":
        path.write_bytes(_receipt().to_json_bytes())
        os.link(path, tmp_path / "other-link")
    else:
        path.write_bytes(b"x" * (64 * 1024 + 1))
    start = time.monotonic()
    with pytest.raises(DailyProgressError):
        persist_transport_result(path, _receipt())
    assert time.monotonic() - start < 2.0


def test_final_receipt_not_created_until_complete_fsynced_temporary_inode(
    tmp_path, monkeypatch,
):
    path = tmp_path / "receipt.json"
    expected = _receipt().to_json_bytes()
    actual_rename = artifact_lifecycle._rename_noreplace
    seen = []

    def probe(parent_fd, temporary, final):
        assert final == path.name
        assert not path.exists()
        info = os.stat(temporary, dir_fd=parent_fd)
        assert info.st_size == len(expected)
        assert stat.S_ISREG(info.st_mode)
        seen.append(info.st_size)
        return actual_rename(parent_fd, temporary, final)

    with monkeypatch.context() as m:
        m.setattr(artifact_lifecycle, "_rename_noreplace", probe)
        previous = os.umask(0o027)
        try:
            assert persist_transport_result(path, _receipt()) == expected
        finally:
            os.umask(previous)
    assert seen == [len(expected)]
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert not list(tmp_path.glob(".obsidian-cas-*.tmp"))


def test_failed_rename_never_exposes_partial_final_receipt(tmp_path, monkeypatch):
    path = tmp_path / "receipt.json"

    def interrupted(*_):
        raise OSError("synthetic rename fault")

    with monkeypatch.context() as m:
        m.setattr(artifact_lifecycle, "_rename_noreplace", interrupted)
        with pytest.raises(DailyProgressError, match="atomic publication failed"):
            persist_transport_result(path, _receipt())
    assert not path.exists()
    assert not list(tmp_path.glob(".obsidian-cas-*.tmp"))
    assert persist_transport_result(path, _receipt()) == path.read_bytes()


def test_parent_fsync_failure_is_reaffirmed_before_retry_is_accepted(tmp_path, monkeypatch):
    path = tmp_path / "receipt.json"
    original = artifact_lifecycle.os.fsync
    failures = []

    def refuse_parent(fd):
        target = os.readlink(f"/proc/self/fd/{fd}")
        if target == str(tmp_path):
            failures.append(target)
            if len(failures) <= 2:
                raise OSError("synthetic post-rename directory fsync failed")
        return original(fd)

    with monkeypatch.context() as m:
        m.setattr(artifact_lifecycle.os, "fsync", refuse_parent)
        with pytest.raises(DailyProgressError):
            persist_transport_result(path, _receipt())
        assert path.exists()
    # The completed but previously unconfirmed final bytes remain immutable.
    first_bytes = path.read_bytes()
    assert first_bytes == _receipt().to_json_bytes()
    assert persist_transport_result(path, _receipt()) == first_bytes
    assert len(failures) >= 2


def test_stage_ref_binds_persisted_receipt_not_new_runtime_outcome(tmp_path, monkeypatch):
    root = tmp_path / "daily"
    root.mkdir()
    directory = root / production.TRANSPORT_DIR
    directory.mkdir()
    original = _receipt()
    projection_sha = original.projection_sha256
    result_path = directory / f"{projection_sha}.github-daily-progress.transport-result.json"
    persisted_bytes = persist_transport_result(result_path, original)
    current = replace(
        original, outcome="already_desired",
        before_content_sha256=original.after_content_sha256,
        completed_at="2026-10-13T00:01:00Z",
    )
    monkeypatch.setattr(production, "_pending_dates", lambda *_: ("2026-10-05",))
    monkeypatch.setattr(
        production, "_projection_for_date",
        lambda *_: (
            projection_sha, tmp_path / "projection.json",
            SimpleNamespace(
                sha256=original.projection_sha256,
                date=original.date,
                target_path=original.target_path,
                evidence_bundle_sha256=original.evidence_bundle_sha256,
                grounded_summary_sha256=original.grounded_summary_sha256,
                section_body_sha256=original.section_body_sha256,
            ),
        ),
    )
    monkeypatch.setattr(production, "_read_password", lambda *_: "synthetic-password")
    monkeypatch.setattr(production, "canonical_io_lock", lambda *_: nullcontext())
    monkeypatch.setattr(production, "apply_projection", lambda *_args, **_kwargs: current)

    completed, retryable = production.apply_pending(
        root=root,
        pipeline_root=tmp_path,
        base_url="https://unused.example",
        username="synthetic",
        password_file=tmp_path / "unused-pw",
    )
    assert completed == ("2026-10-05",)
    assert retryable == ()
    record = production._read_ref(
        production._ref_path(directory, "2026-10-05"),
        stage="transport", target_date="2026-10-05",
    )
    assert record["outcome"] == "applied"
    assert record["transport_result_sha256"] == production.sha256_bytes(persisted_bytes)
    assert result_path.read_bytes() == persisted_bytes


def test_transport_receipt_schema_roundtrip_rejects_lone_surrogate():
    raw = json.dumps({
        **json.loads(_receipt().to_json_bytes()),
        "target_path": "\ud800",
    }).encode()
    with pytest.raises(DailyProgressError):
        parse_transport_result(raw)


def _concurrent_receipt_publisher(path: str, outcome: str, barrier, results):
    try:
        receipt = _receipt(outcome)
        barrier.wait(timeout=5)
        payload = persist_transport_result(Path(path), receipt)
        results.put(("ok", payload))
    except BaseException as exc:
        results.put(("failed", type(exc).__name__))


def test_two_same_projection_producers_admit_one_atomic_immutable_receipt(tmp_path):
    import multiprocessing

    ctx = multiprocessing.get_context("fork")
    barrier = ctx.Barrier(2)
    queue = ctx.Queue()
    path = str(tmp_path / "receipt.json")
    workers = (
        ctx.Process(target=_concurrent_receipt_publisher, args=(path, outcome, barrier, queue))
        for outcome in ("applied", "already_desired")
    )
    tasks = list(workers)
    for worker in tasks:
        worker.start()
    try:
        rows = [queue.get(timeout=8) for _ in tasks]
        for worker in tasks:
            worker.join(timeout=5)
            assert worker.exitcode == 0
        assert all(state == "ok" for state, _ in rows), rows
        published = Path(path).read_bytes()
        assert all(payload == published for _, payload in rows)
        assert parse_transport_result(published).outcome in {"applied", "already_desired"}
        assert not list(tmp_path.glob(".obsidian-cas-*.tmp"))
    finally:
        for worker in tasks:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=5)
        queue.close()


@pytest.mark.parametrize("existing_problem", ["truncated", "wrong_date"])
def test_invalid_existing_receipt_preflight_blocks_remote_writer(tmp_path, monkeypatch, existing_problem):
    root = tmp_path / "daily"
    root.mkdir()
    stage_dir = root / production.TRANSPORT_DIR
    stage_dir.mkdir()
    prior = _receipt()
    result_path = stage_dir / f"{prior.projection_sha256}.github-daily-progress.transport-result.json"
    if existing_problem == "truncated":
        result_path.write_bytes(b'{"record_version":')
    else:
        persist_transport_result(result_path, replace(prior, date="2026-10-06"))

    projection = SimpleNamespace(
        sha256=prior.projection_sha256,
        date=prior.date,
        target_path=prior.target_path,
        evidence_bundle_sha256=prior.evidence_bundle_sha256,
        grounded_summary_sha256=prior.grounded_summary_sha256,
        section_body_sha256=prior.section_body_sha256,
    )
    monkeypatch.setattr(production, "_pending_dates", lambda *_: ("2026-10-05",))
    monkeypatch.setattr(
        production, "_projection_for_date",
        lambda *_: (prior.projection_sha256, tmp_path / "projection.json", projection),
    )
    monkeypatch.setattr(production, "_read_password", lambda *_: "fixture")
    monkeypatch.setattr(production, "canonical_io_lock", lambda *_: nullcontext())
    contacted = []
    monkeypatch.setattr(
        production, "apply_projection", lambda *_args, **_kwargs: contacted.append(True),
    )
    with pytest.raises((DailyProgressError, production.DailyProductionError)):
        production.apply_pending(
            root=root, pipeline_root=tmp_path, base_url="https://unused.example",
            username="fixture", password_file=tmp_path / "unused",
        )
    assert contacted == []
    assert not production._ref_path(stage_dir, "2026-10-05").exists()


def _projection_for_receipt(receipt):
    return SimpleNamespace(
        sha256=receipt.projection_sha256, date=receipt.date,
        target_path=receipt.target_path,
        evidence_bundle_sha256=receipt.evidence_bundle_sha256,
        grounded_summary_sha256=receipt.grounded_summary_sha256,
        section_body_sha256=receipt.section_body_sha256,
    )


def _cli_args(tmp_path, result_path):
    return [
        "--projection", str(tmp_path / "unused-projection.json"),
        "--state-root", str(tmp_path),
        "--base-url", "https://unused.example",
        "--username", "synthetic",
        "--password-file", str(tmp_path / "unused-password"),
        "--result", str(result_path),
    ]


def test_standalone_apply_cli_refuses_corrupt_receipt_before_remote_call(
    tmp_path, monkeypatch, capsys,
):
    from obsidian_automation import github_daily_progress as progress

    path = tmp_path / "receipt.json"
    path.write_bytes(b'{"record_version":')
    model = _projection_for_receipt(_receipt())
    monkeypatch.setattr(progress, "load_projection", lambda *_: model)
    monkeypatch.setattr(progress, "_read_password", lambda *_: "synthetic-password")
    monkeypatch.setattr(progress, "canonical_io_lock", lambda *_: nullcontext())
    invoked = []
    monkeypatch.setattr(
        progress, "apply_projection",
        lambda *_args, **_kwargs: invoked.append(True),
    )
    assert progress.apply_main(_cli_args(tmp_path, path)) == 2
    assert invoked == []
    assert path.read_bytes() == b'{"record_version":'
    diagnostic = json.loads(capsys.readouterr().err)
    assert diagnostic["reason_code"] == "daily_progress_error"
    assert "not valid JSON" in diagnostic["message"]


def test_standalone_apply_cli_event_outcome_matches_immutable_receipt(
    tmp_path, monkeypatch, capsys,
):
    from obsidian_automation import github_daily_progress as progress

    path = tmp_path / "receipt.json"
    first = _receipt(outcome="applied")
    prior = persist_transport_result(path, first)
    current = replace(
        first, outcome="already_desired",
        before_content_sha256=first.after_content_sha256,
        completed_at="2026-10-13T00:01:00Z",
    )
    model = _projection_for_receipt(first)
    monkeypatch.setattr(progress, "load_projection", lambda *_: model)
    monkeypatch.setattr(progress, "_read_password", lambda *_: "synthetic-password")
    monkeypatch.setattr(progress, "canonical_io_lock", lambda *_: nullcontext())
    seen_expected = []

    def observe(*_args, **kwargs):
        seen_expected.append(kwargs["expected_after_content_sha256"])
        return current

    monkeypatch.setattr(progress, "apply_projection", observe)
    assert progress.apply_main(_cli_args(tmp_path, path)) == 0
    event = json.loads(capsys.readouterr().out)
    assert seen_expected == [first.after_content_sha256]
    assert event["outcome"] == "applied"
    assert event["transport_result_sha256"] == progress.sha256_bytes(prior)
    assert path.read_bytes() == prior



@pytest.mark.parametrize(
    "malformed_outcome",
    [[], {}, ["applied"], {"kind": "applied"}, None, True, 1],
)
def test_unhashable_or_nonstring_receipt_outcome_is_managed_failure(
    tmp_path, malformed_outcome,
):
    """A JSON collection must not raise TypeError before our fail-closed gate."""
    from obsidian_automation.artifact_lifecycle import _canonical_json_bytes

    payload = json.loads(_receipt().to_json_bytes())
    payload["outcome"] = malformed_outcome
    corrupt_bytes = _canonical_json_bytes(payload)
    with pytest.raises(DailyProgressError, match="invalid schema or outcome"):
        parse_transport_result(corrupt_bytes)

    path = tmp_path / "existing-transport.json"
    path.write_bytes(corrupt_bytes)
    with pytest.raises(DailyProgressError):
        persist_transport_result(path, _receipt())
    assert path.read_bytes() == corrupt_bytes
