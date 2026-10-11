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
