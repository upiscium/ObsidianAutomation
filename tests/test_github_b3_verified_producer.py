"""Role-isolated B3 producer-file admission: disposable filesystem only."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path

import pytest

from obsidian_automation.github_b3_verified_producer import (
    B3SourceUnavailable, read_verified_b3_proposal_pair,
)
from obsidian_automation.webui_b3_adapter import ProjectSyncRequest
from obsidian_automation import github_b3_verified_producer as source

REQUEST = "a" * 32
BINDING = "b" * 32
REVISION = "c" * 32
GENERATION = "d" * 32
RECORD = "e" * 32


def canonical(d):
    return (
        json.dumps(d, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":"), allow_nan=False) + "\n"
    ).encode("utf-8")


def request():
    return ProjectSyncRequest(REQUEST, REVISION, BINDING)


@pytest.fixture
def owned_fixture(tmp_path, monkeypatch):
    # Emulate the privileged provisioner only inside a disposable private
    # tree. Production must use distinct named Unix producer/reader roles,
    # ROOT / TRUST_ANCHOR unchanged and non-writable by the app/WebUI.
    root = tmp_path / "verified-producer"
    root.mkdir(mode=0o750)
    root.chmod(0o750)
    monkeypatch.setattr(source, "ROOT", root)
    monkeypatch.setattr(source, "TRUST_ANCHOR", tmp_path)
    monkeypatch.setattr(source, "_producer_uid", lambda: os.geteuid())
    monkeypatch.setattr(source, "_pipeline_gid", lambda: os.getegid())
    monkeypatch.setattr(source, "_verify_reader", lambda _: None)
    status = b'{"event":"synthetic-canonical-status"}\n'
    overview = b'{"event":"synthetic-canonical-overview"}\n'
    status_sha = hashlib.sha256(status).hexdigest()
    overview_sha = hashlib.sha256(overview).hexdigest()
    manifest = {
        "record_version": 1, "request_id": REQUEST,
        "binding_ref": BINDING, "expected_revision": REVISION,
        "source_generation": GENERATION, "source_record_id": RECORD,
        "prepared_at": 1800000000.0,
        "status_sha256": status_sha, "overview_sha256": overview_sha,
    }
    paths = [
        root / f"{REQUEST}.b3-source.json",
        root / f"{status_sha}.b3-status.json",
        root / f"{overview_sha}.b3-overview.json",
    ]
    for path, data in zip(paths, (canonical(manifest), status, overview)):
        path.write_bytes(data)
        path.chmod(0o640)
    return root, manifest, paths, status, overview


def assert_unavailable():
    with pytest.raises(B3SourceUnavailable, match="producer_source_unavailable"):
        read_verified_b3_proposal_pair(request())


def test_role_owned_exact_manifest_and_digest_pair(owned_fixture):
    root, m, paths, status, overview = owned_fixture
    pair = read_verified_b3_proposal_pair(request())
    assert pair.request_id == REQUEST
    assert pair.binding_ref == BINDING
    assert pair.expected_revision == REVISION
    assert pair.source_generation == GENERATION
    assert pair.source_record_id == RECORD
    assert pair.status_bytes == status
    assert pair.overview_bytes == overview
    assert pair.status_sha256 == hashlib.sha256(status).hexdigest()
    assert pair.prepared_at == m["prepared_at"]


@pytest.mark.parametrize("key,value", (
    ("record_version", True),
    ("record_version", 2),
    ("request_id", "f" * 32),
    ("binding_ref", "f" * 32),
    ("expected_revision", "f" * 32),
    ("source_record_id", "g" * 32),
    ("source_generation", "g" * 32),
    ("prepared_at", "yesterday"),
    ("prepared_at", -1),
    ("prepared_at", 1e308),
    ("status_sha256", "0" * 64),
    ("overview_sha256", "0" * 64),
))
def test_manifest_field_tampering_fails_closed(owned_fixture, key, value):
    root, m, paths, status, overview = owned_fixture
    m[key] = value
    paths[0].write_bytes(canonical(m))
    assert_unavailable()


def test_duplicate_keys_fail_closed(owned_fixture):
    _, m, paths, _, _ = owned_fixture
    raw = canonical(m)
    paths[0].write_bytes(raw[:-2] + b',"request_id":"'+REQUEST.encode()+b'"}\n')
    assert_unavailable()


def test_noncanonical_or_non_json_manifest_fails(owned_fixture):
    _, m, paths, _, _ = owned_fixture
    for payload in (
        json.dumps(m, indent=2).encode(),
        b"{not json}", b"[]\n", b"\n", b"{}",
    ):
        paths[0].write_bytes(payload)
        assert_unavailable()


def test_digest_catches_modified_payload(owned_fixture):
    _, _, paths, _, _ = owned_fixture
    paths[1].write_bytes(b"forged-producer-bytes")
    assert_unavailable()


@pytest.mark.parametrize("which", (0, 1, 2))
def test_missing_file_fails_closed(owned_fixture, which):
    _, _, paths, _, _ = owned_fixture
    paths[which].unlink()
    assert_unavailable()


@pytest.mark.parametrize("which", (0, 1, 2))
def test_symlinked_object_fails_closed(owned_fixture, which):
    root, _, paths, _, _ = owned_fixture
    target = root / "other-source"
    target.write_bytes(paths[which].read_bytes())
    target.chmod(0o640)
    paths[which].unlink()
    paths[which].symlink_to(target)
    assert_unavailable()


@pytest.mark.parametrize("which", (0, 1, 2))
def test_hardlinked_object_fails_closed(owned_fixture, which):
    root, _, paths, _, _ = owned_fixture
    os.link(paths[which], root / "extra-link")
    assert_unavailable()


@pytest.mark.parametrize("which", (0, 1, 2))
@pytest.mark.parametrize("mode", (0o600, 0o644, 0o660, 0o666))
def test_bad_file_permissions_fail_closed(owned_fixture, which, mode):
    _, _, paths, _, _ = owned_fixture
    paths[which].chmod(mode)
    assert_unavailable()


@pytest.mark.parametrize("mode", (0o777, 0o770, 0o755, 0o750 | 0o002))
def test_group_or_world_writable_parent_refused(owned_fixture, mode):
    root, _, _, _, _ = owned_fixture
    root.chmod(mode)
    assert_unavailable()


def test_producer_directory_requires_group_reader_traversal(owned_fixture):
    root, _, _, _, _ = owned_fixture
    # The real reader is a different UID, so 0700 cannot be supported.
    root.chmod(0o700)
    assert_unavailable()


def test_unavailable_source_directory_fails_closed(owned_fixture):
    root, _, paths, _, _ = owned_fixture
    root.rename(root.parent / "moved")
    assert_unavailable()


def test_symlinked_root_directory_fails_closed(owned_fixture, monkeypatch):
    root, _, paths, _, _ = owned_fixture
    symlink = root.parent / "linked"
    symlink.symlink_to(root, target_is_directory=True)
    monkeypatch.setattr(source, "ROOT", symlink)
    assert_unavailable()


def test_unsafe_anchor_is_not_used(owned_fixture, monkeypatch):
    root, _, paths, _, _ = owned_fixture
    monkeypatch.setattr(source, "TRUST_ANCHOR", Path("/"))
    # Production /tmp ancestors are writable; it must not trust a fixture
    # under /tmp merely because the final directory is private.
    assert_unavailable()


@pytest.mark.parametrize("bad", (object(), None, "a"*32, 123, True, {}))
def test_public_request_must_be_exact_type(owned_fixture, bad):
    with pytest.raises(B3SourceUnavailable):
        read_verified_b3_proposal_pair(bad)


def test_other_request_id_cannot_select_source(owned_fixture):
    with pytest.raises(B3SourceUnavailable):
        read_verified_b3_proposal_pair(replace(request(), requestId="f"*32))


def test_missing_producer_identity_never_falls_back_to_current_user(
    owned_fixture, monkeypatch,
):
    def missing():
        raise KeyError("no-production-producer")
    monkeypatch.setattr(source, "_producer_uid", missing)
    assert_unavailable()


def test_failed_reader_identity_blocks_source(owned_fixture, monkeypatch):
    monkeypatch.setattr(
        source, "_verify_reader",
        lambda _: (_ for _ in ()).throw(B3SourceUnavailable()),
    )
    assert_unavailable()


def test_mutated_manifest_between_two_reads_rejected(owned_fixture, monkeypatch):
    _, m, paths, _, _ = owned_fixture
    original = source._read_file
    count = 0
    def raced(*args, **kwargs):
        nonlocal count
        count += 1
        result = original(*args, **kwargs)
        if count == 3:  # after both bytes were obtained
            m["source_record_id"] = "f" * 32
            paths[0].write_bytes(canonical(m))
        return result
    monkeypatch.setattr(source, "_read_file", raced)
    assert_unavailable()


def test_scope_is_pure_read_only_no_cutover_or_credentials(owned_fixture):
    root, _, paths, _, _ = owned_fixture
    before = {p.name: (p.stat().st_mtime_ns, p.read_bytes()) for p in paths}
    read_verified_b3_proposal_pair(request())
    after = {p.name: (p.stat().st_mtime_ns, p.read_bytes()) for p in paths}
    assert before == after
    assert not hasattr(source, "enable_writer")
    assert not hasattr(source, "cutover_authorized")
    assert not hasattr(source, "revoke_credentials")


def test_verified_role_source_feeds_exact_existing_b3_request_binder(
    owned_fixture,
):
    """No WebDAV/Writer: role-backed bytes become request-correlated inputs."""
    from datetime import datetime, timezone

    from obsidian_automation.github_b3_job import B3JobConfig
    from obsidian_automation.github_b3_request_binding import (
        B3RegisteredScope, bind_b3_registered_inputs,
    )
    from obsidian_automation.github_project_overview import make_overview_proposal
    from obsidian_automation.github_project_status_mutation import (
        parse_watcher_proposal,
    )

    root, manifest, paths, _, _ = owned_fixture
    project = "10-Project/Terreate/Terreate.md"
    repo = "upiscium/Terreate"
    status = parse_watcher_proposal(canonical({
        "change": True,
        "current_status": "running",
        "event": "project-status-observation",
        "latest_commit_at": "2026-08-13T04:11:38Z",
        "latest_commit_sha": "0d022ed5140b497b5d3be886408822ecbd981dd3",
        "observed_at": datetime.fromtimestamp(
            1800000000 - 5, timezone.utc,
        ).isoformat().replace("+00:00", "Z"),
        "open_issues": [203], "open_prs": [42],
        "pending": True, "project": project,
        "proposed_status": "planning",
        "reason": "synthetic no recent update", "repository": repo,
    }))
    overview = make_overview_proposal(
        project_path=project, repository=repo, issues=[], pull_requests=[],
    )
    for path in paths:
        path.unlink()
    manifest.update({
        "status_sha256": hashlib.sha256(status.canonical_bytes).hexdigest(),
        "overview_sha256": hashlib.sha256(overview.canonical_bytes).hexdigest(),
    })
    new_files = (
        (f"{REQUEST}.b3-source.json", canonical(manifest)),
        (f"{manifest['status_sha256']}.b3-status.json", status.canonical_bytes),
        (f"{manifest['overview_sha256']}.b3-overview.json", overview.canonical_bytes),
    )
    for name, raw in new_files:
        p = root / name
        p.write_bytes(raw)
        p.chmod(0o640)
    verified = read_verified_b3_proposal_pair(request())
    config = B3JobConfig(
        binding_ref=BINDING, revision=REVISION, project_path=project,
        repository=repo, registered_base_url=(
            "https://nextcloud.example/remote.php/dav/files/owner/ObsidianVault"
        ),
        # B3 request binding is pure; no authentication or Writer is used.
        # Deliberately supply no credentials in this synthetic fixture.
        username="", password="",
    )
    bound = bind_b3_registered_inputs(
        B3RegisteredScope(config, GENERATION),
        verified, request(), now=1800000000,
    )
    assert bound.request_id == REQUEST
    assert bound.source_record_id == RECORD
    assert bound.status_proposal.sha256 == manifest["status_sha256"]
    assert bound.overview_proposal.sha256 == manifest["overview_sha256"]
    assert bound.config is config
