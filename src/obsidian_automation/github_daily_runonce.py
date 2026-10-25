"""Source-verified, nonpublishing B4 Daily run-once adapter for a single user.

Run-once is a callable that #298 can inject into its own scheduler/Job layer.
It never installs a service, sets a schedule, updates a Vault, or calls an LLM.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import stat
import sys
from zoneinfo import ZoneInfo

from . import github_daily_deterministic_preview as preview
from . import github_daily_standard_source as standard
from . import github_project_watcher as watcher


class DailyPreviewRunError(ValueError):
    """Cannot safely produce a source-verified previous-JST-day preview."""


_TOKEN = re.compile(r'[A-Za-z0-9_-]{8,512}\Z')
_JST = ZoneInfo('Asia/Tokyo')


def _private_file(path: Path, *, maximum: int) -> bytes:
    try:
        # All config and tokens live immediately under a private,
        # caller-owned state root; never resolve or follow a token symlink.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            meta = os.fstat(fd)
            if (not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.geteuid()
                    or stat.S_IMODE(meta.st_mode) != 0o600 or meta.st_nlink != 1
                    or not 0 < meta.st_size <= maximum):
                raise DailyPreviewRunError('invalid private configuration file')
            with os.fdopen(os.dup(fd), 'rb') as stream:
                data = stream.read(maximum + 1)
            if len(data) != meta.st_size:
                raise DailyPreviewRunError('private configuration changed while reading')
            return data
        finally:
            os.close(fd)
    except OSError as exc:
        raise DailyPreviewRunError('private configuration unavailable') from exc


def load_run_config(state: Path) -> tuple[watcher.WatcherConfig, str]:
    """Require existing same-user private state, credentials and mirror."""
    try:
        preview._private_root(state)
        config = state / 'watcher.toml'
        token = state / 'github.token'
        _private_file(config, maximum=4096)
        secret = _private_file(token, maximum=514).decode('ascii')
        if secret.endswith('\n'):
            secret = secret[:-1]
        if _TOKEN.fullmatch(secret) is None:
            raise DailyPreviewRunError('invalid GitHub credential format')
        cfg = watcher.load_config(config)
        if (cfg.github_api_base != 'https://api.github.com'
                or cfg.vault_root != state / 'mirror'
                or cfg.state_db != state / 'watcher.sqlite3'
                or cfg.project_folder != '10-Project'):
            raise DailyPreviewRunError('unsupported B4 source configuration')
        preview._private_root(cfg.vault_root)
        standard._stages(state / 'cas')
        preview._private_root(state / 'preview')
        return cfg, secret
    except (UnicodeError, OSError, watcher.GitHubProjectWatcherError,
            standard.StandardSourceError, preview.DeterministicDigestError) as exc:
        raise DailyPreviewRunError('B4 setup incomplete') from exc


def run_once(
    config: watcher.WatcherConfig, client: watcher.GitHubClient, *,
    cas_root: Path, preview_root: Path, now: datetime | None = None,
) -> dict[str, object]:
    """Collect yesterday's JST GitHub state and expose private Preview only.

    Called by a future #298 scheduled job or run-once request. Retry safety
    comes from immutable source CAS and atomic idempotent digest publication.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise DailyPreviewRunError('invalid run timestamp')
    yesterday = now.astimezone(_JST).date() - timedelta(days=1)
    standard._stages(cas_root)
    preview._private_root(preview_root)
    capture_sha = standard.collect_standard_source(cas_root, config, client, yesterday)
    document = standard.read_standard_digest(cas_root, capture_sha)
    if (document.capture_sha256 != capture_sha or document.date != yesterday.isoformat()
            or document.report()['model_used'] is not False):
        raise DailyPreviewRunError('verified Daily identity mismatch')
    artifacts = preview.publish_digest_artifacts(preview_root, document)
    return {
        'version': 1,
        'status': 'preview_ready',
        'date_jst': document.date,
        'capture_sha256': capture_sha,
        'digest_sha256': document.sha256,
        'original_event_count': document.original_event_count,
        'rendered_entity_count': len(document.items),
        'artifact_names': [path.name for path in artifacts],
        'model_used': False,
        'publication_authorized': False,
        'vault_modified': False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description='Previous-JST-day B4 run once: authenticated GitHub GET to private Preview only')
    parser.add_argument('--state', required=True, type=Path,
                        help='Preconfigured absolute owner-private Standard state root')
    args = parser.parse_args(argv)
    try:
        config, token = load_run_config(args.state)
        api = watcher.GitHubClient(api_base='https://api.github.com', token=token,
                                   timeout_seconds=config.request_timeout_seconds)
        receipt = run_once(config, api, cas_root=args.state / 'cas',
                           preview_root=args.state / 'preview')
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':')))
        return 0
    except Exception as exc:
        # In particular, never emit GitHub headers or credential text from
        # upstream HTTP exceptions. Failed/indeterminate is not publication.
        print(f'github-daily-preview-once blocked: {type(exc).__name__}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
