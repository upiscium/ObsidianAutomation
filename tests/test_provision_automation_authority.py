from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


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


def test_ai_acl_matrix_keeps_semantic_authorities_distinct() -> None:
    acls = authority.AI_ACLS

    assert "u:obsidian-ai-generator:rwx" in acls[
        "/var/lib/obsidian-ai/state/00-Untrusted"
    ]
    assert "u:obsidian-ai-reader:rwx" in acls[
        "/var/lib/obsidian-ai/state/04-Index"
    ]
    assert "u:obsidian-ai-reader:r-x" in acls[
        "/var/lib/obsidian-ai/vault/10-Project"
    ]
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
