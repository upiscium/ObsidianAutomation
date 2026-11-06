"""Fixed root-owned synthetic registration/secret admission, never live files."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from obsidian_automation import github_b3_verified_registration as registration
from obsidian_automation.github_b3_verified_registration import (
    B3RegistrationUnavailable, load_verified_b3_registered_scope,
)

BINDING = "b" * 32
REVISION = "c" * 32
GENERATION = "d" * 32
SECRET_TEST_BYTES = b"Z" * 36


def canonical(record):
    return (
        json.dumps(
            record, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ) + "\n"
    ).encode("utf-8")


@pytest.fixture
def source_tree(tmp_path, monkeypatch):
    folder = tmp_path / "protected-registration"
    folder.mkdir()
    folder.chmod(0o750)
    monkeypatch.setattr(registration, "ROOT", folder)
    monkeypatch.setattr(registration, "TRUST_ANCHOR", tmp_path)
    monkeypatch.setattr(registration, "_root_uid", lambda: os.geteuid())
    monkeypatch.setattr(
        registration, "_reader_ids", lambda: (os.geteuid(), os.getegid()),
    )
    record = {
        "record_version": 1, "source_generation": GENERATION,
        "binding_ref": BINDING, "revision": REVISION,
        "project_path": "10-Project/Terreate/Terreate.md",
        "repository": "upiscium/Terreate",
        "registered_base_url": (
            "https://nextcloud.example/remote.php/dav/files/"
            "clean-writer/ObsidianVault"
        ),
        "username": "clean-writer", "timeout": 30,
    }
    manifest = folder / "registered-scope.json"
    secret = folder / "webdav-password"
    manifest.write_bytes(canonical(record))
    secret.write_bytes(SECRET_TEST_BYTES + b"\n")
    for path in (manifest, secret):
        path.chmod(0o640)
    return folder, record, manifest, secret


def unavailable():
    with pytest.raises(
        B3RegistrationUnavailable, match="registration_source_unavailable"
    ):
        load_verified_b3_registered_scope()


def test_loads_only_fixed_roles_and_root_owned_synthetic_files(source_tree):
    folder, record, manifest, secret = source_tree
    expected_before = tuple((p.stat().st_mtime_ns, p.read_bytes())
                            for p in (manifest, secret))
    scope = load_verified_b3_registered_scope()
    assert scope.source_generation == GENERATION
    assert scope.config.binding_ref == BINDING
    assert scope.config.revision == REVISION
    assert scope.config.project_path == record["project_path"]
    assert scope.config.repository == record["repository"]
    assert scope.config.registered_base_url == record["registered_base_url"]
    assert scope.config.username == record["username"]
    assert scope.config.password.encode("utf-8") == SECRET_TEST_BYTES
    assert scope.config.timeout == 30
    assert SECRET_TEST_BYTES.decode() not in repr(scope)
    assert expected_before == tuple(
        (p.stat().st_mtime_ns, p.read_bytes()) for p in (manifest, secret)
    )


@pytest.mark.parametrize("key,value", (
    ("record_version", True), ("record_version", 2),
    ("source_generation", "q" * 32),
    ("binding_ref", "not hex"),
    ("revision", "wrong"),
    ("project_path", "11-Knowledge/item.md"),
    ("project_path", "10-Project/../secret.md"),
    ("project_path", "10-Project/a.txt"),
    ("repository", "foo"),
    ("repository", "invalid!repo/name"),
    ("username", "user/name"),
    ("username", "user@name"),
    ("username", "user name"),
    ("username", "."),
    ("username", ".."),
    ("username", "name\nother"),
    ("project_path", "10-Project/a\nb.md"),
    ("timeout", 0),
    ("timeout", 121),
    ("timeout", "30"),
))
def test_invalid_manifest_rejected(source_tree, key, value):
    folder, record, manifest, secret = source_tree
    record[key] = value
    manifest.write_bytes(canonical(record))
    unavailable()


@pytest.mark.parametrize("url", (
    "http://nextcloud.example/remote.php/dav/files/clean-writer/ObsidianVault",
    "https://a:b@nextcloud.example/remote.php/dav/files/clean-writer/ObsidianVault",
    "https://nextcloud.example/remote.php/dav/files/other/ObsidianVault",
    "https://nextcloud.example/remote.php/dav/files/clean-writer/ObsidianVault?x=1",
    "https://nextcloud.example/remote.php/dav/files/clean-writer/ObsidianVault#x",
    "https://nextcloud.example/remote.php/dav/files/clean-writer/%2fsecret",
    "https://nextcloud.example/remote.php/dav/files/clean-writer/../other",
    "https://nextcloud.example/remote.php/dav/files/clean-writer/",
    "https://nextcloud.example/remote.php/dav/files/clean-writer",
    "https://nextcloud.example/remote.php/dav/files/clean-writer/Obsidian\tVault",
    "https://nextcloud.example/remote.php/dav/files/clean-writer/Obsidian Vault",
))
def test_url_scope_is_pinned_https_and_same_username(source_tree, url):
    folder, record, manifest, secret = source_tree
    record["registered_base_url"] = url
    manifest.write_bytes(canonical(record))
    unavailable()


@pytest.mark.parametrize("value", (
    b"",
    b"\n",
    b"\x00opaque",
    b"opaque\nsecond",
    b"opaque\r",
    b"\xff\xfe",
    b"Q" * 1025,
))
def test_invalid_secret_rejected(source_tree, value):
    folder, record, manifest, secret = source_tree
    secret.write_bytes(value)
    unavailable()


@pytest.mark.parametrize("path_number", (0, 1))
def test_source_missing_or_symlinked_refused(source_tree, path_number):
    folder, record, manifest, secret = source_tree
    path = (manifest, secret)[path_number]
    copy = folder / "unrelated"
    copy.write_bytes(path.read_bytes())
    copy.chmod(0o640)
    path.unlink()
    path.symlink_to(copy)
    unavailable()


@pytest.mark.parametrize("path_number", (0, 1))
def test_hardlink_attacks_refused(source_tree, path_number):
    folder, record, manifest, secret = source_tree
    os.link((manifest, secret)[path_number], folder / "second-link")
    unavailable()


@pytest.mark.parametrize("path_number", (0, 1))
@pytest.mark.parametrize("mode", (0o600, 0o644, 0o660, 0o666))
def test_bad_file_permissions_refused(source_tree, path_number, mode):
    folder, record, manifest, secret = source_tree
    (manifest, secret)[path_number].chmod(mode)
    unavailable()


@pytest.mark.parametrize("mode", (0o700, 0o755, 0o770, 0o777))
def test_bad_directory_permissions_refused(source_tree, mode):
    folder, record, manifest, secret = source_tree
    folder.chmod(mode)
    unavailable()


def test_fake_missing_reader_role_refused(source_tree, monkeypatch):
    def missing():
        raise KeyError("disposable-test-role-unavailable")
    monkeypatch.setattr(registration, "_reader_ids", missing)
    unavailable()


def test_untrusted_root_owner_refused(source_tree, monkeypatch):
    monkeypatch.setattr(registration, "_root_uid", lambda: 0 if os.geteuid() else 9999)
    unavailable()


def test_trust_anchor_must_cover_private_root(source_tree, monkeypatch):
    folder, record, manifest, secret = source_tree
    monkeypatch.setattr(registration, "TRUST_ANCHOR", Path("/"))
    # The test's tmp ancestors are not the fixed production root owner.
    unavailable()


def test_noncanonical_json_duplicate_keys_refused(source_tree):
    folder, record, manifest, secret = source_tree
    manifest.write_bytes(
        canonical(record)[:-2]
        + b',"binding_ref":"'
        + BINDING.encode()
        + b'"}\n'
    )
    unavailable()


def test_noncanonical_json_whitespace_refused(source_tree):
    folder, record, manifest, secret = source_tree
    manifest.write_bytes(json.dumps(record, indent=2).encode())
    unavailable()


def test_manifest_race_detected(source_tree, monkeypatch):
    folder, record, manifest, secret = source_tree
    original = registration._read_fixed_file
    count = 0

    def race(*a, **kw):
        nonlocal count
        count += 1
        data = original(*a, **kw)
        if count == 2:
            record["revision"] = "e" * 32
            manifest.write_bytes(canonical(record))
        return data

    monkeypatch.setattr(registration, "_read_fixed_file", race)
    unavailable()


def test_secret_rotation_detected(source_tree, monkeypatch):
    folder, record, manifest, secret = source_tree
    original = registration._read_fixed_file
    count = 0

    def race(*a, **kw):
        nonlocal count
        count += 1
        data = original(*a, **kw)
        if count == 2:
            secret.write_bytes(b"Y" * 36 + b"\n")
        return data

    monkeypatch.setattr(registration, "_read_fixed_file", race)
    unavailable()


def test_root_directory_symlink_refused(source_tree, monkeypatch):
    folder, record, manifest, secret = source_tree
    symlink = folder.parent / "linked-root"
    symlink.symlink_to(folder, target_is_directory=True)
    monkeypatch.setattr(registration, "ROOT", symlink)
    unavailable()


def test_no_http_origin_or_user_controllable_paths(source_tree):
    # The reader has no request/path arguments; registered source must
    # still be supplied only after independently verified legacy cutover.
    assert load_verified_b3_registered_scope.__code__.co_argcount == 0
    assert not hasattr(registration, "register_webui")
    assert not hasattr(registration, "authorize_cutover")
