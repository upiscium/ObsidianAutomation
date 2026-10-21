"""Service-identity access checks must not depend on GNU external test."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest


SCRIPTS = (
    ("examples/ai/bootstrap-pre-review-authority.sh", 7),
    ("examples/ai/pre-review-authority-upgrade-gate.sh", 3),
    ("examples/github-sync/bootstrap-compactor-authority.sh", 4),
)


@pytest.mark.parametrize(("path", "check_count"), SCRIPTS)
def test_root_authority_gates_use_posix_shell_builtin_under_service_uid(
    path: str, check_count: int,
) -> None:
    script = Path(path).read_text()
    assert script.count("probe_role_access") == check_count + 1
    assert '''runuser -u "$1" -- sh -c ''' in script
    assert '''test "$1" "$2"''' in script
    assert 'case "$obsidian_access_verdict" in' in script
    assert 'DENY) return 1' in script
    assert 'invalid authority access probe verdict' in script
    assert "-- test -r " not in script
    assert "-- test -w " not in script
    assert "-- test -x " not in script


def test_posix_shell_probe_evaluates_access_as_runtime_user(tmp_path: Path) -> None:
    directory = tmp_path / "fixture with spaces"
    directory.mkdir(mode=0o700)
    command = 'test "$1" "$2"'
    for flag in ("-r", "-w", "-x"):
        proc = subprocess.run(
            ["sh", "-c", command, "sh", flag, str(directory)],
            check=False, capture_output=True, text=True,
        )
        assert proc.returncode == 0
    if os.geteuid() != 0:
        try:
            directory.chmod(0)
            for flag in ("-r", "-w", "-x"):
                proc = subprocess.run(
                    ["sh", "-c", command, "sh", flag, str(directory)],
                    check=False, capture_output=True, text=True,
                )
                assert proc.returncode == 1
        finally:
            directory.chmod(0o700)


@pytest.mark.parametrize(("path", "_"), SCRIPTS)
@pytest.mark.parametrize(
    ("verdict", "expected_returncode", "expected_result"),
    [
        ("ALLOW", 0, "RESULT=0"),
        ("DENY", 0, "RESULT=1"),
        ("UNRECOGNIZED", 1, None),
        ("ERROR", 1, None),
    ],
)
def test_service_uid_shell_probe_distinguishes_denied_from_failed(
    path: str, _: int, verdict: str, expected_returncode: int,
    expected_result: str | None,
) -> None:
    source = Path(path).read_text()
    start = source.index("probe_role_access() {")
    end = source.index("\n}\n", start) + len("\n}")
    function = source[start:end]
    # Stub only the external runuser command; execute the actual POSIX
    # function body with no credentials, groups, privileges or filesystem
    # modifications. Nonzero launch must not be mistaken for a DENY verdict.
    harness = (
        function + "\n"
        'runuser() {\n'
        '  case "$MOCK_PROBE_REPLY" in\n'
        '    ERROR) return 125 ;;\n'
        '    *) printf "%s\\n" "$MOCK_PROBE_REPLY"; return 0 ;;\n'
        '  esac\n'
        '}\n'
        'probe_role_access fixture -r "/var/lib/fixture with spaces"\n'
        'rc=$?\n'
        'printf "RESULT=%s\\n" "$rc"\n'
    )
    environment = os.environ.copy()
    environment["MOCK_PROBE_REPLY"] = verdict
    proc = subprocess.run(
        ["sh", "-c", harness],
        env=environment, capture_output=True, text=True, check=False,
    )
    assert proc.returncode == expected_returncode, proc.stderr
    if expected_result is not None:
        assert expected_result in proc.stdout
    else:
        assert "RESULT=" not in proc.stdout
