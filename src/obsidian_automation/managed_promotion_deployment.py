from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
import sys
from typing import Any, Sequence

from . import promotion_deployment as _deployment
from .core_live_reconciliation import ReconciliationError, audit_live, store_artifact
from .core_promotion_transport import PromotionTransportConflict, PromotionTransportError, load_checkpoint
from .managed_promotion_transport import execute_managed_promotion
from .webdav_create import WebDAVCreateError, _read_password


def _with_managed_transport(call: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    previous = _deployment.execute_promotion
    _deployment.execute_promotion = execute_managed_promotion
    try:
        return call(*args, **kwargs)
    finally:
        _deployment.execute_promotion = previous


def run_promotion_cycle(**kwargs: Any):
    """Ordered promotion, then read-only Live verification when Core is unchanged.

    A missing/modified Live file produces live_drift, never an automatic PUT.
    Repair is an explicit, digest-selected reconciliation CLI operation.
    """
    result = _with_managed_transport(_deployment.run_promotion_cycle, **kwargs)
    if result.result != "up_to_date":
        return result
    root = Path(kwargs["state_root"])
    config = Path(kwargs["config_path"])
    git_runner = kwargs.get("git_runner", _deployment._run_git)
    # The ordinary transport released this same lock. Revalidate the local
    # fetched HEAD/checkpoint before observing Live so another promotion cannot
    # silently advance the desired state between those two critical sections.
    with _deployment._production_lock(root / "promotion.lock"):
        checkpoint = load_checkpoint(root / "checkpoint.json")
        current = _deployment._head_commit(root / "ObsidianCore", git_runner=git_runner)
        if (current != result.head_commit or checkpoint.last_observed_core_commit != current
            or checkpoint.source_repository != _deployment.CORE_REPOSITORY
            or checkpoint.policy_version != _deployment.PROMOTION_POLICY_VERSION
            or checkpoint.policy_sha256 != _deployment._policy_sha256(config)):
            raise ReconciliationError("promotion_state_changed_before_live_audit")
        plan = audit_live(
            core_repository=root / "ObsidianCore", core_commit=current, config_path=config,
            base_url=kwargs["base_url"], username=kwargs["username"],
            password=_read_password(Path(kwargs["password_file"])),
            timeout=kwargs.get("timeout", 30.0), transport=kwargs.get("http_transport"),
        )
        if not plan["observations"]:
            return result
        digest, path = store_artifact(root / "drift/plans", plan, "live-drift-plan")
        return _deployment.PromotionCycleResult(
            result="live_drift", base_commit=current, head_commit=current,
            plan_sha256=digest, plan_path=path, receipt_sha256=None, receipt_path=None,
        )


def main(argv: Sequence[str] | None = None) -> int:
    """Production CLI: exit 3 on detected drift; never repair from a timer."""
    args = _deployment._parser().parse_args(argv)
    try:
        result = run_promotion_cycle(
            state_root=args.state_root, config_path=args.config,
            base_url=args.base_url or "", username=args.username or "",
            password_file=args.password_file, timeout=args.timeout,
        )
    except PromotionTransportConflict:
        print("conflict: promotion transport conflict", file=sys.stderr)
        return 3
    except (OSError, WebDAVCreateError, _deployment.PromotionDeploymentError, PromotionTransportError):
        print("error: promotion or Live audit failed; no convergence claimed", file=sys.stderr)
        return 2
    print(json.dumps({
        "result": result.result, "base_commit": result.base_commit,
        "head_commit": result.head_commit, "plan_sha256": result.plan_sha256,
        "plan_path": None if result.plan_path is None else str(result.plan_path),
        "receipt_sha256": result.receipt_sha256,
        "receipt_path": None if result.receipt_path is None else str(result.receipt_path),
        "audit_scope": "core_tracked_managed_paths" if result.result in {"up_to_date", "live_drift"} else "ordered_core_changes",
    }, ensure_ascii=False, sort_keys=True))
    return 3 if result.result == "live_drift" else 0
