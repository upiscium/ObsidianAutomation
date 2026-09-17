from __future__ import annotations

from pathlib import Path

from obsidian_automation import managed_promotion_deployment, promotion_deployment
from obsidian_automation.managed_promotion_transport import execute_managed_promotion


def test_production_console_entrypoint_uses_managed_deployment_wrapper() -> None:
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    assert (
        'obsidian-core-promotion-run = "obsidian_automation.managed_promotion_deployment:main"'
        in pyproject
    )
    assert (
        'obsidian-core-promotion-transport = "obsidian_automation.core_promotion_transport:main"'
        in pyproject
    )


def test_managed_deployment_wrapper_scopes_and_restores_transport_override() -> None:
    original = promotion_deployment.execute_promotion
    observed = []

    def probe(value: str) -> str:
        observed.append(promotion_deployment.execute_promotion)
        return value

    assert managed_promotion_deployment._with_managed_transport(probe, "ok") == "ok"
    assert observed == [execute_managed_promotion]
    assert promotion_deployment.execute_promotion is original


def test_managed_deployment_wrapper_restores_transport_after_failure() -> None:
    original = promotion_deployment.execute_promotion

    class Expected(Exception):
        pass

    def fail() -> None:
        assert promotion_deployment.execute_promotion is execute_managed_promotion
        raise Expected

    try:
        managed_promotion_deployment._with_managed_transport(fail)
    except Expected:
        pass
    else:
        raise AssertionError("probe must raise")

    assert promotion_deployment.execute_promotion is original
