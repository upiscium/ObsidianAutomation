"""Local-only source CAS transaction/role/ACL adversarial fixtures (#336 B)."""
from __future__ import annotations

import os
import stat
import struct
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import hashlib

import pytest

from obsidian_automation import github_daily_route_cas as cas
from obsidian_automation import artifact_lifecycle as lifecycle
from test_github_daily_source_route import _collect


def _acl_blob(*, directory: bool, reader: int, default: bool = False) -> bytes:
    # linux posix ACL xattr v2, 1=user_obj, 2=user, 4=group_obj,
    # 16=mask, 32=other; -1 means not an individual UID.
    entries = [
        (1, 7 if directory else 6, -1),
        (2, 5 if directory else 4, reader),
        (4, 0, -1),
        (16, 7 if default else (5 if directory else 4), -1),
        (32, 0, -1),
    ]
    return struct.pack("<I", 2) + b"".join(
        struct.pack("<HHI", tag, perms, uid & 0xffffffff)
        for tag, perms, uid in entries
    )


def _provision_dir(path: Path, reader: int):
    path.mkdir(mode=0o750)
    os.chmod(path, 0o750)
    os.setxattr(path, "system.posix_acl_access", _acl_blob(directory=True, reader=reader))
    os.setxattr(path, "system.posix_acl_default", _acl_blob(directory=True, reader=reader, default=True))
    assert stat.S_IMODE(path.stat().st_mode) == 0o750


@pytest.fixture()
def safe_layout(tmp_path: Path, monkeypatch):
    reader = os.geteuid() + 12345
    if reader == os.geteuid():
        reader += 1
    root = tmp_path / "trusted-route-cas"
    try:
        _provision_dir(root, reader)
        for stage in cas.STAGES:
            _provision_dir(root / stage, reader)
    except OSError as exc:
        pytest.skip(f"Filesystem does not support POSIX ACL fixture: {exc}")
    monkeypatch.setattr(cas, "ROOT", root)
    # Test-only trusted anchor avoids depending on the world-writable /tmp
    # hierarchy; the unmodified production constant pins verification at /.
    monkeypatch.setattr(cas, "TRUST_ANCHOR", tmp_path)
    roles = cas.CollectorRoles(os.geteuid(), os.getegid(), reader)
    monkeypatch.setattr(cas, "_roles", lambda: roles)
    return root, roles


def _capture_path(root: Path, sha256: str) -> Path:
    return root / "capture" / cas._filename("capture", sha256)


def _snapshot(root: Path):
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


def test_atomic_publication_and_verified_reader_with_real_named_acl(tmp_path, safe_layout):
    root, roles = safe_layout
    collected = _collect(tmp_path)
    result = cas.publish_fresh_capture(collected)
    assert result.daily_publication_authorized is False
    handle = cas.read_verified_capture(result.index_sha256)
    assert handle.collector_storage_attested is True
    assert handle.daily_publication_authorized is False
    assert handle.routes and len(handle.routes) == collected.route_count
    assert handle.evidence_bytes == collected.evidence.canonical_bytes
    assert handle.receipt_bytes == collected.receipt_bytes
    assert handle.capture_index.evidence_sha256 == collected.evidence.sha256
    assert handle.capture_index.route_receipt_sha256 == collected.receipt_sha256
    assert _capture_path(root, result.index_sha256).is_file()
    before = _snapshot(root)
    assert cas.publish_fresh_capture(collected) == result
    assert _snapshot(root) == before
    for stage, sha in (("evidence", result.evidence_sha256),
                       ("receipt", result.receipt_sha256),
                       ("capture", result.index_sha256)):
        object_path = root / stage / cas._filename(stage, sha)
        assert stat.S_IMODE(object_path.stat().st_mode) == 0o640


@pytest.mark.parametrize("fail_at", [1, 2, 3])
def test_partial_publication_never_commits_manifest(tmp_path, safe_layout, monkeypatch, fail_at):
    root, _ = safe_layout
    collected = _collect(tmp_path)
    import obsidian_automation.artifact_lifecycle as storage
    genuine = storage._store_immutable
    calls = []

    def fault(path, data):
        calls.append(path.parent.name)
        if len(calls) == fail_at:
            raise lifecycle.ArtifactLifecycleError("synthetic crash")
        return genuine(path, data)

    monkeypatch.setattr(storage, "_store_immutable", fault)
    with pytest.raises(cas.DailyRouteCASError, match="atomic"):
        cas.publish_fresh_capture(collected)
    monkeypatch.setattr(storage, "_store_immutable", genuine)
    index = cas.index.build_capture_index(collected)
    assert not _capture_path(root, index.sha256).exists()
    with pytest.raises(cas.DailyRouteCASError):
        cas.read_verified_capture(index.sha256)
    # Independent exact retry can recover from either incomplete stage.
    completed = cas.publish_fresh_capture(collected)
    assert cas.read_verified_capture(completed.index_sha256).routes


def test_manifest_is_written_last_and_never_authorizes_daily(tmp_path, safe_layout, monkeypatch):
    _, _roles = safe_layout
    collected = _collect(tmp_path)
    genuine = lifecycle._store_immutable
    order = []

    def trace(path, data):
        order.append(path.parent.name)
        return genuine(path, data)

    monkeypatch.setattr(lifecycle, "_store_immutable", trace)
    cas.publish_fresh_capture(collected)
    assert order == ["evidence", "receipt", "capture"]


def test_current_service_role_required_for_publishing_and_reading(tmp_path, safe_layout, monkeypatch):
    root, roles = safe_layout
    collected = _collect(tmp_path)
    monkeypatch.setattr(cas, "_roles", lambda: replace(roles, owner_uid=roles.owner_uid + 1))
    with pytest.raises(cas.DailyRouteCASError, match="configured Collector"):
        cas.publish_fresh_capture(collected)
    assert not _snapshot(root)
    monkeypatch.setattr(cas, "_roles", lambda: roles)
    result = cas.publish_fresh_capture(collected)
    monkeypatch.setattr(cas, "_roles", lambda: replace(roles, reader_uid=roles.reader_uid + 12))
    with pytest.raises(cas.DailyRouteCASError):
        cas.read_verified_capture(result.index_sha256)


def test_unsafe_legacy_store_implementation_fails_before_writing(tmp_path, safe_layout, monkeypatch):
    root, _ = safe_layout
    collected = _collect(tmp_path)
    monkeypatch.setattr(lifecycle, "_rename_noreplace", None)
    with pytest.raises(cas.DailyRouteCASError, match="primitive unavailable"):
        cas.publish_fresh_capture(collected)
    assert not _snapshot(root)


def test_altered_source_receipt_rejected_before_any_storage(tmp_path, safe_layout):
    root, _ = safe_layout
    collected = _collect(tmp_path)
    bad = replace(collected, receipt_bytes=collected.receipt_bytes + b" ")
    with pytest.raises(cas.DailyRouteCASError, match="prepublication"):
        cas.publish_fresh_capture(bad)
    assert not _snapshot(root)


@pytest.mark.parametrize("bad", ["xyz", "../" + "a" * 64, "a" * 63, "A" * 64, True, None])
def test_reader_never_opens_unapproved_names(tmp_path, safe_layout, bad):
    with pytest.raises(cas.DailyRouteCASError, match="identity"):
        cas.read_verified_capture(bad)


def test_missing_receipt_or_manifest_fails_not_empty(tmp_path, safe_layout):
    root, _ = safe_layout
    collected = _collect(tmp_path)
    result = cas.publish_fresh_capture(collected)
    (root / "receipt" / cas._filename("receipt", result.receipt_sha256)).unlink()
    with pytest.raises(cas.DailyRouteCASError, match="unavailable"):
        cas.read_verified_capture(result.index_sha256)


def test_symlink_hardlink_fifo_or_changed_blob_never_admitted(tmp_path, safe_layout):
    root, _ = safe_layout
    collected = _collect(tmp_path)
    result = cas.publish_fresh_capture(collected)
    filename = _capture_path(root, result.index_sha256)
    backup = root / "capture" / "inactive-original.tmp"
    os.rename(filename, backup)
    os.symlink(backup.name, filename)
    with pytest.raises(cas.DailyRouteCASError):
        cas.read_verified_capture(result.index_sha256)
    filename.unlink()
    os.link(backup, filename)
    with pytest.raises(cas.DailyRouteCASError, match="inode"):
        cas.read_verified_capture(result.index_sha256)
    filename.unlink()
    os.mkfifo(filename)
    with pytest.raises(cas.DailyRouteCASError, match="inode"):
        cas.read_verified_capture(result.index_sha256)
    filename.unlink()
    os.rename(backup, filename)
    with filename.open("r+b") as fp:
        fp.write(b"X")
    with pytest.raises(cas.DailyRouteCASError, match="content hash"):
        cas.read_verified_capture(result.index_sha256)


def test_bad_acl_and_directory_mode_are_fail_closed(tmp_path, safe_layout):
    root, roles = safe_layout
    collected = _collect(tmp_path)
    result = cas.publish_fresh_capture(collected)
    blob = _capture_path(root, result.index_sha256)
    original = os.getxattr(blob, "system.posix_acl_access")
    os.removexattr(blob, "system.posix_acl_access")
    with pytest.raises(cas.DailyRouteCASError, match="ACL"):
        cas.read_verified_capture(result.index_sha256)
    os.setxattr(blob, "system.posix_acl_access", original)
    os.chmod(root / "receipt", 0o770)
    with pytest.raises(cas.DailyRouteCASError, match="directory"):
        cas.read_verified_capture(result.index_sha256)


def test_corrupt_rehashed_manifest_is_not_authorized(tmp_path, safe_layout):
    root, _ = safe_layout
    collected = _collect(tmp_path)
    result = cas.publish_fresh_capture(collected)
    manifest = _capture_path(root, result.index_sha256)
    data = manifest.read_bytes().replace(b'"publication_authorized":false', b'"publication_authorized":true ')
    assert data != manifest.read_bytes()
    manifest.write_bytes(data)
    with pytest.raises(cas.DailyRouteCASError):
        cas.read_verified_capture(result.index_sha256)


def test_missing_or_untrusted_storage_prevents_all_effects(tmp_path, safe_layout, monkeypatch):
    root, _ = safe_layout
    monkeypatch.setattr(cas, "ROOT", root / "does-not-exist")
    with pytest.raises(cas.DailyRouteCASError):
        cas.publish_fresh_capture(_collect(tmp_path))
    monkeypatch.setattr(cas, "ROOT", Path("../bad"))
    with pytest.raises(cas.DailyRouteCASError):
        cas.publish_fresh_capture(_collect(tmp_path))


def test_symlinked_or_group_writable_ancestor_is_rejected(tmp_path, safe_layout, monkeypatch):
    root, roles = safe_layout
    original_root = root
    sibling = tmp_path / "alias"
    sibling.symlink_to(original_root, target_is_directory=True)
    monkeypatch.setattr(cas, "ROOT", sibling)
    with pytest.raises(cas.DailyRouteCASError, match="inspection"):
        cas.read_verified_capture("a" * 64)
    # A non-privileged parent with write authority for untrusted peers is not
    # a valid anchor to a privileged source namespace.
    middle = tmp_path / "untrusted-middle"
    middle.mkdir(mode=0o777)
    middle.chmod(0o777)
    _provision_dir(middle / "trusted-route-cas", roles.reader_uid)
    for stage in cas.STAGES:
        _provision_dir(middle / "trusted-route-cas" / stage, roles.reader_uid)
    monkeypatch.setattr(cas, "ROOT", middle / "trusted-route-cas")
    with pytest.raises(cas.DailyRouteCASError, match="ancestor"):
        cas.read_verified_capture("a" * 64)


def test_missing_default_acl_refuses_publication(tmp_path, safe_layout):
    root, _ = safe_layout
    os.removexattr(root / "evidence", "system.posix_acl_default")
    with pytest.raises(cas.DailyRouteCASError, match="ACL"):
        cas.publish_fresh_capture(_collect(tmp_path))
    assert not _snapshot(root)


def test_account_resolution_disallows_root_shared_or_missing_roles(monkeypatch):
    from types import SimpleNamespace
    original = cas.pwd.getpwnam
    monkeypatch.setattr(cas.pwd, "getpwnam", lambda x: (
        SimpleNamespace(pw_uid=0, pw_gid=0) if x == cas.COLLECTOR_ACCOUNT
        else SimpleNamespace(pw_uid=2001, pw_gid=2001)
    ))
    with pytest.raises(cas.DailyRouteCASError, match="isolated"):
        # Invoke original role resolver, not the synthetic ACL role fixture.
        cas._roles()
    monkeypatch.setattr(cas.pwd, "getpwnam", lambda x: SimpleNamespace(pw_uid=1001, pw_gid=1001))
    with pytest.raises(cas.DailyRouteCASError, match="isolated"):
        cas._roles()
    monkeypatch.setattr(cas.pwd, "getpwnam", lambda x: (_ for _ in ()).throw(KeyError(x)))
    with pytest.raises(cas.DailyRouteCASError, match="unavailable"):
        cas._roles()
    monkeypatch.setattr(cas.pwd, "getpwnam", original)


def _capture_utc(collected):
    import json
    return datetime.fromisoformat(
        json.loads(collected.receipt_bytes)["captured_at_utc"].replace("Z", "+00:00")
    )


def test_stale_first_publication_is_rejected_before_any_cas_write(
    tmp_path, safe_layout, monkeypatch
):
    root, _ = safe_layout
    collection = _collect(tmp_path)
    monkeypatch.setattr(cas, "_utc_now", lambda: _capture_utc(collection) + timedelta(days=3))
    with pytest.raises(cas.DailyRouteCASError, match="too old"):
        cas.publish_fresh_capture(collection)
    assert not _snapshot(root)


def test_future_dated_first_publication_is_rejected(
    tmp_path, safe_layout, monkeypatch
):
    root, _ = safe_layout
    collection = _collect(tmp_path)
    monkeypatch.setattr(cas, "_utc_now", lambda: _capture_utc(collection) - timedelta(minutes=4))
    with pytest.raises(cas.DailyRouteCASError, match="future"):
        cas.publish_fresh_capture(collection)
    assert not _snapshot(root)


def test_stale_exact_byte_replay_of_already_committed_capture_is_idempotent(
    tmp_path, safe_layout, monkeypatch
):
    root, _ = safe_layout
    collection = _collect(tmp_path)
    published = cas.publish_fresh_capture(collection)
    before = _snapshot(root)
    monkeypatch.setattr(cas, "_utc_now", lambda: _capture_utc(collection) + timedelta(days=10))
    assert cas.publish_fresh_capture(collection) == published
    assert _snapshot(root) == before
    assert cas.read_verified_capture(published.index_sha256).routes


def test_stale_partial_pair_never_receives_first_manifest(
    tmp_path, safe_layout, monkeypatch
):
    root, _ = safe_layout
    collection = _collect(tmp_path)
    published = cas.publish_fresh_capture(collection)
    _capture_path(root, published.index_sha256).unlink()
    monkeypatch.setattr(cas, "_utc_now", lambda: _capture_utc(collection) + timedelta(days=4))
    with pytest.raises(cas.DailyRouteCASError, match="too old"):
        cas.publish_fresh_capture(collection)
    assert not _capture_path(root, published.index_sha256).exists()


def test_crash_after_completed_manifest_can_recover_as_exact_retry(
    tmp_path, safe_layout, monkeypatch
):
    root, _ = safe_layout
    collection = _collect(tmp_path)
    import obsidian_automation.artifact_lifecycle as storage
    genuine = storage._store_immutable

    def lose_ack(path, data):
        result = genuine(path, data)
        if path.parent.name == "capture":
            raise lifecycle.ArtifactLifecycleError("synthetic host lost acknowledgement")
        return result

    monkeypatch.setattr(storage, "_store_immutable", lose_ack)
    with pytest.raises(cas.DailyRouteCASError, match="atomic"):
        cas.publish_fresh_capture(collection)
    monkeypatch.setattr(storage, "_store_immutable", genuine)
    expected = cas.index.build_capture_index(collection)
    assert cas.read_verified_capture(expected.sha256).routes
    monkeypatch.setattr(cas, "_utc_now", lambda: _capture_utc(collection) + timedelta(days=3))
    recovered = cas.publish_fresh_capture(collection)
    assert recovered.index_sha256 == expected.sha256


def test_missing_manifest_after_publication_is_unavailable(tmp_path, safe_layout):
    root, _ = safe_layout
    collection = _collect(tmp_path)
    published = cas.publish_fresh_capture(collection)
    _capture_path(root, published.index_sha256).unlink()
    with pytest.raises(cas.DailyRouteCASError, match="unavailable"):
        cas.read_verified_capture(published.index_sha256)


def test_correctly_sha_named_but_malformed_manifest_cannot_be_read(
    tmp_path, safe_layout
):
    root, _ = safe_layout
    raw = b'{"not":"a canonical capture index"}\n'
    digest = hashlib.sha256(raw).hexdigest()
    lifecycle._store_immutable(root / "capture" / cas._filename("capture", digest), raw)
    with pytest.raises(cas.DailyRouteCASError, match="schema"):
        cas.read_verified_capture(digest)


@pytest.mark.parametrize("captured_at,published_at,diagnostic", [
    # A forged or replayed scheduled collection from eight days ago cannot
    # acquire an initially committed manifest in today's Collector namespace.
    (datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc),
     datetime(2026, 10, 13, 6, 0, tzinfo=timezone.utc), "too old"),
    # A short interval can nevertheless cross JST midnight: Oct 5 is no
    # longer today/yesterday once local day becomes Oct 7.
    (datetime(2026, 10, 6, 14, 55, tzinfo=timezone.utc),
     datetime(2026, 10, 6, 15, 5, tzinfo=timezone.utc), "JST day"),
])
def test_scheduled_capture_freshness_is_checked_at_first_publish(
    tmp_path, safe_layout, monkeypatch, captured_at, published_at, diagnostic
):
    root, _ = safe_layout
    from obsidian_automation import github_daily_source_route as route
    from test_github_daily_source_route import DATE, _FakeClient, _config
    monkeypatch.setattr(route, "_utc_now", lambda: captured_at)
    collection = route.collect_fresh_verified_routes(
        _config(tmp_path), target_date=DATE, client=_FakeClient(),
    )
    assert b'"collection_mode":"scheduled_recent_api_reverified"' in collection.receipt_bytes
    monkeypatch.setattr(cas, "_utc_now", lambda: published_at)
    with pytest.raises(cas.DailyRouteCASError, match=diagnostic):
        cas.publish_fresh_capture(collection)
    assert not _snapshot(root)
