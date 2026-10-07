from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest


def _load_provisioner():
    path = Path("tools/provision_automation_authority.py")
    spec = importlib.util.spec_from_file_location("production_authority_tool", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


authority = _load_provisioner()


def test_consolidated_authority_contains_expected_identities() -> None:
    users = {user for user, _group, _home in authority.PRIMARY_USERS}
    assert users == {
        "gitea-runner",
        "obsidian-core-promoter",
        "obsidian-ai-sync",
        "obsidian-ai-reader",
        "obsidian-ai-embedder",
        "obsidian-ai-generator",
        "obsidian-ai-validator",
        "obsidian-ai-evaluator",
        "obsidian-ai-status",
        "obsidian-ai-reviewer",
        "obsidian-ai-executor",
        "obsidian-github-mirror",
        "obsidian-github-sync",
        "obsidian-github-writer",
        "obsidian-github-compactor",
        "obsidian-github-summarizer",
        "obsidian-github-renderer",
        "obsidian-github-daily-writer",
    }
    assert set(authority.SHARED_GROUPS) == {
        "obsidian-github-vault",
        "obsidian-github-pipeline",
    }


def test_snapshot_trust_domain_is_not_provisioned() -> None:
    rendered = repr(
        (
            authority.PRIMARY_USERS,
            authority.SHARED_GROUPS,
            authority.DIRECTORIES,
            authority.AI_ACLS,
        )
    )
    assert "obsidian-snapshot" not in rendered
    assert "/etc/obsidian-snapshot" not in rendered
    assert "/var/lib/obsidian-snapshot" not in rendered


def test_provisioner_creates_no_credential_files_or_systemd_units() -> None:
    paths = {path for path, _owner, _group, _mode in authority.DIRECTORIES}

    assert all(not path.endswith(".service") for path in paths)
    assert all(not path.endswith(".timer") for path in paths)

    forbidden_files = (
        "rclone.conf",
        "webdav-password",
        "credentials.env",
        "nextcloud.password",
        "pre-review-generator.env",
        "pre-review-evaluator.env",
    )
    assert all(
        not any(path.endswith(name) for name in forbidden_files)
        for path in paths
    )


def test_github_handoff_groups_preserve_role_separation() -> None:
    assert authority.SUPPLEMENTARY_GROUPS == {
        "obsidian-github-mirror": ("obsidian-github-vault",),
        "obsidian-github-sync": (
            "obsidian-github-vault",
            "obsidian-github-pipeline",
        ),
        "obsidian-github-writer": ("obsidian-github-pipeline",),
        "obsidian-github-compactor": ("obsidian-github-pipeline",),
    }


def test_daily_progress_authorities_are_stage_scoped() -> None:
    acls = authority.DAILY_GITHUB_ACLS

    assert acls[
        "/var/lib/obsidian-github-pipeline/daily-progress/10-Evidence"
    ] == (
        "u:obsidian-github-sync:rwx",
        "u:obsidian-github-summarizer:r-x",
        "u:obsidian-github-renderer:r-x",
    )
    assert acls[
        "/var/lib/obsidian-github-pipeline/daily-progress/20-Summary"
    ] == (
        "u:obsidian-github-summarizer:rwx",
        "u:obsidian-github-renderer:r-x",
    )
    assert acls[
        "/var/lib/obsidian-github-pipeline/daily-progress/30-Projection"
    ] == (
        "u:obsidian-github-renderer:rwx",
        "u:obsidian-github-daily-writer:r-x",
    )
    assert acls[
        "/var/lib/obsidian-github-pipeline/daily-progress/40-Transport"
    ] == (
        "u:obsidian-github-daily-writer:rwx",
    )

    directories = {
        path: (owner, group, mode)
        for path, owner, group, mode in authority.DIRECTORIES
    }
    assert directories[
        "/etc/obsidian-github-summarizer"
    ] == ("root", "obsidian-github-summarizer", 0o750)
    assert directories[
        "/etc/obsidian-github-daily-writer"
    ] == ("root", "obsidian-github-daily-writer", 0o750)


def test_ai_acl_matrix_keeps_semantic_authorities_distinct() -> None:
    acls = authority.AI_ACLS

    assert "u:obsidian-ai-generator:rwx" in acls[
        "/var/lib/obsidian-ai/state/00-Untrusted"
    ]
    assert "u:obsidian-ai-reader:rwx" in acls[
        "/var/lib/obsidian-ai/state/04-Index"
    ]
    assert "u:obsidian-ai-reader:r-x" in acls[
        "/var/lib/obsidian-ai/vault/00-DailyNote"
    ]
    assert "u:obsidian-ai-reader:r-x" in acls[
        "/var/lib/obsidian-ai/vault/05-Idea"
    ]
    assert "u:obsidian-ai-embedder:--x" in acls[
        "/var/lib/obsidian-ai/state/04-Index"
    ]
    assert "u:obsidian-ai-embedder:r-x" in acls[
        "/var/lib/obsidian-ai/state/04-Index/semantic-embedding-requests"
    ]
    assert "u:obsidian-ai-embedder:rwx" in acls[
        "/var/lib/obsidian-ai/state/04-Index/semantic-embedding-results"
    ]
    assert acls[
        "/var/lib/obsidian-ai/state/04-Index/semantic-refresh-reader"
    ] == (
        "u:obsidian-ai-reader:rwx",
        "u:obsidian-ai-embedder:r-x",
    )
    assert acls[
        "/var/lib/obsidian-ai/state/04-Index/semantic-refresh-embedder"
    ] == (
        "u:obsidian-ai-reader:r-x",
        "u:obsidian-ai-embedder:rwx",
    )
    assert not any(
        entry.startswith("u:obsidian-ai-embedder:")
        for entry in acls["/var/lib/obsidian-ai/state/04-Index/semantic-corpus"]
    )
    assert "u:obsidian-ai-reader:r-x" in acls[
        "/var/lib/obsidian-ai/vault/10-Project"
    ]
    assert acls[
        "/var/lib/obsidian-ai/state/02-Orchestration/semantic-selections"
    ] == ("u:obsidian-ai-reader:rwx",)
    assert not any(
        entry.startswith("u:obsidian-ai-validator:")
        or entry.startswith("u:obsidian-ai-executor:")
        for entry in acls["/var/lib/obsidian-ai/vault/10-Project"]
    )
    assert "u:obsidian-ai-validator:rwx" in acls[
        "/var/lib/obsidian-ai/state/10-Validation"
    ]
    assert "u:obsidian-ai-evaluator:rwx" in acls[
        "/var/lib/obsidian-ai/state/15-Evaluation"
    ]
    assert "u:obsidian-ai-generator:rwx" in acls[
        "/var/lib/obsidian-ai/state/16-Human-Projection/generator"
    ]
    assert "u:obsidian-ai-sync:r-x" in acls[
        "/var/lib/obsidian-ai/state/16-Human-Projection/generator"
    ]
    assert not any(
        entry.startswith("u:obsidian-ai-sync:rwx")
        for entry in acls["/var/lib/obsidian-ai/state/16-Human-Projection/generator"]
    )
    assert "u:obsidian-ai-sync:rwx" in acls[
        "/var/lib/obsidian-ai/state/17-Human-Projection-Result"
    ]
    assert "u:obsidian-ai-reviewer:r-x" in acls[
        "/var/lib/obsidian-ai/state/16-Human-Projection/evaluator"
    ]
    assert "u:obsidian-ai-reviewer:r-x" in acls[
        "/var/lib/obsidian-ai/state/17-Human-Projection-Result"
    ]
    assert "u:obsidian-ai-reviewer:rwx" in acls[
        "/var/lib/obsidian-ai/state/20-Review"
    ]
    assert "u:obsidian-ai-reader:r-x" in acls[
        "/var/lib/obsidian-ai/state/20-Review"
    ]
    assert "u:obsidian-ai-executor:rwx" in acls[
        "/var/lib/obsidian-ai/state/25-Execution"
    ]
    assert "u:obsidian-ai-sync:rwx" in acls[
        "/var/lib/obsidian-ai/state/27-Transport"
    ]
    assert "u:obsidian-ai-executor:rwx" in acls[
        "/var/lib/obsidian-ai/state/30-Receipts"
    ]
    assert "u:obsidian-ai-reader:r-x" in acls[
        "/var/lib/obsidian-ai/state/30-Receipts"
    ]
    assert "u:obsidian-ai-sync:r-x" in acls[
        "/var/lib/obsidian-ai/state/30-Receipts"
    ]
    assert "u:obsidian-ai-sync:rwx" not in acls[
        "/var/lib/obsidian-ai/state/30-Receipts"
    ]


def test_status_identity_gets_metadata_projection_only() -> None:
    acls = authority.AI_ACLS

    assert "u:obsidian-ai-status:r-x" in acls[
        "/var/lib/obsidian-ai/state/02-Orchestration"
    ]
    assert "u:obsidian-ai-status:rwx" in acls[
        "/var/lib/obsidian-ai/state/02-Orchestration/status"
    ]
    assert all(
        not any(entry.startswith("u:obsidian-ai-status:") for entry in entries)
        for path, entries in acls.items()
        if path
        not in {
            "/var/lib/obsidian-ai/state",
            "/var/lib/obsidian-ai/state/02-Orchestration",
            "/var/lib/obsidian-ai/state/02-Orchestration/status",
        }
    )


def test_result_contract_explicitly_reports_no_activation() -> None:
    # The implementation returns these exact flags after the OS-level gates pass.
    source = Path("tools/provision_automation_authority.py").read_text()
    assert '"credentials_installed": False' in source
    assert '"systemd_units_installed": False' in source
    assert '"recurring_services_activated": False' in source


def test_github_mirror_read_view_lock_directory_is_provisioned() -> None:
    directories = {
        path: (owner, group, mode)
        for path, owner, group, mode in authority.DIRECTORIES
    }

    assert directories[
        "/var/lib/obsidian-github-mirror/state/24-Locks/read-view"
    ] == (
        "obsidian-github-mirror",
        "obsidian-github-mirror",
        0o700,
    )


def test_refresh_only_upgrade_creates_exact_handoffs_and_checks_cross_role_access() -> None:
    commands = []

    def runner(argv):
        command = tuple(map(str, argv))
        commands.append(command)
        if command[:1] == ("runuser",):
            user, flag, path = command[2], command[-2], command[-1]
            allowed = flag == "-r" or (
                user.endswith("reader") and not path.endswith("embedder")
            ) or (user.endswith("embedder") and path.endswith("embedder"))
            return authority.CommandResult(0 if allowed else 1, "", "")
        return authority.CommandResult(0, "", "")

    result = authority.provision_semantic_refresh(runner=runner, require_root=False)
    root = "/var/lib/obsidian-ai/state/04-Index"
    paths = {f"{root}/semantic-refresh-reader", f"{root}/semantic-refresh-embedder"}
    assert {command[-1] for command in commands if command[0] == "install"} == paths
    assert {command[-1] for command in commands if command[0] == "setfacl"} == paths
    assert not any(command[0] in {"useradd", "groupadd", "systemctl"} for command in commands)
    for path in paths:
        for entry in authority.AI_ACLS[path]:
            assert ("setfacl", "-m", f"d:{entry}", path) in commands
    assert result["directory_count"] == 2
    assert result["recurring_services_activated"] is False


def test_refresh_only_upgrade_requires_existing_reader_index_authority() -> None:
    commands = []

    def runner(argv):
        command = tuple(map(str, argv))
        commands.append(command)
        return authority.CommandResult(1 if command[:1] == ("runuser",) else 0, "", "")

    with pytest.raises(authority.AuthorityProvisionError, match="reader writes existing Index"):
        authority.provision_semantic_refresh(runner=runner, require_root=False)
    assert not any(command[0] in {"install", "setfacl"} for command in commands)
