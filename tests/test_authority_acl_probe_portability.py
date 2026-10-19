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
    assert '''runuser -u "$1" -- sh -c 'test "$1" "$2"' sh "$2" "$3"''' in script
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
