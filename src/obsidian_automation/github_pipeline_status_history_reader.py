"""Fixed, old-host-only read port for the existing Status Writer terminal files.

No new producer, ledger, command, WebUI route, remote service or Vault operation.
Shares the audited v0 reader's no-follow, identity, owner/mode and size checks.
This deliberately ignores Overview files (separately owned stage) and does NOT
report current Vault convergence, all-stage pipeline success or active incidents.
"""
from __future__ import annotations

import grp
import os
from pathlib import Path
import pwd
import re
from datetime import datetime

from .github_pipeline_evidence import MAX_RECEIPTS_PER_KIND, InvalidEvidence
from .github_pipeline_evidence_reader import (
    _DIR_ENTRIES_LIMIT,
    _RECEIPT_ROOT,
    _WRITER_GROUP,
    _WRITER_USER,
    SourceRejected,
    _open_directory,
    _read_regular_file,
    _same_identity,
)
from .github_pipeline_status_history import (
    parse_status_rejection,
    parse_status_success,
    summarize_status_history,
)

_KINDS = (
    ("results", ".github-status.transport-result.json",
     re.compile(r"([0-9a-f]{64})\.github-status\.transport-result\.json\Z")),
    ("rejections", ".github-status.rejection.json",
     re.compile(r"([0-9a-f]{64})\.github-status\.rejection\.json\Z")),
)


def _unknown(reason: str, now: datetime) -> dict[str, object]:
    result = summarize_status_history(
        {"results": [], "rejections": []}, now=now,
    )
    result["evidence_state"] = reason
    return result


def _read_root(
    directory: Path, *, uid: int, gid: int, now: datetime,
) -> dict[str, object]:
    """Private fixture entrypoint, NOT an API with caller-controlled filesystem paths."""
    if (
        type(uid) is not int or uid < 0
        or type(gid) is not int or gid < 0
        or not isinstance(now, datetime) or now.tzinfo is None
    ):
        raise ValueError("invalid_fixed_reader_configuration")
    try:
        fd = _open_directory(directory, uid=uid, gid=gid)
    except (OSError, SourceRejected):
        return _unknown("source_unavailable", now)

    try:
        before = os.fstat(fd)
        names: list[str] = []
        with os.scandir(fd) as entries:
            for item in entries:
                if len(names) >= _DIR_ENTRIES_LIMIT:
                    return _unknown("capacity_exceeded", now)
                names.append(item.name)

        source: dict[str, list[bytes]] = {"results": [], "rejections": []}
        observed_files: dict[str, os.stat_result] = {}
        for name in sorted(names):
            for kind, suffix, pattern in _KINDS:
                if not name.endswith(suffix):
                    continue
                match = pattern.fullmatch(name)
                if match is None:
                    return _unknown("source_invalid", now)
                if len(source["results"]) + len(source["rejections"]) >= MAX_RECEIPTS_PER_KIND:
                    return _unknown("capacity_exceeded", now)
                try:
                    raw, metadata = _read_regular_file(fd, name, uid=uid, gid=gid)
                    terminal = (
                        parse_status_success(raw, now=now) if kind == "results"
                        else parse_status_rejection(raw, now=now)
                    )
                    if terminal.proposal_sha256 != match.group(1):
                        return _unknown("source_invalid", now)
                    source[kind].append(raw)
                    observed_files[name] = metadata
                except (OSError, SourceRejected, InvalidEvidence):
                    return _unknown("source_invalid", now)
                break

        # The whole stream must remain stable before attesting its provenance.
        for name, original in observed_files.items():
            try:
                current = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except OSError:
                return _unknown("source_unavailable", now)
            if not _same_identity(original, current):
                return _unknown("source_unavailable", now)
        if not _same_identity(before, os.fstat(fd)):
            return _unknown("source_unavailable", now)
        return summarize_status_history(source, now=now, source_attested=True)
    except OSError:
        return _unknown("source_unavailable", now)
    finally:
        os.close(fd)


def read_local_status_history(*, now: datetime) -> dict[str, object]:
    """Only the existing same-host Writer's fixed UID/GID and result directory.

    The new single-LXC service (obsidian-control) does not own these paths.
    A new-LXC B3 adapter must supply its own producer-attested records instead
    of using this old-host reader or inferring attachment from importability.
    """
    try:
        uid = pwd.getpwnam(_WRITER_USER).pw_uid
        gid = grp.getgrnam(_WRITER_GROUP).gr_gid
    except KeyError:
        return _unknown("source_unavailable", now)
    return _read_root(_RECEIPT_ROOT, uid=uid, gid=gid, now=now)
