from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from obsidian_automation.github_daily_progress import (
    DailyProgressTargetMissing,
)
from obsidian_automation import github_daily_production as production


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "daily-progress"
    root.mkdir()
    for name in (
        production.SCHEDULE_DIR,
        production.EVIDENCE_DIR,
        production.SUMMARY_DIR,
        production.PROJECTION_DIR,
        production.TRANSPORT_DIR,
    ):
        (root / name).mkdir()
    return root


def test_previous_jst_date_handles_midnight_boundary() -> None:
    zone = ZoneInfo("Asia/Tokyo")

    assert production.previous_jst_date(
        datetime(2026, 10, 7, 0, 0, 0, tzinfo=zone)
    ) == "2026-10-06"
    assert production.previous_jst_date(
        datetime(2026, 10, 7, 23, 59, 59, tzinfo=zone)
    ) == "2026-10-06"


def test_schedule_ref_is_idempotent_and_manual_date_is_exact(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)

    first = production.enqueue_date(root, "2026-10-05")
    second = production.enqueue_date(root, "2026-10-05")

    assert first == second
    assert production.scheduled_dates(root) == ("2026-10-05",)


def test_stage_ref_conflict_fails_closed(tmp_path: Path) -> None:
    root = _root(tmp_path)
    schedule = root / production.SCHEDULE_DIR
    path = production._ref_path(schedule, "2026-10-05")
    production._write_ref(
        path,
        {
            "record_version": 1,
            "stage": "schedule",
            "date": "2026-10-05",
        },
    )

    with pytest.raises(
        production.DailyProductionError,
        match="different bytes",
    ):
        production._write_ref(
            path,
            {
                "record_version": 1,
                "stage": "schedule",
                "date": "2026-10-05",
                "unexpected": True,
            },
        )


def test_old_pending_date_survives_new_schedule_date(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    production.enqueue_date(root, "2026-10-05")
    production.enqueue_date(root, "2026-10-06")

    transport = root / production.TRANSPORT_DIR
    production._write_ref(
        production._ref_path(transport, "2026-10-06"),
        {
            "record_version": 1,
            "stage": "transport",
            "date": "2026-10-06",
            "projection_sha256": "a" * 64,
            "transport_result_sha256": "b" * 64,
            "outcome": "applied",
        },
    )

    assert production._pending_dates(
        root,
        "transport",
        production.TRANSPORT_DIR,
    ) == ("2026-10-05",)


def test_invalid_revision_fails_before_provider_contact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(tmp_path)
    production.enqueue_date(root, "2026-10-05")
    contacted = False

    def infer(**_kwargs):
        nonlocal contacted
        contacted = True
        raise AssertionError("provider must not be configured")

    monkeypatch.setattr(production, "ollama_infer", infer)

    with pytest.raises(
        production.DailyProductionError,
        match="implementation revision",
    ):
        production.summarize_pending(
            root=root,
            provider="ollama",
            base_url="https://provider.example",
            model="model",
            implementation_revision="main",
        )

    assert contacted is False


def test_provider_failure_leaves_summary_stage_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(tmp_path)
    production.enqueue_date(root, "2026-10-05")

    fake_bundle = SimpleNamespace(sha256="a" * 64)
    monkeypatch.setattr(
        production,
        "_evidence_for_date",
        lambda _root, _target: (
            "a" * 64,
            tmp_path / "evidence.json",
            fake_bundle,
        ),
    )
    monkeypatch.setattr(
        production,
        "ollama_infer",
        lambda **_kwargs: object(),
    )

    calls = 0

    def fail_then_pass(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("provider unavailable")
        return SimpleNamespace(
            evidence_bundle_sha256="a" * 64,
            grounded_summary_sha256="c" * 64,
        )

    monkeypatch.setattr(
        production,
        "_run_pipeline_unlocked",
        fail_then_pass,
    )

    with pytest.raises(RuntimeError, match="provider unavailable"):
        production.summarize_pending(
            root=root,
            provider="ollama",
            base_url="https://provider.example",
            model="model",
            implementation_revision="d" * 40,
        )

    summary_ref = production._ref_path(
        root / production.SUMMARY_DIR,
        "2026-10-05",
    )
    assert not summary_ref.exists()

    completed = production.summarize_pending(
        root=root,
        provider="ollama",
        base_url="https://provider.example",
        model="model",
        implementation_revision="d" * 40,
    )

    assert completed == ("2026-10-05",)
    assert calls == 2
    assert production._read_ref(
        summary_ref,
        stage="summary",
        target_date="2026-10-05",
    )["grounded_summary_sha256"] == "c" * 64


def test_target_missing_stays_pending_without_transport_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(tmp_path)
    production.enqueue_date(root, "2026-10-05")
    writer_identity = "fixture-writer"
    credential_fixture = tmp_path / "credential-fixture"
    projection = SimpleNamespace(sha256="a" * 64, date="2026-10-05")

    monkeypatch.setattr(
        production,
        "_projection_for_date",
        lambda _root, _target: (
            "a" * 64,
            tmp_path / "projection.json",
            projection,
        ),
    )
    monkeypatch.setattr(
        production,
        "_read_password",
        lambda _path: "fixture-value",
    )
    monkeypatch.setattr(
        production,
        "canonical_io_lock",
        lambda _root: nullcontext(),
    )
    monkeypatch.setattr(
        production,
        "apply_projection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            DailyProgressTargetMissing()
        ),
    )

    completed, retryable = production.apply_pending(
        root=root,
        pipeline_root=tmp_path,
        base_url="https://nextcloud.example",
        username=writer_identity,
        password_file=credential_fixture,
    )

    assert completed == ()
    assert retryable == ("2026-10-05",)
    assert not production._ref_path(
        root / production.TRANSPORT_DIR,
        "2026-10-05",
    ).exists()


def test_successful_transport_ref_makes_rerun_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(tmp_path)
    production.enqueue_date(root, "2026-10-05")
    writer_identity = "fixture-writer"
    credential_fixture = tmp_path / "credential-fixture"
    projection = SimpleNamespace(sha256="a" * 64, date="2026-10-05")
    calls = 0

    monkeypatch.setattr(
        production,
        "_projection_for_date",
        lambda _root, _target: (
            "a" * 64,
            tmp_path / "projection.json",
            projection,
        ),
    )
    monkeypatch.setattr(
        production,
        "_read_password",
        lambda _path: "fixture-value",
    )
    monkeypatch.setattr(
        production,
        "canonical_io_lock",
        lambda _root: nullcontext(),
    )

    def apply(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return SimpleNamespace(outcome="already_desired")

    monkeypatch.setattr(production, "apply_projection", apply)
    monkeypatch.setattr(
        production,
        "persist_transport_result",
        lambda _path, _result: b'{"ok":true}\n',
    )

    first = production.apply_pending(
        root=root,
        pipeline_root=tmp_path,
        base_url="https://nextcloud.example",
        username=writer_identity,
        password_file=credential_fixture,
    )
    second = production.apply_pending(
        root=root,
        pipeline_root=tmp_path,
        base_url="https://nextcloud.example",
        username=writer_identity,
        password_file=credential_fixture,
    )

    assert first == (("2026-10-05",), ())
    assert second == ((), ())
    assert calls == 1


def test_live_status_distinguishes_pending_and_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(tmp_path)
    production.enqueue_date(root, "2026-10-05")

    pending = production.live_status(root, "2026-10-05")
    assert pending["complete"] is False
    assert pending["stages"]["evidence"] == "pending"

    fake_bundle = SimpleNamespace(sha256="a" * 64)
    fake_summary = SimpleNamespace(sha256="b" * 64)
    fake_projection = SimpleNamespace(
        sha256="c" * 64,
        date="2026-10-05",
        evidence_bundle_sha256="a" * 64,
        grounded_summary_sha256="b" * 64,
    )
    monkeypatch.setattr(
        production,
        "_evidence_for_date",
        lambda _root, _target: (
            "a" * 64,
            tmp_path / "evidence.json",
            fake_bundle,
        ),
    )
    monkeypatch.setattr(
        production,
        "_summary_for_date",
        lambda _root, _target, bundle: (
            "b" * 64,
            tmp_path / "summary.json",
            fake_summary,
        ),
    )
    monkeypatch.setattr(
        production,
        "_projection_for_date",
        lambda _root, _target: (
            "c" * 64,
            tmp_path / "projection.json",
            fake_projection,
        ),
    )

    stages = (
        (production.EVIDENCE_DIR, "evidence", {
            "evidence_sha256": "a" * 64,
        }),
        (production.SUMMARY_DIR, "summary", {
            "evidence_sha256": "a" * 64,
            "grounded_summary_sha256": "b" * 64,
        }),
        (production.PROJECTION_DIR, "projection", {
            "evidence_sha256": "a" * 64,
            "grounded_summary_sha256": "b" * 64,
            "projection_sha256": "c" * 64,
        }),
    )
    for directory, stage, extra in stages:
        production._write_ref(
            production._ref_path(root / directory, "2026-10-05"),
            {
                "record_version": 1,
                "stage": stage,
                "date": "2026-10-05",
                **extra,
            },
        )

    result_data = (
        '{"outcome":"recovered","projection_sha256":"'
        + "c" * 64
        + '"}\n'
    ).encode()
    result_sha = production.sha256_bytes(result_data)
    result_path = (
        root
        / production.TRANSPORT_DIR
        / (
            "c" * 64
            + ".github-daily-progress.transport-result.json"
        )
    )
    result_path.write_bytes(result_data)
    production._write_ref(
        production._ref_path(
            root / production.TRANSPORT_DIR,
            "2026-10-05",
        ),
        {
            "record_version": 1,
            "stage": "transport",
            "date": "2026-10-05",
            "projection_sha256": "c" * 64,
            "transport_result_sha256": result_sha,
            "outcome": "recovered",
        },
    )

    # Ref publication can leave a complete visible pathname after its parent
    # directory fsync failed. No stage may be reported transport-complete
    # until every validated ref parent is successfully reaffirmed.
    import os
    real_fsync = os.fsync
    ref_dirs = (
        production.SCHEDULE_DIR, production.EVIDENCE_DIR,
        production.SUMMARY_DIR, production.PROJECTION_DIR,
        production.TRANSPORT_DIR,
    )
    before = {
        name: production._ref_path(root / name, "2026-10-05").read_bytes()
        for name in ref_dirs
    }
    for name in ref_dirs:
        affected = root / name
        errors = []

        def refuse_one_parent(fd):
            if os.readlink(f"/proc/self/fd/{fd}") == str(affected):
                errors.append("ref-parent-fsync-failed")
                raise OSError("simulated incomplete ref directory commit")
            return real_fsync(fd)

        with monkeypatch.context() as m:
            m.setattr(production.os, "fsync", refuse_one_parent)
            with pytest.raises(
                production.DailyProductionError,
                match="existing stage refs are not durable",
            ):
                production.live_status(root, "2026-10-05")
            assert production.smoke_main([
                "--profile", "live", "--root", str(root),
                "--date", "2026-10-05",
            ]) == 2
        assert errors == ["ref-parent-fsync-failed"] * 2

    # Live status becomes true only after revalidation and a real fsync in
    # every stage, not after rewriting or re-generating a published object.
    durable = []
    def record_ref_parent_fsync(fd):
        path = os.readlink(f"/proc/self/fd/{fd}")
        if path in {str(root / name) for name in ref_dirs}:
            durable.append(path)
        return real_fsync(fd)
    with monkeypatch.context() as m:
        m.setattr(production.os, "fsync", record_ref_parent_fsync)
        complete = production.live_status(root, "2026-10-05")
    assert complete["complete"] is True
    assert complete["stages"]["transport_outcome"] == "recovered"
    assert durable == [str(root / name) for name in ref_dirs]
    assert before == {
        name: production._ref_path(root / name, "2026-10-05").read_bytes()
        for name in ref_dirs
    }

    # An otherwise hash-bound but invalid UTF-8 transport artifact must be
    # reported as managed invalid JSON, never escape as UnicodeDecodeError.
    bad_result = b"\xff"
    bad_sha = production.sha256_bytes(bad_result)
    result_path.write_bytes(bad_result)
    transport_path = production._ref_path(
        root / production.TRANSPORT_DIR, "2026-10-05"
    )
    transport_path.unlink()  # isolated corruption fixture only
    production._write_ref(transport_path, {
        "record_version": 1,
        "stage": "transport",
        "date": "2026-10-05",
        "projection_sha256": "c" * 64,
        "transport_result_sha256": bad_sha,
        "outcome": "recovered",
    })
    with pytest.raises(
        production.DailyProductionError,
        match="live canary transport result is invalid JSON",
    ):
        production.live_status(root, "2026-10-05")

    # A final transport receipt is also an input to Live Smoke. A FIFO must
    # not block before the inode-type check, even when the upstream stage refs
    # and all their durability attestations are already present.
    import multiprocessing
    result_path.unlink()
    os.mkfifo(result_path, 0o640)
    fork = multiprocessing.get_context("fork")
    worker = fork.Process(
        target=_probe_live_transport_result_in_child,
        args=(str(root),),
    )
    worker.start()
    worker.join(timeout=3)
    if worker.is_alive():
        worker.terminate()
        worker.join(timeout=2)
        pytest.fail("Live Smoke blocked on special-inode transport result")
    assert worker.exitcode == 0

    # Other noncanonical final inodes fail before reading/hash comparison too.
    result_path.unlink()
    result_path.write_bytes(result_data)
    sibling = result_path.with_name("extra-hardlink")
    os.link(result_path, sibling)
    with pytest.raises(
        production.DailyProductionError,
        match="transport result cannot be safely read",
    ):
        production.live_status(root, "2026-10-05")
    sibling.unlink()
    result_path.write_bytes(b"x" * (production.MAX_TRANSPORT_RESULT_BYTES + 1))
    with pytest.raises(
        production.DailyProductionError,
        match="transport result cannot be safely read",
    ):
        production.live_status(root, "2026-10-05")


def _probe_live_transport_result_in_child(root: str) -> None:
    import os
    try:
        production.live_status(Path(root), "2026-10-05")
    except production.DailyProductionError as exc:
        if "transport result cannot be safely read" in str(exc):
            os._exit(0)
    os._exit(81)


def test_stage_ref_publishes_complete_inode_at_exact_0640_mode(
    tmp_path, monkeypatch
):
    import os
    import stat
    root = _root(tmp_path)
    path = production._ref_path(root / production.SUMMARY_DIR, "2026-10-05")
    payload = {"record_version": 1, "stage": "summary", "date": "2026-10-05"}
    expected = production._canonical_json_bytes(payload)
    original_rename = production._rename_noreplace
    observed = []

    def validate_before_rename(parent_fd, staged_name, target_name):
        assert target_name == path.name
        assert not path.exists()
        assert os.stat(staged_name, dir_fd=parent_fd).st_size == len(expected)
        assert stat.S_IMODE(os.stat(staged_name, dir_fd=parent_fd).st_mode) == 0o640
        observed.append("complete-before-rename")
        return original_rename(parent_fd, staged_name, target_name)

    with monkeypatch.context() as m:
        m.setattr(production, "_rename_noreplace", validate_before_rename)
        old_mask = os.umask(0o022)
        try:
            assert production._write_ref(path, payload) == expected
        finally:
            os.umask(old_mask)
    assert observed == ["complete-before-rename"]
    assert path.read_bytes() == expected
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert list(path.parent.glob(".obsidian-daily-ref-*.tmp")) == []
    assert production._write_ref(path, payload) == expected


def test_stage_ref_publish_error_leaves_no_partial_final(tmp_path, monkeypatch):
    root = _root(tmp_path)
    path = production._ref_path(root / production.SUMMARY_DIR, "2026-10-05")
    payload = {"record_version": 1, "stage": "summary", "date": "2026-10-05"}
    with monkeypatch.context() as m:
        def refuse_rename(*_args):
            raise OSError("injected pre-publication fault")
        m.setattr(production, "_rename_noreplace", refuse_rename)
        with pytest.raises(production.DailyProductionError, match="atomic publication failed"):
            production._write_ref(path, payload)
    assert not path.exists()
    assert list(path.parent.glob(".obsidian-daily-ref-*.tmp")) == []
    assert production._write_ref(path, payload) == production._canonical_json_bytes(payload)


def test_summary_ref_is_committed_while_pipeline_root_lock_held(tmp_path, monkeypatch):
    root = _root(tmp_path)
    production.enqueue_date(root, "2026-10-05")
    monkeypatch.setattr(production, "ollama_infer", lambda **_kwargs: object())
    monkeypatch.setattr(
        production, "_evidence_for_date",
        lambda _root, _target: ("a" * 64, tmp_path / "unused.json", object()),
    )
    monkeypatch.setattr(
        production, "_run_pipeline_unlocked",
        lambda **_kwargs: SimpleNamespace(
            evidence_bundle_sha256="a" * 64,
            grounded_summary_sha256="b" * 64,
        ),
    )
    original_write = production._write_ref
    calls = []

    def check_locked(path, payload):
        from obsidian_automation.github_daily_summary import _exclusive_summary_pipeline_root
        with pytest.raises(production.GitHubDailySummaryError, match="already active"):
            with _exclusive_summary_pipeline_root(root / production.SUMMARY_DIR):
                pass
        calls.append("under-lock")
        return original_write(path, payload)

    monkeypatch.setattr(production, "_write_ref", check_locked)
    result = production.summarize_pending(
        root=root, provider="ollama", base_url="http://unused.local",
        model="test", implementation_revision="a" * 40,
    )
    assert result == ("2026-10-05",)
    assert calls == ["under-lock"]
    assert production._pending_dates(root, "summary", production.SUMMARY_DIR) == ()


def test_parallel_summary_producer_does_not_select_or_infer(tmp_path, monkeypatch):
    from obsidian_automation.github_daily_summary import _exclusive_summary_pipeline_root
    root = _root(tmp_path)
    production.enqueue_date(root, "2026-10-05")
    monkeypatch.setattr(production, "ollama_infer", lambda **_kwargs: object())
    def invalid_date_selection(*_args):
        raise AssertionError("date selection must occur after exclusive lock")
    monkeypatch.setattr(production, "_pending_dates", invalid_date_selection)
    with _exclusive_summary_pipeline_root(root / production.SUMMARY_DIR):
        with pytest.raises(production.GitHubDailySummaryError, match="already active"):
            production.summarize_pending(
                root=root, provider="ollama", base_url="http://unused.local",
                model="test", implementation_revision="a" * 40,
            )


@pytest.mark.parametrize("bad_version", [True, 1.0])
def test_daily_stage_ref_rejects_noninteger_version(tmp_path, bad_version):
    root = _root(tmp_path)
    path = production._ref_path(root / production.SUMMARY_DIR, "2026-10-05")
    obj = {"record_version": bad_version, "stage": "summary", "date": "2026-10-05"}
    path.write_bytes(production._canonical_json_bytes(obj))
    with pytest.raises(production.DailyProductionError, match="version is unsupported"):
        production._read_ref(path, stage="summary", target_date="2026-10-05")


def test_stage_ref_parent_fsync_failure_never_exposes_partial_ref(
    tmp_path, monkeypatch
):
    import os
    import stat
    root = _root(tmp_path)
    path = production._ref_path(root / production.SUMMARY_DIR, "2026-10-05")
    payload = {"record_version": 1, "stage": "summary", "date": "2026-10-05"}
    expected = production._canonical_json_bytes(payload)
    actual_fsync = os.fsync
    rejected = []

    def fail_at_parent(fd):
        if os.readlink(f"/proc/self/fd/{fd}") == str(path.parent):
            rejected.append("post-rename-parent-fsync")
            raise OSError("injected parent durability failure")
        return actual_fsync(fd)

    with monkeypatch.context() as m:
        m.setattr(production.os, "fsync", fail_at_parent)
        with pytest.raises(
            production.DailyProductionError, match="atomic publication failed"
        ):
            production._write_ref(path, payload)
    assert rejected == ["post-rename-parent-fsync"]
    assert path.read_bytes() == expected
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert path.stat().st_nlink == 1
    assert list(path.parent.glob(".obsidian-daily-ref-*.tmp")) == []
    assert production._write_ref(path, payload) == expected


@pytest.mark.parametrize("bad_version", [True, 1.0])
def test_daily_evidence_rejects_noninteger_version(bad_version):
    from obsidian_automation.github_daily_summary import (
        GitHubDailySummaryError, parse_evidence_bundle,
    )
    obj = {
        "record_version": bad_version, "date": "2026-10-05",
        "timezone": "Asia/Tokyo", "window_start": "2026-10-04T15:00:00Z",
        "window_end": "2026-10-05T15:00:00Z", "projects": [],
        "repositories": [], "events": [],
    }
    with pytest.raises(
        GitHubDailySummaryError, match="evidence bundle properties"
    ):
        parse_evidence_bundle(production._canonical_json_bytes(obj))


@pytest.mark.parametrize("bad_version", [True, 1.0])
def test_daily_grounded_summary_rejects_noninteger_version(bad_version):
    from obsidian_automation.github_daily_progress import (
        DailyProgressError, parse_grounded_summary,
    )
    obj = {
        "record_version": bad_version, "stage": "grounded_summary",
        "evidence_bundle_sha256": "a" * 64, "claims": [],
        "rejected_claims": [], "grounding_output_sha256s": [],
    }
    with pytest.raises(
        DailyProgressError, match="grounded summary properties"
    ):
        parse_grounded_summary(
            production._canonical_json_bytes(obj),
            bundle=SimpleNamespace(sha256="a" * 64),
        )


@pytest.mark.parametrize("bad_version", [True, 1.0])
def test_daily_projection_rejects_noninteger_version(bad_version):
    from obsidian_automation.github_daily_progress import (
        DailyProgressError, PROJECTION_STAGE, parse_projection,
    )
    obj = {
        "record_version": bad_version, "stage": PROJECTION_STAGE,
        "date": "2026-10-05", "target_path": "00-DailyNote/2026-10-05.md",
        "evidence_bundle_sha256": "a" * 64,
        "grounded_summary_sha256": "b" * 64,
        "section_body_sha256": "c" * 64, "section_body": "",
    }
    with pytest.raises(
        DailyProgressError, match="projection properties do not match contract"
    ):
        parse_projection(production._canonical_json_bytes(obj))


def test_summary_retry_reaffirms_existing_ref_after_failed_parent_fsync(
    tmp_path, monkeypatch
):
    import os
    root = _root(tmp_path)
    production.enqueue_date(root, "2026-10-05")
    summary_dir = root / production.SUMMARY_DIR
    path = production._ref_path(summary_dir, "2026-10-05")
    payload = {
        "record_version": 1, "stage": "summary", "date": "2026-10-05",
        "evidence_sha256": "a" * 64,
        "grounded_summary_sha256": "b" * 64,
    }
    real_fsync = os.fsync
    durability_observed = []

    def refuse_summary_durability(fd):
        if os.readlink(f"/proc/self/fd/{fd}") == str(summary_dir):
            durability_observed.append("failed-summary-directory-sync")
            raise OSError("injected summary directory-fsync failure")
        return real_fsync(fd)

    with monkeypatch.context() as m:
        m.setattr(production.os, "fsync", refuse_summary_durability)
        with pytest.raises(production.DailyProductionError, match="atomic publication failed"):
            production._write_ref(path, payload)
    assert durability_observed == ["failed-summary-directory-sync"]
    assert path.read_bytes() == production._canonical_json_bytes(payload)

    def no_inference(**_args):
        raise AssertionError("already-published summary must not invoke inference")

    monkeypatch.setattr(production, "ollama_infer", lambda **_kwargs: object())
    monkeypatch.setattr(production, "_run_pipeline_unlocked", no_inference)
    # A repeat fsync failure must refuse the entire attempt, *not* report
    # the date complete while its ref's durability remains uncertain.
    with monkeypatch.context() as m:
        m.setattr(production.os, "fsync", refuse_summary_durability)
        with pytest.raises(
            production.DailyProductionError,
            match="existing stage refs are not durable",
        ):
            production.summarize_pending(
                root=root, provider="ollama", base_url="http://fixture.invalid",
                model="test", implementation_revision="a" * 40,
            )
    assert len(durability_observed) == 2

    syncs = []
    def record_sync(fd):
        value = os.readlink(f"/proc/self/fd/{fd}")
        if value == str(summary_dir):
            syncs.append("reaffirmed-summary-entry")
        return real_fsync(fd)
    with monkeypatch.context() as m:
        m.setattr(production.os, "fsync", record_sync)
        assert production.summarize_pending(
            root=root, provider="ollama", base_url="http://fixture.invalid",
            model="test", implementation_revision="a" * 40,
        ) == ()
    assert syncs == ["reaffirmed-summary-entry"]
    assert production._pending_dates(root, "summary", production.SUMMARY_DIR) == ()


def test_pending_stage_rejects_dangling_symlink_ref_before_reinfer(tmp_path):
    root = _root(tmp_path)
    production.enqueue_date(root, "2026-10-05")
    pointer = production._ref_path(
        root / production.SUMMARY_DIR, "2026-10-05"
    )
    pointer.symlink_to(root / "missing-summary-ref")
    with pytest.raises(
        production.DailyProductionError, match="stage ref cannot be safely read"
    ):
        production._pending_dates(root, "summary", production.SUMMARY_DIR)
    assert pointer.is_symlink()


def test_schedule_read_reaffirms_parent_directory_on_retry(tmp_path, monkeypatch):
    import os
    root = _root(tmp_path)
    schedule_dir = root / production.SCHEDULE_DIR
    real_fsync = os.fsync
    evidence = []
    def fail_once(fd):
        if os.readlink(f"/proc/self/fd/{fd}") == str(schedule_dir):
            evidence.append("failed-schedule-parent-fsync")
            raise OSError("injected schedule metadata fsync failure")
        return real_fsync(fd)
    with monkeypatch.context() as m:
        m.setattr(production.os, "fsync", fail_once)
        with pytest.raises(production.DailyProductionError, match="atomic publication failed"):
            production.enqueue_date(root, "2026-10-05")
    ref = production._ref_path(schedule_dir, "2026-10-05")
    assert ref.exists()
    assert ref.read_bytes() == production._canonical_json_bytes({
        "record_version": 1, "stage": "schedule", "date": "2026-10-05",
    })
    def record_sync(fd):
        if os.readlink(f"/proc/self/fd/{fd}") == str(schedule_dir):
            evidence.append("reaffirmed-schedule-parent")
        return real_fsync(fd)
    with monkeypatch.context() as m:
        m.setattr(production.os, "fsync", record_sync)
        assert production.scheduled_dates(root) == ("2026-10-05",)
    assert evidence == ["failed-schedule-parent-fsync", "reaffirmed-schedule-parent"]


def test_live_smoke_rejects_dangling_symlink_ref_without_unhandled_error(
    tmp_path, capsys
):
    root = _root(tmp_path)
    production.enqueue_date(root, "2026-10-05")
    damaged = production._ref_path(
        root / production.EVIDENCE_DIR, "2026-10-05"
    )
    damaged.symlink_to(root / "missing-evidence-ref")
    assert production.smoke_main([
        "--profile", "live", "--root", str(root), "--date", "2026-10-05"
    ]) == 2
    out = capsys.readouterr()
    assert "stage ref cannot be safely read" in out.err
    assert "Traceback" not in out.err
    assert out.out == ""
    assert damaged.is_symlink()


def _probe_bounded_stage_ref_reader_in_child(root: str) -> None:
    """Exit immediately on managed failure; the parent enforces a timeout."""
    import os
    try:
        production._pending_dates(
            Path(root), "summary", production.SUMMARY_DIR
        )
    except production.DailyProductionError:
        os._exit(0)
    os._exit(85)


def test_fifo_stage_ref_refuses_without_blocking(tmp_path):
    import multiprocessing
    import os
    root = _root(tmp_path)
    production.enqueue_date(root, "2026-10-05")
    fifo = production._ref_path(
        root / production.SUMMARY_DIR, "2026-10-05"
    )
    os.mkfifo(fifo, 0o640)
    process = multiprocessing.get_context("fork").Process(
        target=_probe_bounded_stage_ref_reader_in_child,
        args=(str(root),),
    )
    process.start()
    process.join(timeout=3)
    if process.is_alive():
        process.terminate()
        process.join(timeout=2)
        pytest.fail("Daily stage-ref reader blocked on a FIFO")
    assert process.exitcode == 0
    assert fifo.is_fifo()


def test_oversized_and_hardlinked_stage_refs_fail_closed(tmp_path):
    import os
    root = _root(tmp_path)
    stage = root / production.SUMMARY_DIR
    path = production._ref_path(stage, "2026-10-05")
    path.write_bytes(b'+' + b'x' * production.MAX_STAGE_REF_BYTES)
    with pytest.raises(production.DailyProductionError, match="cannot be safely read"):
        production._read_ref(path, stage="summary", target_date="2026-10-05")
    path.unlink()
    production._write_ref(path, {
        "record_version": 1, "stage": "summary", "date": "2026-10-05"
    })
    os.link(path, stage / "extra-reference")
    with pytest.raises(production.DailyProductionError, match="cannot be safely read"):
        production._read_ref(path, stage="summary", target_date="2026-10-05")


def test_invalid_utf8_stage_ref_is_a_managed_cli_failure(tmp_path, capsys):
    root = _root(tmp_path)
    path = production.enqueue_date(root, "2026-10-05")
    path.write_bytes(b"\xff")
    assert production.smoke_main([
        "--profile", "live", "--root", str(root), "--date", "2026-10-05",
    ]) == 2
    result = capsys.readouterr()
    assert "stage ref is invalid JSON" in result.err
    assert "Traceback" not in result.err
    assert result.out == ""



def test_escaped_lone_surrogate_stage_ref_has_managed_failure(tmp_path, capsys):
    import json
    root = _root(tmp_path)
    ref = production.enqueue_date(root, "2026-10-05")
    # json.loads intentionally accepts this escaped lone surrogate as a Python
    # string, but canonical UTF-8 serialization must reject it fail-closed.
    bad = {
        "record_version": 1,
        "stage": "schedule",
        "date": "2026-10-05",
        "note": chr(0xD800),
    }
    raw = json.dumps(
        bad, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8") + b"\n"
    assert b"\\ud800" in raw
    ref.write_bytes(raw)
    with pytest.raises(
        production.DailyProductionError, match="stage ref is not canonical JSON"
    ):
        production.scheduled_dates(root)
    assert production.smoke_main([
        "--profile", "live", "--root", str(root), "--date", "2026-10-05",
    ]) == 2
    observed = capsys.readouterr()
    assert "stage ref is not canonical JSON" in observed.err
    assert "Traceback" not in observed.err
    assert observed.out == ""
