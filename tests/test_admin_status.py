from __future__ import annotations

import subprocess

from obsidian_automation.admin_status import (
    SERVICES,
    SERVICE_FIELDS,
    TIMER_FIELDS,
    _parse_properties,
    _run_properties,
    aggregate_status,
    classify_service,
    classify_timer,
)


def timer(**kw: str) -> dict[str, str]:
    return {
        "LoadState": "loaded", "UnitFileState": "enabled",
        "ActiveState": "active", "SubState": "waiting", **kw,
    }


def service(**kw: str) -> dict[str, str]:
    return {
        "LoadState": "loaded", "ActiveState": "inactive", "SubState": "dead",
        "Type": "oneshot", "Result": "success", "ExecMainCode": "1",
        "ExecMainStatus": "0",
        "ExecMainStartTimestampMonotonic": "100",
        "ExecMainExitTimestampMonotonic": "200",
        **kw,
    }


def test_timer_scheduled_is_not_failed() -> None:
    assert classify_timer(timer()) == "scheduled"


def test_timer_disabled_is_not_error() -> None:
    assert classify_timer(timer(UnitFileState="disabled", ActiveState="inactive", SubState="dead")) == "paused"


def test_timer_elapsed_not_scheduled() -> None:
    assert classify_timer(timer(SubState="elapsed")) == "no_next_run"


def test_timer_failed_and_unknown_do_not_turn_healthy() -> None:
    assert classify_timer(timer(ActiveState="failed")) == "failed"
    assert classify_timer(timer(ActiveState="active", SubState="bad-value")) == "unknown"
    assert classify_timer(timer(LoadState="not-found")) == "not_installed"
    assert classify_timer(None) == "unknown"


def test_service_oneshot_success_requires_actual_completed_attempt() -> None:
    assert classify_service(service()) == "succeeded"
    assert classify_service(service(
        ExecMainStartTimestampMonotonic="0", ExecMainExitTimestampMonotonic="0",
        ExecMainCode="0",
    )) == "not_observed"
    assert classify_service(service(ExecMainStartTimestampMonotonic="garbage")) == "unknown"
    assert classify_service(service(ExecMainExitTimestampMonotonic="50")) == "unknown"


def test_service_running_and_failure() -> None:
    assert classify_service(service(ActiveState="activating", SubState="start")) == "running"
    assert classify_service(service(ActiveState="active", SubState="running")) == "running"
    assert classify_service(service(ActiveState="failed", Result="exit-code")) == "failed"
    assert classify_service(service(ActiveState="inactive", Result="timeout")) == "failed"
    assert classify_service(service(Type="simple")) == "unknown"


def test_parse_response_strict_and_bounded() -> None:
    valid = "\n".join(f"{k}=x" for k in TIMER_FIELDS) + "\n"
    assert _parse_properties(valid, TIMER_FIELDS) == {k: "x" for k in TIMER_FIELDS}
    assert _parse_properties(valid + "LoadState=another\n", TIMER_FIELDS) is None
    assert _parse_properties(valid + "TOKEN=secret\n", TIMER_FIELDS) is None
    assert _parse_properties(valid.replace("SubState=x\n", ""), TIMER_FIELDS) is None
    assert _parse_properties("x" * 32769, TIMER_FIELDS) is None


def test_arbitrary_unit_request_never_calls_systemctl() -> None:
    seen = []
    def runner(*args, **kw):
        seen.append(args)
        raise AssertionError("not allowed")
    assert _run_properties("ssh.service", TIMER_FIELDS, runner=runner) is None
    assert not seen


def test_all_services_are_hardcoded_and_unique() -> None:
    assert len(SERVICES) == 5
    assert len({x.identifier for x in SERVICES}) == len(SERVICES)
    assert len({x.timer for x in SERVICES}) == len(SERVICES)
    assert len({x.service for x in SERVICES}) == len(SERVICES)
    assert all(x.timer.endswith(".timer") and x.service.endswith(".service") for x in SERVICES)


def test_aggregate_projections_are_sanitized_and_distinguish_health() -> None:
    # systemctl fake never returns arbitrary journal/ExecStart/Environment values.
    samples = {}
    cases = [
        (timer(), service()),
        (timer(UnitFileState="disabled", ActiveState="inactive", SubState="dead"),
         service(ExecMainStartTimestampMonotonic="0", ExecMainExitTimestampMonotonic="0", ExecMainCode="0")),
        (timer(), service(Result="exit-code", ActiveState="failed")),
        (timer(), service(ExecMainStartTimestampMonotonic="0", ExecMainExitTimestampMonotonic="0", ExecMainCode="0")),
        (timer(ActiveState="active", SubState="elapsed"), service()),
    ]
    for spec, (t, s) in zip(SERVICES, cases):
        samples[spec.timer] = t
        samples[spec.service] = s
    calls = []
    def runner(argv, **kw):
        calls.append(tuple(argv))
        assert argv[:2] == ["systemctl", "show"]
        assert argv[2] in samples
        assert kw["timeout"] == 3
        assert kw["capture_output"] is True
        assert kw["check"] is False
        fields = TIMER_FIELDS if argv[2].endswith(".timer") else SERVICE_FIELDS
        values = "\n".join(f"{key}={samples[argv[2]][key]}" for key in fields) + "\n"
        # Synthetic unrequested stderr canary, not an actual credential.
        return subprocess.CompletedProcess(argv, 0, values, "IGNORED_FIELD=redaction-canary")
    view = aggregate_status(runner=runner)
    assert [s["health"] for s in view] == [
        "observed_success", "paused", "failed", "unverified", "unverified",
    ]
    assert view[1]["latest_execution"] == "not_observed"
    assert view[3]["latest_execution"] == "not_observed"
    assert len(calls) == 10
    assert "redaction-canary" not in str(view)




def test_missing_systemd_unit_is_not_described_as_healthy() -> None:
    def missing(argv, **kw):
        fields = TIMER_FIELDS if argv[2].endswith(".timer") else SERVICE_FIELDS
        props = timer(LoadState="not-found") if argv[2].endswith(".timer") else service(LoadState="not-found")
        raw = "\n".join(f"{key}={props[key]}" for key in fields) + "\n"
        return subprocess.CompletedProcess(argv, 0, raw, "")
    assert all(x["health"] == "not_installed" for x in aggregate_status(runner=missing))

def test_failed_systemctl_is_unknown_not_healthy() -> None:
    def fail(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, "", "secret-content")
    assert all(x["health"] == "unknown" for x in aggregate_status(runner=fail))

def test_proven_failure_has_priority_over_unknown_timer_substate() -> None:
    samples = {}
    for spec in SERVICES:
        samples[spec.timer] = timer(SubState="not-a-known-state")
        samples[spec.service] = service(ActiveState="failed", Result="exit-code")

    def runner(argv, **kw):
        fields = TIMER_FIELDS if argv[2].endswith(".timer") else SERVICE_FIELDS
        raw = "\n".join(f"{key}={samples[argv[2]][key]}" for key in fields) + "\n"
        return subprocess.CompletedProcess(argv, 0, raw, "")

    assert all(entry["health"] == "failed" for entry in aggregate_status(runner=runner))


def test_observed_success_does_not_claim_current_freshness() -> None:
    samples = {}
    for spec in SERVICES:
        samples[spec.timer] = timer()
        samples[spec.service] = service()

    def runner(argv, **kw):
        fields = TIMER_FIELDS if argv[2].endswith(".timer") else SERVICE_FIELDS
        raw = "\n".join(f"{key}={samples[argv[2]][key]}" for key in fields) + "\n"
        return subprocess.CompletedProcess(argv, 0, raw, "")

    assert all(entry["health"] == "observed_success" for entry in aggregate_status(runner=runner))

def test_proven_failure_remains_visible_when_companion_unit_missing() -> None:
    samples = {}
    for spec in SERVICES:
        samples[spec.timer] = timer(LoadState="not-found")
        samples[spec.service] = service(ActiveState="failed", Result="exit-code")

    def runner(argv, **kwargs):
        fields = TIMER_FIELDS if argv[2].endswith(".timer") else SERVICE_FIELDS
        raw = "\n".join(f"{key}={samples[argv[2]][key]}" for key in fields) + "\n"
        return subprocess.CompletedProcess(argv, 0, raw, "")

    assert all(entry["health"] == "failed" for entry in aggregate_status(runner=runner))
