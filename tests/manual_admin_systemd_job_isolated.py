"""Actual *isolated user-manager* systemd Job admission; NO product units.

Creates randomly named transient test-only user units, and deletes all files
after stopping them. Never talks to system systemd Manager and never
installs/mutates production ObsidianAutomation services or the Live Vault.
Run manually on a host with a running user systemd D-Bus session.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tempfile
import time

from obsidian_automation import admin_systemd_admission as admission
from obsidian_automation.admin_status import ServiceSpec

JOB = re.compile(r'o "(/org/freedesktop/systemd1/job/[1-9][0-9]{0,18})"\n?\Z')
BASE = Path("/run/current-system/sw/bin")
ENV = dict(os.environ)
ENV["XDG_RUNTIME_DIR"] = f"/run/user/{os.geteuid()}"
ENV["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={ENV['XDG_RUNTIME_DIR']}/bus"
ENV["PATH"] = "/run/current-system/sw/bin:/usr/bin:/bin"
TIMEOUT = 15


def run(*args: str, valid: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, check=False, timeout=TIMEOUT, env=ENV,
    )
    if valid and result.returncode != 0:
        raise RuntimeError(f"test_command_failed:{args[0]}:{result.returncode}")
    return result


def observe(unit: str, *fields: str) -> dict[str, str]:
    out = run(
        str(BASE / "systemctl"), "--user", "show", "--no-pager", unit,
        *(f"--property={key}" for key in fields),
    ).stdout
    pairs = {}
    for line in out.splitlines():
        key, sep, value = line.partition("=")
        if not sep or key in pairs:
            raise RuntimeError("invalid_systemd_properties")
        pairs[key] = value
    if set(pairs) != set(fields):
        raise RuntimeError("missing_systemd_properties")
    return pairs


def submit(unit: str, *, expect_success: bool = True) -> str | None:
    result = run(
        str(BASE / "busctl"), "--user", "call",
        "org.freedesktop.systemd1", "/org/freedesktop/systemd1",
        "org.freedesktop.systemd1.Manager", "StartUnit", "ss", unit, "fail",
        valid=expect_success,
    )
    if not expect_success and result.returncode != 0:
        return None
    match = JOB.fullmatch(result.stdout)
    if match is None:
        raise RuntimeError("startunit_reply_not_a_valid_job")
    return match.group(1)


def actual_user_manager_adapter(spec: ServiceSpec):
    """Exercise the production fixed/limited busctl parser via test-only bus.

    No product unit is ever in this test tuple; the Popen wrapper changes ONLY
    the transport from --system to --user and the NixOS executable path.
    The production class and immutable source allowlist remain unchanged.
    """
    def factory(argv, **kwargs):
        assert argv == (
            "/usr/bin/busctl", "--system", "call",
            "org.freedesktop.systemd1", "/org/freedesktop/systemd1",
            "org.freedesktop.systemd1.Manager", "StartUnit",
            "ss", spec.service, "fail",
        ), "unexpected_busctl_command"
        replacement = (str(BASE/"busctl"), "--user", *argv[2:])
        original_env = kwargs["env"]
        assert original_env == {
            "PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C",
        }
        kwargs["env"] = ENV
        return subprocess.Popen(replacement, **kwargs)
    return admission.BusctlStartUnitManager(popen=factory)


def wait_for(pred, seconds: float = 12.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(.08)
    return pred()


def main() -> int:
    if os.geteuid() == 0:
        raise RuntimeError("test_must_not_run_as_root")
    # All validation that may fail before registration is done BEFORE a
    # scratch directory or transient timer can be created.
    if not (BASE / "systemd-run").exists() or not (BASE / "bash").exists():
        raise RuntimeError("required_test_binary_missing")
    name = "oa300-isolated-" + secrets.token_hex(6)
    scratch = Path(tempfile.mkdtemp(prefix=name+"-", dir="/tmp"))
    scratch.chmod(0o700)
    script = scratch / "fixture.sh"
    output = scratch / "starts.log"
    release = scratch / "release-first-job"
    unit = f"{name}.service"
    timer = f"{name}.timer"
    touched: list[str] = []
    try:
        script.write_text(
            "#!/run/current-system/sw/bin/bash\n"
            "set -euo pipefail\n"
            f"printf 'START\\n' >> '{output}'\n"
            # Hold the first execution IN FLIGHT until the test observes it
            # and submits the concurrent StartUnit. This eliminates timing-
            # dependent false positives if the oneshot finishes too quickly.
            "for (( attempt = 0; attempt < 200; attempt++ )); do\n"
            f"  if [[ -f '{release}' ]]; then exit 0; fi\n"
            "  /run/current-system/sw/bin/sleep 0.1\n"
            "done\n"
            "exit 124\n",
            encoding="utf-8",
        )
        script.chmod(0o700)
        # A transient service is created but its timer is set one hour later.
        # Leave the timer *waiting*: stopping it first causes systemd to garbage
        # collect the inactive transient service. The hour-long delay cannot
        # fire during this bounded acceptance test; cleanup stops both.
        touched.extend((unit,timer))
        run(
            str(BASE / "systemd-run"), "--user", "--on-active=3600",
            f"--unit={name}", "--property=Type=oneshot",
            "--property=TimeoutStartSec=30",
            str(BASE / "bash"), str(script),
        )
        state = observe(unit, "LoadState", "ActiveState", "Job", "Type")
        if (
            state["LoadState"] != "loaded"
            or state["ActiveState"] != "inactive"
            or state["Job"] not in ("", "0")
            or state["Type"] != "oneshot"
        ):
            raise RuntimeError("isolated_unit_not_initially_quiescent:" + repr(state))

        # Exercise the exact-reviewed bounded BusctlStartUnitManager using
        # ONLY this ephemeral user-systemd service identity.
        spec = ServiceSpec(name, "isolated fixture", timer, unit)
        old_services = admission.SERVICES
        admission.SERVICES = (spec,)
        try:
            bounded_manager = actual_user_manager_adapter(spec)
            first = bounded_manager.start_unit(spec)
        finally:
            admission.SERVICES = old_services
        if first is None:
            raise RuntimeError("no_initial_job")
        # Deliberately block the service at an in-flight file barrier.
        # The test refuses to submit its duplicate unless the original is
        # OBSERVED executing and its release flag is still absent.
        if not wait_for(
            lambda: (
                output.exists()
                and output.read_text(encoding="utf-8").count("START") == 1
                and observe(unit, "ActiveState", "Type") == {
                    "ActiveState": "activating", "Type": "oneshot",
                }
            ),
            seconds=7.0,
        ):
            raise RuntimeError("first_job_not_observed_in_flight")
        if release.exists():
            raise RuntimeError("first_fixture_released_before_duplicate")

        # A second distinct request against an actually running oneshot
        # must be coalesced or rejected by systemd, not launch another run.
        second = submit(unit, expect_success=False)
        if second is not None and second != first:
            raise RuntimeError("concurrent_job_was_not_coalesced")
        if release.exists() or output.read_text(encoding="utf-8").count("START") != 1:
            raise RuntimeError("duplicate_was_not_in_flight")
        release.write_text("go\n", encoding="ascii")
        if not wait_for(lambda: observe(unit, "ActiveState")["ActiveState"] == "inactive"):
            raise RuntimeError("first_unit_never_completed")
        if output.read_text(encoding="utf-8").count("START") != 1:
            raise RuntimeError("duplicate_unit_execution")
        after = observe(unit, "Job", "Result", "ExecMainStatus", "ActiveState")
        if after["Job"] not in ("", "0") or after["ActiveState"] != "inactive":
            raise RuntimeError("job_did_not_return_to_idle")
        if after["Result"] != "success":
            raise RuntimeError("first_job_result_failed")

        # A new operation when the previous one has really finished is valid.
        admission.SERVICES = (spec,)
        try:
            third = bounded_manager.start_unit(spec)
        finally:
            admission.SERVICES = old_services
        if not wait_for(lambda: output.exists() and
                        output.read_text(encoding="utf-8").count("START") == 2):
            raise RuntimeError("second_legitimate_run_missing")
        if not wait_for(lambda: observe(unit, "ActiveState")["ActiveState"] == "inactive"):
            raise RuntimeError("second_run_never_completed")

        # Important distinction: a Manager StartUnit Job object is emitted
        # before a failing oneshot has exited. A real nonzero ExecMainStatus
        # must NOT turn that Job acknowledgement into a business success.
        failed_unit = f"{name}-failure.service"
        failed_timer = f"{name}-failure.timer"
        failed_script = scratch / "failed-fixture.sh"
        failed_script.write_text(
            "#!/run/current-system/sw/bin/bash\n"
            "set -euo pipefail\n"
            "/run/current-system/sw/bin/sleep 1\n"
            "exit 23\n",
            encoding="utf-8",
        )
        failed_script.chmod(0o700)
        touched.extend((failed_unit, failed_timer))
        run(
            str(BASE / "systemd-run"), "--user", "--on-active=3600",
            f"--unit={name}-failure", "--property=Type=oneshot",
            "--property=TimeoutStartSec=10",
            str(BASE / "bash"), str(failed_script),
        )
        if observe(failed_unit, "ActiveState")["ActiveState"] != "inactive":
            raise RuntimeError("failed_fixture_not_initially_idle")
        failed_job = submit(failed_unit)
        if not failed_job:
            raise RuntimeError("failed_fixture_ack_missing")
        if not wait_for(
            lambda: observe(failed_unit, "ActiveState")["ActiveState"] == "failed",
        ):
            raise RuntimeError("failed_fixture_missing_terminal_failure")
        failed_final = observe(
            failed_unit, "Job", "Result", "ExecMainStatus", "ActiveState",
        )
        if (
            failed_final["Job"] not in ("", "0")
            or failed_final["Result"] != "exit-code"
            or failed_final["ExecMainStatus"] != "23"
        ):
            raise RuntimeError("job_ack_did_not_preserve_failure_difference")

        print("ACTUAL_USER_SYSTEMD_JOB_ADMISSION_PASS")
        print("PRODUCTION_BOUNDED_BUSCTL_PARSER_ON_REAL_USER_MANAGER_PASS")
        print("STARTUNIT_FIRST_JOB="+first)
        print("STARTUNIT_CONCURRENT_REPLY="+("REJECTED" if second is None else "JOB_OBJECT"))
        if second is not None:
            print("STARTUNIT_CONCURRENT_SAME_OBJECT="+str(second == first).lower())
        print("STARTUNIT_SECOND_JOB="+third)
        print("RUN_COUNT=2 (one initial, one new only after completion)")
        print("JOB_ZERO_AFTER_FIRST=true")
        print("RESULT_FIRST=success")
        print("FAILURE_JOB="+failed_job)
        print("FAILURE_RESULT=exit-code")
        print("FAILURE_EXIT_STATUS=23")
        print("FAILURE_JOB_ACK_IS_NOT_SERVICE_SUCCESS=true")
        print("EFFECT_SCOPE=isolated_ephemeral_user_unit_only")
        return 0
    finally:
        # Stop EVERY known fixture unit, timers FIRST. Do not let one failed
        # subprocess call skip the other stops or the final live observation.
        targets = [
            *[v for v in touched if v.endswith(".timer")],
            *[v for v in touched if v.endswith(".service")],
        ]
        for target in targets:
            try:
                run(
                    str(BASE / "systemctl"), "--user", "stop",
                    "--no-block", target, valid=False,
                )
            except (OSError, subprocess.SubprocessError):
                # Continue stopping the others; the final verified idle state
                # (not a lost command reply) decides whether cleanup is safe.
                pass

        def all_stopped() -> bool:
            for target in targets:
                try:
                    values = observe(target, "LoadState", "ActiveState", "Job")
                except (RuntimeError, OSError, subprocess.SubprocessError):
                    return False
                if (
                    values["ActiveState"] not in ("inactive", "failed")
                    or values["Job"] not in ("", "0")
                ):
                    return False
            return True

        # If timer cancellation cannot be proved, retain its ExecStart script
        # rather than remove files while a callback may later launch them.
        # Never claim test success or silently leave a scheduled timer.
        if not wait_for(all_stopped, seconds=6.0):
            raise RuntimeError("disposable_systemd_cleanup_unqualified")
        for target in targets:
            try:
                run(
                    str(BASE / "systemctl"), "--user", "reset-failed",
                    target, valid=False,
                )
            except (OSError, subprocess.SubprocessError):
                pass
        if scratch.parent != Path("/tmp") or not scratch.name.startswith("oa300-isolated-"):
            raise RuntimeError("unsafe_test_cleanup")
        if scratch.is_symlink():
            raise RuntimeError("unsafe_test_cleanup_symlink")
        shutil.rmtree(scratch)


if __name__ == "__main__":
    raise SystemExit(main())
