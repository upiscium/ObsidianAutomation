"""Fixed five-workflow catalog for the fresh single-WebUI MVI.

This is a *read-only integration map*, not a health monitor for legacy
systemd workers. Only the explicitly attached B2 SnapshotJob is live here.
Never infer B1/B3/B4/B5 availability from source-code presence.
"""
from __future__ import annotations

from typing import Protocol


class SnapshotObservation(Protocol):
    def status(self) -> dict: ...


# The old dashboard's five systemd services are not these five product
# workflows. Their status, credentials and operator authorization cannot be
# silently reused as proof of integration.
_PENDING = (
    ("b1", "AIによる派生ノート生成", "既存AI処理は別管理。新しい共通Jobへの接続は未実施"),
    ("b3", "ProjectデータをVaultに反映", "既存GitHub同期・Writerは別管理。新しい共通Jobへの接続は未実施"),
    ("b4", "GitHub進捗をDaily Noteに反映", "決定的生成の実装と、共通Job・Writer接続の受入は別"),
    ("b5", "ChatGPT Exportの取り込み", "既存Importer・Human Reviewと共通WebUIの接続は未実施"),
)
_RESULT = frozenset({"saved", "no_changes", "failed", "unconfirmed", "queued", "running"})


def _pending(identifier: str, label: str, note: str) -> dict[str, object]:
    return {
        "id": identifier, "label": label, "note": note,
        "integration": "pending", "configuration": "not_observed",
        "schedule": "not_applicable" if identifier == "b5" else "not_observed",
        "last_result": None, "control_available": False,
    }


def _snapshot(job: SnapshotObservation | None) -> dict[str, object]:
    result: dict[str, object] = {
        "id": "b2", "label": "Vault Snapshot → private Gitea",
        "note": "実行設定と結果はこのWebUIのSnapshot操作欄で確認",
        "integration": "not_attached" if job is None else "unavailable",
        "configuration": "not_observed", "schedule": "not_observed",
        "last_result": None, "control_available": False,
    }
    if job is None:
        return result
    try:
        observation = job.status()
        if (
            type(observation) is not dict or observation.get("version") != 1
            or observation.get("job") != "vault_snapshot"
            or type(observation.get("settings")) is not dict
        ):
            return result
        settings = observation["settings"]
        if type(settings.get("configured")) is not bool or type(settings.get("enabled")) is not bool:
            return result
        configured = settings["configured"]
        result["integration"] = "connected"
        result["configuration"] = "configured" if configured else "missing"
        result["schedule"] = "on" if configured and settings["enabled"] else "off"
        result["control_available"] = configured
        latest = observation.get("last_run")
        if latest is not None:
            # This API never exposes arbitrary runner messages, private paths,
            # repository URLs, tokens, request IDs or Git refs.
            result["last_result"] = (
                latest.get("status") if type(latest) is dict
                and latest.get("status") in _RESULT else "unknown"
            )
    except Exception:
        # No traceback/exception text may escape through the loginless UI.
        return {
            **result, "integration": "unavailable",
            "configuration": "not_observed", "schedule": "not_observed",
            "last_result": None, "control_available": False,
        }
    return result


def workflow_catalog(snapshot_job: SnapshotObservation | None) -> dict[str, object]:
    pending = {identifier: _pending(identifier, label, note)
               for identifier, label, note in _PENDING}
    return {
        "version": 1,
        "authority": "observation_only",
        "workflows": [
            pending["b1"], _snapshot(snapshot_job),
            pending["b3"], pending["b4"], pending["b5"],
        ],
    }
