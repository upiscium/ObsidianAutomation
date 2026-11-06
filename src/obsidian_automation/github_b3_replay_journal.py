"""Unattached root-only, append-and-fsync B3 sequence journal.

A future separately approved privileged installer must provision the fixed
root-only directory and a valid, nonempty genesis journal. No provisioning,
service wiring, HMAC verification, signing, credential, remote probe or
canonical Writer authorization is performed here. This is a *local* sequence
checkpoint only; a privileged root actor can still roll back the entire file.
"""
from __future__ import annotations

from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat

VERSION = "github-b3-replay-journal/v1"
TRUST_ANCHOR = Path("/")
JOURNAL_ROOT = Path("/var/lib/obsidian-b3-replay-ledger")
JOURNAL_NAME = "retirement-sequences.jsonl"
_ID = re.compile(r"[0-9a-f]{32}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = (
    os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW |
    os.O_CLOEXEC | os.O_NONBLOCK
)
_MAX_LINE = 2048
_MAX_BYTES = 1 << 20
_MAX_RECORDS = 512
_MAX_SEQUENCE = (1 << 63) - 1
_ZERO = "0" * 64
_FIELDS = frozenset({
    "schema_version", "kind", "epoch", "binding_ref", "revision",
    "source_generation", "provider_key_id", "host_key_id",
    "provider_sequence", "host_sequence", "bundle_sha256",
    "previous_sha256", "sha256",
})
_CONTEXT = (
    "epoch", "binding_ref", "revision", "source_generation",
    "provider_key_id", "host_key_id",
)


@dataclass(frozen=True, slots=True)
class B3ReplayDiagnostic:
    """Locally claimed context; not authenticated signed provider evidence."""

    epoch: str
    binding_ref: str
    revision: str
    source_generation: str
    provider_key_id: str
    host_key_id: str
    provider_sequence: int
    host_sequence: int
    bundle_sha256: str


class B3ReplayJournalUnavailable(RuntimeError):
    """Fixed, redacted diagnostic for unsafe paths/ledger state/errors."""

    def __init__(self) -> None:
        super().__init__("b3_replay_journal_unavailable")


def _owner_uid() -> int:
    return 0


def _owner_gid() -> int:
    return 0


def _id(value: object, pattern: re.Pattern[str] = _ID) -> bool:
    return type(value) is str and pattern.fullmatch(value) is not None


def _sequence(value: object) -> bool:
    return type(value) is int and 0 <= value <= _MAX_SEQUENCE


def _context(obj: object) -> tuple[object, ...]:
    return tuple(getattr(obj, name) if isinstance(obj, B3ReplayDiagnostic)
                 else obj[name] for name in _CONTEXT)


def _identity(st: os.stat_result) -> tuple[int, ...]:
    return (
        st.st_dev, st.st_ino, st.st_mode, st.st_nlink,
        st.st_uid, st.st_gid, st.st_size, st.st_mtime_ns, st.st_ctime_ns,
    )


def _inode(st: os.stat_result) -> tuple[int, ...]:
    return (st.st_dev, st.st_ino, st.st_mode, st.st_nlink, st.st_uid, st.st_gid)


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, ensure_ascii=False,
                   separators=(",", ":"), allow_nan=False) + "\n"
    ).encode("utf-8")


def _sealed_fields(fields: dict[str, object]) -> bytes:
    return _canonical({
        **fields,
        "sha256": hashlib.sha256(_canonical(fields)).hexdigest(),
    })


def _decode_line(line: bytes) -> dict[str, object]:
    if not 0 < len(line) <= _MAX_LINE or not line.endswith(b"\n"):
        raise B3ReplayJournalUnavailable()

    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for k, v in pairs:
            if k in result:
                raise B3ReplayJournalUnavailable()
            result[k] = v
        return result

    def nonfinite(_: str) -> object:
        raise B3ReplayJournalUnavailable()

    obj = json.loads(line, object_pairs_hook=unique,
                     parse_constant=nonfinite)
    if type(obj) is not dict or set(obj) != _FIELDS:
        raise B3ReplayJournalUnavailable()
    if line != _canonical(obj):
        raise B3ReplayJournalUnavailable()
    digest = obj["sha256"]
    if not _id(digest, _SHA):
        raise B3ReplayJournalUnavailable()
    unsigned = {k: v for k, v in obj.items() if k != "sha256"}
    if hashlib.sha256(_canonical(unsigned)).hexdigest() != digest:
        raise B3ReplayJournalUnavailable()
    if (
        obj["schema_version"] != VERSION
        or obj["kind"] not in ("genesis", "advance")
        or any(not _id(obj[n]) for n in _CONTEXT)
        or obj["provider_key_id"] == obj["host_key_id"]
        or not _sequence(obj["provider_sequence"])
        or not _sequence(obj["host_sequence"])
        or not _id(obj["bundle_sha256"], _SHA)
        or not _id(obj["previous_sha256"], _SHA)
    ):
        raise B3ReplayJournalUnavailable()
    return obj


def _verify_journal(raw: bytes) -> tuple[dict[str, object], int, frozenset[str]]:
    """Read a complete strict hash chain, never repair a torn/corrupt tail."""
    if type(raw) is not bytes or not 0 < len(raw) <= _MAX_BYTES:
        raise B3ReplayJournalUnavailable()
    lines = raw.splitlines(keepends=True)
    if not 1 <= len(lines) <= _MAX_RECORDS:
        raise B3ReplayJournalUnavailable()
    items = [_decode_line(x) for x in lines]
    first = items[0]
    if (
        first["kind"] != "genesis"
        or first["previous_sha256"] != _ZERO
        or first["bundle_sha256"] != _ZERO
    ):
        raise B3ReplayJournalUnavailable()
    known: set[str] = set()
    last = first
    for record in items[1:]:
        if (
            record["kind"] != "advance"
            or _context(record) != _context(first)
            or record["previous_sha256"] != last["sha256"]
            or record["provider_sequence"] <= last["provider_sequence"]
            or record["host_sequence"] <= last["host_sequence"]
            or record["bundle_sha256"] == _ZERO
            or record["bundle_sha256"] in known
        ):
            raise B3ReplayJournalUnavailable()
        known.add(record["bundle_sha256"])
        last = record
    return last, len(items), frozenset(known)


def _owner_ids() -> tuple[int, int]:
    uid, gid = _owner_uid(), _owner_gid()
    if (type(uid) is not int or type(gid) is not int
            or uid < 0 or gid < 0
            or os.geteuid() != uid or os.getegid() != gid):
        raise B3ReplayJournalUnavailable()
    return uid, gid


def _open_root(owner: int, group: int) -> int:
    if (
        type(TRUST_ANCHOR) is not type(Path("/"))
        or type(JOURNAL_ROOT) is not type(Path("/"))
        or not TRUST_ANCHOR.is_absolute()
        or not JOURNAL_ROOT.is_absolute()
        or JOURNAL_ROOT == TRUST_ANCHOR
        or not JOURNAL_ROOT.is_relative_to(TRUST_ANCHOR)
        or ".." in TRUST_ANCHOR.parts or ".." in JOURNAL_ROOT.parts
    ):
        raise B3ReplayJournalUnavailable()
    fd = os.open(TRUST_ANCHOR, _DIR_FLAGS)
    try:
        st = os.fstat(fd)
        if (not stat.S_ISDIR(st.st_mode) or st.st_uid != owner
                or stat.S_IMODE(st.st_mode) & 0o022):
            raise B3ReplayJournalUnavailable()
        for part in JOURNAL_ROOT.relative_to(TRUST_ANCHOR).parts:
            next_fd = os.open(part, _DIR_FLAGS, dir_fd=fd)
            try:
                st = os.fstat(next_fd)
                if (not stat.S_ISDIR(st.st_mode) or st.st_uid != owner
                        or stat.S_IMODE(st.st_mode) & 0o022):
                    raise B3ReplayJournalUnavailable()
            except BaseException:
                os.close(next_fd)
                raise
            os.close(fd)
            fd = next_fd
        st = os.fstat(fd)
        if (st.st_uid != owner or st.st_gid != group
                or stat.S_IMODE(st.st_mode) != 0o700):
            raise B3ReplayJournalUnavailable()
        result, fd = fd, None
        return result
    finally:
        if fd is not None:
            os.close(fd)


def _check_root(root_fd: int, owner: int, group: int) -> None:
    fd = _open_root(owner, group)
    try:
        if _inode(os.fstat(fd)) != _inode(os.fstat(root_fd)):
            raise B3ReplayJournalUnavailable()
    finally:
        os.close(fd)


def _check_file(fd: int, dir_fd: int, owner: int, group: int) -> os.stat_result:
    first = os.fstat(fd)
    path = os.stat(JOURNAL_NAME, dir_fd=dir_fd, follow_symlinks=False)
    if (
        not stat.S_ISREG(first.st_mode) or first.st_nlink != 1
        or first.st_uid != owner or first.st_gid != group
        or stat.S_IMODE(first.st_mode) != 0o600
        or not 0 < first.st_size <= _MAX_BYTES
        or _identity(first) != _identity(path)
    ):
        raise B3ReplayJournalUnavailable()
    return first


def _diagnostic(obj: object) -> bool:
    return (
        type(obj) is B3ReplayDiagnostic
        and all(_id(getattr(obj, name)) for name in _CONTEXT)
        and obj.provider_key_id != obj.host_key_id
        and _sequence(obj.provider_sequence)
        and _sequence(obj.host_sequence)
        and _id(obj.bundle_sha256, _SHA)
        and obj.bundle_sha256 != _ZERO
    )


def _result(state: str, reason: str) -> dict[str, object]:
    return {
        "schema_version": VERSION, "state": state,
        "reason": reason, "cutover_authorized": False,
    }


def append_b3_replay_diagnostic(observation: B3ReplayDiagnostic) -> dict[str, object]:
    """Append one *local diagnostic* checkpoint; never a B3 authorization.

    This effect can run ONLY under preprovisioned root-owned paths and an
    approved root invocation. Tests override the root to a disposable
    directory and real user ownership. Absolutely no runtime caller is
    registered by this source-only slice.
    """
    if not _diagnostic(observation):
        return _result("unknown", "invalid_replay_observation")
    root_fd = None
    file_fd = None
    locked = False
    try:
        uid, gid = _owner_ids()
        root_fd = _open_root(uid, gid)
        file_fd = os.open(JOURNAL_NAME, _FILE_FLAGS, dir_fd=root_fd)
        first = _check_file(file_fd, root_fd, uid, gid)
        # Nonblocking interprocess serialization: BUSY must never mean PASS.
        fcntl.flock(file_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked = True
        locked_stat = _check_file(file_fd, root_fd, uid, gid)
        if _inode(locked_stat) != _inode(first):
            raise B3ReplayJournalUnavailable()
        _check_root(root_fd, uid, gid)

        os.lseek(file_fd, 0, os.SEEK_SET)
        raw = os.read(file_fd, _MAX_BYTES + 1)
        if len(raw) != locked_stat.st_size:
            raise B3ReplayJournalUnavailable()
        last, count, seen = _verify_journal(raw)
        if (
            _context(observation) != _context(last)
            or observation.provider_sequence <= last["provider_sequence"]
            or observation.host_sequence <= last["host_sequence"]
            or observation.bundle_sha256 in seen
        ):
            return _result("blocked", "stale_or_reused_replay_bundle")
        if count >= _MAX_RECORDS:
            return _result("unknown", "journal_capacity_exhausted")

        values: dict[str, object] = {
            "schema_version": VERSION, "kind": "advance",
            **{name: getattr(observation, name) for name in _CONTEXT},
            "provider_sequence": observation.provider_sequence,
            "host_sequence": observation.host_sequence,
            "bundle_sha256": observation.bundle_sha256,
            "previous_sha256": last["sha256"],
        }
        record = _sealed_fields(values)
        if (
            not 0 < len(record) <= _MAX_LINE
            or locked_stat.st_size + len(record) > _MAX_BYTES
            or _identity(_check_file(file_fd, root_fd, uid, gid))
               != _identity(locked_stat)
            or os.lseek(file_fd, 0, os.SEEK_END) != locked_stat.st_size
        ):
            raise B3ReplayJournalUnavailable()
        _check_root(root_fd, uid, gid)
        # Never truncate, repair or create files. A short write leaves an
        # invalid tail, deliberately blocking all future processing.
        if os.write(file_fd, record) != len(record):
            raise B3ReplayJournalUnavailable()
        os.fsync(file_fd)
        after = _check_file(file_fd, root_fd, uid, gid)
        if (_inode(after) != _inode(locked_stat)
                or after.st_size != locked_stat.st_size + len(record)):
            raise B3ReplayJournalUnavailable()
        _check_root(root_fd, uid, gid)
        return _result("recorded", "local_sequence_checkpoint_only")
    except Exception:
        return _result("unknown", "replay_journal_unavailable")
    finally:
        if file_fd is not None:
            try:
                if locked:
                    fcntl.flock(file_fd, fcntl.LOCK_UN)
            finally:
                os.close(file_fd)
        if root_fd is not None:
            os.close(root_fd)
