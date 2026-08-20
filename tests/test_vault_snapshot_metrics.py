from __future__ import annotations

from pathlib import Path

from obsidian_automation import vault_snapshot, vault_snapshot_cli
from obsidian_automation.vault_snapshot import Change, SnapshotResult


_ARGS = [
    "--source",
    "/tmp/live",
    "--destination",
    "/tmp/repo",
    "--config",
    "/tmp/snapshot.toml",
]


def _set_monotonic(monkeypatch, *values: float) -> None:
    iterator = iter(values)
    monkeypatch.setattr(vault_snapshot_cli.time, "monotonic", lambda: next(iterator))


def test_cli_emits_success_metrics(monkeypatch, capsys) -> None:
    result = SnapshotResult(
        changed=True,
        commit_sha="a" * 40,
        manifest_sha256="b" * 64,
        changes=(Change("ADD", "note.md"),),
    )
    monkeypatch.setattr(vault_snapshot, "snapshot_vault", lambda **_: result)
    _set_monotonic(monkeypatch, 100.0, 101.25)

    assert vault_snapshot_cli.main(_ARGS) == 0

    captured = capsys.readouterr()
    assert "Created snapshot commit " + "a" * 40 in captured.out
    assert "Snapshot metrics: result=success" in captured.out
    assert "duration_seconds=1.250" in captured.out
    assert "changed=true" in captured.out
    assert "dry_run=false" in captured.out
    assert "commit_sha=" + "a" * 40 in captured.out
    assert "manifest_sha256=" + "b" * 64 in captured.out
    assert "changes=1" in captured.out
    assert "started_at=" in captured.out
    assert "finished_at=" in captured.out
    assert captured.err == ""


def test_cli_emits_noop_metrics(monkeypatch, capsys) -> None:
    result = SnapshotResult(
        changed=False,
        commit_sha=None,
        manifest_sha256="c" * 64,
        changes=(),
    )
    monkeypatch.setattr(vault_snapshot, "snapshot_vault", lambda **_: result)
    _set_monotonic(monkeypatch, 5.0, 5.5)

    assert vault_snapshot_cli.main(_ARGS) == 0

    captured = capsys.readouterr()
    assert "No snapshot commit created." in captured.out
    assert "Snapshot metrics: result=no-op" in captured.out
    assert "duration_seconds=0.500" in captured.out
    assert "changed=false" in captured.out
    assert "commit_sha=-" in captured.out
    assert "changes=0" in captured.out


def test_cli_emits_failure_metrics(monkeypatch, capsys) -> None:
    def fail(**_):
        raise vault_snapshot.SnapshotError("unstable source")

    monkeypatch.setattr(vault_snapshot, "snapshot_vault", fail)
    _set_monotonic(monkeypatch, 10.0, 12.75)

    assert vault_snapshot_cli.main(_ARGS) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "error: unstable source" in captured.err
    assert "Snapshot metrics: result=failure" in captured.err
    assert "duration_seconds=2.750" in captured.err
    assert "changed=-" in captured.err
    assert "dry_run=false" in captured.err
    assert "commit_sha=-" in captured.err
    assert "manifest_sha256=-" in captured.err
    assert "changes=-" in captured.err


def test_cli_marks_dry_run_separately(monkeypatch, capsys) -> None:
    result = SnapshotResult(
        changed=True,
        commit_sha=None,
        manifest_sha256="d" * 64,
        changes=(Change("UPDATE", "note.md"),),
    )
    monkeypatch.setattr(vault_snapshot, "snapshot_vault", lambda **_: result)
    _set_monotonic(monkeypatch, 20.0, 20.125)

    assert vault_snapshot_cli.main([*_ARGS, "--dry-run"]) == 0

    captured = capsys.readouterr()
    assert "Dry run; no commit created." in captured.out
    assert "Snapshot metrics: result=dry-run" in captured.out
    assert "duration_seconds=0.125" in captured.out
    assert "changed=true" in captured.out
    assert "dry_run=true" in captured.out
