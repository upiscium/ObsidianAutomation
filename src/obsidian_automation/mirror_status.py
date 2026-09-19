"""Read-only systemd mirror diagnostics, not durable success history.

Run on the AI Writer host, in the same time namespace as its system manager.
This tool reads only an allowlist of systemd properties; never Vault, lifecycle
artifacts, credentials, ExecStart, Environment, or journal messages.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from typing import Callable, Mapping, Sequence


TIMER = "obsidian-ai-vault-pull.timer"
SERVICE = "obsidian-ai-vault-pull.service"
TIMER_PROPERTIES = ("LoadState", "UnitFileState", "ActiveState")
SERVICE_PROPERTIES = ("LoadState", "Type", "ActiveState", "SubState", "Result", "ExecMainCode", "ExecMainStatus",
                      "ExecMainStartTimestampMonotonic", "ExecMainExitTimestampMonotonic")
EXIT_CODES = {"OK": 0, "WARNING": 1, "CRITICAL": 2, "UNKNOWN": 3}
LOAD_STATES = ("loaded", "not-found", "masked", "error", "bad-setting")
ACTIVE_STATES = ("active", "inactive", "activating", "deactivating", "reloading", "failed", "refreshing")
FAILURE_RESULTS = ("exit-code", "signal", "core-dump", "timeout", "watchdog", "start-limit-hit", "resources", "protocol", "oom-kill", "exec-condition")


class StatusError(ValueError):
    """A fixed diagnostic code; never stderr, command text, or property data."""


def _enum(value: object, allowed: Sequence[str]) -> str:
    return value if isinstance(value, str) and value in allowed else "unknown"


def _uint(value: object) -> int | None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,20}", value):
        return None
    return int(value)


def parse_properties(text: str, expected: Sequence[str]) -> dict[str, str]:
    if len(text) > 65536:
        raise StatusError("systemd_response_too_large")
    result = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if key not in expected:
            continue
        if not sep or key in result:
            raise StatusError("invalid_systemd_response")
        result[key] = value
    if set(result) != set(expected):
        raise StatusError("missing_systemd_properties")
    return result


def read_unit(unit: str, properties: Sequence[str], *, runner: Callable = subprocess.run) -> dict[str, str]:
    if (unit, tuple(properties)) not in ((TIMER, TIMER_PROPERTIES), (SERVICE, SERVICE_PROPERTIES)):
        raise StatusError("unsupported_unit_request")
    command = ["systemctl", "show", unit, "--no-pager"]
    command.extend(f"--property={key}" for key in properties)
    try:
        result = runner(command, check=False, capture_output=True, text=True, timeout=10,
                        env={**os.environ, "LC_ALL": "C", "SYSTEMD_PAGER": "cat", "SYSTEMD_COLORS": "0"})
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise StatusError("systemd_observation_failed") from exc
    if result.returncode != 0:
        raise StatusError("systemd_observation_failed")
    return parse_properties(result.stdout, properties)


def classify(timer: Mapping[str, str], service: Mapping[str, str], *, now_us: int,
             warning_seconds: int = 900, critical_seconds: int = 1800) -> dict:
    if any(type(x) is not int for x in (now_us, warning_seconds, critical_seconds)) or not 0 < warning_seconds < critical_seconds or now_us < 0:
        raise StatusError("invalid_clock_or_thresholds")
    observations: list[tuple[str, str]] = []
    tload = _enum(timer.get("LoadState"), LOAD_STATES)
    sload = _enum(service.get("LoadState"), LOAD_STATES)
    tactive = _enum(timer.get("ActiveState"), ACTIVE_STATES)
    sactive = _enum(service.get("ActiveState"), ACTIVE_STATES)
    enabled = _enum(timer.get("UnitFileState"), ("enabled", "enabled-runtime", "disabled", "masked", "masked-runtime", "static", "indirect", "linked", "linked-runtime"))
    result = _enum(service.get("Result"), ("success", *FAILURE_RESULTS))
    substate = _enum(service.get("SubState"), ("dead", "failed", "exited", "start", "start-pre", "start-post", "running", "stop", "stop-sigterm", "stop-sigkill"))
    code, status = _uint(service.get("ExecMainCode")), _uint(service.get("ExecMainStatus"))
    start = _uint(service.get("ExecMainStartTimestampMonotonic"))
    finish = _uint(service.get("ExecMainExitTimestampMonotonic"))
    success_age = runtime = None
    attempt = "unknown"
    for scope, load in (("timer", tload), ("service", sload)):
        if load != "loaded":
            observations.append(("UNKNOWN" if load == "unknown" else "CRITICAL", f"{scope}_not_loaded"))
    if tactive != "active":
        observations.append(("UNKNOWN" if tactive == "unknown" else "CRITICAL", "timer_not_active"))
    if enabled not in {"enabled", "enabled-runtime"}:
        observations.append(("CRITICAL" if enabled in {"disabled", "masked", "masked-runtime"} else "UNKNOWN", "timer_not_enabled"))
    if service.get("Type") != "oneshot":
        observations.append(("UNKNOWN", "unexpected_service_type"))
    elif sload != "loaded":
        pass
    elif sactive in {"activating", "deactivating", "reloading", "refreshing"} or (sactive == "active" and substate == "running"):
        attempt = "in_progress"
        if start is not None and 0 < start <= now_us:
            runtime = (now_us - start) // 1000000
        observations.append(("UNKNOWN", "run_in_progress_success_history_unavailable"))
    elif sactive == "failed" or result in FAILURE_RESULTS:
        attempt = "failed"
        observations.append(("CRITICAL", "latest_attempt_failed"))
    elif sactive != "inactive" or substate != "dead":
        observations.append(("UNKNOWN", "unexpected_service_state"))
    elif start == 0 and finish == 0 and code == 0:
        attempt = "not_observed"
        observations.append(("UNKNOWN", "no_completed_attempt_observed"))
    elif result == "success" and code == 1 and status == 0:
        if start is None or finish is None or not 0 < start <= finish <= now_us:
            observations.append(("UNKNOWN", "inconsistent_monotonic_timestamps"))
        else:
            attempt = "succeeded"
            success_age = (now_us - finish) // 1000000
            if success_age >= critical_seconds:
                observations.append(("CRITICAL", "refresh_success_stale"))
            elif success_age >= warning_seconds:
                observations.append(("WARNING", "refresh_success_aging"))
    else:
        observations.append(("UNKNOWN", "completion_not_proven"))
    rank = {"OK": 0, "WARNING": 1, "UNKNOWN": 2, "CRITICAL": 3}
    health = max((severity for severity, _ in observations), key=rank.get, default="OK")
    return {"record_version": 1, "source": "systemd_latest_attempt", "health": health,
            "reasons": [reason for _, reason in observations],
            "timer": {"load_state": tload, "active_state": tactive, "enabled_state": enabled},
            "service": {"load_state": sload, "active_state": sactive, "sub_state": substate,
                        "result": result, "exec_main_code": code, "exec_main_status": status},
            "latest_attempt": attempt, "last_success_age_seconds": success_age,
            "running_seconds": runtime, "lock_wait_seconds": None,
            "durable_success_history": False, "atomic_snapshot": False,
            "thresholds_seconds": {"warning": warning_seconds, "critical": critical_seconds}}


def collect_status(*, warning_seconds: int = 900, critical_seconds: int = 1800,
                   runner: Callable = subprocess.run, clock: Callable = time.monotonic_ns) -> dict:
    if any(type(x) is not int for x in (warning_seconds, critical_seconds)) or not 0 < warning_seconds < critical_seconds:
        raise StatusError("invalid_clock_or_thresholds")
    timer = read_unit(TIMER, TIMER_PROPERTIES, runner=runner)
    service = read_unit(SERVICE, SERVICE_PROPERTIES, runner=runner)
    return classify(timer, service, now_us=clock() // 1000, warning_seconds=warning_seconds, critical_seconds=critical_seconds)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--warning-seconds", type=int, default=900)
    parser.add_argument("--critical-seconds", type=int, default=1800)
    args = parser.parse_args(argv)
    try:
        report = collect_status(warning_seconds=args.warning_seconds, critical_seconds=args.critical_seconds)
    except StatusError as exc:
        report = {"record_version": 1, "source": "systemd_latest_attempt", "health": "UNKNOWN",
                  "reasons": [str(exc)], "last_success_age_seconds": None, "durable_success_history": False}
    if args.json:
        print(json.dumps(report, sort_keys=True, indent=2))
    else:
        age = report["last_success_age_seconds"]
        print(f"Mirror health: {report['health']}")
        print(f"Last confirmed success age: {str(age) + 's' if age is not None else 'unknown'}")
        print("Reasons: " + (", ".join(report["reasons"]) or "latest attempt succeeded; timer enabled/active"))
        print("Scope: latest systemd attempt only; no durable success history, lock timing, or remote verification.")
    return EXIT_CODES[report["health"]]


if __name__ == "__main__":
    sys.exit(main())
