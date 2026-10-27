"""Single-service-user local CAS for verified GitHub Daily, source-only.

The caller owns an existing private directory with precreated stage directories.
The same nonroot service process collects and reads; this Standard profile does
not claim the separate-owner/reader ACL assurance of the legacy Strict profile.
No timer, WebDAV, Vault update or model work runs in this module.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat

from . import github_daily_source_route as route
from . import github_daily_temporal_evidence as temporal
from . import github_daily_route_capture_index as index
from . import github_daily_verified_source as verified
from . import github_daily_deterministic_digest as digest
from . import github_daily_deterministic_preview as preview


_SHA = re.compile(r'[0-9a-f]{64}\Z')


class StandardSourceError(ValueError):
    """Standard capture is missing, corrupted or not owned by this service."""


def _stages(root: Path) -> dict[str, Path]:
    try:
        paths = {stage: root / stage for stage in ('evidence', 'receipt', 'capture')}
        for path in (root, *paths.values()):
            preview._private_root(path)
        return paths
    except (OSError, digest.DeterministicDigestError) as exc:
        raise StandardSourceError('private Standard capture root is not ready') from exc


def _file(stage: str, sha: str) -> str:
    if type(sha) is not str or _SHA.fullmatch(sha) is None:
        raise StandardSourceError('invalid content-addressed capture identity')
    return f'{sha}.github-daily-{verified.SUFFIX[stage]}.json'


def _read(path: Path, name: str, expected_sha: str, maximum: int) -> bytes:
    try:
        fd = os.open(path / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            metadata = os.fstat(fd)
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                    or metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) != 0o600
                    or not 0 < metadata.st_size <= maximum):
                raise StandardSourceError('Standard capture file identity mismatch')
            with os.fdopen(os.dup(fd), 'rb') as stream:
                data = stream.read(maximum + 1)
            if len(data) != metadata.st_size or verified._sha(data) != expected_sha:
                raise StandardSourceError('Standard capture file differs from its SHA')
            return data
        finally:
            os.close(fd)
    except OSError as exc:
        raise StandardSourceError('Standard capture not available') from exc


def read_standard_digest(root: Path, capture_sha: str) -> digest.DeterministicDigest:
    """Re-admit all original CAS bytes under a caller-owned private store."""
    paths = _stages(root)
    index_data = _read(paths['capture'], _file('capture', capture_sha),
                       capture_sha, verified.SIZES['capture'])
    header = verified._object(index_data)
    evidence_sha = header.get('evidence_sha256')
    receipt_sha = header.get('route_receipt_sha256')
    evidence = _read(paths['evidence'], _file('evidence', evidence_sha),
                     evidence_sha, verified.SIZES['evidence'])
    receipt = _read(paths['receipt'], _file('receipt', receipt_sha),
                    receipt_sha, verified.SIZES['receipt'])
    source = verified._verify(index_data, evidence, receipt, capture_sha)
    if source.capture_sha256 != capture_sha:
        raise StandardSourceError('verified capture changed identity')
    return digest._build_admitted_source(source)


def collect_standard_source(root: Path, config, client, target_date) -> str:
    """Collect fresh, v3 source once; persist only complete verified originals.

    All GitHub calls happen inside the existing source route producer. Hashes
    alone are not provider authentication; the pinned Collector client is the
    source-process trust boundary in this single-user Standard profile.
    """
    paths = _stages(root)
    received = route.collect_fresh_verified_routes(
        config, target_date=target_date, client=client,
        period_policy=temporal.POLICY)
    capture = index.build_capture_index(received)
    verified._verify(capture.canonical_bytes, received.evidence.canonical_bytes,
                     received.receipt_bytes, capture.sha256)
    files = {
        'evidence': (received.evidence.sha256, received.evidence.canonical_bytes),
        'receipt': (received.receipt_sha256, received.receipt_bytes),
        'capture': (capture.sha256, capture.canonical_bytes),
    }
    try:
        # Preflight all three stages before publishing any; retries accept only
        # byte-identical final artifacts, never overwriting a canonical result.
        for stage, (sha, data) in files.items():
            preview._preflight(paths[stage], [(_file(stage, sha), data)])
        for stage, (sha, data) in files.items():
            preview._publish(paths[stage], _file(stage, sha), data)
        read_standard_digest(root, capture.sha256)
    except (OSError, digest.DeterministicDigestError) as exc:
        raise StandardSourceError('Standard capture publication was not completed') from exc
    return capture.sha256
