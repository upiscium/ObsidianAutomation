"""Fixed-source read-only Status Writer history, never full pipeline health."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from obsidian_automation import github_pipeline_status_history_reader as reader
from obsidian_automation.github_project_status_mutation import ProjectStatusTransportResult

NOW = datetime(2026, 11, 1, 3, 0, tzinfo=timezone.utc)
A, B, C = "a" * 64, "b" * 64, "c" * 64


def _dir(tmp_path: Path) -> Path:
    root = tmp_path / "27-Transport"
    root.mkdir(mode=0o750)
    return root


def _result(sha=A):
    return ProjectStatusTransportResult(
        proposal_sha256=sha, project_path="10-Project/Status.md",
        repository="org/repo", expected_status="planning",
        desired_status="running", outcome="applied",
        before_content_sha256=A, after_content_sha256=B,
        completed_at="2026-11-01T02:10:00Z",
    ).to_json_bytes()


def _rejection(sha=B):
    return (
        json.dumps({
            "record_version": 1, "stage": "github_project_status_transport",
            "proposal_sha256": sha, "project": "10-Project/team/Status.md",
            "outcome": "rejected_transport", "reason": "authority_rejection",
            "http_status": 403, "completed_at": "2026-11-01T02:20:00Z",
        }, sort_keys=True) + "\n"
    ).encode()


def _put(root, kind, sha, raw):
    suffix = ("transport-result" if kind == "results" else "rejection")
    target = root / f"{sha}.github-status.{suffix}.json"
    target.write_bytes(raw)
    target.chmod(0o640)
    return target


def _read(root):
    return reader._read_root(root, uid=os.getuid(), gid=os.getgid(), now=NOW)


def test_attested_writer_directory_reads_real_producer_results_without_effect(tmp_path):
    root = _dir(tmp_path)
    good = _put(root, "results", A, _result())
    reject = _put(root, "rejections", B, _rejection())
    # Unrelated Overview artifacts must not be interpreted as B3 status.
    overview = root / f"{C}.github-overview.transport-result.json"
    overview.write_bytes(b"private-unrelated")
    overview.chmod(0o640)
    before = [(p.stat().st_mtime_ns, p.read_bytes()) for p in (good, reject, overview)]
    result = _read(root)
    assert result["evidence_state"] == "producer_attested_terminal_history"
    assert result["last_terminal_observation"]["category"] == "authority_rejected"
    assert result["last_status_rejection"]["http_status"] == 403
    assert result["last_verified_status_readback"]["outcome"] == "applied"
    assert result["history_completeness"] == "terminal_only"
    assert result["last_attempt"] is None
    assert result["active_incident"] == "unknown"
    assert result["last_verified_pipeline_success"] is None
    raw = json.dumps(result)
    for secret in ("org/repo", "10-Project", A, B):
        assert secret not in raw
    assert [(p.stat().st_mtime_ns, p.read_bytes()) for p in (good, reject, overview)] == before


def test_no_files_is_not_idle_or_known_success(tmp_path):
    output = _read(_dir(tmp_path))
    assert output["evidence_state"] == "missing"
    assert output["overall_health"] == "unknown"


def test_missing_or_untrusted_source_fails_closed(tmp_path):
    assert _read(tmp_path / "missing")["evidence_state"] == "source_unavailable"
    root = _dir(tmp_path)
    _put(root, "results", A, _result())
    assert reader._read_root(root, uid=os.getuid() + 1, gid=os.getgid(), now=NOW)[
        "evidence_state"
    ] == "source_unavailable"
    root.chmod(0o777)
    assert _read(root)["evidence_state"] == "source_unavailable"


def test_symlink_file_and_dir_fail_closed(tmp_path):
    root = _dir(tmp_path)
    file = _put(root, "results", A, _result())
    shortcut = tmp_path / "shortcut"
    shortcut.symlink_to(root, target_is_directory=True)
    assert _read(shortcut)["evidence_state"] == "source_unavailable"
    source = tmp_path / "private-original"
    source.write_bytes(_result())
    source.chmod(0o640)
    file.unlink()
    file.symlink_to(source)
    assert _read(root)["evidence_state"] == "source_invalid"


def test_regular_file_permissions_enforced(tmp_path):
    root = _dir(tmp_path)
    file = _put(root, "rejections", B, _rejection())
    file.chmod(0o644)
    assert _read(root)["evidence_state"] == "source_invalid"


def test_mismatched_sha_in_filename_rejected(tmp_path):
    root = _dir(tmp_path)
    _put(root, "rejections", A, _rejection(sha=B))
    output = _read(root)
    assert output["evidence_state"] == "source_invalid"
    assert output["last_status_rejection"] is None


def test_proposal_success_and_rejection_conflict_is_invalid(tmp_path):
    root = _dir(tmp_path)
    _put(root, "results", A, _result(sha=A))
    _put(root, "rejections", A, _rejection(sha=A))
    output = _read(root)
    assert output["evidence_state"] == "invalid"
    assert output["last_verified_status_readback"] is None


@pytest.mark.parametrize("bad", (b"{}", b"{", b"\xff", b"x" * 8193))
def test_invalid_payload_cannot_hide_behind_success(tmp_path, bad):
    root = _dir(tmp_path)
    _put(root, "results", A, _result())
    _put(root, "rejections", B, bad)
    output = _read(root)
    assert output["evidence_state"] == "source_invalid"
    assert output["last_verified_status_readback"] is None


def test_malformed_status_filename_fails_closed(tmp_path):
    root = _dir(tmp_path)
    target = root / "bad.github-status.rejection.json"
    target.write_bytes(_rejection())
    target.chmod(0o640)
    assert _read(root)["evidence_state"] == "source_invalid"


def test_mid_read_replacement_fails_closed(tmp_path, monkeypatch):
    root = _dir(tmp_path)
    file = _put(root, "results", A, _result())
    original = reader._read_regular_file

    def replace_after_read(dirfd, name, *, uid, gid):
        observed = original(dirfd, name, uid=uid, gid=gid)
        file.write_bytes(_result().replace(b"planning", b"stable"))
        return observed

    monkeypatch.setattr(reader, "_read_regular_file", replace_after_read)
    assert _read(root)["evidence_state"] == "source_unavailable"


def test_record_budget_is_bounded(tmp_path, monkeypatch):
    root = _dir(tmp_path)
    _put(root, "results", A, _result())
    _put(root, "rejections", B, _rejection())
    monkeypatch.setattr(reader, "MAX_RECEIPTS_PER_KIND", 1)
    assert _read(root)["evidence_state"] == "capacity_exceeded"


def test_scandir_never_materializes_unbounded_entries(tmp_path, monkeypatch):
    root = _dir(tmp_path)
    monkeypatch.setattr(reader, "_DIR_ENTRIES_LIMIT", 2)

    class TooMany:
        count = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            for i in range(1000):
                self.count += 1
                if self.count > 3:
                    raise AssertionError("scanned past bounded decision")
                yield type("Entry", (), {"name": f"noise-{i}"})()

    scanner = TooMany()
    monkeypatch.setattr(reader.os, "scandir", lambda _fd: scanner)
    result = _read(root)
    assert result["evidence_state"] == "capacity_exceeded"
    assert scanner.count == 3


def test_bad_timestamp_and_http_result_are_not_authoritative(tmp_path):
    root = _dir(tmp_path)
    row = json.loads(_rejection())
    row["http_status"] = 200
    _put(root, "rejections", B, json.dumps(row).encode())
    assert _read(root)["evidence_state"] == "source_invalid"


def test_reader_refuses_bad_argument_types(tmp_path):
    root = _dir(tmp_path)
    with pytest.raises(ValueError):
        reader._read_root(root, uid=True, gid=os.getgid(), now=NOW)
