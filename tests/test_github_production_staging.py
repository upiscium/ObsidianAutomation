"""Two-phase production update checks: no unattended Writer during Stage."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from obsidian_automation import github_production_staging as staged
from obsidian_automation.github_production_update import CommandResult, ProductionUpdateError, main as legacy_main
from test_github_production_update import FakeRunner, PREVIOUS, TARGET, _layout


class Runner(FakeRunner):
    def __init__(self, app: Path, venv: Path, *, allow_fast_forward=True,
                 active_extra_services=(), extra_loaded_timers=(),
                 unloaded_timers=(), extra_loaded_services=(), extra_installed_services=(),
                 alternate_unit_paths=(), effective_dropins=(), bad_fragments=(),
                 fail_role_import=None, fail_disable=False,
                 sync_substate="waiting", sync_next_elapse="5min", **kwargs):
        super().__init__(
            app, venv, daily_present=True,
            daily_enabled=True, daily_active=False, **kwargs,
        )
        self.allow_fast_forward = allow_fast_forward
        self.active_extra_services = frozenset(active_extra_services)
        self.extra_loaded_timers = set(extra_loaded_timers)
        self.unloaded_timers = set(unloaded_timers)
        self.extra_loaded_services = set(extra_loaded_services)
        self.extra_installed_services = set(extra_installed_services)
        self.alternate_unit_paths = tuple(alternate_unit_paths)
        self.effective_dropins = set(effective_dropins)
        self.bad_fragments = set(bad_fragments)
        self.fail_role_import = fail_role_import
        self.fail_disable = fail_disable
        self.sync_substate = sync_substate
        self.sync_next_elapse = sync_next_elapse
        self.systemd_dir = None

    def __call__(self, argv):
        argv = tuple(str(x) for x in argv)
        if (self.fail_disable and argv ==
                ("systemctl", "disable", "--now", "obsidian-github-sync.timer")):
            self.calls.append(argv)
            return CommandResult(1, "", "simulated timer stop failure")
        if argv[:2] == ("runuser", "-u"):
            self.calls.append(argv)
            if argv[2] == self.fail_role_import:
                return CommandResult(1, "", "injected permission denial")
            return CommandResult(0, "", "")
        if argv[:2] == ("systemd-analyze", "unit-paths"):
            self.calls.append(argv)
            return CommandResult(
                0, "".join(f"{path}\n" for path in
                           (self.systemd_dir, *self.alternate_unit_paths)), "",
            )
        if argv[:2] == ("systemctl", "list-unit-files"):
            self.calls.append(argv)
            kind = ".service" if "--type=service" in argv else ".timer"
            files = {p.name for p in self.systemd_dir.glob("obsidian-github-*" + kind)}
            if kind == ".service":
                files |= self.extra_installed_services
            return CommandResult(
                0, "".join(f"{name} enabled enabled\n" for name in sorted(files)), "",
            )
        if argv[:2] == ("systemctl", "list-units"):
            self.calls.append(argv)
            kind = ".service" if "--type=service" in argv else ".timer"
            loaded = {p.name for p in self.systemd_dir.glob("obsidian-github-*" + kind)}
            if kind == ".timer":
                loaded -= self.unloaded_timers
                loaded |= self.extra_loaded_timers
            else:
                loaded |= self.extra_loaded_services
            return CommandResult(
                0, "".join(f"{name} loaded inactive dead fixture\n" for name in sorted(loaded)), "",
            )
        if (argv[:3] == ("systemctl", "show", "obsidian-github-sync.timer")
                and "--property=SubState" in argv):
            self.calls.append(argv)
            return CommandResult(0, (
                f"SubState={self.sync_substate}\n"
                f"NextElapseUSecMonotonic={self.sync_next_elapse}\n"
            ), "")
        if argv[:2] == ("systemctl", "show") and "-p" in argv:
            name = argv[-1]
            self.calls.append(argv)
            if name in self.bad_fragments:
                fragment = "/run/systemd/system/" + name
            else:
                fragment = str(self.systemd_dir / name)
            drop = "/etc/systemd/system/" + name + ".d/override.conf" if name in self.effective_dropins else ""
            return CommandResult(
                0, f"LoadState=loaded\nFragmentPath={fragment}\nDropInPaths={drop}\n", ""
            )
        if argv == ("git", "-C", str(self.app_root), "merge-base",
                    "--is-ancestor", PREVIOUS, TARGET):
            self.calls.append(argv)
            return CommandResult(0 if self.allow_fast_forward else 1, "", "")
        if (argv[:2] == ("systemctl", "is-active") and len(argv) == 3
                and argv[2].startswith("obsidian-github-") and argv[2].endswith(".service")):
            self.calls.append(argv)
            if argv[2] in self.active_extra_services:
                return CommandResult(0, "active\n", "")
            return CommandResult(3, "inactive\n", "")
        return super().__call__(argv)


def setup(tmp_path: Path, **runner_options):
    app, venv, units, receipts = _layout(tmp_path)
    # Production already has managed unit files; model that initial state.
    for source in (app / "examples" / "github-sync").glob("obsidian-github-*"):
        (units / source.name).write_bytes(source.read_bytes())
    source_package = app / "src" / "obsidian_automation"
    # Exact canonical-structure fixture: roles are parsed, not executed.
    authority_dir = app / "tools"
    authority_dir.mkdir()
    (authority_dir / "provision_automation_authority.py").write_text(
        "PRIMARY_USERS: tuple[tuple[str, str, str], ...] = (\n"
        + "".join(f"    ('obsidian-test-role-{i}', 'obsidian-test-role-{i}', '/nonexistent'),\n"
                  for i in range(10))
        + ")\n"
    )
    (venv / "bin" / "python").write_text("#!/bin/sh\n", encoding="utf-8")
    (venv / "bin" / "python").chmod(0o755)
    installed_package = venv / "lib" / "python3.12" / "site-packages" / "obsidian_automation"
    source_package.mkdir(parents=True)
    installed_package.mkdir(parents=True)
    for name in ("__init__.py", "github_production_staging.py"):
        contents = f"# reviewed package fixture: {name}\\n"
        (source_package / name).write_text(contents)
        (installed_package / name).write_text(contents)
    receipts.mkdir(mode=0o700)
    config = tmp_path / "etc/obsidian-github-summarizer/config.env"
    config.write_text("DAILY_SUMMARY_MODEL=gemma4:12b\n")
    revision = config.parent / "revision.env"
    runner = Runner(app, venv, **runner_options)
    runner.systemd_dir = units
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



def test_stage_receipt_binds_all_canonical_role_smokes(tmp_path):
    runner, opts = setup(tmp_path)
    receipt = stage(runner, opts)
    record = staged._receipt_read(opts["receipt_dir"] / staged.STAGING_ROOT, receipt)
    roles = record["service_role_smoke"]["roles"]
    assert len(roles) == 10
    assert roles[0] == "obsidian-test-role-0"
    calls = [c for c in runner.calls if c[:2] == ("runuser", "-u")]
    assert [c[2] for c in calls] == roles
    assert all(c[5:8] == ("-I", "-B", "-c") for c in calls)
    assert all("obsidian-github-production-smoke" in " ".join(c) for c in calls)
    assert not _called(runner, ("--profile", "live"))


def test_denied_service_role_import_blocks_staging_without_live_effects(tmp_path):
    runner, opts = setup(tmp_path, fail_role_import="obsidian-test-role-3")
    with pytest.raises(ProductionUpdateError, match="service-role import"):
        stage(runner, opts)
    assert _control(opts)["status"] == "failed"
    assert not runner.enabled and not runner.daily_enabled
    assert not _called(runner, ("--profile", "live"))


def test_poststage_service_role_permission_drift_blocks_activation(tmp_path):
    runner, opts = setup(tmp_path)
    receipt = stage(runner, opts)
    runner.fail_role_import = "obsidian-test-role-7"
    with pytest.raises(ProductionUpdateError, match="service-role import"):
        activate(receipt, opts)
    assert _control(opts)["status"] == "staged"
    assert not _called(runner, ("--profile", "live"))


def test_invalid_authority_manifest_blocks_staging_after_timer_stop(tmp_path):
    runner, opts = setup(tmp_path)
    manifest = opts["app_root"] / "tools" / "provision_automation_authority.py"
    manifest.write_text("PRIMARY_USERS = arbitrary_function_call()\n")
    with pytest.raises(ProductionUpdateError, match="authority role manifest"):
        stage(runner, opts)
    assert _control(opts)["status"] == "failed"
    assert not runner.enabled and not runner.daily_enabled
    assert not _called(runner, ("--profile", "live"))


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
    with staged._lock(opts["receipt_dir"], require_root=False) as state:
        staged._control_write(state, "preparing", TARGET, None)
    calls = list(runner.calls)
    with pytest.raises(ProductionUpdateError, match="unresolved"):
        stage(runner, opts)
    assert runner.calls == calls


def test_second_staging_process_cannot_acquire_transaction_lock(tmp_path):
    runner, opts = setup(tmp_path)
    with staged._lock(opts["receipt_dir"], require_root=False):
        with pytest.raises(ProductionUpdateError, match="already in progress"):
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



def test_stage_removes_stale_setuptools_build_and_egg_info(tmp_path):
    runner, opts = setup(tmp_path)
    checkout = opts["app_root"]
    build = checkout / "build" / "lib" / "obsidian_automation"
    build.mkdir(parents=True)
    (build / "removed_legacy_module.py").write_text("stale Python module")
    egg_info = checkout / "src" / "obsidian_automation.egg-info"
    egg_info.mkdir()
    (egg_info / "SOURCES.txt").write_text("legacy info")
    receipt = stage(runner, opts)
    assert len(receipt) == 64
    assert not (checkout / "build").exists()
    assert not egg_info.exists()
    assert _called(runner, ("--profile", "safe"))
    assert not _called(runner, ("--profile", "live"))
    assert runner.enabled is False and runner.daily_enabled is False


def test_symlinked_derived_build_path_is_rejected_without_traversal(tmp_path):
    runner, opts = setup(tmp_path)
    outside = tmp_path / "operator-files"
    outside.mkdir()
    marker = outside / "IMPORTANT.md"
    marker.write_text("must remain untouched")
    (opts["app_root"] / "build").symlink_to(outside)
    with pytest.raises(ProductionUpdateError, match="unsafe ignored package build path"):
        stage(runner, opts)
    assert marker.read_text() == "must remain untouched"
    assert (opts["app_root"] / "build").is_symlink()
    assert _control(opts) == {"status": "none"}
    assert runner.enabled and runner.active and runner.daily_enabled
    assert not _called(runner, ("--profile", "live"))



def _peek_umask():
    # The tested Stage path is single-threaded; immediately restore the value.
    current = __import__("os").umask(0o077)
    __import__("os").umask(current)
    return current


@pytest.mark.parametrize("fail_pip", (False, True))
def test_stage_pip_has_pinned_umask_and_restores_operator_mask(tmp_path, fail_pip):
    import os
    runner, opts = setup(tmp_path, fail_pip=fail_pip)
    captured = []
    class UmaskRunner:
        def __call__(self, argv):
            if argv and argv[0] == str(opts["venv_root"] / "bin" / "pip"):
                captured.append(_peek_umask())
            return runner(argv)
    opts["runner"] = UmaskRunner()
    old_mask = os.umask(0o077)
    try:
        if fail_pip:
            with pytest.raises(ProductionUpdateError, match="stage install reviewed package"):
                stage(runner, opts)
        else:
            stage(runner, opts)
        assert _peek_umask() == 0o077
    finally:
        os.umask(old_mask)
    assert captured == [0o022]
    assert not _called(runner, ("--profile", "live"))


def test_preflight_checks_all_derived_directories_before_cleanup(tmp_path):
    runner, opts = setup(tmp_path)
    checkout = opts["app_root"]
    build = checkout / "build"
    build.mkdir()
    (build / "stale").write_text("keep until entire cleanup set validated")
    external = tmp_path / "outside"
    external.mkdir()
    (checkout / "dist").symlink_to(external)
    with pytest.raises(ProductionUpdateError, match="unsafe ignored package build path"):
        stage(runner, opts)
    assert (build / "stale").read_text() == "keep until entire cleanup set validated"
    assert _control(opts) == {"status": "none"}
    assert runner.enabled and runner.active and runner.daily_enabled
    assert not _called(runner, ("--profile", "live"))


@pytest.mark.parametrize("case,expected_error", (
    ("nested_symlink", "unreviewed ignored package build file type"),
    ("nested_mount", "unsafe or oversized ignored package build subtree"),
    ("nested_special", "unreviewed ignored package build file type"),
    ("hardlinked", "hardlinked ignored package build artifact"),
    ("too_many", "unsafe or oversized ignored package build subtree"),
    ("too_large", "oversized ignored package build artifacts"),
    ("world_writable_dir", "insecure ignored package build directory"),
))
def test_unsafe_nested_ignored_build_fails_before_timer_or_marker(
    tmp_path, monkeypatch, case, expected_error,
):
    import os
    runner, opts = setup(tmp_path)
    build = opts["app_root"] / "build"
    child = build / "lib"
    child.mkdir(parents=True)
    marker = child / "stale.py"
    marker.write_text("must not be deleted")
    if case == "nested_symlink":
        (child / "external").symlink_to(tmp_path)
    elif case == "nested_mount":
        injected = child / "mounted"
        injected.mkdir()
        originals = staged._current_mount_points
        monkeypatch.setattr(
            staged, "_current_mount_points",
            lambda: originals() | frozenset({injected}),
        )
    elif case == "nested_special":
        os.mkfifo(child / "old-fifo")
    elif case == "hardlinked":
        os.link(marker, child / "shared.py")
    elif case == "too_many":
        monkeypatch.setattr(staged, "MAX_IGNORED_BUILD_ENTRIES", 1)
    elif case == "too_large":
        monkeypatch.setattr(staged, "MAX_IGNORED_BUILD_BYTES", 1)
    elif case == "world_writable_dir":
        child.chmod(0o777)
    with pytest.raises(ProductionUpdateError, match=expected_error):
        stage(runner, opts)
    assert marker.read_text() == "must not be deleted"
    assert _control(opts) == {"status": "none"}
    assert runner.enabled and runner.active and runner.daily_enabled
    assert not _called(runner, ("--profile", "live"))


def test_ignored_build_tree_checked_before_removing_other_candidates(tmp_path):
    runner, opts = setup(tmp_path)
    build = opts["app_root"] / "build"
    build.mkdir()
    marker = build / "old.py"
    marker.write_text("keep")
    unsafe = opts["app_root"] / "src" / "obsidian_automation.egg-info"
    unsafe.mkdir()
    (unsafe / "external").symlink_to(tmp_path)
    with pytest.raises(ProductionUpdateError, match="unreviewed ignored package build file type"):
        stage(runner, opts)
    assert marker.read_text() == "keep"
    assert _control(opts) == {"status": "none"}
    assert runner.enabled and runner.active


def test_mountinfo_parser_detects_escaped_bind_mount_path(tmp_path, monkeypatch):
    from pathlib import Path
    marker = "42 1 0:22 / /tmp/test" + chr(92) + "040space rw,relatime - ext4 /dev/root rw"
    original = Path.read_text

    def mocked_read(self, *args, **kwargs):
        if str(self) == "/proc/self/mountinfo":
            return marker + chr(10)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", mocked_read)
    assert Path("/tmp/test space") in staged._current_mount_points()


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


def test_stage_failure_disables_both_managed_timers(tmp_path):
    runner, opts = setup(tmp_path, fail_profile="safe")
    with pytest.raises(ProductionUpdateError):
        stage(runner, opts)
    assert runner.enabled is False and runner.active is False
    assert runner.daily_enabled is False and runner.daily_active is False
    assert _control(opts)["status"] == "failed"
    assert not _called(runner, ("--profile", "live"))


def test_running_daily_render_blocks_stage_before_any_production_effect(tmp_path):
    """A preexisting Daily renderer must not be ignored by the idle gate."""
    active = "obsidian-github-daily-render.service"
    runner, opts = setup(tmp_path, active_extra_services=(active,))
    with pytest.raises(ProductionUpdateError, match="not confirmed idle"):
        stage(runner, opts)
    assert runner.current_sha == PREVIOUS
    assert runner.enabled and runner.active
    assert not _called(runner, ("--profile", "safe"))
    assert _control(opts) == {"status": "none"}


def test_unlisted_installed_managed_service_must_also_be_idle(tmp_path):
    """The full existing unit namespace is authoritative, not a fixed shortlist."""
    active = "obsidian-github-unanticipated.service"
    runner, opts = setup(tmp_path, active_extra_services=(active,))
    (opts["systemd_dir"] / active).write_text("[Service]\nExecStart=/bin/true\n")
    with pytest.raises(ProductionUpdateError, match="not confirmed idle"):
        stage(runner, opts)
    assert runner.current_sha == PREVIOUS
    assert runner.enabled and runner.active
    assert _control(opts) == {"status": "none"}


@pytest.mark.parametrize("corruption", [
    "installed_python", "console_script", "extra_module", "missing_module"
])
def test_poststage_deployed_code_drift_blocks_live_smoke(tmp_path, corruption):
    runner, opts = setup(tmp_path)
    receipt = stage(runner, opts)
    assert "code_manifest" in staged._receipt_read(
        opts["receipt_dir"] / staged.STAGING_ROOT, receipt
    )

    package = opts["venv_root"] / "lib" / "python3.12" / "site-packages" / "obsidian_automation"
    if corruption == "installed_python":
        (package / "github_production_staging.py").write_text("# compromised installed code\n")
    elif corruption == "console_script":
        (opts["venv_root"] / "bin" / "obsidian-github-production-smoke").write_text(
            "# changed executable\n"
        )
    elif corruption == "extra_module":
        (package / "hidden.py").write_text("# unexpected module\n")
    else:
        (package / "__init__.py").unlink()

    with pytest.raises(ProductionUpdateError, match="installed"):
        activate(receipt, opts)
    assert not _called(runner, ("--profile", "live"))
    assert _control(opts)["status"] == "staged"


def test_stage_rejects_installed_package_mismatch_before_safe_smoke(tmp_path):
    runner, opts = setup(tmp_path)
    package = opts["venv_root"] / "lib" / "python3.12" / "site-packages" / "obsidian_automation"
    (package / "github_production_staging.py").write_text("# bad build output\n")
    with pytest.raises(ProductionUpdateError, match="installed package bytes"):
        stage(runner, opts)
    assert _control(opts)["status"] == "failed"
    assert runner.enabled is False and runner.active is False
    assert not _called(runner, ("--profile", "safe"))


def test_unknown_installed_timer_is_rejected_before_effects(tmp_path):
    runner, opts = setup(tmp_path)
    (opts["systemd_dir"] / "obsidian-github-surprise.timer").write_text(
        "[Timer]\nOnCalendar=hourly\n"
    )
    with pytest.raises(ProductionUpdateError, match="unknown or missing installed GitHub timer"):
        stage(runner, opts)
    assert runner.current_sha == PREVIOUS
    assert runner.enabled and runner.active
    assert _control(opts) == {"status": "none"}
    assert not _called(runner, ("--profile", "safe"))


def test_unknown_loaded_timer_not_on_disk_is_rejected_before_effects(tmp_path):
    runner, opts = setup(tmp_path, extra_loaded_timers=("obsidian-github-transient.timer",))
    with pytest.raises(ProductionUpdateError, match="unexpected loaded or installed"):
        stage(runner, opts)
    assert runner.current_sha == PREVIOUS
    assert runner.enabled and runner.active
    assert _control(opts) == {"status": "none"}


def test_unknown_loaded_timer_appearing_after_stage_blocks_activate(tmp_path):
    runner, opts = setup(tmp_path)
    receipt = stage(runner, opts)
    runner.extra_loaded_timers.add("obsidian-github-unexpected.timer")
    with pytest.raises(ProductionUpdateError, match="unexpected loaded or installed"):
        activate(receipt, opts)
    assert _control(opts)["status"] == "staged"
    assert not _called(runner, ("--profile", "live"))


def test_installed_dropin_prevents_stage_before_timer_stop(tmp_path):
    runner, opts = setup(tmp_path)
    override = opts["systemd_dir"] / "obsidian-github-writer.service.d"
    override.mkdir()
    (override / "override.conf").write_text("[Service]\nExecStart=\n")
    with pytest.raises(ProductionUpdateError, match="drop-in"):
        stage(runner, opts)
    assert _control(opts) == {"status": "none"}
    assert runner.enabled and runner.active
    assert runner.current_sha == PREVIOUS


def test_pending_unloaded_dropin_blocks_activate(tmp_path):
    runner, opts = setup(tmp_path)
    receipt = stage(runner, opts)
    override = opts["systemd_dir"] / "obsidian-github-sync.service.d"
    override.mkdir()
    (override / "override.conf").write_text("[Service]\nEnvironment=UNREVIEWED=1\n")
    with pytest.raises(ProductionUpdateError, match="drop-in"):
        activate(receipt, opts)
    assert _control(opts)["status"] == "staged"
    assert not _called(runner, ("--profile", "live"))


def test_systemd_effective_dropin_caught_before_stage(tmp_path):
    runner, opts = setup(tmp_path, effective_dropins=("obsidian-github-writer.service",))
    with pytest.raises(ProductionUpdateError, match="drop-in"):
        stage(runner, opts)
    assert _control(opts) == {"status": "none"}
    assert runner.enabled and runner.active


def test_systemd_effective_dropin_added_after_stage_caught_before_live(tmp_path):
    runner, opts = setup(tmp_path)
    receipt = stage(runner, opts)
    runner.effective_dropins.add("obsidian-github-compactor.service")
    with pytest.raises(ProductionUpdateError, match="drop-in"):
        activate(receipt, opts)
    assert _control(opts)["status"] == "staged"
    assert not _called(runner, ("--profile", "live"))


def test_systemd_unit_fragment_rebinding_fails_closed(tmp_path):
    runner, opts = setup(tmp_path, bad_fragments=("obsidian-github-writer.service",))
    with pytest.raises(ProductionUpdateError, match="unit origin"):
        stage(runner, opts)
    assert _control(opts) == {"status": "none"}
    assert runner.current_sha == PREVIOUS


@pytest.mark.parametrize("dirname", [
    "obsidian-github-writer.service.d",
    "service.d",
])
def test_vendor_or_global_pending_dropin_blocks_activate(tmp_path, dirname):
    vendor = tmp_path / "vendor-units"
    vendor.mkdir()
    runner, opts = setup(tmp_path, alternate_unit_paths=(vendor,))
    receipt = stage(runner, opts)
    dropin = vendor / dirname
    dropin.mkdir()
    (dropin / "override.conf").write_text(
        "[Service]\nEnvironment=UNREVIEWED=1\n"
    )
    with pytest.raises(ProductionUpdateError, match="drop-in"):
        activate(receipt, opts)
    assert _control(opts)["status"] == "staged"
    assert not _called(runner, ("--profile", "live"))


def test_whitespace_indented_environment_binding_is_hashed(tmp_path):
    runner, opts = setup(tmp_path)
    config = tmp_path / "writer-environment.env"
    config.write_text("WRITER_MODE=before\n")
    source = opts["app_root"] / "examples" / "github-sync" / "obsidian-github-writer.service"
    source.write_text(f"[Service]\n \t EnvironmentFile = {config}\n")
    receipt = stage(runner, opts)
    stage_record = staged._receipt_read(opts["receipt_dir"] / staged.STAGING_ROOT, receipt)
    assert str(config) in stage_record["env_manifest"]
    config.write_text("WRITER_MODE=after\n")
    with pytest.raises(ProductionUpdateError, match="EnvironmentFile drift"):
        activate(receipt, opts)
    assert _control(opts)["status"] == "staged"
    assert not _called(runner, ("--profile", "live"))


def test_external_loaded_service_blocks_stage_before_effects(tmp_path):
    runner, opts = setup(
        tmp_path, extra_loaded_services=("obsidian-github-transient-worker.service",),
    )
    with pytest.raises(ProductionUpdateError, match="unexpected installed or loaded GitHub service"):
        stage(runner, opts)
    assert _control(opts) == {"status": "none"}
    assert runner.current_sha == PREVIOUS
    assert runner.enabled and runner.active


def test_external_installed_service_blocks_stage_before_effects(tmp_path):
    runner, opts = setup(
        tmp_path, extra_installed_services=("obsidian-github-external-worker.service",),
    )
    with pytest.raises(ProductionUpdateError, match="unexpected installed or loaded GitHub service"):
        stage(runner, opts)
    assert _control(opts) == {"status": "none"}
    assert runner.current_sha == PREVIOUS


def test_external_loaded_service_added_after_stage_blocks_activate(tmp_path):
    runner, opts = setup(tmp_path)
    receipt = stage(runner, opts)
    runner.extra_loaded_services.add("obsidian-github-transient-worker.service")
    with pytest.raises(ProductionUpdateError, match="unexpected installed or loaded GitHub service"):
        activate(receipt, opts)
    assert _control(opts)["status"] == "staged"
    assert not _called(runner, ("--profile", "live"))


def test_direct_sync_toml_drift_is_bound_before_live_smoke(tmp_path):
    runner, opts = setup(tmp_path)
    direct = tmp_path / "sync-config.toml"
    direct.write_text("github_api_base = 'https://api.github.com'\n")
    unit = opts["app_root"] / "examples" / "github-sync" / "obsidian-github-sync.service"
    unit.write_text(
        f"[Service]\nExecStart=/opt/sync-cli \\\n"
        f"  --config {direct}\n"
    )
    receipt = stage(runner, opts)
    proof = staged._receipt_read(opts["receipt_dir"] / staged.STAGING_ROOT, receipt)
    assert str(direct) in proof["direct_manifest"]
    assert "github_api_base" not in str(proof)
    direct.write_text("github_api_base = 'https://unexpected.invalid'\n")
    with pytest.raises(ProductionUpdateError, match="direct service file input drift"):
        activate(receipt, opts)
    assert _control(opts)["status"] == "staged"
    assert not _called(runner, ("--profile", "live"))


@pytest.mark.parametrize("flag", ("--rclone-config", "--filter-file", "--password-file"))
def test_additional_direct_files_bound_before_activate(tmp_path, flag):
    runner, opts = setup(tmp_path)
    secret = tmp_path / "bound-input"
    secret.write_text("PRIVATE_TEST_MARKER\n")
    unit = opts["app_root"] / "examples" / "github-sync" / "obsidian-github-sync.service"
    unit.write_text(f"[Service]\nExecStart=/opt/sync-cli {flag}={secret}\n")
    receipt = stage(runner, opts)
    assert str(secret) in staged._receipt_read(
        opts["receipt_dir"] / staged.STAGING_ROOT, receipt
    )["direct_manifest"]
    secret.write_text("PRIVATE_TEST_MARKER_MODIFIED\n")
    with pytest.raises(ProductionUpdateError, match="direct service file input drift"):
        activate(receipt, opts)
    assert not _called(runner, ("--profile", "live"))


@pytest.mark.parametrize("flag_style", ("separate", "equals"))
def test_exec_condition_direct_input_is_bound_and_activation_checks_drift(tmp_path, flag_style):
    runner, opts = setup(tmp_path)
    config = tmp_path / "conditional-config.toml"
    config.write_text("condition = 'before'\n")
    unit = opts["app_root"] / "examples" / "github-sync" / "obsidian-github-sync.service"
    suffix = f"--config={config}" if flag_style == "equals" else f"--config {config}"
    unit.write_text(f"[Service]\nExecCondition=/opt/test-condition {suffix}\nExecStart=/opt/sync-cli\n")
    receipt = stage(runner, opts)
    proof = staged._receipt_read(opts["receipt_dir"] / staged.STAGING_ROOT, receipt)
    assert str(config) in proof["direct_manifest"]
    config.write_text("condition = 'after'\n")
    with pytest.raises(ProductionUpdateError, match="direct service file input drift"):
        activate(receipt, opts)
    assert _control(opts)["status"] == "staged"
    assert not _called(runner, ("--profile", "live"))


def test_unknown_exec_condition_file_flag_fails_closed(tmp_path):
    runner, opts = setup(tmp_path)
    unit = opts["app_root"] / "examples" / "github-sync" / "obsidian-github-sync.service"
    unit.write_text("[Service]\nExecCondition=/opt/test-condition --another-config=/etc/secret\n")
    with pytest.raises(ProductionUpdateError, match="unreviewed direct file argument"):
        stage(runner, opts)
    assert _control(opts)["status"] == "failed"
    assert not runner.enabled and not runner.active
    assert not _called(runner, ("--profile", "live"))


@pytest.mark.parametrize("directive", ("ExecStart", "ExecCondition"))
def test_systemd_c_escape_in_direct_path_is_rejected_not_misbound(tmp_path, directive):
    runner, opts = setup(tmp_path)
    actual = tmp_path / "input.toml"
    decoy = tmp_path / "inputx2etoml"
    actual.write_text("real confidential input\n")
    decoy.write_text("decoy data, must never bind in place of real\n")
    unit = opts["app_root"] / "examples" / "github-sync" / "obsidian-github-sync.service"
    # A systemd C-style \x2e escape denotes '.', but POSIX shlex would
    # incorrectly hash the filename inputx2etoml if it is present.
    unit.write_text(
        f"[Service]\n{directive}=/opt/test-condition "
        f"--config={tmp_path}/input\\x2etoml\n"
    )
    with pytest.raises(ProductionUpdateError, match="unsupported systemd Exec backslash escape"):
        stage(runner, opts)
    assert _control(opts)["status"] == "failed"
    assert not runner.enabled and not runner.active
    assert not _called(runner, ("--profile", "live"))


def test_unreviewed_sensitive_file_flag_fails_stage(tmp_path):
    runner, opts = setup(tmp_path)
    unit = opts["app_root"] / "examples" / "github-sync" / "obsidian-github-sync.service"
    unit.write_text("[Service]\nExecStart=/opt/sync-cli --new-api-config=/etc/secret\n")
    with pytest.raises(ProductionUpdateError, match="unreviewed direct file argument"):
        stage(runner, opts)
    assert _control(opts)["status"] == "failed"
    assert runner.enabled is False and runner.active is False
    assert not _called(runner, ("--profile", "live"))


def test_missing_mandatory_direct_file_blocks_stage(tmp_path):
    runner, opts = setup(tmp_path)
    missing = tmp_path / "not-yet-provisioned.toml"
    unit = opts["app_root"] / "examples" / "github-sync" / "obsidian-github-sync.service"
    unit.write_text(f"[Service]\nExecStart=/opt/sync-cli --config {missing}\n")
    with pytest.raises(FileNotFoundError):
        stage(runner, opts)
    assert _control(opts)["status"] == "failed"
    assert not runner.enabled and not runner.active
    assert not _called(runner, ("--profile", "live"))


def test_stage_renders_consolidated_unit_execution_paths(tmp_path):
    runner, opts = setup(tmp_path)
    source = opts["app_root"] / "examples/github-sync/obsidian-github-writer.service"
    source.write_text(
        "[Service]\n"
        "WorkingDirectory=/opt/obsidian-github-sync/app\n"
        "ExecStart=/opt/obsidian-github-sync/venv/bin/obsidian-github-project-status-worker\n"
    )
    receipt = stage(runner, opts)
    installed = (opts["systemd_dir"] / source.name).read_text()
    assert f"WorkingDirectory={opts['app_root']}" in installed
    assert f"ExecStart={opts['venv_root']}/bin/obsidian-github-project-status-worker" in installed
    assert "/opt/obsidian-github-sync" not in installed
    proof = staged._receipt_read(opts["receipt_dir"] / staged.STAGING_ROOT, receipt)
    assert source.name in proof["unit_manifest"]
    assert not _called(runner, ("--profile", "live"))


def test_stage_rejects_unmapped_legacy_runtime_path(tmp_path):
    runner, opts = setup(tmp_path)
    source = opts["app_root"] / "examples/github-sync/obsidian-github-writer.service"
    source.write_text("[Service]\nExecStart=/opt/obsidian-github-sync/app/custom-script\n")
    with pytest.raises(ProductionUpdateError, match="unrendered legacy runtime"):
        stage(runner, opts)
    assert _control(opts)["status"] == "failed"
    assert not _called(runner, ("--profile", "live"))


def test_installed_but_unloaded_known_timer_is_valid_idle_inventory(tmp_path):
    runner, opts = setup(
        tmp_path, unloaded_timers=("obsidian-github-daily-progress.timer",)
    )
    assert len(stage(runner, opts)) == 64
    assert not _called(runner, ("--profile", "live"))


def test_environment_file_specifier_is_not_misbound(tmp_path):
    runner, opts = setup(tmp_path)
    source = opts["app_root"] / "examples/github-sync/obsidian-github-writer.service"
    source.write_text("[Service]\nEnvironmentFile=/etc/private/%n.env\n")
    with pytest.raises(ProductionUpdateError, match="unsupported EnvironmentFile specifier"):
        stage(runner, opts)
    assert _control(opts)["status"] == "failed"
    assert not _called(runner, ("--profile", "live"))


def test_shell_wrapped_exec_inputs_are_not_misbound(tmp_path):
    runner, opts = setup(tmp_path)
    source = opts["app_root"] / "examples/github-sync/obsidian-github-writer.service"
    source.write_text("[Service]\nExecStart=/bin/sh -c 'run --config /etc/private/config.toml'\n")
    with pytest.raises(ProductionUpdateError, match="shell-command Exec inputs"):
        stage(runner, opts)
    assert _control(opts)["status"] == "failed"
    assert not _called(runner, ("--profile", "live"))


def test_failed_stage_with_failed_timer_stop_reports_unconfirmed_containment(tmp_path):
    runner, opts = setup(tmp_path, fail_disable=True)
    with pytest.raises(ProductionUpdateError, match="TIMER_CONTAINMENT_UNCONFIRMED"):
        stage(runner, opts)
    assert _control(opts)["status"] == "failed"
    assert runner.enabled and runner.active
    assert not _called(runner, ("--profile", "live"))


def test_failed_activate_with_failed_timer_stop_reports_unconfirmed_containment(tmp_path):
    runner, opts = setup(tmp_path)
    receipt = stage(runner, opts)
    runner.fail_profile = "live"
    runner.fail_disable = True
    with pytest.raises(ProductionUpdateError, match="TIMER_CONTAINMENT_UNCONFIRMED"):
        activate(receipt, opts)
    assert _control(opts)["status"] == "failed"
    assert _called(runner, ("--profile", "live"))


def test_stage_git_runner_strips_inherited_object_and_config_overrides(monkeypatch):
    import os
    from types import SimpleNamespace

    seen = []

    def fake_run(argv, **kwargs):
        seen.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(staged.subprocess, "run", fake_run)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    monkeypatch.setenv("GIT_NO_REPLACE_OBJECTS", "0")
    result = staged._trusted_stage_runner(("git", "-C", "/tmp/fixture", "status", "--porcelain"))
    assert result.returncode == 0 and result.stdout == "ok"
    argv, kwargs = seen[0]
    assert argv[:5] == (
        "/usr/bin/git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null"
    )
    assert kwargs["env"]["GIT_NO_REPLACE_OBJECTS"] == "1"
    assert kwargs["env"]["GIT_CONFIG_NOSYSTEM"] == "1"
    assert "GIT_CONFIG_COUNT" not in kwargs["env"]
    assert os.environ["GIT_CONFIG_COUNT"] == "1"


def test_real_consolidated_unit_templates_render_without_old_runtime(tmp_path):
    templates = Path(__file__).resolve().parents[1] / "examples/github-sync"
    app = tmp_path / "app"
    venv = tmp_path / "venv"
    units = tmp_path / "systemd"
    sources = app / "examples/github-sync"
    sources.mkdir(parents=True)
    units.mkdir()
    for source in templates.glob("obsidian-github-*"):
        if source.suffix in {".timer", ".service"}:
            (sources / source.name).write_bytes(source.read_bytes())
    installed = staged._install_stage_managed_units(app, venv, units)
    assert len(installed) == 11
    for name in installed:
        data = (units / name).read_text()
        assert "/opt/obsidian-github-sync/" not in data
        if name.endswith(".service"):
            assert f"ExecStart={venv}/bin/" in data
            if "WorkingDirectory=" in data:
                assert f"WorkingDirectory={app}" in data


def test_stage_unit_validation_before_any_unit_replacement(tmp_path):
    runner, opts = setup(tmp_path)
    first = opts["systemd_dir"] / "obsidian-github-compactor.service"
    original = first.read_bytes()
    last = opts["app_root"] / "examples/github-sync/obsidian-github-writer.service"
    last.write_text("[Service]\nExecStart=/opt/obsidian-github-sync/app/unsupported\n")
    with pytest.raises(ProductionUpdateError, match="unrendered legacy runtime"):
        stage(runner, opts)
    # Validation of the last source must not replace an earlier installed Unit.
    assert first.read_bytes() == original
    assert _control(opts)["status"] == "failed"


def test_pending_host_lifecycle_blocks_stage_and_activate_before_effects(tmp_path):
    runner, opts = setup(tmp_path)
    host = opts["receipt_dir"].parent / "host-receipts"
    host.mkdir(mode=0o700)
    pending = host / "pending-runtime.json"
    pending.write_text('{"phase":"incomplete"}\n')
    with pytest.raises(ProductionUpdateError, match="host lifecycle reconciliation"):
        stage(runner, opts)
    assert not runner.calls
    assert not (opts["receipt_dir"] / staged.STAGING_ROOT).exists()

    pending.unlink()
    receipt = stage(runner, opts)
    calls = list(runner.calls)
    pending.write_text('{"phase":"failed"}\n')
    with pytest.raises(ProductionUpdateError, match="host lifecycle reconciliation"):
        activate(receipt, opts)
    assert runner.calls == calls
    assert _control(opts)["status"] == "staged"
    assert not _called(runner, ("--profile", "live"))


def test_host_wide_lock_serializes_stage_with_legacy(tmp_path):
    runner, opts = setup(tmp_path)
    host = opts["receipt_dir"].parent / "host-receipts"
    with staged.legacy._host_update_guard(host, require_root=False):
        with pytest.raises(ProductionUpdateError, match="already in progress"):
            stage(runner, opts)
        with pytest.raises(ProductionUpdateError, match="already in progress"):
            staged.legacy.execute_update(
                target_sha=TARGET, app_root=opts["app_root"],
                venv_root=opts["venv_root"],
                systemd_dir=opts["systemd_dir"],
                receipt_dir=opts["receipt_dir"],
                daily_revision_env=opts["daily_revision_env"],
                runner=runner, require_root=False,
            )
    assert not runner.calls
    assert not (opts["receipt_dir"] / staged.STAGING_ROOT).exists()


def test_legacy_live_cannot_bypass_existing_stage(tmp_path):
    runner, opts = setup(tmp_path)
    stage(runner, opts)
    calls = list(runner.calls)
    with pytest.raises(ProductionUpdateError, match="unresolved staged transaction"):
        staged.legacy.execute_update(
            target_sha=TARGET, app_root=opts["app_root"],
            venv_root=opts["venv_root"],
            systemd_dir=opts["systemd_dir"],
            receipt_dir=opts["receipt_dir"],
            daily_revision_env=opts["daily_revision_env"],
            runner=runner, require_root=False,
        )
    assert runner.calls == calls
    assert _control(opts)["status"] == "staged"
    assert not _called(runner, ("--profile", "live"))


def test_first_staged_state_directory_parent_fsync_required(tmp_path, monkeypatch):
    runner, opts = setup(tmp_path)
    real_fsync = staged.os.fsync
    seen = []

    def fail_parent_fsync(fd):
        if staged.os.readlink(f"/proc/self/fd/{fd}") == str(opts["receipt_dir"]):
            seen.append("root-parent")
            raise OSError("injected new directory durability failure")
        return real_fsync(fd)

    with monkeypatch.context() as m:
        m.setattr(staged.os, "fsync", fail_parent_fsync)
        with pytest.raises(OSError, match="injected new directory durability"):
            stage(runner, opts)
    assert seen == ["root-parent"]
    assert not runner.calls
    assert not (opts["receipt_dir"] / staged.STAGING_ROOT / staged.CONTROL_FILE).exists()


@pytest.mark.parametrize("substate,next_elapse", [
    ("elapsed", "infinity"),
    ("waiting", "infinity"),
    ("waiting", "0"),
    ("waiting", ""),
    ("waiting", "forever"),
    ("waiting", "not-yet-armed"),
    ("waiting", "-1s"),
    ("waiting", "0s 0ms"),
    ("waiting", "2min garbage"),
    ("waiting", "1.2.3s"),
    ("waiting", "1fortnight"),
    ("waiting", "1e100s"),
    ("waiting", "1h  30min"),
])
def test_sync_timer_restore_requires_confirmed_next_firing(tmp_path, substate, next_elapse):
    runner, opts = setup(
        tmp_path, sync_substate=substate, sync_next_elapse=next_elapse
    )
    receipt = stage(runner, opts)
    with pytest.raises(ProductionUpdateError, match="restored Sync timer not armed"):
        activate(receipt, opts, sync=True)
    assert _control(opts)["status"] == "failed"
    assert not runner.enabled and not runner.active
    assert not runner.daily_enabled and not runner.daily_active


@pytest.mark.parametrize("next_elapse", [
    "5min",
    "1h 3min 4.2s",
    "1month 2w 5d 13h 51min 2.256ms",
    "0s 2.1ms",
])
def test_sync_timer_restore_waiting_with_next_firing_passes(tmp_path, next_elapse):
    runner, opts = setup(tmp_path, sync_substate="waiting", sync_next_elapse=next_elapse)
    receipt = stage(runner, opts)
    activate(receipt, opts, sync=True)
    assert runner.enabled and runner.active
    assert _control(opts)["status"] == "activated"


@pytest.mark.parametrize("dirname", ["obsidian-.service.d", "obsidian-github-.service.d"])
def test_dashed_prefix_dropins_block_before_stage_effects(tmp_path, dirname):
    runner, opts = setup(tmp_path)
    folder = opts["systemd_dir"] / dirname
    folder.mkdir()
    (folder / "override.conf").write_text("[Service]\nExecStart=\n")
    with pytest.raises(ProductionUpdateError, match="drop-in"):
        stage(runner, opts)
    assert _control(opts) == {"status": "none"}
    assert runner.enabled and runner.active
    assert not runner.calls or not _called(runner, ("disable", "--now"))


@pytest.mark.parametrize("command", [
    "|/bin/sh -c 'run --config /etc/private/hidden.toml'",
    "-|/bin/sh -c 'run --password-file /etc/private/secret'",
    "/usr/bin/env sh -c 'run --config /etc/private/hidden.toml'",
    "/bin/bash -lc 'run --config /etc/private/hidden.toml'",
])
def test_shell_wrapped_direct_inputs_are_rejected(tmp_path, command):
    runner, opts = setup(tmp_path)
    unit = opts["app_root"] / "examples/github-sync/obsidian-github-writer.service"
    unit.write_text(f"[Service]\nExecStart={command}\n")
    with pytest.raises(ProductionUpdateError, match="shell-command Exec inputs"):
        stage(runner, opts)
    assert _control(opts)["status"] == "failed"
    assert not _called(runner, ("--profile", "live"))


def test_stage_pip_subprocess_ignores_inherited_pip_and_python_settings(monkeypatch):
    from types import SimpleNamespace
    observed = []

    def fake_run(command, **kwargs):
        observed.append((command, kwargs["env"]))
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setenv("PIP_CONFIG_FILE", "/tmp/attacker-pip.conf")
    monkeypatch.setenv("PIP_INDEX_URL", "https://untrusted.example.invalid")
    monkeypatch.setenv("PYTHONPATH", "/tmp/malicious-package")
    monkeypatch.setenv("LD_PRELOAD", "/tmp/evil.so")
    monkeypatch.setenv("PATH", "/tmp/rogue")
    monkeypatch.setattr(staged.subprocess, "run", fake_run)
    pip = "/opt/obsidian-automation/venv/bin/pip"
    result = staged._trusted_stage_runner((pip, "install", "--no-deps", "/opt/app"))
    assert result.returncode == 0
    command, env = observed[0]
    assert command[0] == pip
    assert env["PIP_CONFIG_FILE"] == "/dev/null"
    assert env["PYTHONNOUSERSITE"] == "1"
    assert env["PATH"] == "/usr/sbin:/usr/bin:/sbin:/bin"
    for name in ("PIP_INDEX_URL", "PYTHONPATH", "LD_PRELOAD"):
        assert name not in env


@pytest.mark.parametrize("phase", ["preparing", "activating"])
def test_hard_process_loss_keeps_durable_control_and_blocks_reentry(tmp_path, phase):
    from multiprocessing import get_context
    import os

    runner, opts = setup(tmp_path)
    receipt = stage(runner, opts)
    before = list(runner.calls)

    def crash_after_marker():
        with staged._lock(opts["receipt_dir"], require_root=False) as state:
            staged._control_write(
                state, phase, TARGET, receipt if phase == "activating" else None
            )
            os._exit(73)

    worker = get_context("fork").Process(target=crash_after_marker)
    worker.start()
    worker.join(timeout=10)
    assert worker.exitcode == 73
    assert _control(opts)["status"] == phase
    with pytest.raises(ProductionUpdateError, match="matching inert stage"):
        activate(receipt, opts)
    with pytest.raises(ProductionUpdateError, match="unresolved staged transaction"):
        stage(runner, opts)
    assert runner.calls == before
    assert not _called(runner, ("--profile", "live"))


def test_immutable_receipt_link_failure_cannot_claim_staged(tmp_path, monkeypatch):
    runner, opts = setup(tmp_path)
    real_link = staged.os.link

    def refuse_stage_receipt(source, target, *args, **kwargs):
        if str(target).endswith(".staging-receipt.json"):
            raise OSError("injected receipt link failure")
        return real_link(source, target, *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(staged.os, "link", refuse_stage_receipt)
        with pytest.raises(OSError, match="receipt link failure"):
            stage(runner, opts)
    assert _control(opts)["status"] == "failed"
    assert not runner.enabled and not runner.active
    assert not _called(runner, ("--profile", "live"))


def test_root_stage_receipt_redirection_cannot_bypass_shared_authority(tmp_path, monkeypatch):
    runner, opts = setup(tmp_path)
    monkeypatch.setattr(staged.os, "geteuid", lambda: 0)
    with pytest.raises(ProductionUpdateError, match="receipt root is fixed"):
        staged.stage_update(target_sha=TARGET, **{**opts, "require_root": True})
    with pytest.raises(ProductionUpdateError, match="receipt root is fixed"):
        staged.legacy.execute_update(
            target_sha=TARGET, app_root=opts["app_root"],
            venv_root=opts["venv_root"], systemd_dir=opts["systemd_dir"],
            receipt_dir=opts["receipt_dir"],
            daily_revision_env=opts["daily_revision_env"],
            runner=runner, require_root=True,
        )
    assert not runner.calls


def test_retry_after_failed_stage_parent_fsync_rechecks_durability(tmp_path, monkeypatch):
    runner, opts = setup(tmp_path)
    real_fsync = staged.os.fsync
    attempts = []

    def fail_parent(fd):
        if staged.os.readlink(f"/proc/self/fd/{fd}") == str(opts["receipt_dir"]):
            attempts.append("root-parent")
            raise OSError("injected parent fsync failure")
        return real_fsync(fd)

    for _ in range(2):
        with monkeypatch.context() as m:
            m.setattr(staged.os, "fsync", fail_parent)
            with pytest.raises(OSError, match="injected parent fsync failure"):
                stage(runner, opts)
        assert not runner.calls
        assert (opts["receipt_dir"] / staged.STAGING_ROOT).is_dir()
        assert not (opts["receipt_dir"] / staged.STAGING_ROOT / staged.CONTROL_FILE).exists()
    # On the second attempt the directory already existed. Stage still had to
    # call fsync on the receipt parent, instead of inferring durability.
    assert attempts == ["root-parent", "root-parent"]
    assert len(stage(runner, opts)) == 64
    assert _control(opts)["status"] == "staged"
    assert not _called(runner, ("--profile", "live"))


def test_stage_pip_uses_preprovisioned_offline_build_backend_only(tmp_path):
    runner, opts = setup(tmp_path)
    stage(runner, opts)
    pip_executable = str(opts["venv_root"] / "bin" / "pip")
    invocations = [call for call in runner.calls if call[:1] == (pip_executable,)]
    assert len(invocations) == 1
    argv = invocations[0]
    assert argv[1:2] == ("install",)
    assert "--no-index" in argv
    assert "--no-build-isolation" in argv
    assert "--no-cache-dir" in argv
    assert "--no-deps" in argv
    assert "--force-reinstall" in argv
    assert argv[-1] == str(opts["app_root"])
    assert not any(part.startswith(("https://", "http://")) for part in argv)
    assert not _called(runner, ("--profile", "live"))


def _trusted_build_venv_fixture(tmp_path, *, venv=None, python_version="python3.13"):
    venv = (tmp_path / "build-venv") if venv is None else venv
    bin_dir = venv / "bin"
    site = venv / "lib" / python_version / "site-packages"
    bin_dir.mkdir(parents=True, exist_ok=True)
    site.mkdir(parents=True, exist_ok=True)
    (bin_dir / "python").unlink(missing_ok=True)
    (bin_dir / "python3").unlink(missing_ok=True)
    (bin_dir / "python").symlink_to("/usr/bin/python3")
    (bin_dir / "python3").symlink_to("/usr/bin/python3")
    (venv / "pyvenv.cfg").write_text(
        "home = /usr/bin\ninclude-system-site-packages = false\n"
    )
    pip = bin_dir / "pip"
    pip.write_text(f"#!{bin_dir}/python3\nprint('simulated pip')\n")
    pip.chmod(0o755)
    for project, version in (("pip", "25.1.1"), ("setuptools", "78.1.1"), ("wheel", "0.46.1")):
        module = site / project
        module.mkdir()
        (module / "__init__.py").write_text("# test-only\n")
        metadata = site / f"{project}-{version}.dist-info"
        metadata.mkdir()
        (metadata / "METADATA").write_text(f"Name: {project}\nVersion: {version}\n")
    (site / "distutils-precedence.pth").write_text(
        "import os; var = 'SETUPTOOLS_USE_DISTUTILS'; enabled = "
        "os.environ.get(var, 'local') == 'local'; enabled and "
        "__import__('_distutils_hack').add_shim(); \n"
    )
    return venv, site


def test_stage_trusted_local_pip_backend_preflight_accepts_private_fixture(tmp_path):
    venv, site = _trusted_build_venv_fixture(tmp_path)
    manifest = staged._assert_trusted_stage_build_venv(
        venv, trusted_uid=os.geteuid()
    )
    assert manifest["setuptools"] == "78.1.1"
    assert manifest["pip"] == "25.1.1"
    assert manifest["wheel"] == "0.46.1"
    assert len(manifest["digest_sha256"]) == 64
    assert manifest == staged._assert_trusted_stage_build_venv(
        venv, trusted_uid=os.geteuid()
    )
    assert (site / "setuptools-78.1.1.dist-info/METADATA").exists()
    (site / "setuptools/__init__.py").write_text("# changed bytes\n")
    changed = staged._assert_trusted_stage_build_venv(
        venv, trusted_uid=os.geteuid()
    )
    assert changed["digest_sha256"] != manifest["digest_sha256"]


@pytest.mark.parametrize("corruption", (
    "world_writable_pip", "symlinked_pip", "world_writable_backend",
    "symlinked_backend", "hardlinked_module", "writable_pth",
    "missing_setuptools", "old_setuptools", "writable_venv",
    "untrusted_python_target", "writable_pyvenv_config", "invalid_pip_shebang",
    "system_site_packages_enabled", "root_owned_external_pth",
    "modified_setuptools_pth"
))
def test_untrusted_local_stage_build_source_is_rejected(tmp_path, corruption):
    venv, site = _trusted_build_venv_fixture(tmp_path)
    pip = venv / "bin/pip"
    backend = site / "setuptools"
    if corruption == "world_writable_pip":
        pip.chmod(0o777)
    elif corruption == "symlinked_pip":
        pip.unlink()
        pip.symlink_to("/bin/sh")
    elif corruption == "world_writable_backend":
        backend.chmod(0o777)
    elif corruption == "symlinked_backend":
        (backend / "__init__.py").unlink()
        (backend / "__init__.py").symlink_to("/etc/passwd")
    elif corruption == "hardlinked_module":
        file = backend / "__init__.py"
        os.link(file, backend / "hardlinked.py")
    elif corruption == "writable_pth":
        (site / "distutils-precedence.pth").chmod(0o666)
    elif corruption == "missing_setuptools":
        (site / "setuptools-78.1.1.dist-info").rename(site / "setuptools-tampered-info")
    elif corruption == "old_setuptools":
        file = site / "setuptools-78.1.1.dist-info/METADATA"
        file.write_text("Name: setuptools\nVersion: 74.0.0\n")
    elif corruption == "untrusted_python_target":
        (venv / "bin/python3").unlink()
        (venv / "bin/python3").symlink_to("/bin/sh")
    elif corruption == "writable_pyvenv_config":
        (venv / "pyvenv.cfg").chmod(0o666)
    elif corruption == "invalid_pip_shebang":
        pip.write_text("#!/usr/bin/env python3\n")
    elif corruption == "system_site_packages_enabled":
        (venv / "pyvenv.cfg").write_text(
            "home = /usr/bin\ninclude-system-site-packages = true\n"
        )
    elif corruption == "root_owned_external_pth":
        outside = tmp_path / "writable-external-package"
        outside.mkdir()
        outside.chmod(0o777)
        (site / "unreviewed-path.pth").write_text(str(outside) + "\n")
    elif corruption == "modified_setuptools_pth":
        (site / "distutils-precedence.pth").write_text(
            "import os; __import__('malicious_startup')\n"
        )
    else:
        venv.chmod(0o777)
    with pytest.raises(ProductionUpdateError, match="Stage|stage|venv|build|setuptools"):
        staged._assert_trusted_stage_build_venv(venv, trusted_uid=os.geteuid())


def test_trusted_build_manifest_excludes_only_reinstalled_app(tmp_path):
    venv, site = _trusted_build_venv_fixture(tmp_path)
    before = staged._assert_trusted_stage_build_venv(
        venv, trusted_uid=os.geteuid()
    )
    app = site / "obsidian_automation"
    app.mkdir()
    (app / "__init__.py").write_text("# replaced application module\n")
    dist = site / "obsidian_automation-0.1.0.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text("Name: obsidian-automation\nVersion: 0.1.0\n")
    after_install = staged._assert_trusted_stage_build_venv(
        venv, trusted_uid=os.geteuid()
    )
    assert before == after_install
    (site / "unknown-extra-root-managed.py").write_text("# unexpected import code\n")
    after_import_drift = staged._assert_trusted_stage_build_venv(
        venv, trusted_uid=os.geteuid()
    )
    assert before["digest_sha256"] != after_import_drift["digest_sha256"]


def test_stage_receipt_rejects_untrusted_build_manifest_shape(tmp_path):
    import hashlib

    runner, opts = setup(tmp_path)
    original_sha = stage(runner, opts)
    state = opts["receipt_dir"] / staged.STAGING_ROOT
    original = staged._receipt_read(state, original_sha)
    assert original["build_manifest"] is None  # simulated, no root authorization

    for bad in (
        "unbound backend",
        {"digest_sha256": "not-a-sha", "pip": "25.1.1",
         "setuptools": "78.1.1", "wheel": "0.46.1"},
        {"digest_sha256": "a" * 64, "pip": "25.1.1",
         "setuptools": 78, "wheel": "0.46.1"},
    ):
        tampered = dict(original, build_manifest=bad)
        payload = staged._encode(tampered)
        sha = hashlib.sha256(payload).hexdigest()
        (state / f"{sha}.staging-receipt.json").write_bytes(payload)
        with pytest.raises(ProductionUpdateError, match="build provenance"):
            staged._receipt_read(state, sha)


@pytest.mark.parametrize("injected_name", (
    "github_production_staging.cpython-313-x86_64-linux-gnu.so",
    "github_production_staging.pyd",
    "unknown-executable.sh",
))
def test_stage_rejects_unaccounted_installed_app_executables(tmp_path, injected_name):
    runner, opts = setup(tmp_path)
    package = (
        opts["venv_root"] / "lib" / "python3.12"
        / "site-packages" / "obsidian_automation"
    )
    (package / injected_name).write_bytes(b"unreviewed code bytes")
    with pytest.raises(ProductionUpdateError, match="unreviewed executable app file"):
        stage(runner, opts)
    assert _control(opts)["status"] == "failed"
    assert not _called(runner, ("--profile", "live"))


def test_stage_rejects_orphan_bytecode(tmp_path):
    runner, opts = setup(tmp_path)
    package = (
        opts["venv_root"] / "lib" / "python3.12"
        / "site-packages" / "obsidian_automation"
    )
    cached = package / "__pycache__"
    cached.mkdir()
    (cached / "orphan.cpython-312.pyc").write_bytes(b"no source module")
    with pytest.raises(ProductionUpdateError, match="orphan installed application bytecode"):
        stage(runner, opts)
    assert _control(opts)["status"] == "failed"
    assert not _called(runner, ("--profile", "live"))


def test_stage_binds_supported_bytecode_cache_and_blocks_activation_drift(tmp_path):
    runner, opts = setup(tmp_path)
    package = (
        opts["venv_root"] / "lib" / "python3.12"
        / "site-packages" / "obsidian_automation"
    )
    cached = package / "__pycache__"
    cached.mkdir()
    file = cached / "github_production_staging.cpython-312.pyc"
    file.write_bytes(b"trusted review fixture bytes")
    receipt = stage(runner, opts)
    record = staged._receipt_read(opts["receipt_dir"] / staged.STAGING_ROOT, receipt)
    assert len(record["code_manifest"]["bytecode:manifest"]) == 64
    file.write_bytes(b"changed bytecode after Stage")
    with pytest.raises(ProductionUpdateError, match="installed package drift"):
        activate(receipt, opts)
    assert _control(opts)["status"] == "staged"
    assert not _called(runner, ("--profile", "live"))


def test_stage_receipt_v2_keeps_host_control_v1_compatibility(tmp_path):
    runner, opts = setup(tmp_path)
    digest = stage(runner, opts)
    root = opts["receipt_dir"] / staged.STAGING_ROOT
    record = staged._receipt_read(root, digest)
    control = staged._control_read(root)
    assert record["record_version"] == staged.STAGE_RECEIPT_VERSION == 2
    assert control["record_version"] == staged.VERSION == 1
    assert record["build_manifest"] is None  # offline nonroot simulation only


def test_legacy_v1_stage_receipt_never_authorizes_new_activate(tmp_path):
    import hashlib

    runner, opts = setup(tmp_path)
    digest = stage(runner, opts)
    root = opts["receipt_dir"] / staged.STAGING_ROOT
    v2 = staged._receipt_read(root, digest)
    v1 = dict(v2)
    v1["record_version"] = 1
    del v1["build_manifest"]
    raw = staged._encode(v1)
    v1_digest = hashlib.sha256(raw).hexdigest()
    (root / f"{v1_digest}.staging-receipt.json").write_bytes(raw)
    with pytest.raises(ProductionUpdateError, match="stage receipt schema differs"):
        staged._receipt_read(root, v1_digest)
    assert _control(opts)["status"] == "staged"
    assert not _called(runner, ("--profile", "live"))


def _simulated_privileged_stage(tmp_path, monkeypatch):
    """Run root-mode Stage only inside ephemeral fixture paths (never /opt)."""
    runner, opts = setup(tmp_path)
    venv, site = _trusted_build_venv_fixture(
        tmp_path, venv=opts["venv_root"], python_version="python3.12"
    )
    host = tmp_path / "host-receipts"
    host.mkdir(mode=0o700)
    monkeypatch.setattr(staged, "PRODUCTION_STAGE_VENV_ROOT", venv)
    monkeypatch.setattr(staged.legacy, "DEFAULT_RECEIPT_DIR", opts["receipt_dir"])
    monkeypatch.setattr(staged.legacy, "HOST_RECEIPT_DIR", host)
    opts["require_root"] = True
    return runner, opts, site


@pytest.mark.skipif(os.geteuid() != 0, reason="isolated root-mode fixture requires root")
def test_privileged_stage_receipt_binds_backend_then_rechecks_activation(tmp_path, monkeypatch):
    runner, opts, site = _simulated_privileged_stage(tmp_path, monkeypatch)
    digest = stage(runner, opts)
    root = opts["receipt_dir"] / staged.STAGING_ROOT
    record = staged._receipt_read(root, digest)
    assert record["record_version"] == staged.STAGE_RECEIPT_VERSION == 2
    assert record["build_manifest"]["setuptools"] == "78.1.1"
    assert len(record["build_manifest"]["digest_sha256"]) == 64
    (site / "setuptools/__init__.py").write_text("# root fixture changed after Stage\n")
    with pytest.raises(ProductionUpdateError, match="Stage build environment changed"):
        activate(digest, opts)
    assert _control(opts)["status"] == "staged"
    assert not _called(runner, ("--profile", "live"))


@pytest.mark.skipif(os.geteuid() != 0, reason="isolated root-mode fixture requires root")
def test_privileged_stage_rejects_root_owned_external_pth_before_effect(tmp_path, monkeypatch):
    runner, opts, site = _simulated_privileged_stage(tmp_path, monkeypatch)
    target = tmp_path / "unsafe-external-import"
    target.mkdir()
    target.chmod(0o777)
    (site / "unexpected.pth").write_text(str(target) + "\n")
    with pytest.raises(ProductionUpdateError, match="unreviewed Stage .pth startup"):
        stage(runner, opts)
    assert _control(opts)["status"] == "none"
    assert not any(c[:2] == ("systemctl", "disable") for c in runner.calls)
    assert not _called(runner, ("--profile", "live"))


@pytest.mark.skipif(os.geteuid() != 0, reason="isolated root-mode fixture requires root")
def test_privileged_stage_receipt_activates_only_with_simulated_consent(tmp_path, monkeypatch):
    runner, opts, _ = _simulated_privileged_stage(tmp_path, monkeypatch)
    digest = stage(runner, opts)
    before = list(runner.calls)
    with pytest.raises(ProductionUpdateError, match="approval"):
        activate(digest, opts, approve=False)
    assert before == runner.calls
    receipt = activate(digest, opts)
    assert len(receipt) == 64
    assert _control(opts)["status"] == "activated"
    assert _called(runner, ("--profile", "live"))  # FakeRunner only; no real Writer
