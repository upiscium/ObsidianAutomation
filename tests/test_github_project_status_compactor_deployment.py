from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples" / "github-sync"


def test_timer_targets_compactor_and_compactor_depends_on_writer() -> None:
    timer = (EXAMPLES / "obsidian-github-sync.timer").read_text(encoding="utf-8")
    service = (EXAMPLES / "obsidian-github-compactor.service").read_text(encoding="utf-8")

    assert "Unit=obsidian-github-compactor.service" in timer
    assert "Requires=obsidian-github-writer.service" in service
    assert "After=obsidian-github-writer.service" in service
    assert "User=obsidian-github-compactor" in service
    assert "Group=obsidian-github-compactor" in service
    assert "SupplementaryGroups=obsidian-github-pipeline" in service


def test_compactor_systemd_sandbox_has_only_required_pipeline_write_scope() -> None:
    service = (EXAMPLES / "obsidian-github-compactor.service").read_text(encoding="utf-8")

    assert (
        "ReadWritePaths=/var/lib/obsidian-github-pipeline/25-Execution"
        in service
    )
    assert (
        "ReadOnlyPaths=/var/lib/obsidian-github-pipeline/27-Transport"
        in service
    )
    assert "/etc/obsidian-github-sync" in service
    assert "/etc/obsidian-github-writer" in service
    assert "/etc/obsidian-github-mirror" in service
    assert "/srv/obsidian-github-sync/vault" in service


def test_writer_request_directory_remains_read_only() -> None:
    service = (EXAMPLES / "obsidian-github-writer.service").read_text(encoding="utf-8")

    assert (
        "ReadOnlyPaths=/etc/obsidian-github-writer "
        "/var/lib/obsidian-github-pipeline/25-Execution"
        in service
    )
    assert (
        "ReadWritePaths=/var/lib/obsidian-github-pipeline/24-Locks "
        "/var/lib/obsidian-github-pipeline/27-Transport"
        in service
    )


def test_compactor_authority_bootstrap_script_is_shell_valid() -> None:
    path = EXAMPLES / "bootstrap-compactor-authority.sh"
    completed = subprocess.run(
        ["sh", "-n", str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr

    script = path.read_text(encoding="utf-8")
    assert 'setfacl -m "u:$COMPACTOR_USER:rwx" "$REQUEST_DIR"' in script
    assert 'test -w "$REQUEST_DIR"' in script
    assert 'test -r "$RESULT_DIR"' in script
    assert 'test -w "$RESULT_DIR"' in script
