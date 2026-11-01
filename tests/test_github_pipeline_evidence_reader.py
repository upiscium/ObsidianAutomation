"""#316: same-host, non-mutating, writer-identity receipt observation."""
from __future__ import annotations

import hashlib
import json
import os

from datetime import datetime, timezone

import pytest

from obsidian_automation import github_pipeline_evidence_reader as reader
from obsidian_automation.github_project_status_mutation import (
    ProjectStatusTransportResult,
)
from obsidian_automation.github_project_overview import (
    ProjectOverviewTransportResult,
)

NOW = datetime(2026, 11, 1, 3, 0, tzinfo=timezone.utc)
SHA1 = "a" * 64
SHA2 = "b" * 64
PATH = "10-Project/example.md"


def _status(sha=SHA1):
    return ProjectStatusTransportResult(
        proposal_sha256=sha, project_path=PATH,
        repository="some-org/private-project", expected_status="planning",
        desired_status="running", outcome="applied",
        before_content_sha256=SHA1, after_content_sha256=SHA2,
        completed_at="2026-11-01T02:59:00Z",
    ).to_json_bytes()


def _overview():
    return ProjectOverviewTransportResult(
        proposal_sha256=SHA2, project_path=PATH,
        status_path="10-Project/Status.md",
        repository="some-org/private-project", outcome="updated",
        before_content_sha256=SHA1, after_content_sha256=SHA2,
        completed_at="2026-11-01T02:58:00Z",
    ).to_json_bytes()


def _dir(tmp_path):
    root = tmp_path / "27-Transport"
    root.mkdir(mode=0o750)
    return root


def _path(root, kind="status", sha=SHA1):
    return root / f"{sha}.github-{kind}.transport-result.json"


def _put(root, kind="status", *, payload=None, sha=None):
    if sha is None:
        sha = SHA1 if kind == "status" else hashlib.sha256(PATH.encode()).hexdigest()
    target = _path(root, kind, sha)
    target.write_bytes(payload if payload is not None else (
        _status(sha=sha) if kind == "status" else _overview()
    ))
    target.chmod(0o640)
    return target


def _read(root):
    return reader._read_root(root, uid=os.getuid(), gid=os.getgid(), now=NOW)


def test_matches_both_producer_file_contracts_and_does_not_mutate(tmp_path):
    root = _dir(tmp_path)
    a, b = _put(root), _put(root, "overview")
    before = [(f.stat().st_mtime_ns, f.read_bytes()) for f in (a, b)]
    output = _read(root)
    assert output["generated_at"] == "2026-11-01T03:00:00Z"
    assert output["status"]["evidence_state"] == "producer_readback"
    assert output["status"]["last_outcome"] == "applied"
    assert output["overview"]["last_outcome"] == "updated"
    assert output["last_verified_pipeline_success"] is None
    assert output["overall_health"] == "unknown"
    result_json = json.dumps(output)
    assert "some-org" not in result_json and "example.md" not in result_json
    assert [(f.stat().st_mtime_ns, f.read_bytes()) for f in (a, b)] == before


def test_source_owner_gid_and_permissions_are_required(tmp_path):
    root = _dir(tmp_path)
    _put(root)
    assert _read(root)["status"]["evidence_state"] == "producer_readback"
    assert reader._read_root(
        root, uid=os.getuid() + 1, gid=os.getgid(), now=NOW,
    )["status"]["evidence_state"] == "source_unavailable"
    assert reader._read_root(
        root, uid=os.getuid(), gid=os.getgid() + 1, now=NOW,
    )["status"]["evidence_state"] == "source_unavailable"
    root.chmod(0o777)
    assert _read(root)["status"]["evidence_state"] == "source_unavailable"


def test_directory_symlink_and_ancestor_symlink_fail_closed(tmp_path):
    root = _dir(tmp_path)
    _put(root)
    external = tmp_path / "shortcut"
    external.symlink_to(root, target_is_directory=True)
    assert _read(external)["status"]["evidence_state"] == "source_unavailable"
    middle = tmp_path / "nested"
    middle.symlink_to(tmp_path, target_is_directory=True)
    assert _read(middle / root.name)["status"]["evidence_state"] == "source_unavailable"


@pytest.mark.parametrize("mode", (0o666, 0o644, 0o600 | 0o001))
def test_world_or_group_access_is_rejected(tmp_path, mode):
    root = _dir(tmp_path)
    f = _put(root)
    f.chmod(mode)
    assert _read(root)["status"]["evidence_state"] == "source_invalid"


def test_unexpected_filename_binding_does_not_count_as_success(tmp_path):
    root = _dir(tmp_path)
    _put(root, sha=SHA2, payload=_status(sha=SHA1))
    assert _read(root)["status"]["evidence_state"] == "source_invalid"


def test_symlink_file_fifo_and_hardlink_not_followed(tmp_path):
    root = _dir(tmp_path)
    original = tmp_path / "original"
    original.write_bytes(_status())
    original.chmod(0o640)
    _path(root).symlink_to(original)
    assert _read(root)["status"]["evidence_state"] == "source_invalid"
    _path(root).unlink()
    os.mkfifo(_path(root), mode=0o640)
    assert _read(root)["status"]["evidence_state"] == "source_invalid"
    _path(root).unlink()
    os.link(original, _path(root))
    assert _read(root)["status"]["evidence_state"] == "source_invalid"


@pytest.mark.parametrize("payload", (
    b"{}", b"{", b"\xff", _status() + b" " * 8192,
))
def test_malformed_receipts_never_become_verified(tmp_path, payload):
    root = _dir(tmp_path)
    _put(root, payload=payload)
    result = _read(root)
    assert result["status"]["evidence_state"] == "source_invalid"
    assert result["last_verified_pipeline_success"] is None


def test_extreme_time_offset_fails_closed_in_reader(tmp_path):
    root = _dir(tmp_path)
    value = json.loads(_status())
    value["completed_at"] = "0001-01-01T00:00:00+23:59"
    _put(root, payload=json.dumps(value).encode("utf-8"))
    result = _read(root)
    assert result["status"]["evidence_state"] == "source_invalid"
    assert result["overall_health"] == "unknown"


def test_bad_status_does_not_hide_good_overview(tmp_path):
    root = _dir(tmp_path)
    _put(root, payload=b"{}")
    _put(root, "overview")
    result = _read(root)
    assert result["status"]["evidence_state"] == "source_invalid"
    assert result["overview"]["evidence_state"] == "producer_readback"


def test_entry_budget_and_kind_budget_are_fail_closed(tmp_path, monkeypatch):
    root = _dir(tmp_path)
    _put(root)
    _put(root, sha=SHA2, payload=_status(sha=SHA2))
    monkeypatch.setattr(reader, "MAX_RECEIPTS_PER_KIND", 1)
    assert _read(root)["status"]["evidence_state"] == "capacity_exceeded"
    monkeypatch.setattr(reader, "_DIR_ENTRIES_LIMIT", 1)
    assert _read(root)["status"]["evidence_state"] == "capacity_exceeded"


def test_listing_never_materializes_all_entries(tmp_path, monkeypatch):
    root = _dir(tmp_path)
    _put(root)
    original = reader.os.listdir
    def forbidden(*_args, **_kwargs):
        raise AssertionError("full os.listdir materialization is forbidden")
    monkeypatch.setattr(reader.os, "listdir", forbidden)
    assert _read(root)["status"]["evidence_state"] == "producer_readback"
    monkeypatch.setattr(reader.os, "listdir", original)


def test_file_replacement_during_read_rejects_snapshot(tmp_path, monkeypatch):
    root = _dir(tmp_path)
    target = _put(root)
    original = reader._read_regular_file

    def replace_after_read(fd, name, *, uid, gid):
        raw = original(fd, name, uid=uid, gid=gid)
        target.write_bytes(b"{}")
        return raw

    monkeypatch.setattr(reader, "_read_regular_file", replace_after_read)
    assert _read(root)["status"]["evidence_state"] == "source_unavailable"


def test_missing_dir_and_nontrusted_root_fail_closed(tmp_path):
    assert _read(tmp_path / "missing")["status"]["evidence_state"] == "source_unavailable"
    with pytest.raises(ValueError):
        reader._read_root(tmp_path, uid=True, gid=os.getgid(), now=NOW)


def test_legacy_production_scale_998_status_22_overviews(tmp_path):
    """Known 2026-11-01 inventory must not falsely overflow the old 128 cap."""
    root = _dir(tmp_path)
    for number in range(1, 999):
        key = f"{number:064x}"
        _put(root, sha=key, payload=_status(key))
    for number in range(22):
        project_path = f"10-Project/fixture-{number}.md"
        payload = ProjectOverviewTransportResult(
            proposal_sha256=SHA2, project_path=project_path,
            status_path="10-Project/Status.md",
            repository="some-org/private-project", outcome="updated",
            before_content_sha256=SHA1, after_content_sha256=SHA2,
            completed_at="2026-11-01T02:58:00Z",
        ).to_json_bytes()
        project_key = hashlib.sha256(project_path.encode()).hexdigest()
        _put(root, "overview", payload=payload, sha=project_key)
    output = _read(root)
    assert output["status"]["evidence_state"] == "producer_readback"
    assert output["overview"]["evidence_state"] == "producer_readback"
    assert len(list(root.iterdir())) == 1020
    assert output["last_verified_pipeline_success"] is None


def test_scandir_stops_after_limit_plus_one(tmp_path, monkeypatch):
    """An oversized directory must not require materializing every filename."""
    root = _dir(tmp_path)
    monkeypatch.setattr(reader, "_DIR_ENTRIES_LIMIT", 3)

    class BoundedEntries:
        reads = 0

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def __iter__(self):
            for i in range(1000):
                self.reads += 1
                if self.reads > 4:
                    raise AssertionError("scandir advanced beyond the decision budget")
                yield type("Entry", (), {"name": f"unrelated-{i}"})()

    scanner = BoundedEntries()
    monkeypatch.setattr(reader.os, "scandir", lambda _fd: scanner)
    output = _read(root)
    assert output["status"]["evidence_state"] == "capacity_exceeded"
    assert output["overview"]["evidence_state"] == "capacity_exceeded"
    assert scanner.reads == 4


def test_status_md_project_receipt_from_status_producer_is_not_false_invalid(tmp_path):
    root = _dir(tmp_path)
    raw = json.loads(_status())
    raw["project_path"] = "10-Project/team/Status.md"
    _put(root, payload=json.dumps(raw).encode("utf-8"))
    result = _read(root)
    assert result["status"]["evidence_state"] == "producer_readback"
    assert result["status"]["last_outcome"] == "applied"
    assert result["last_verified_pipeline_success"] is None
