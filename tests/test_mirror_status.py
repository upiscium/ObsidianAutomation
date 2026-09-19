from __future__ import annotations

import json
import subprocess

import pytest

from obsidian_automation import mirror_status as m


def timer():
    return {"LoadState": "loaded", "ActiveState": "active", "UnitFileState": "enabled"}


def service():
    return {"LoadState": "loaded", "Type": "oneshot", "ActiveState": "inactive", "SubState": "dead",
            "Result": "success", "ExecMainCode": "1", "ExecMainStatus": "0",
            "ExecMainStartTimestampMonotonic": "1000000", "ExecMainExitTimestampMonotonic": "3000000"}


def test_completed_oneshot_is_healthy_not_failed():
    report = m.classify(timer(), service(), now_us=21000000)
    assert report["health"] == "OK"
    assert report["last_success_age_seconds"] == 18
    assert report["durable_success_history"] is False
    assert report["lock_wait_seconds"] is None


@pytest.mark.parametrize("age,health", [(899, "OK"), (900, "WARNING"), (1799, "WARNING"), (1800, "CRITICAL")])
def test_age_boundaries(age, health):
    assert m.classify(timer(), service(), now_us=(3 + age) * 1000000)["health"] == health


@pytest.mark.parametrize("changes", [{"UnitFileState": "disabled"}, {"ActiveState": "inactive"}, {"LoadState": "not-found"}])
def test_timer_problem_not_hidden_by_recent_success(changes):
    t = timer()
    t.update(changes)
    assert m.classify(t, service(), now_us=21000000)["health"] == "CRITICAL"


def test_latest_failure_does_not_invent_last_success():
    s = service()
    s.update({"ActiveState": "failed", "SubState": "failed", "Result": "exit-code", "ExecMainStatus": "1"})
    report = m.classify(timer(), s, now_us=21000000)
    assert report["health"] == "CRITICAL"
    assert report["latest_attempt"] == "failed"
    assert report["last_success_age_seconds"] is None


def test_running_is_unknown_and_runtime_is_not_lock_wait():
    s = service()
    s.update({"ActiveState": "activating", "SubState": "running", "Result": "success",
              "ExecMainStartTimestampMonotonic": "10000000", "ExecMainExitTimestampMonotonic": "0"})
    report = m.classify(timer(), s, now_us=21000000)
    assert report["health"] == "UNKNOWN"
    assert report["latest_attempt"] == "in_progress"
    assert report["running_seconds"] == 11
    assert report["lock_wait_seconds"] is None


def test_never_run_is_unknown_not_critical():
    s = service()
    s.update({"Result": "success", "ExecMainCode": "0", "ExecMainStatus": "0",
              "ExecMainStartTimestampMonotonic": "0", "ExecMainExitTimestampMonotonic": "0"})
    report = m.classify(timer(), s, now_us=21000000)
    assert report["health"] == "UNKNOWN"
    assert report["latest_attempt"] == "not_observed"
    assert report["last_success_age_seconds"] is None


@pytest.mark.parametrize("key,value", [("LoadState", "future-value"), ("ActiveState", "future-value"), ("Result", "future-value")])
def test_unknown_enums_never_echo_value_or_return_ok(key, value):
    s = service()
    s[key] = value
    report = m.classify(timer(), s, now_us=21000000)
    assert report["health"] != "OK"
    dumped = json.dumps(report)
    assert value not in dumped


def test_inconsistent_or_future_timestamps_are_unknown():
    s = service()
    s["ExecMainExitTimestampMonotonic"] = "999999999"
    report = m.classify(timer(), s, now_us=21000000)
    assert report["health"] == "UNKNOWN"
    assert "inconsistent_monotonic_timestamps" in report["reasons"]


def test_invalid_thresholds_fail_before_observation():
    called = False

    def runner(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError

    with pytest.raises(m.StatusError, match="invalid_clock_or_thresholds"):
        m.collect_status(warning_seconds=0, critical_seconds=1, runner=runner)
    assert called is False


def test_parse_requires_exact_property_set_and_does_not_return_extra():
    text = "LoadState=loaded\nUnitFileState=enabled\nActiveState=active\nSecret=do-not-return\n"
    parsed = m.parse_properties(text, m.TIMER_PROPERTIES)
    assert parsed == {"LoadState": "loaded", "UnitFileState": "enabled", "ActiveState": "active"}


def test_read_unit_uses_read_only_fixed_command_and_sanitized_env():
    calls = []

    class Result:
        returncode = 0
        stdout = "LoadState=loaded\nUnitFileState=enabled\nActiveState=active\n"
        stderr = "private stderr"

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return Result()

    m.read_unit(m.TIMER, m.TIMER_PROPERTIES, runner=runner)
    assert calls[0][0][:4] == ["systemctl", "show", m.TIMER, "--no-pager"]
    assert set(calls[0][0][4:]) == {f"--property={key}" for key in m.TIMER_PROPERTIES}
    assert calls[0][1]["check"] is False
    assert calls[0][1]["capture_output"] is True
    assert calls[0][1]["text"] is True
    assert calls[0][1]["timeout"] == 10
    assert calls[0][1]["env"]["LC_ALL"] == "C"
    assert calls[0][1]["env"]["SYSTEMD_PAGER"] == "cat"


def test_read_failure_is_fixed_error_not_stderr():
    class Result:
        returncode = 1
        stdout = ""
        stderr = "sensitive"

    def runner(*args, **kwargs):
        return Result()

    with pytest.raises(m.StatusError) as exc:
        m.read_unit(m.TIMER, m.TIMER_PROPERTIES, runner=runner)
    assert str(exc.value) == "systemd_observation_failed"
    assert "sensitive" not in str(exc.value)


def test_read_timeout_is_sanitized():
    def runner(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], timeout=10)

    with pytest.raises(m.StatusError, match="systemd_observation_failed"):
        m.read_unit(m.TIMER, m.TIMER_PROPERTIES, runner=runner)


def test_cli_json_and_exit_code(monkeypatch, capsys):
    report = m.classify(timer(), service(), now_us=21000000)
    monkeypatch.setattr(m, "collect_status", lambda **kwargs: report)
    assert m.main(["--json"]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["health"] == "OK"


def test_cli_unknown_on_status_error(monkeypatch, capsys):
    def fail(**kwargs):
        raise m.StatusError("systemd_observation_failed")

    monkeypatch.setattr(m, "collect_status", fail)
    assert m.main(["--json"]) == 3
    value = json.loads(capsys.readouterr().out)
    assert value["health"] == "UNKNOWN"
    assert value["reasons"] == ["systemd_observation_failed"]
