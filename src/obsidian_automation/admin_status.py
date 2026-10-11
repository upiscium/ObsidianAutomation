"""Read-only, fixed-registry systemd status projection for the management UI.

No service control, credentials, command construction from requests, journal reads,
environment fields, ExecStart, arbitrary unit paths, or Vault content are exposed.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

MAX_OUTPUT_BYTES = 32768
TIMER_FIELDS = (
    "LoadState", "UnitFileState", "ActiveState", "SubState",
)
SERVICE_FIELDS = (
    "LoadState", "ActiveState", "SubState", "Type", "Result",
    "ExecMainCode", "ExecMainStatus",
    "ExecMainStartTimestampMonotonic", "ExecMainExitTimestampMonotonic",
)
LOADED = frozenset(("loaded", "not-found", "masked", "error", "bad-setting"))
ACTIVE = frozenset(("active", "inactive", "activating", "deactivating", "failed"))
UNIT_FILES = frozenset(("enabled", "enabled-runtime", "disabled", "masked", "masked-runtime", "static"))
SERVICE_RESULT = frozenset((
    "success", "exit-code", "signal", "timeout", "watchdog", "resources",
    "core-dump", "start-limit-hit", "oom-kill", "exec-condition",
))

@dataclass(frozen=True)
class ServiceSpec:
    identifier: str
    label: str
    timer: str
    service: str

# Names are hard-coded; no endpoint can supply an arbitrary unit or systemctl argument.
SERVICES: tuple[ServiceSpec, ...] = (
    ServiceSpec("ai-vault-pull", "Vault Mirror", "obsidian-ai-vault-pull.timer", "obsidian-ai-vault-pull.service"),
    ServiceSpec("pre-review", "AI Pre-review", "obsidian-pre-review.timer", "obsidian-pre-review-status.service"),
    ServiceSpec("github-sync", "GitHub Project Sync", "obsidian-github-sync.timer", "obsidian-github-compactor.service"),
    ServiceSpec("github-daily", "GitHub Daily Progress", "obsidian-github-daily-progress.timer", "obsidian-github-daily-apply.service"),
    ServiceSpec("core-promotion", "Core Promotion", "obsidian-core-promotion.timer", "obsidian-core-promotion.service"),
)

Runner = Callable[..., subprocess.CompletedProcess[str]]


def _enum(value: object, choices: frozenset[str]) -> str:
    return value if isinstance(value, str) and value in choices else "unknown"


def _parse_properties(raw: str, properties: Sequence[str]) -> Mapping[str, str] | None:
    if not isinstance(raw, str) or len(raw.encode("utf-8", errors="replace")) > MAX_OUTPUT_BYTES:
        return None
    values: dict[str, str] = {}
    for line in raw.splitlines():
        key, sep, value = line.partition("=")
        if not sep or key not in properties or key in values or len(value) > 100:
            return None
        values[key] = value
    if set(values) != set(properties):
        return None
    return values


def _run_properties(
    unit: str, properties: Sequence[str], *, runner: Runner,
) -> Mapping[str, str] | None:
    if not any(unit in (item.timer, item.service) for item in SERVICES):
        return None
    argv = ["systemctl", "show", unit, "--no-pager", *(f"--property={key}" for key in properties)]
    try:
        output = runner(argv, check=False, capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None
    if output.returncode != 0:
        return None
    return _parse_properties(output.stdout, properties)


def _uint(value: object) -> int | None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,20}", value):
        return None
    return int(value)


def classify_timer(props: Mapping[str, str] | None) -> str:
    if props is None:
        return "unknown"
    load = _enum(props.get("LoadState"), LOADED)
    active = _enum(props.get("ActiveState"), ACTIVE)
    unitfile = _enum(props.get("UnitFileState"), UNIT_FILES)
    state = props.get("SubState")
    if load == "not-found":
        return "not_installed"
    if load != "loaded" or active == "unknown" or unitfile == "unknown":
        return "unknown"
    if unitfile in {"masked", "masked-runtime"}:
        return "blocked"
    if unitfile == "disabled" and active == "inactive":
        return "paused"
    if unitfile in {"enabled", "enabled-runtime"} and active == "active" and state == "waiting":
        return "scheduled"
    if active == "active" and state == "running":
        return "triggering"
    if active == "failed":
        return "failed"
    if active == "active" and state == "elapsed":
        return "no_next_run"
    if active == "inactive":
        return "stopped"
    return "unknown"


def classify_service(props: Mapping[str, str] | None) -> str:
    if props is None:
        return "unknown"
    load = _enum(props.get("LoadState"), LOADED)
    active = _enum(props.get("ActiveState"), ACTIVE)
    result = _enum(props.get("Result"), SERVICE_RESULT)
    if load == "not-found":
        return "not_installed"
    if load != "loaded" or active == "unknown" or props.get("Type") != "oneshot":
        return "unknown"
    if active in {"activating", "deactivating"} or (active == "active" and props.get("SubState") == "running"):
        return "running"
    if active == "failed" or (result not in {"success", "unknown"}):
        return "failed"
    if active != "inactive" or props.get("SubState") != "dead" or result == "unknown":
        return "unknown"
    started = _uint(props.get("ExecMainStartTimestampMonotonic"))
    finished = _uint(props.get("ExecMainExitTimestampMonotonic"))
    code = _uint(props.get("ExecMainCode"))
    status = _uint(props.get("ExecMainStatus"))
    if started == 0 and finished == 0 and code == 0:
        return "not_observed"
    if started is None or finished is None or not 0 < started <= finished:
        return "unknown"
    if result == "success" and code == 1 and status == 0:
        return "succeeded"
    return "unknown"


def aggregate_status(*, runner: Runner = subprocess.run) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for spec in SERVICES:
        timer = classify_timer(_run_properties(spec.timer, TIMER_FIELDS, runner=runner))
        service = classify_service(_run_properties(spec.service, SERVICE_FIELDS, runner=runner))
        if timer == "not_installed" or service == "not_installed":
            health = "not_installed"
        elif service == "failed" or timer in {"blocked", "failed"}:
            # A verified failure must not disappear behind an unrelated
            # unknown timer/service observation.
            health = "failed"
        elif timer == "unknown" or service == "unknown":
            health = "unknown"
        elif timer == "paused":
            health = "paused"
        elif timer in {"scheduled", "triggering"} and service == "succeeded":
            # A completed oneshot may be arbitrarily stale. This is not
            # evidence that its latest data or provider is currently healthy.
            health = "observed_success"
        else:
            health = "unverified"
        results.append({
            "id": spec.identifier,
            "label": spec.label,
            "timer": spec.timer,
            "service": spec.service,
            "schedule": timer,
            "latest_execution": service,
            "health": health,
        })
    return results
