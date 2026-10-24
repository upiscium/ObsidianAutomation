"""Synthetic one-service-user B4 Collector/VerifiedSource/Preview E2E (#444).

No network/provider writes or actual Nextcloud/Vault paths. Tests only the
Standard profile's private file store and exact original source admission.
"""
from datetime import datetime, timezone
from pathlib import Path
import json
import os

import pytest

from obsidian_automation import github_daily_source_route as route
from obsidian_automation import github_daily_standard_source as standard
from obsidian_automation import github_daily_deterministic_preview as cli
from test_github_daily_source_route import _FakeClient, _config, DATE


def private_dirs(tmp_path: Path) -> Path:
    store = tmp_path / 'private-cas'
    store.mkdir(mode=0o700)
    for name in ('evidence', 'receipt', 'capture'):
        (store / name).mkdir(mode=0o700)
    return store


def collect(tmp_path, monkeypatch):
    store = private_dirs(tmp_path)
    monkeypatch.setattr(route, '_utc_now',
                        lambda: datetime(2026, 10, 6, 1, tzinfo=timezone.utc))
    args = (_config(tmp_path), _FakeClient(
        visible_pr=True, include_commit=True, include_review_comment=True), DATE)
    sha = standard.collect_standard_source(store, *args)
    assert standard.collect_standard_source(store, *args) == sha
    return store, sha


def test_single_user_verified_v3_roundtrip_and_idempotence(tmp_path, monkeypatch):
    store, sha = collect(tmp_path, monkeypatch)
    result = standard.read_standard_digest(store, sha)
    assert result.capture_sha256 == sha
    assert result.date == DATE.isoformat()
    assert result.report()['model_used'] is False
    assert result.report()['publication_authorized'] is False
    assert len(result.items) == 2
    assert 'PR #2' in result.render()
    for stage in ('evidence', 'receipt', 'capture'):
        files = list((store / stage).iterdir())
        assert len(files) == 1
        file = files[0]
        assert file.stat().st_mode & 0o777 == 0o600
        assert file.stat().st_uid == os.geteuid()


def test_standard_cli_preview_three_files_twice_without_daily_mutation(tmp_path, monkeypatch):
    store, sha = collect(tmp_path, monkeypatch)
    output = tmp_path / 'output'
    output.mkdir(mode=0o700)
    daily = tmp_path / 'daily.md'
    old = (b'---\ntype: daily-review\n---\n# Project Progress\nold\n'
           b'# My Notes\nuntouched\n')
    daily.write_bytes(old)
    args = ['--capture-sha', sha, '--target-date', DATE.isoformat(),
            '--standard-cas-root', str(store), '--output-dir', str(output),
            '--preview-daily', str(daily)]
    assert cli.main(args) == 0
    saved = {f.name: f.read_bytes() for f in output.iterdir()}
    assert len(saved) == 3
    assert cli.main(args) == 0
    assert {f.name: f.read_bytes() for f in output.iterdir()} == saved
    assert daily.read_bytes() == old
    assert sum(name.endswith('.daily-note.preview.md') for name in saved) == 1


def test_corrupt_original_evidence_rejected_before_daily_preview(tmp_path, monkeypatch):
    store, sha = collect(tmp_path, monkeypatch)
    file = next((store / 'evidence').iterdir())
    file.write_bytes(file.read_bytes() + b' ')
    with pytest.raises(standard.StandardSourceError):
        standard.read_standard_digest(store, sha)
    output = tmp_path / 'output'
    output.mkdir(mode=0o700)
    assert cli.main(['--capture-sha', sha, '--target-date', DATE.isoformat(),
                     '--standard-cas-root', str(store),
                     '--output-dir', str(output)]) == 2
    assert list(output.iterdir()) == []


def test_private_root_and_wrong_dates_fail_closed(tmp_path, monkeypatch):
    store, sha = collect(tmp_path, monkeypatch)
    bad = tmp_path / 'public'
    bad.mkdir(mode=0o755)
    with pytest.raises(standard.StandardSourceError):
        standard.read_standard_digest(bad, sha)
    assert cli.main(['--capture-sha', sha, '--target-date', '2026-10-06',
                     '--standard-cas-root', str(store)]) == 2


def test_mismatched_existing_capture_is_never_overwritten(tmp_path, monkeypatch):
    store, sha = collect(tmp_path, monkeypatch)
    source = next((store / 'receipt').iterdir())
    before = source.read_bytes()
    source.write_bytes(b'forged')
    monkeypatch.setattr(route, '_utc_now',
                        lambda: datetime(2026, 10, 6, 1, tzinfo=timezone.utc))
    with pytest.raises((standard.StandardSourceError,
                        cli.DeterministicDigestError)):
        standard.collect_standard_source(
            store, _config(tmp_path), _FakeClient(
                visible_pr=True, include_commit=True,
                include_review_comment=True), DATE)
    assert source.read_bytes() == b'forged'
    assert source.read_bytes() != before
