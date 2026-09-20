from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


def _load_stage_tool():
    path = Path("tools/stage_automation_units.py")
    spec = importlib.util.spec_from_file_location("stage_automation_units_tool", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


stage = _load_stage_tool()
TARGET = "a" * 40


class FakeRunner:
    def __init__(self, source_root: Path):
        self.source_root = source_root
        self.commands: list[tuple[str, ...]] = []

    def __call__(self, argv):
        args = tuple(str(item) for item in argv)
        self.commands.append(args)

        if args[:3] == ("git", "-C", str(self.source_root)):
            if args[3:] == ("rev-parse", "HEAD"):
                return stage.CommandResult(0, TARGET + "\n", "")
            if args[3:] == ("status", "--porcelain"):
                return stage.CommandResult(0, "", "")

        if args[:2] == ("systemctl", "show"):
            return stage.CommandResult(0, "not-found\n", "")

        if args[:2] == ("systemctl", "is-enabled"):
            return stage.CommandResult(1, "disabled\n", "")

        if args[:2] == ("systemctl", "is-active"):
            return stage.CommandResult(3, "inactive\n", "")

        if args[:2] in {
            ("systemctl", "disable"),
            ("systemctl", "stop"),
            ("systemctl", "daemon-reload"),
        }:
            return stage.CommandResult(0, "", "")

        return stage.CommandResult(0, "", "")


def _fixture_source(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    root.mkdir()

    for unit, relative in stage.SOURCE_LAYOUT.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)

        if unit.endswith(".timer"):
            target.write_text(
                "[Unit]\nDescription=test\n"
                "[Timer]\nOnBootSec=1min\n"
                f"Unit={unit.removesuffix('.timer')}.service\n",
                encoding="utf-8",
            )
            continue

        prefix = "/opt/obsidian-ai/venv/bin"
        working = ""
        if unit.startswith("obsidian-github-"):
            prefix = "/opt/obsidian-github-sync/venv/bin"
            if unit != "obsidian-github-sync-vault-pull.service":
                working = "WorkingDirectory=/opt/obsidian-github-sync/app\n"
        elif unit.startswith("obsidian-core-promotion"):
            prefix = "/opt/obsidian-core-promotion/venv/bin"

        target.write_text(
            "[Unit]\nDescription=test\n"
            "[Service]\nType=oneshot\n"
            f"ExecStart={prefix}/example-entrypoint\n"
            + working,
            encoding="utf-8",
        )

    return root


def test_managed_unit_set_is_complete_and_unique() -> None:
    names = set(stage.SOURCE_LAYOUT)
    assert len(names) == 16
    assert names == set(
        (*stage.AI_UNITS, *stage.GITHUB_UNITS, *stage.PROMOTION_UNITS)
    )
    assert set(stage.TIMER_UNITS) == {
        "obsidian-ai-vault-pull.timer",
        "obsidian-pre-review.timer",
        "obsidian-github-sync.timer",
        "obsidian-core-promotion.timer",
    }


def test_render_rewrites_all_legacy_runtime_paths() -> None:
    source = """
ExecStart=/opt/obsidian-ai/venv/bin/a
ExecStart=/opt/obsidian-github-sync/venv/bin/b
ExecStart=/opt/obsidian-core-promotion/venv/bin/c
WorkingDirectory=/opt/obsidian-github-sync/app
"""
    rendered = stage._render_unit(source)

    assert "/opt/obsidian-ai/" not in rendered
    assert "/opt/obsidian-github-sync/" not in rendered
    assert "/opt/obsidian-core-promotion/" not in rendered
    assert rendered.count("/opt/obsidian-automation/venv/bin") == 3
    assert (
        "WorkingDirectory=/opt/obsidian-automation/app"
        in rendered
    )


def test_stage_installs_units_and_leaves_host_inert(tmp_path: Path) -> None:
    source_root = _fixture_source(tmp_path)
    systemd_dir = tmp_path / "systemd"
    systemd_dir.mkdir()

    config_dir = tmp_path / "etc/obsidian-ai"
    config_dir.mkdir(parents=True)
    revision_env = config_dir / "pre-review-revision.env"

    runner = FakeRunner(source_root)

    result = stage.stage_units(
        target_sha=TARGET,
        source_root=source_root,
        systemd_dir=systemd_dir,
        revision_env=revision_env,
        runner=runner,
        require_root=False,
    )

    assert result["result"] == "passed"
    assert result["installed_unit_count"] == 15
    assert result["timers_enabled"] is False
    assert result["timers_active"] is False
    assert result["services_active"] is False
    assert result["production_activation"] == "not_attempted"

    assert revision_env.read_text() == (
        f"OBSIDIAN_AUTOMATION_REVISION={TARGET}\n"
    )
    assert revision_env.stat().st_mode & 0o777 == 0o644

    for unit in stage.SOURCE_LAYOUT:
        installed = (systemd_dir / unit).read_text()
        assert all(
            prefix not in installed
            for prefix in stage.LEGACY_PREFIXES
        )
        if unit.endswith(".service"):
            assert stage.CONSOLIDATED_VENV_BIN in installed

    for unit in (
        "obsidian-github-sync.service",
        "obsidian-github-writer.service",
        "obsidian-github-compactor.service",
    ):
        installed = (systemd_dir / unit).read_text()
        assert (
            f"WorkingDirectory={stage.CONSOLIDATED_APP_ROOT}"
            in installed
        )

    assert ("systemctl", "daemon-reload") in runner.commands
    for timer in stage.TIMER_UNITS:
        assert (
            "systemctl",
            "disable",
            "--now",
            timer,
        ) in runner.commands


class ExistingActiveRunner(FakeRunner):
    def __call__(self, argv):
        args = tuple(str(item) for item in argv)
        if (
            args[:2] == ("systemctl", "show")
            and args[2] == "obsidian-pre-review.timer"
        ):
            return stage.CommandResult(0, "loaded\n", "")
        if args == (
            "systemctl",
            "is-enabled",
            "obsidian-pre-review.timer",
        ):
            return stage.CommandResult(0, "enabled\n", "")
        if args == (
            "systemctl",
            "is-active",
            "obsidian-pre-review.timer",
        ):
            return stage.CommandResult(0, "active\n", "")
        return super().__call__(argv)


def test_stage_refuses_enabled_or_active_existing_timer(tmp_path: Path) -> None:
    source_root = _fixture_source(tmp_path)
    systemd_dir = tmp_path / "systemd"
    systemd_dir.mkdir()
    config_dir = tmp_path / "etc/obsidian-ai"
    config_dir.mkdir(parents=True)

    runner = ExistingActiveRunner(source_root)

    try:
        stage.stage_units(
            target_sha=TARGET,
            source_root=source_root,
            systemd_dir=systemd_dir,
            revision_env=config_dir / "pre-review-revision.env",
            runner=runner,
            require_root=False,
        )
    except stage.UnitStagingError as exc:
        assert str(exc) == "timer_enabled:obsidian-pre-review.timer"
    else:
        raise AssertionError("active production timer was not rejected")


def test_stage_requires_exact_clean_target(tmp_path: Path) -> None:
    source_root = _fixture_source(tmp_path)
    runner = FakeRunner(source_root)

    try:
        stage.stage_units(
            target_sha="b" * 40,
            source_root=source_root,
            systemd_dir=tmp_path,
            revision_env=tmp_path / "revision.env",
            runner=runner,
            require_root=False,
        )
    except stage.UnitStagingError as exc:
        assert str(exc) == "source_root_not_exact_target"
    else:
        raise AssertionError("wrong target source was not rejected")


def test_real_timer_sources_rearm_from_timer_activation() -> None:
    for timer in stage.TIMER_UNITS:
        source = Path(stage.SOURCE_LAYOUT[timer])
        text = source.read_text(encoding="utf-8")
        assert "OnActiveSec=" in text, timer
        assert "OnBootSec=" not in text, timer


def test_real_unit_sources_render_to_consolidated_paths() -> None:
    for unit, relative in stage.SOURCE_LAYOUT.items():
        source = Path(relative)
        assert source.is_file(), unit
        rendered = stage._render_unit(source.read_text(encoding="utf-8"))

        assert all(prefix not in rendered for prefix in stage.LEGACY_PREFIXES)
        if unit.endswith(".service"):
            assert stage.CONSOLIDATED_VENV_BIN in rendered

    for unit in (
        "obsidian-github-sync.service",
        "obsidian-github-writer.service",
        "obsidian-github-compactor.service",
    ):
        source = Path(stage.SOURCE_LAYOUT[unit])
        rendered = stage._render_unit(source.read_text(encoding="utf-8"))
        assert (
            f"WorkingDirectory={stage.CONSOLIDATED_APP_ROOT}"
            in rendered
        )
