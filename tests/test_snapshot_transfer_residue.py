"""#607: Nextcloud transfer residue never enters a Snapshot."""
from pathlib import Path

from obsidian_automation.vault_snapshot import (
    TRANSFER_RESIDUE_RCLONE_FILTER, SnapshotConfig, build_manifest,
)

RESIDUE = "ca05901997a09923835bd9e24dc9d2f1.ocTransferId1007821865.part"


def test_residue_excluded_at_root_and_nested_even_with_empty_policy(tmp_path):
    root = tmp_path / "vault"
    (root / "98-System").mkdir(parents=True)
    for rel in (RESIDUE, f"98-System/{RESIDUE}", "Dashboard.md", "98-System/a.md",
                "notes.part", "x.ocTransferIdfoo.txt"):
        (root / rel).write_text("x")
    manifest = build_manifest(root, SnapshotConfig(exclude=(), repository_owned=()))
    assert set(manifest) == {"Dashboard.md", "98-System/a.md", "notes.part",
                             "x.ocTransferIdfoo.txt"}


def test_rclone_filter_is_a_basename_glob():
    assert TRANSFER_RESIDUE_RCLONE_FILTER == "*.ocTransferId*.part"
