"""Two-phase production update checks: no unattended Writer during Stage."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from obsidian_automation import github_production_staging as staged
from obsidian_automation.github_production_update import CommandResult, ProductionUpdateError, main as legacy_main
from test_github_production_update import FakeRunner, PREVIOUS, TARGET, _layout


class Runner(FakeRunner):
    def __init__(self, app: Path, venv: Path, *, allow_fast_forward=True, **kwargs):
        super().__init__(
            app, venv, daily_present=True,
            daily_enabled=True, daily_active=False, **kwargs,
        )
        self.allow_fast_forward = allow_fast_forward

    def __call__(self, argv):
        argv = tuple(str(x) for x in argv)
        if argv == ("git", "-C", str(self.app_root), "merge-base",
                    "--is-ancestor", PREVIOUS, TARGET):
            self.calls.append(argv)
            return CommandResult(0 if self.allow_fast_forward else 1, "", "")
        if argv[:2] == ("systemctl", "is-active") and argv[2] in staged.EFFECT_SERVICES:
            self.calls.append(argv)
            return CommandResult(3, "inactive\n", "")
        return super().__call__(argv)


def setup(tmp_path: Path, **runner_options):
    app, venv, units, receipts = _layout(tmp_path)
    receipts.mkdir(mode=0o700)
    config = tmp_path / "etc/obsidian-github-summarizer/config.env"
    config.write_text("DAILY_SUMMARY_MODEL=gemma4:12b\n")
    revision = config.parent / "revision.env"
    runner = Runner(app, venv, **runner_options)
    opts = dict(
        app_root=app, venv_root=venv, systemd_dir=units,
        receipt_dir=receipts, daily_revision_env=revision,
        config_files=(config,), runner=runner, require_root=False,
    )
    return runner, opts


def stage(runner, options):
    return staged.stage_update(target_sha=TARGET, **options)


def activate(receipt, options, *, approve=True, sync=False, daily=False):
    return staged.activate_update(
        stage_sha256=receipt, approve_live_github_writer=approve,
        restore_sync_timer=sync, restore_daily_timer=daily, **options,
    )


def _control(options):
    return staged.inspect_stage(receipt_dir=options["receipt_dir"])


def _called(runner, suffix):
    return any(call[-len(suffix):] == suffix for call in runner.calls)


def test_stage_is_safe_only_and_timers_remain_inert(tmp_path):
    runner, opts = setup(tmp_path)
    receipt = stage(runner, opts)
    assert len(receipt) == 64
    assert runner.current_sha == TARGET
    assert not runner.enabled and not runner.active
    assert runner.daily_present and not runner.daily_enabled and not runner.daily_active
    assert _control(opts)["status"] == "staged"
    assert _control(opts)["stage_sha256"] == receipt
    assert _called(runner, ("--profile", "safe"))
    assert not _called(runner, ("--profile", "live"))
    assert not any("enable" in call for call in runner.calls)
    assert (opts["daily_revision_env"]).read_text() == f"OBSIDIAN_AUTOMATION_REVISION={TARGET}\n"
    proof = staged._receipt_read(opts["receipt_dir"] / staged.STAGING_ROOT, receipt)
    assert proof["previous_sha"] == PREVIOUS
    assert proof["timer_states"][staged.legacy.TIMER_UNIT] == {"enabled": True, "active": True}
    assert proof["timer_states"][staged.legacy.DAILY_TIMER_UNIT] == {"enabled": True, "active": False}


def test_activate_requires_explicit_consent_without_side_effect(tmp_path):
    runner, opts = setup(tmp_path)
    receipt = stage(runner, opts)
    calls = list(runner.calls)
    with pytest.raises(ProductionUpdateError, match="approval"):
        activate(receipt, opts, approve=False)
    assert runner.calls == calls
    assert _control(opts)["status"] == "staged"
    assert not _called(runner, ("--profile", "live"))


def test_activate_rejects_wrong_receipt_before_live(tmp_path):
    runner, opts = setup(tmp_path)
    stage(runner, opts)
    with pytest.raises(ProductionUpdateError, match="matching"):
        activate("f"*64, opts)
    assert _control(opts)["status"] == "staged"
    assert not _called(runner, ("--profile", "live"))


def test_activate_live_restores_only_explicitly_approved_sync_timer(tmp_path):
    runner, opts = setup(tmp_path)
    receipt = stage(runner, opts)
    activation = activate(receipt, opts, sync=True)
    assert len(activation) == 64
    assert _called(runner, ("--profile", "live"))
    assert runner.enabled and runner.active
    assert not runner.daily_enabled and not runner.daily_active
    assert _control(opts)["status"] == "activated"
    calls = list(runner.calls)
    with pytest.raises(ProductionUpdateError, match="matching"):
        activate(receipt, opts)
    assert calls == runner.calls


def test_activation_default_remains_inert_even_after_live_smoke(tmp_path):
    runner, opts = setup(tmp_path)
    receipt = stage(runner, opts)
    activate(receipt, opts)
    assert _called(runner, ("--profile", "live"))
    assert not runner.enabled and not runner.active
    assert not runner.daily_enabled and not runner.daily_active


def test_stage_failure_is_fail_closed_and_not_retried(tmp_path):
    runner, opts = setup(tmp_path, fail_profile="safe")
    with pytest.raises(ProductionUpdateError):
        stage(runner, opts)
    assert _control(opts)["status"] == "failed"
    assert not runner.enabled and not runner.active
    assert not _called(runner, ("--profile", "live"))
    with pytest.raises(ProductionUpdateError, match="unresolved"):
        stage(runner, opts)


def test_second_stage_is_blocked_until_activation_or_explicit_recovery(tmp_path):
    runner, opts = setup(tmp_path)
    receipt = stage(runner, opts)
    with pytest.raises(ProductionUpdateError, match="unresolved"):
        stage(runner, opts)
    assert _control(opts)["stage_sha256"] == receipt


def test_config_drift_fails_before_live(tmp_path):
    runner, opts = setup(tmp_path)
    receipt = stage(runner, opts)
    opts["config_files"][0].write_text("DAILY_SUMMARY_MODEL=changed\n")
    with pytest.raises(ProductionUpdateError, match="configuration drift"):
        activate(receipt, opts)
    assert _control(opts)["status"] == "staged"
    assert not _called(runner, ("--profile", "live"))


def test_unit_drift_fails_before_live(tmp_path):
    runner, opts = setup(tmp_path)
    receipt = stage(runner, opts)
    (opts["systemd_dir"] / "obsidian-github-sync.service").write_text("modified\n")
    with pytest.raises(ProductionUpdateError, match="unit-file drift"):
        activate(receipt, opts)
    assert _control(opts)["status"] == "staged"
    assert not _called(runner, ("--profile", "live"))


def test_revision_binding_drift_fails_closed(tmp_path):
    runner, opts = setup(tmp_path)
    receipt = stage(runner, opts)
    opts["daily_revision_env"].write_text("OBSIDIAN_AUTOMATION_REVISION=wrong\n")
    with pytest.raises(ProductionUpdateError, match="revision binding"):
        activate(receipt, opts)
    assert _control(opts)["status"] == "staged"
    assert not _called(runner, ("--profile", "live"))


def test_corrupt_content_addressed_stage_receipt_not_adopted(tmp_path):
    runner, opts = setup(tmp_path)
    receipt = stage(runner, opts)
    path = opts["receipt_dir"] / staged.STAGING_ROOT / f"{receipt}.staging-receipt.json"
    path.write_bytes(b'{"record_version":1}\n')
    with pytest.raises(ProductionUpdateError, match="content hash"):
        activate(receipt, opts)
    assert not _called(runner, ("--profile", "live"))


def test_live_smoke_failure_blocks_replay_and_leaves_timers_disabled(tmp_path):
    runner, opts = setup(tmp_path, fail_profile="live")
    receipt = stage(runner, opts)
    with pytest.raises(ProductionUpdateError, match="live"):
        activate(receipt, opts, sync=True)
    assert _control(opts)["status"] == "failed"
    assert not runner.enabled and not runner.active
    with pytest.raises(ProductionUpdateError, match="matching"):
        activate(receipt, opts)
    assert sum(call[-2:] == ("--profile", "live") for call in runner.calls) == 1


def test_config_binding_required_for_staging(tmp_path):
    runner, opts = setup(tmp_path)
    opts["config_files"] = ()
    with pytest.raises(ProductionUpdateError, match="config bindings"):
        stage(runner, opts)
    assert runner.current_sha == PREVIOUS
    assert not _called(runner, ("--profile", "live"))


def test_missing_operator_timer_authority_is_denied_before_live(tmp_path):
    runner, opts = setup(tmp_path)
    runner.enabled = False
    runner.active = False
    receipt = stage(runner, opts)
    with pytest.raises(ProductionUpdateError, match="timer restore exceeds"):
        activate(receipt, opts, sync=True)
    assert not _called(runner, ("--profile", "live"))
    assert _control(opts)["status"] == "staged"


def test_daily_timer_restoration_is_not_part_of_activate(tmp_path):
    runner, opts = setup(tmp_path)
    receipt = stage(runner, opts)
    with pytest.raises(ProductionUpdateError, match="separate date-scoped"):
        activate(receipt, opts, daily=True)
    assert not _called(runner, ("--profile", "live"))
    assert _control(opts)["status"] == "staged"
    assert runner.daily_enabled is False and runner.daily_active is False


def test_inspect_without_stage_is_read_only(tmp_path):
    _, opts = setup(tmp_path)
    assert staged.inspect_stage(receipt_dir=opts["receipt_dir"]) == {"status": "none"}
    assert not (opts["receipt_dir"] / staged.STAGING_ROOT).exists()


def test_entrypoint_dispatch_does_not_authorize_live(tmp_path, monkeypatch, capsys):
    _, opts = setup(tmp_path)
    calls = []
    def fake_stager(**kwargs):
        calls.append(("stage", kwargs["target_sha"]))
        return "4"*64
    def fake_activator(**kwargs):
        calls.append(("activate", kwargs["approve_live_github_writer"]))
        if not kwargs["approve_live_github_writer"]:
            raise ProductionUpdateError("live GitHub Writer approval not supplied")
        return "5"*64
    with monkeypatch.context() as m:
        m.setattr(staged, "stage_update", fake_stager)
        m.setattr(staged, "activate_update", fake_activator)
        args = ["--app-root", str(opts["app_root"]), "--venv-root", str(opts["venv_root"]),
                "--bind-config-file", str(opts["config_files"][0])]
        assert legacy_main(["stage", "--target-sha", TARGET, *args]) == 0
        assert legacy_main(["activate", "--stage-sha256", "4"*64, *args]) == 2
        assert legacy_main(["activate", "--stage-sha256", "4"*64,
                            "--approve-live-github-writer", *args]) == 0
    assert calls == [("stage", TARGET), ("activate", False), ("activate", True)]
    assert "failed" in capsys.readouterr().err


def test_legacy_cli_requires_distinct_live_effect_approval(tmp_path, monkeypatch, capsys):
    # Denied *before* execute_update is even called.
    from obsidian_automation import github_production_update as module
    attempts = []
    def forbidden(**kwargs):
        attempts.append(kwargs)
        raise AssertionError("legacy live updater reached without approval")
    with monkeypatch.context() as m:
        m.setattr(module, "execute_update", forbidden)
        assert legacy_main(["--target-sha", TARGET]) == 2
    assert attempts == []
    assert "legacy_live_effects_not_approved" in capsys.readouterr().err


def test_interrupted_preparing_marker_refuses_unreviewed_reentry(tmp_path):
    runner, opts = setup(tmp_path)
    with staged._lock(opts["receipt_dir"]) as state:
        staged._control_write(state, "preparing", TARGET, None)
    calls = list(runner.calls)
    with pytest.raises(ProductionUpdateError, match="unresolved"):
        stage(runner, opts)
    assert runner.calls == calls


def test_second_staging_process_cannot_acquire_transaction_lock(tmp_path):
    runner, opts = setup(tmp_path)
    with staged._lock(opts["receipt_dir"]):
        with pytest.raises(ProductionUpdateError, match="already running"):
            stage(runner, opts)
    assert runner.calls == []


def test_staged_unit_symlink_or_corrupted_bound_file_fails_before_live(tmp_path):
    runner, opts = setup(tmp_path)
    receipt = stage(runner, opts)
    source = opts["systemd_dir"] / "obsidian-github-sync.service"
    source.unlink()
    source.symlink_to(opts["systemd_dir"] / "obsidian-github-sync.timer")
    with pytest.raises((OSError, ProductionUpdateError)):
        activate(receipt, opts)
    assert _control(opts)["status"] == "staged"
    assert not _called(runner, ("--profile", "live"))


def test_activate_verifies_postsmoke_timer_states(tmp_path):
    runner, opts = setup(tmp_path)
    receipt = stage(runner, opts)
    class LyingRestore:
        def __init__(self, base):
            self.base = base
        def __call__(self, argv):
            if tuple(argv) == ("systemctl", "enable", "--now", staged.legacy.TIMER_UNIT):
                return CommandResult(0, "", "")  # lies: did not change timer
            return self.base(argv)
    opts["runner"] = LyingRestore(runner)
    with pytest.raises(ProductionUpdateError, match="post-activation"):
        activate(receipt, opts, sync=True)
    assert _control(opts)["status"] == "failed"
    assert runner.enabled is False and runner.active is False


def test_automatically_bound_writer_environment_file_drift_blocks_live(tmp_path):
    runner, opts = setup(tmp_path)
    writer_config = tmp_path / "etc" / "writer-config.env"
    writer_config.write_text("WRITER_MODE=old\n")
    unit_src = opts["app_root"] / "examples" / "github-sync" / "obsidian-github-writer.service"
    unit_src.write_text(f"[Service]\nEnvironmentFile={writer_config}\n")
    receipt = stage(runner, opts)
    proof = staged._receipt_read(opts["receipt_dir"] / staged.STAGING_ROOT, receipt)
    assert str(writer_config) in proof["env_manifest"]
    assert str(writer_config) not in proof["config_manifest"]
    writer_config.write_text("WRITER_MODE=new\n")
    with pytest.raises(ProductionUpdateError, match="EnvironmentFile drift"):
        activate(receipt, opts)
    assert not _called(runner, ("--profile", "live"))
    assert _control(opts)["status"] == "staged"


def test_optional_environment_file_absence_is_bound(tmp_path):
    runner, opts = setup(tmp_path)
    optional = tmp_path / "etc" / "optional.env"
    unit_src = opts["app_root"] / "examples" / "github-sync" / "obsidian-github-sync.service"
    unit_src.write_text(f"[Service]\nEnvironmentFile=-{optional}\n")
    receipt = stage(runner, opts)
    proof = staged._receipt_read(opts["receipt_dir"] / staged.STAGING_ROOT, receipt)
    assert proof["env_manifest"][str(optional)] == "absent"
    optional.write_text("NEW_OPTIONAL_SECRET=present\n")
    with pytest.raises(ProductionUpdateError, match="EnvironmentFile drift"):
        activate(receipt, opts)
    assert not _called(runner, ("--profile", "live"))


def test_missing_required_environment_file_blocks_stage(tmp_path):
    runner, opts = setup(tmp_path)
    missing = tmp_path / "etc" / "missing_required.env"
    unit_src = opts["app_root"] / "examples" / "github-sync" / "obsidian-github-sync.service"
    unit_src.write_text(f"[Service]\nEnvironmentFile={missing}\n")
    with pytest.raises(ProductionUpdateError, match="required EnvironmentFile"):
        stage(runner, opts)
    assert _control(opts)["status"] == "failed"
    assert not runner.enabled and not runner.active
    assert not _called(runner, ("--profile", "live"))


def test_new_managed_unit_after_stage_blocks_activation(tmp_path):
    runner, opts = setup(tmp_path)
    receipt = stage(runner, opts)
    (opts["systemd_dir"] / "obsidian-github-unexpected.service").write_text(
        "[Service]\nExecStart=/bin/true\n"
    )
    with pytest.raises(ProductionUpdateError, match="installed unit set drift"):
        activate(receipt, opts)
    assert _control(opts)["status"] == "staged"
    assert not _called(runner, ("--profile", "live"))


def test_refuse_non_fast_forward(tmp_path):
    runner, opts = setup(tmp_path, allow_fast_forward=False)
    with pytest.raises(ProductionUpdateError, match="non-fast-forward"):
        stage(runner, opts)
    assert runner.current_sha == PREVIOUS
    assert runner.enabled and runner.active
    assert _control(opts) == {"status": "none"}
