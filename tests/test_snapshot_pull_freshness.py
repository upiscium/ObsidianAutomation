import json
from pathlib import Path

import obsidian_automation.snapshot_adapter as snapshot_adapter
from obsidian_automation.snapshot_adapter import Connections, SnapshotAdapter


_INITIAL = b"A" * 32
_UPDATED = b"B" * 32
_LATER = b"C" * 32
_SECRET = "SECRET_TOKEN_CANARY /private-vault/private-note"


class _StopAtManifest(Exception):
    """Intentional sentinel to prove no Git stage runs in these tests."""


def _adapter(tmp_path: Path) -> tuple[SnapshotAdapter, Path]:
    source = tmp_path / "source"
    source.mkdir()
    (source / "note.md").write_bytes(_INITIAL)
    policy = tmp_path / "policy.toml"
    policy.write_text(
        "version = 1\nsettle_seconds = 0\nstability_attempts = 1\n"
        "exclude = []\nrepository_owned = []\n"
    )
    rclone_config = tmp_path / "rclone.conf"
    rclone_config.write_text("synthetic config; no real rclone is invoked\n")
    connections = Connections(
        state=tmp_path / "state",
        rclone_remote="nextcloud:Vault",
        git_remote="https://git.example.invalid/private.git",
        branch="main",
        policy=policy,
        rclone_config=rclone_config,
    )
    return SnapshotAdapter(connections), source


def _install_command_mock(monkeypatch, source: Path, *, check_mismatches=()):
    """Simulate rclone's size-only sync and byte-aware --download check."""
    calls: list[tuple[list[str], float]] = []
    events: list[tuple[str, int, bool | None]] = []
    mismatch_bytes = iter(check_mismatches)
    sync_count = 0
    check_count = 0

    def command(args, *, cwd, env, timeout, diagnostics):
        nonlocal sync_count, check_count
        # No subprocesses (rclone or Git) are used by this focused test.
        assert args[0] == "rclone", "unexpected command before manifest sentinel"
        assert diagnostics is not None
        calls.append((list(args), timeout))
        action = args[3]
        assert args[4] == "nextcloud:Vault"
        mirror = Path(args[5])

        if action == "sync":
            sync_count += 1
            remote_file = source / "note.md"
            mirror_file = mirror / "note.md"
            remote_bytes = remote_file.read_bytes()
            # Model the vendor's relevant size-only fast path: without
            # --ignore-times, a same-size update remains stale in the mirror.
            transfer = (
                "--ignore-times" in args
                or not mirror_file.exists()
                or mirror_file.stat().st_size != len(remote_bytes)
            )
            if transfer:
                mirror.mkdir(parents=True, exist_ok=True)
                mirror_file.write_bytes(remote_bytes)
            events.append(("sync", sync_count, None))
            if sync_count == 1:
                # First traversal downloaded A; the remote changes to same-size
                # B before the second traversal begins.
                assert mirror_file.read_bytes() == _INITIAL
                remote_file.write_bytes(_UPDATED)
            diagnostics.update(exit_code=0, reason="ok")
            return True, _SECRET

        assert action == "check"
        check_count += 1
        try:
            remote_mutation = next(mismatch_bytes)
        except StopIteration:
            remote_mutation = None
        if remote_mutation is not None:
            (source / "note.md").write_bytes(remote_mutation)

        remote_bytes = (source / "note.md").read_bytes()
        mirror_bytes = (mirror / "note.md").read_bytes()
        # Without --download, model the metadata-only outcome. With it, check
        # the bytes themselves, including same-sized contents.
        matches = (
            remote_bytes == mirror_bytes
            if "--download" in args
            else len(remote_bytes) == len(mirror_bytes)
        )
        events.append(("check", check_count, matches))
        diagnostics.update(
            exit_code=0 if matches else 1,
            reason="ok" if matches else "code_1_ambiguous",
        )
        return matches, _SECRET

    monkeypatch.setattr(snapshot_adapter, "_command", command)
    monkeypatch.setattr(snapshot_adapter.time, "sleep", lambda _: None)
    return calls, events


def _assert_rclone_argv(calls, cycles: int) -> None:
    assert len(calls) == cycles * 3
    assert [args[3] for args, _ in calls] == ["sync", "sync", "check"] * cycles
    for args, timeout in calls:
        assert args[0] == "rclone"
        assert 0 < timeout <= snapshot_adapter._RCLONE_IO_TIMEOUT_SECONDS
        if args[3] == "sync":
            assert "--ignore-times" in args
            assert "--delete-after" in args
            assert "--download" not in args
        else:
            assert "--download" in args
            assert "--ignore-times" not in args


def _receipt_text(adapter: SnapshotAdapter, run_id: str) -> str:
    receipt = adapter.config.state / "runs" / run_id / "rclone-receipt.json"
    payload = receipt.read_text(encoding="ascii")
    parsed = json.loads(payload)
    assert 1 <= len(parsed["attempts"]) <= 9
    for secret in (_SECRET, "SECRET_TOKEN_CANARY", "nextcloud:Vault",
                   "git.example.invalid", str(adapter.config.rclone_config)):
        assert secret not in payload
    return payload


def test_same_size_change_is_fresh_before_manifest_and_git(tmp_path, monkeypatch):
    adapter, source = _adapter(tmp_path)
    calls, events = _install_command_mock(monkeypatch, source)
    observed_manifest = []

    def stop_at_manifest(mirror, policy):
        observed_manifest.append((list(events), (mirror / "note.md").read_bytes()))
        raise _StopAtManifest

    monkeypatch.setattr(snapshot_adapter, "build_manifest", stop_at_manifest)
    run_id = "a" * 32

    # The sentinel is caught by the adapter's existing fail-closed manifest
    # boundary. It prevents every Git initialization/stage/push operation.
    assert adapter(run_id) == ("failed", "sync", None)
    assert observed_manifest == [
        ([ ("sync", 1, None), ("sync", 2, None), ("check", 1, True) ], _UPDATED)
    ]
    _assert_rclone_argv(calls, cycles=1)
    _receipt_text(adapter, run_id)


def test_transient_content_mismatch_retries_whole_cycle_before_manifest(
    tmp_path, monkeypatch,
):
    adapter, source = _adapter(tmp_path)
    calls, events = _install_command_mock(
        monkeypatch, source, check_mismatches=(_LATER,)
    )
    observed_manifest = []

    def stop_at_manifest(mirror, policy):
        observed_manifest.append((list(events), (mirror / "note.md").read_bytes()))
        raise _StopAtManifest

    monkeypatch.setattr(snapshot_adapter, "build_manifest", stop_at_manifest)
    run_id = "b" * 32

    assert adapter(run_id) == ("failed", "sync", None)
    assert observed_manifest == [
        (
            [
                ("sync", 1, None), ("sync", 2, None), ("check", 1, False),
                ("sync", 3, None), ("sync", 4, None), ("check", 2, True),
            ],
            _LATER,
        )
    ]
    _assert_rclone_argv(calls, cycles=2)
    receipt = json.loads(_receipt_text(adapter, run_id))
    assert [(item["phase"], item["attempt"]) for item in receipt["attempts"]] == [
        ("sync_initial", 1), ("sync_settled", 1), ("check", 1),
        ("sync_initial", 2), ("sync_settled", 2), ("check", 2),
    ]


def test_persistent_content_mismatch_exhausts_three_whole_cycles_before_manifest(
    tmp_path, monkeypatch,
):
    adapter, source = _adapter(tmp_path)
    calls, events = _install_command_mock(
        monkeypatch, source, check_mismatches=(_LATER, b"D" * 32, b"E" * 32)
    )
    manifest_events = []

    def unexpected_manifest(mirror, policy):
        manifest_events.append((list(events), (mirror / "note.md").read_bytes()))
        raise _StopAtManifest

    monkeypatch.setattr(snapshot_adapter, "build_manifest", unexpected_manifest)
    run_id = "c" * 32

    assert adapter(run_id) == ("failed", "sync", None)
    assert manifest_events == []
    _assert_rclone_argv(calls, cycles=3)
    receipt = json.loads(_receipt_text(adapter, run_id))
    assert [(item["phase"], item["attempt"], item["reason"]) for item in receipt["attempts"]] == [
        ("sync_initial", 1, "ok"), ("sync_settled", 1, "ok"),
        ("check", 1, "code_1_ambiguous"),
        ("sync_initial", 2, "ok"), ("sync_settled", 2, "ok"),
        ("check", 2, "code_1_ambiguous"),
        ("sync_initial", 3, "ok"), ("sync_settled", 3, "ok"),
        ("check", 3, "code_1_ambiguous"),
    ]
    assert snapshot_adapter._RCLONE_MAX_ATTEMPTS == 3
    assert snapshot_adapter._RCLONE_IO_TIMEOUT_SECONDS == 600
    assert snapshot_adapter._RCLONE_TOTAL_BUDGET_SECONDS == 900
    assert snapshot_adapter._RCLONE_RETRY_DELAYS_SECONDS == (2, 4)
