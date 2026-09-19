from __future__ import annotations

import pytest

from obsidian_automation.github_production_smoke import (
    CommandResult,
    ProductionSmokeError,
    run_live_smoke,
    run_safe_smokes,
)


def test_safe_smoke_registry_passes() -> None:
    assert run_safe_smokes() == ("http-classification",)


def test_live_smoke_starts_writer_and_requires_successful_dependency_chain() -> None:
    calls: list[tuple[str, ...]] = []

    def runner(argv: tuple[str, ...]) -> CommandResult:
        calls.append(tuple(argv))
        if argv == ("systemctl", "start", "obsidian-github-writer.service"):
            return CommandResult(0, "", "")
        if argv[:2] == ("systemctl", "show"):
            return CommandResult(0, "Result=success\nExecMainStatus=0\n", "")
        raise AssertionError(argv)

    completed = run_live_smoke(runner=runner)

    assert completed == (
        "obsidian-github-sync-vault-pull.service",
        "obsidian-github-sync.service",
        "obsidian-github-writer.service",
    )
    assert calls[0] == ("systemctl", "start", "obsidian-github-writer.service")


def test_live_smoke_fails_closed_on_nonzero_service_result() -> None:
    def runner(argv: tuple[str, ...]) -> CommandResult:
        if argv == ("systemctl", "start", "obsidian-github-writer.service"):
            return CommandResult(0, "", "")
        if argv[:3] == (
            "systemctl",
            "show",
            "obsidian-github-sync-vault-pull.service",
        ):
            return CommandResult(0, "Result=success\nExecMainStatus=0\n", "")
        if argv[:3] == ("systemctl", "show", "obsidian-github-sync.service"):
            return CommandResult(0, "Result=exit-code\nExecMainStatus=1\n", "")
        raise AssertionError(argv)

    with pytest.raises(ProductionSmokeError, match="Result is not success"):
        run_live_smoke(runner=runner)
