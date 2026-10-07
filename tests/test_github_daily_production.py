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
        "run_pipeline",
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

    complete = production.live_status(root, "2026-10-05")
    assert complete["complete"] is True
    assert complete["stages"]["transport_outcome"] == "recovered"
