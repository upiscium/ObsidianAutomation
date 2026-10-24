"""Offline nonpublishing single-user Daily Run Once tests (#444 / #298)."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os

import pytest

from obsidian_automation import github_daily_runonce as runonce
from obsidian_automation import github_daily_source_route as source
from obsidian_automation import github_daily_standard_source as standard
from test_github_daily_source_route import _FakeClient

NOW = datetime(2026, 10, 6, 1, tzinfo=timezone.utc)
TOKEN = 'fixture-secret-no-real-credential'


def setup_state(tmp_path):
    root = tmp_path / 'b4-state'
    root.mkdir(mode=0o700)
    mirror = root / 'mirror'
    mirror.mkdir(mode=0o700)
    project = mirror / '10-Project' / 'Example' / 'Example.md'
    project.parent.mkdir(parents=True, exist_ok=True)
    project.write_text(
        '---\ntype: project\nstatus: running\n'
        'github_repo: upiscium/Test\ngithub_watch: true\n---\n',
        encoding='utf-8')
    for part in ('cas', 'cas/evidence', 'cas/receipt', 'cas/capture', 'preview'):
        (root / part).mkdir(mode=0o700)
    conf = root / 'watcher.toml'
    conf.write_text(
        '[watcher]\n'
        f'vault_root = "{mirror}"\n'
        f'state_db = "{root / "watcher.sqlite3"}"\n'
        'github_api_base = "https://api.github.com"\n'
        'request_timeout_seconds = 10\n')
    conf.chmod(0o600)
    token = root / 'github.token'
    token.write_text(TOKEN + '\n')
    token.chmod(0o600)
    return root


def test_setup_is_private_and_token_not_in_public_receipt(tmp_path, monkeypatch):
    root = setup_state(tmp_path)
    cfg, token = runonce.load_run_config(root)
    assert token == TOKEN
    assert cfg.vault_root == root / 'mirror'
    assert cfg.github_api_base == 'https://api.github.com'
    monkeypatch.setattr(source, '_utc_now', lambda: NOW)
    receipt = runonce.run_once(
        cfg, _FakeClient(visible_pr=True, include_commit=True,
                         include_review_comment=True),
        cas_root=root / 'cas', preview_root=root / 'preview', now=NOW)
    assert receipt['status'] == 'preview_ready'
    assert receipt['date_jst'] == '2026-10-05'
    assert receipt['original_event_count'] == 6
    assert receipt['rendered_entity_count'] == 2
    assert receipt['model_used'] is False
    assert receipt['publication_authorized'] is False
    assert receipt['vault_modified'] is False
    assert TOKEN not in json.dumps(receipt)
    assert sorted(receipt['artifact_names']) == sorted(p.name for p in (root / 'preview').iterdir())
    assert len(receipt['artifact_names']) == 2
    for file in (root / 'preview').iterdir():
        assert file.stat().st_mode & 0o777 == 0o600
        assert file.stat().st_uid == os.geteuid()


def test_duplicate_run_same_source_is_exactly_idempotent(tmp_path, monkeypatch):
    root = setup_state(tmp_path)
    cfg, _ = runonce.load_run_config(root)
    monkeypatch.setattr(source, '_utc_now', lambda: NOW)
    api = _FakeClient(visible_pr=True, include_commit=True)
    first = runonce.run_once(cfg, api, cas_root=root/'cas',
                             preview_root=root/'preview', now=NOW)
    files = {f.name:f.read_bytes() for f in (root/'preview').iterdir()}
    second = runonce.run_once(cfg, api, cas_root=root/'cas',
                              preview_root=root/'preview', now=NOW)
    assert first == second
    assert {f.name:f.read_bytes() for f in (root/'preview').iterdir()} == files
    assert all(len(list((root/'cas'/part).iterdir())) == 1
               for part in ('capture', 'receipt', 'evidence'))


def test_conflicting_output_is_not_replaced(tmp_path, monkeypatch):
    root = setup_state(tmp_path)
    cfg, _ = runonce.load_run_config(root)
    monkeypatch.setattr(source, '_utc_now', lambda: NOW)
    api = _FakeClient(visible_pr=True)
    good = runonce.run_once(cfg, api, cas_root=root/'cas',
                            preview_root=root/'preview', now=NOW)
    file = next((root/'preview').glob('*.deterministic-daily.md'))
    file.write_bytes(b'conflict-not-identical')
    before = {f.name:f.read_bytes() for f in (root/'preview').iterdir()}
    with pytest.raises((runonce.DailyPreviewRunError,
                        standard.digest.DeterministicDigestError)):
        runonce.run_once(cfg, api, cas_root=root/'cas',
                         preview_root=root/'preview', now=NOW)
    assert {f.name:f.read_bytes() for f in (root/'preview').iterdir()} == before
    assert good['publication_authorized'] is False


def test_missing_unowned_and_symlink_token_fail_before_network(tmp_path):
    root = setup_state(tmp_path)
    token = root / 'github.token'
    token.chmod(0o644)
    with pytest.raises(runonce.DailyPreviewRunError):
        runonce.load_run_config(root)
    token.chmod(0o600)
    token.unlink()
    outside = tmp_path / 'hidden-token'
    outside.write_text(TOKEN)
    outside.chmod(0o600)
    token.symlink_to(outside)
    with pytest.raises(runonce.DailyPreviewRunError):
        runonce.load_run_config(root)
    token.unlink()
    with pytest.raises(runonce.DailyPreviewRunError):
        runonce.load_run_config(root)
    assert not list((root/'cas'/'capture').iterdir())


def test_invalid_token_syntax_and_file_identity(tmp_path):
    root = setup_state(tmp_path)
    token = root / 'github.token'
    token.write_text('invalid token with spaces\n')
    with pytest.raises(runonce.DailyPreviewRunError):
        runonce.load_run_config(root)
    token.write_text('x'*515)
    with pytest.raises(runonce.DailyPreviewRunError):
        runonce.load_run_config(root)
    token.write_text(TOKEN)
    token.chmod(0o600)
    root.chmod(0o755)
    with pytest.raises(runonce.DailyPreviewRunError):
        runonce.load_run_config(root)


def test_unapproved_api_or_untrusted_mirror_rejected(tmp_path):
    root = setup_state(tmp_path)
    config = root/'watcher.toml'
    original = config.read_text()
    config.write_text(original.replace('https://api.github.com', 'https://evil.invalid'))
    with pytest.raises(runonce.DailyPreviewRunError):
        runonce.load_run_config(root)
    config.write_text(original.replace('vault_root = ', 'vault_root = "/tmp/not-private" # '))
    with pytest.raises(runonce.DailyPreviewRunError):
        runonce.load_run_config(root)


def test_future_or_noncanonical_clock_blocks_without_capture(tmp_path):
    root = setup_state(tmp_path)
    cfg, _ = runonce.load_run_config(root)
    with pytest.raises(runonce.DailyPreviewRunError):
        runonce.run_once(cfg, _FakeClient(), cas_root=root/'cas',
                         preview_root=root/'preview', now=datetime(2026,10,6,1))
    assert not list((root/'cas'/'capture').iterdir())


def test_bad_existing_cas_source_does_not_publish_preview(tmp_path, monkeypatch):
    root = setup_state(tmp_path)
    cfg, _ = runonce.load_run_config(root)
    monkeypatch.setattr(source, '_utc_now', lambda: NOW)
    api = _FakeClient(visible_pr=True)
    first = runonce.run_once(cfg, api, cas_root=root/'cas',
                             preview_root=root/'preview', now=NOW)
    (root/'preview'/first['artifact_names'][0]).unlink()
    receipt = next((root/'cas'/'receipt').iterdir())
    receipt.write_bytes(b'broken')
    with pytest.raises((runonce.DailyPreviewRunError,
                        standard.StandardSourceError,
                        standard.digest.DeterministicDigestError)):
        runonce.run_once(cfg, api, cas_root=root/'cas',
                         preview_root=root/'preview', now=NOW)
    assert not (root/'preview'/first['artifact_names'][0]).exists()


def test_cli_error_is_bounded_and_not_secret_bearing(tmp_path, capsys):
    root = setup_state(tmp_path)
    (root/'github.token').write_text('invalid raw token with spaces')
    assert runonce.main(['--state',str(root)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ''
    assert 'blocked' in captured.err
    assert TOKEN not in captured.err
    assert 'invalid raw token' not in captured.err


def test_cli_success_returns_nosecret_typed_receipt(tmp_path, monkeypatch, capsys):
    root = setup_state(tmp_path)
    monkeypatch.setattr(runonce, 'run_once',
                        lambda config,client,**kwargs: {
                          'version':1,'status':'preview_ready',
                          'date_jst':'2026-10-05', 'model_used':False,
                          'publication_authorized':False, 'vault_modified':False})
    assert runonce.main(['--state',str(root)]) == 0
    obj=json.loads(capsys.readouterr().out)
    assert obj['status']=='preview_ready'
    assert obj['publication_authorized'] is False
    assert TOKEN not in json.dumps(obj)
