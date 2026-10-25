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
    ("b1", "AIによる派生ノート生成", "共通WebUIからのAI Provider設定・生成・Review操作は未接続。既存処理が存在してもこの画面からは実行できません"),
    ("b3", "ProjectデータをVaultに反映", "Project同期・Writerは共通WebUI未接続。Vaultへ反映する操作は利用できません"),
    ("b4", "GitHub進捗をDaily Noteに反映", "決定的生成の実装と、共通Job・Writer接続の受入は別"),
    ("b5", "ChatGPT Exportの取り込み", "バックエンドはありますがWebUIからのZIPアップロード・検証・取込は未接続。定期ジョブではなく手動取込の予定です"),
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


def _daily(job: SnapshotObservation | None) -> dict[str, object]:
    result = _pending(
        "b4", "GitHub進捗をDaily Noteに反映",
        "GitHub Daily Previewの設定・Run Onceは共通WebUIから利用可能。Writerはこの環境では未導入（別担当）")
    if job is None:
        return result
    result.update(integration="unavailable")
    try:
        observed = job.status()
        if (type(observed) is not dict or observed.get("version") != 1
                or observed.get("job") != "github_daily_preview"
                or type(observed.get("settings")) is not dict):
            return result
        settings = observed["settings"]
        if type(settings.get("enabled")) is not bool or type(settings.get("configured")) is not bool:
            return result
        result["integration"] = "connected"
        result["configuration"] = "configured" if settings["configured"] else "missing"
        result["schedule"] = "on" if settings["configured"] and settings["enabled"] else "off"
        result["control_available"] = settings["configured"]
        result["note"] = ("Writer有効。Daily Note反映は各Runの検証済み結果で確認"
                          if observed.get("publication_enabled") is True else
                          "GitHub Daily Previewの設定・Run Onceは共通WebUIから利用可能。Writerはこの環境では未導入（別担当）")
        latest = observed.get("last_run")
        if latest is not None:
            allowed = {"preview_ready", "published", "publish_failed",
                       "failed", "unconfirmed", "queued", "running"}
            result["last_result"] = (
                latest.get("status") if type(latest) is dict and
                latest.get("status") in allowed else "unknown")
    except Exception:
        result.update(integration="unavailable", configuration="not_observed",
                      schedule="not_observed", last_result=None, control_available=False)
    return result


def workflow_catalog(
    snapshot_job: SnapshotObservation | None,
    daily_job: SnapshotObservation | None = None,
) -> dict[str, object]:
    pending = {identifier: _pending(identifier, label, note)
               for identifier, label, note in _PENDING}
    return {
        "version": 1,
        "authority": "observation_only",
        "workflows": [
            pending["b1"], _snapshot(snapshot_job),
            pending["b3"], _daily(daily_job), pending["b5"],
        ],
    }
