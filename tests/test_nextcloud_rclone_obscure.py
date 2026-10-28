"""Regression: rclone obscure emits unpadded URL-safe Base64, not standard Base64."""
from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from obsidian_automation.snapshot_setup import SetupStore, _RCLONE_OBSCURED_RE
from test_snapshot_setup import sample

NC_ONLY = ("webdav_url", "vault_folder", "nextcloud_user", "nextcloud_password")


@pytest.mark.parametrize("with_gitea", [False, True])
def test_urlsafe_obscure_is_accepted_for_nextcloud_and_full_snapshot(tmp_path, with_gitea):
    """Previously '-' or '_' caused false negative after a valid PROPFIND."""
    secret = "OBSCURED_VALID_RCLONE-_URLSAFE"
    store = SetupStore(tmp_path, probe=lambda _: True, obscure=lambda _: secret)
    store.nextcloud_probe = lambda _: True
    value = sample()
    if with_gitea:
        assert store.save(value) is True
        assert store.load() == value
        assert store.connections() is not None
    else:
        source = {k: value[k] for k in NC_ONLY}
        assert store.save_nextcloud(source) is True
        assert store.load_nextcloud() == source
        assert store.connections() is None
    assert store.rclone.is_file()
    assert f"pass = {secret}\n" in store.rclone.read_text()
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.rclone.stat().st_mode & 0o777 == 0o600
    assert value["nextcloud_password"] not in store.rclone.read_text()
    assert _RCLONE_OBSCURED_RE.fullmatch(secret) is not None


@pytest.mark.parametrize("bad", ["", "invalid obscured", "invalid\nobscured",
                                "token@evil", "A" * 4097, "é"])
def test_obscured_output_still_rejects_invalid_chars(tmp_path, bad):
    store = SetupStore(tmp_path, probe=lambda _: True, obscure=lambda _: bad)
    store.nextcloud_probe = lambda _: True
    value = {k: sample()[k] for k in NC_ONLY}
    assert store.save_nextcloud(value) is False
    assert not store.rclone.exists()
    assert not store.path.exists()


@pytest.mark.skipif(shutil.which("rclone") is None, reason="rclone not on test runner")
def test_real_rclone_obscure_roundtrip_random_urlsafe_output(tmp_path):
    """Integration contract with the actual local rclone executable when available."""
    fixture_password = "SYNTHETIC_ONLY_NOT_REAL_NEXTCLOUD_APP_PASSWORD"
    for i in range(16):
        data = subprocess.run(["rclone", "obscure", "-"],
                              input=fixture_password + "\n",
                              text=True, capture_output=True, timeout=8, check=True)
        obscured = data.stdout.strip()
        assert _RCLONE_OBSCURED_RE.fullmatch(obscured) is not None
        store = SetupStore(tmp_path / str(i), obscure=lambda _, s=obscured: s)
        store.nextcloud_probe = lambda _: True
        assert store.save_nextcloud({k: sample()[k] for k in NC_ONLY})
        assert store.load_nextcloud() is not None
        decoded = subprocess.run(["rclone", "reveal", obscured],
                                 text=True, capture_output=True, timeout=8, check=True)
        assert decoded.stdout.strip() == fixture_password



def test_long_unicode_password_has_sufficient_rclone_output_budget(tmp_path):
    # 512 characters of a 4-byte Unicode code point is 2048 input bytes.
    # rclone adds a 16-byte IV before unpadded URL-safe Base64 encoding.
    import base64
    unicode_password = "🔑" * 512
    assert len(unicode_password) == 512
    fake_valid_output = base64.urlsafe_b64encode(
        b"0" * 16 + unicode_password.encode("utf-8")).decode().rstrip("=")
    assert 2500 < len(fake_valid_output) < 4096
    store = SetupStore(tmp_path, probe=lambda _: True,
                       obscure=lambda _: fake_valid_output)
    store.nextcloud_probe = lambda _: True
    data = {key: sample()[key] for key in NC_ONLY}
    data["nextcloud_password"] = unicode_password
    assert store.save_nextcloud(data)
    assert store.load_nextcloud() == data



@pytest.mark.skipif(shutil.which("rclone") is None, reason="rclone not on test runner")
def test_real_rclone_accepts_max_utf8_valid_password(tmp_path):
    unicode_password = "🔑" * 512
    result = subprocess.run(["rclone", "obscure", "-"],
                            input=unicode_password + "\n",
                            text=True, capture_output=True, timeout=12, check=True)
    encoded = result.stdout.strip()
    assert len(encoded) < 4096
    assert _RCLONE_OBSCURED_RE.fullmatch(encoded) is not None
    store = SetupStore(tmp_path, obscure=lambda _: encoded)
    store.nextcloud_probe = lambda _: True
    source = {k: sample()[k] for k in NC_ONLY}
    source["nextcloud_password"] = unicode_password
    assert store.save_nextcloud(source)
    assert store.load_nextcloud() == source
