"""Read-only canonical Evidence partition report for #290."""
from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest

from obsidian_automation.github_daily_activity import (
    ProjectBinding, _bounded_text, _date_window, _make_event,
    make_daily_evidence_bundle,
)
from obsidian_automation.github_daily_coverage_eval import (
    CoverageError, verified_partition_report,
)


def _evidence_file(tmp_path):
    start, _ = _date_window(date(2026, 10, 5))
    excerpt = _bounded_text(
        "sensitive private GitHub event that must not be logged",
        limit=4096, label="message",
    )
    assert excerpt is not None
    events = []
    for idx, repo in enumerate((
        "upiscium/RepoA", "upiscium/RepoB",
        "upiscium/RepoA", "upiscium/RepoB",
    )):
        events.append(_make_event(
            kind="default_branch_commit", repository=repo,
            occurred_at=start + timedelta(seconds=idx),
            url=f"https://github.com/{repo}/commit/{idx+1:040x}",
            actor="fixture-actor", entity_type="commit", number=None,
            source_id=f"fixture-{idx}", sha=f"{idx+1:040x}", message=excerpt,
        ))
    bundle = make_daily_evidence_bundle(
        target_date=date(2026, 10, 5),
        projects=[
            ProjectBinding(project_path=f"10-Project/{repo}/{repo}.md", repository=f"upiscium/{repo}")
            for repo in ("RepoA", "RepoB")
        ],
        events=events,
    )
    path = tmp_path / f"{bundle.sha256}.github-daily-evidence.json"
    path.write_bytes(bundle.canonical_bytes)
    return path, bundle


def test_real_artifact_partition_loader_handles_interleaved_repositories(tmp_path):
    path, bundle = _evidence_file(tmp_path)
    report = verified_partition_report(path, max_partial_context_bytes=5000)
    assert report["evidence_bundle_sha256"] == bundle.sha256
    assert report["repository_count"] == 2
    assert report["original_event_count"] == report["partitioned_event_count"] == 4
    assert report["partial_context_count"] == 2
    assert len(report["partition_sha256"]) == 64
    assert not report["source_text_returned"]
    assert report["publication_authorized"] is False
    assert "sensitive private" not in repr(report)
    assert "upiscium/RepoA" not in repr(report)
    repeat = verified_partition_report(path, max_partial_context_bytes=5000)
    assert repeat == report
    # Even the same Evidence has a different proposal identity at a different
    # partition budget, whether or not the resulting batch grouping changes.
    alternate = verified_partition_report(path, max_partial_context_bytes=4900)
    assert alternate["partition_sha256"] != report["partition_sha256"]


def test_evidence_artifact_metadata_rejects_corruption_and_unsafe_paths(tmp_path):
    path, _ = _evidence_file(tmp_path)
    noncanonical = tmp_path / ("0" * 64 + ".github-daily-evidence.json")
    noncanonical.write_bytes(path.read_bytes())
    with pytest.raises(Exception, match="filename"):
        verified_partition_report(noncanonical, max_partial_context_bytes=5000)
    symlink = tmp_path / ("b" * 64 + ".github-daily-evidence.json")
    symlink.symlink_to(path)
    with pytest.raises(CoverageError, match="inode"):
        verified_partition_report(symlink, max_partial_context_bytes=5000)
    with pytest.raises(CoverageError, match="invalid"):
        verified_partition_report(path, max_partial_context_bytes=0)
    path.write_bytes(b"not canonical Evidence")
    with pytest.raises(Exception):
        verified_partition_report(path, max_partial_context_bytes=5000)


def test_evidence_file_size_guard_precedes_content_read(tmp_path):
    path, _ = _evidence_file(tmp_path)
    with open(path, "r+b") as out:
        out.truncate(16 * 1024 * 1024 + 1)
    with pytest.raises(CoverageError, match="bounded"):
        verified_partition_report(path, max_partial_context_bytes=5000)


def test_canonical_evidence_fifo_cannot_block_metadata_report(tmp_path):
    """Opening a FIFO with O_RDONLY alone would hang before inode validation."""
    import os
    import time

    path, _ = _evidence_file(tmp_path)
    path.unlink()
    os.mkfifo(path, 0o600)
    start = time.monotonic()
    with pytest.raises(CoverageError, match="bounded"):
        verified_partition_report(path, max_partial_context_bytes=5000)
    assert time.monotonic() - start < 2.0


def test_canonical_evidence_hardlink_cannot_bypass_inode_admission(tmp_path):
    import os

    path, _ = _evidence_file(tmp_path)
    os.link(path, tmp_path / "second-link")
    with pytest.raises(CoverageError, match="bounded"):
        verified_partition_report(path, max_partial_context_bytes=5000)


def test_canonical_evidence_replaced_with_fifo_before_open_fails_nonblocking(
    tmp_path, monkeypatch,
):
    """A pathname swap exactly at read admission cannot create FIFO deadlock."""
    import os
    import time
    from obsidian_automation import github_daily_resume as resume

    path, _ = _evidence_file(tmp_path)
    original_reader = resume._read_bounded
    calls = []

    def change_path_then_open(value, *, max_bytes):
        calls.append(value)
        path.unlink()
        os.mkfifo(path, 0o600)
        return original_reader(value, max_bytes=max_bytes)

    monkeypatch.setattr(resume, "_read_bounded", change_path_then_open)
    start = time.monotonic()
    with pytest.raises(CoverageError, match="bounded"):
        verified_partition_report(path, max_partial_context_bytes=5000)
    assert calls == [path]
    assert time.monotonic() - start < 2.0


def _rewritten_evidence(tmp_path, original_path, *, make_event=None, pretty=False):
    import hashlib
    import json
    from obsidian_automation.artifact_lifecycle import _canonical_json_bytes

    raw = json.loads(original_path.read_bytes())
    if make_event is not None:
        event = make_event(dict(raw["events"][0]))
        unhashed = {key: value for key, value in event.items() if key != "evidence_id"}
        event["evidence_id"] = hashlib.sha256(_canonical_json_bytes(unhashed)).hexdigest()
        raw["events"][0] = event
    if pretty:
        data = (json.dumps(raw, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode()
    else:
        data = _canonical_json_bytes(raw)
    path = tmp_path / (
        hashlib.sha256(data).hexdigest() + ".github-daily-evidence.json"
    )
    path.write_bytes(data)
    return path


def test_rehashed_minimal_event_does_not_count_as_collector_evidence(tmp_path):
    original_path, _ = _evidence_file(tmp_path)
    path = _rewritten_evidence(
        tmp_path, original_path,
        make_event=lambda event: {"repository": event["repository"]},
    )
    with pytest.raises(CoverageError, match="Collector schema"):
        verified_partition_report(path, max_partial_context_bytes=5000)


def test_rehashed_wrong_event_type_is_rejected_despite_valid_self_hash(tmp_path):
    original_path, _ = _evidence_file(tmp_path)
    path = _rewritten_evidence(
        tmp_path, original_path,
        make_event=lambda event: {**event, "number": True},
    )
    with pytest.raises(CoverageError, match="field types"):
        verified_partition_report(path, max_partial_context_bytes=5000)


def test_rehashed_invalid_text_excerpt_is_rejected(tmp_path):
    original_path, _ = _evidence_file(tmp_path)
    path = _rewritten_evidence(
        tmp_path, original_path,
        make_event=lambda event: {**event, "message": {
            "text": "arbitrary", "truncated": [],
            "included_bytes": 9, "original_bytes": 9,
        }},
    )
    with pytest.raises(CoverageError, match="text excerpt"):
        verified_partition_report(path, max_partial_context_bytes=5000)


def test_self_hashed_noncanonical_json_is_not_accepted_as_original_evidence(tmp_path):
    original_path, _ = _evidence_file(tmp_path)
    path = _rewritten_evidence(tmp_path, original_path, pretty=True)
    with pytest.raises(CoverageError, match="not canonical"):
        verified_partition_report(path, max_partial_context_bytes=5000)
