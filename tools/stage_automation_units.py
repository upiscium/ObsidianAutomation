#!/usr/bin/env python3
"""Stage consolidated ObsidianAutomation production systemd units inertly.

This tool is intentionally for a non-serving host. It refuses to operate if any
managed timer is enabled/active or any managed service is active. It installs
the reviewed unit sources with consolidated /opt paths, writes the derived AI
revision env, reloads systemd, and leaves every timer disabled/inactive.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
from typing import Callable, Iterable, Sequence


DEFAULT_SOURCE_ROOT = Path("/opt/obsidian-automation/app")
DEFAULT_SYSTEMD_DIR = Path("/etc/systemd/system")
DEFAULT_REVISION_ENV = Path("/etc/obsidian-ai/pre-review-revision.env")
CONSOLIDATED_APP_ROOT = "/opt/obsidian-automation/app"
CONSOLIDATED_VENV_BIN = "/opt/obsidian-automation/venv/bin"
_SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")


class UnitStagingError(RuntimeError):
    """Raised when inert systemd unit staging cannot complete safely."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[Sequence[str]], CommandResult]


def _default_runner(argv: Sequence[str]) -> CommandResult:
    completed = subprocess.run(
        [str(item) for item in argv],
        check=False,
        capture_output=True,
        text=True,
    )
    return CommandResult(
        completed.returncode,
        completed.stdout,
        completed.stderr,
    )


AI_UNITS = (
    "obsidian-ai-vault-pull.service",
    "obsidian-ai-vault-pull.timer",
    "obsidian-pre-review-generator.service",
    "obsidian-pre-review-validator.service",
    "obsidian-pre-review-reader.service",
    "obsidian-pre-review-evaluator.service",
    "obsidian-pre-review-status.service",
    "obsidian-pre-review.timer",
)

GITHUB_UNITS = (
    "obsidian-github-sync-vault-pull.service",
    "obsidian-github-sync.service",
    "obsidian-github-writer.service",
    "obsidian-github-compactor.service",
    "obsidian-github-sync.timer",
)

PROMOTION_UNITS = (
    "obsidian-core-promotion.service",
    "obsidian-core-promotion.timer",
)

TIMER_UNITS = (
    "obsidian-ai-vault-pull.timer",
    "obsidian-pre-review.timer",
    "obsidian-github-sync.timer",
    "obsidian-core-promotion.timer",
)

SERVICE_UNITS = tuple(
    name
    for name in (*AI_UNITS, *GITHUB_UNITS, *PROMOTION_UNITS)
    if name.endswith(".service")
)

SOURCE_LAYOUT: dict[str, Path] = {
    **{
        name: Path("examples/ai") / name
        for name in AI_UNITS
    },
    **{
        name: Path("examples/github-sync") / name
        for name in GITHUB_UNITS
    },
    **{
        name: Path("examples/promotion") / name
        for name in PROMOTION_UNITS
    },
}

LEGACY_PREFIXES = (
    "/opt/obsidian-ai/venv/bin",
    "/opt/obsidian-github-sync/venv/bin",
    "/opt/obsidian-core-promotion/venv/bin",
    "/opt/obsidian-github-sync/app",
)


def _run(
    runner: Runner,
    argv: Sequence[str],
    *,
    label: str,
    ok: tuple[int, ...] = (0,),
) -> CommandResult:
    result = runner(tuple(str(item) for item in argv))
    if result.returncode not in ok:
        raise UnitStagingError(
            f"{label} failed with exit status {result.returncode}"
        )
    return result


def _require_dir(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise UnitStagingError(f"{label}_missing") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise UnitStagingError(f"{label}_unsafe")


def _require_regular(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise UnitStagingError(f"{label}_missing") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise UnitStagingError(f"{label}_unsafe")


def _git_output(
    runner: Runner,
    root: Path,
    *args: str,
    label: str,
) -> str:
    return _run(
        runner,
        ("git", "-C", str(root), *args),
        label=label,
    ).stdout.strip()


def _verify_source(
    source_root: Path,
    target_sha: str,
    runner: Runner,
) -> None:
    if _SHA_RE.fullmatch(target_sha) is None:
        raise UnitStagingError("invalid_target_sha")
    _require_dir(source_root, "source_root")
    if _git_output(
        runner,
        source_root,
        "rev-parse",
        "HEAD",
        label="read source HEAD",
    ) != target_sha:
        raise UnitStagingError("source_root_not_exact_target")
    if _git_output(
        runner,
        source_root,
        "status",
        "--porcelain",
        label="read source status",
    ):
        raise UnitStagingError("source_root_not_clean")


def _unit_load_state(runner: Runner, unit: str) -> str:
    result = runner(
        (
            "systemctl",
            "show",
            unit,
            "--property=LoadState",
            "--value",
        )
    )
    if result.returncode != 0:
        return "not-found"
    value = result.stdout.strip()
    return value or "not-found"


def _unit_active_state(runner: Runner, unit: str) -> str:
    result = runner(("systemctl", "is-active", unit))
    value = result.stdout.strip()
    if value in {"active", "inactive", "failed", "activating", "deactivating"}:
        return value
    if result.returncode != 0:
        return "inactive"
    raise UnitStagingError(f"unexpected_active_state:{unit}")


def _unit_enabled_state(runner: Runner, unit: str) -> str:
    result = runner(("systemctl", "is-enabled", unit))
    value = result.stdout.strip()
    if value in {
        "enabled",
        "disabled",
        "static",
        "indirect",
        "masked",
        "generated",
        "transient",
        "linked",
        "linked-runtime",
        "alias",
    }:
        return value
    if result.returncode != 0 and value in {"", "not-found"}:
        return "not-found"
    raise UnitStagingError(f"unexpected_enabled_state:{unit}")


def _preflight_inert(runner: Runner) -> None:
    for unit in TIMER_UNITS:
        load = _unit_load_state(runner, unit)
        if load == "not-found":
            continue
        enabled = _unit_enabled_state(runner, unit)
        active = _unit_active_state(runner, unit)
        if enabled == "enabled":
            raise UnitStagingError(f"timer_enabled:{unit}")
        if active != "inactive":
            raise UnitStagingError(f"timer_not_inactive:{unit}")

    for unit in SERVICE_UNITS:
        load = _unit_load_state(runner, unit)
        if load == "not-found":
            continue
        active = _unit_active_state(runner, unit)
        if active != "inactive":
            raise UnitStagingError(f"service_not_inactive:{unit}")


def _render_unit(source: str) -> str:
    rendered = source
    rendered = rendered.replace(
        "/opt/obsidian-ai/venv/bin",
        CONSOLIDATED_VENV_BIN,
    )
    rendered = rendered.replace(
        "/opt/obsidian-github-sync/venv/bin",
        CONSOLIDATED_VENV_BIN,
    )
    rendered = rendered.replace(
        "/opt/obsidian-core-promotion/venv/bin",
        CONSOLIDATED_VENV_BIN,
    )
    rendered = rendered.replace(
        "WorkingDirectory=/opt/obsidian-github-sync/app",
        f"WorkingDirectory={CONSOLIDATED_APP_ROOT}",
    )
    return rendered


def _atomic_install(data: bytes, destination: Path, mode: int) -> None:
    _require_dir(destination.parent, "destination_parent")
    if os.path.lexists(destination) and destination.is_symlink():
        raise UnitStagingError("destination_symlink")

    fd, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        dir=destination.parent,
    )
    temporary_path = Path(temporary)
    try:
        os.fchmod(fd, mode)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise UnitStagingError("short_write")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)

    try:
        os.replace(temporary_path, destination)
        dir_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _install_units(
    source_root: Path,
    systemd_dir: Path,
) -> tuple[str, ...]:
    _require_dir(systemd_dir, "systemd_dir")
    installed: list[str] = []

    for unit in sorted(SOURCE_LAYOUT):
        relative = SOURCE_LAYOUT[unit]
        source = source_root / relative
        _require_regular(source, f"source_unit:{unit}")

        rendered = _render_unit(source.read_text(encoding="utf-8"))
        if any(prefix in rendered for prefix in LEGACY_PREFIXES):
            raise UnitStagingError(f"legacy_path_remains:{unit}")

        if unit.endswith(".service"):
            if CONSOLIDATED_VENV_BIN not in rendered:
                raise UnitStagingError(f"consolidated_venv_missing:{unit}")
        if unit in {
            "obsidian-github-sync.service",
            "obsidian-github-writer.service",
            "obsidian-github-compactor.service",
        }:
            if f"WorkingDirectory={CONSOLIDATED_APP_ROOT}" not in rendered:
                raise UnitStagingError(f"working_directory_not_consolidated:{unit}")

        _atomic_install(
            rendered.encode("utf-8"),
            systemd_dir / unit,
            0o644,
        )
        installed.append(unit)

    return tuple(installed)


def _write_revision_env(path: Path, target_sha: str) -> None:
    _require_dir(path.parent, "revision_env_parent")
    _atomic_install(
        f"OBSIDIAN_AUTOMATION_REVISION={target_sha}\n".encode("utf-8"),
        path,
        0o644,
    )
    os.chown(path, 0, 0)


def _leave_inert(runner: Runner) -> None:
    for unit in TIMER_UNITS:
        _run(
            runner,
            ("systemctl", "disable", "--now", unit),
            label=f"disable timer {unit}",
        )
    for unit in SERVICE_UNITS:
        _run(
            runner,
            ("systemctl", "stop", unit),
            label=f"stop service {unit}",
        )

    for unit in TIMER_UNITS:
        if _unit_enabled_state(runner, unit) != "disabled":
            raise UnitStagingError(f"timer_not_disabled_after_stage:{unit}")
        if _unit_active_state(runner, unit) != "inactive":
            raise UnitStagingError(f"timer_not_inactive_after_stage:{unit}")

    for unit in SERVICE_UNITS:
        if _unit_active_state(runner, unit) != "inactive":
            raise UnitStagingError(f"service_active_after_stage:{unit}")


def stage_units(
    *,
    target_sha: str,
    source_root: Path = DEFAULT_SOURCE_ROOT,
    systemd_dir: Path = DEFAULT_SYSTEMD_DIR,
    revision_env: Path = DEFAULT_REVISION_ENV,
    runner: Runner = _default_runner,
    require_root: bool = True,
) -> dict[str, object]:
    if require_root and os.geteuid() != 0:
        raise UnitStagingError("unit_staging_requires_root")

    _verify_source(source_root, target_sha, runner)
    _preflight_inert(runner)
    installed = _install_units(source_root, systemd_dir)
    _write_revision_env(revision_env, target_sha)

    _run(
        runner,
        ("systemctl", "daemon-reload"),
        label="systemd daemon-reload",
    )
    _leave_inert(runner)

    return {
        "record_version": 1,
        "stage": "consolidated_unit_staging",
        "target_sha": target_sha,
        "installed_unit_count": len(installed),
        "installed_units": list(installed),
        "revision_env": str(revision_env),
        "timers_enabled": False,
        "timers_active": False,
        "services_active": False,
        "production_activation": "not_attempted",
        "result": "passed",
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="obsidian-automation-stage-units",
        description=(
            "Install consolidated production units only when the host is inert. "
            "All managed timers remain disabled/inactive."
        ),
    )
    parser.add_argument("--target-sha", required=True)
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    parser.add_argument("--systemd-dir", type=Path, default=DEFAULT_SYSTEMD_DIR)
    parser.add_argument("--revision-env", type=Path, default=DEFAULT_REVISION_ENV)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        result = stage_units(
            target_sha=args.target_sha,
            source_root=args.source_root,
            systemd_dir=args.systemd_dir,
            revision_env=args.revision_env,
        )
    except UnitStagingError as exc:
        print(
            json.dumps(
                {
                    "event": "obsidian-automation-unit-staging",
                    "status": "failed",
                    "message": str(exc),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1

    print(
        json.dumps(
            {
                "event": "obsidian-automation-unit-staging",
                "status": "completed",
                **result,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
