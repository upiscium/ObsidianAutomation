from __future__ import annotations

from collections.abc import Callable
from typing import Any, Sequence

from . import promotion_deployment as _deployment
from .managed_promotion_transport import execute_managed_promotion


def _with_managed_transport(call: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    previous = _deployment.execute_promotion
    _deployment.execute_promotion = execute_managed_promotion
    try:
        return call(*args, **kwargs)
    finally:
        _deployment.execute_promotion = previous


def run_promotion_cycle(**kwargs: Any):
    """Run one production promotion cycle with semantic appearance handling."""
    return _with_managed_transport(_deployment.run_promotion_cycle, **kwargs)


def main(argv: Sequence[str] | None = None) -> int:
    """Production CLI entrypoint with semantic appearance handling."""
    return _with_managed_transport(_deployment.main, argv)
