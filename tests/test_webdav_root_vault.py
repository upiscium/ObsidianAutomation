"""#607: a generic WebDAV URL may itself be the Vault root (vault_folder="/")."""
import pytest

from obsidian_automation import snapshot_setup as setup
from obsidian_automation.connection_fields import FieldError, _validate_field
from obsidian_automation.snapshot_adapter import Connections
from obsidian_automation.webdav_connection import (
    ROOT_VAULT, valid_vault_folder, vault_collection_url, vault_folder_parts,
)

GENERIC = "https://vault.example.invalid/"
GENERIC_SUB = "https://dav.example.invalid/webdav/"
NEXTCLOUD = "https://nas.example.invalid/remote.php/dav/files/user/"


@pytest.mark.parametrize("url", [GENERIC, GENERIC_SUB])
def test_root_marker_accepted_for_generic_urls(url):
    assert valid_vault_folder("/", url)


def test_root_marker_refused_for_nextcloud_and_without_url():
    assert not valid_vault_folder("/", NEXTCLOUD)
    assert not valid_vault_folder("/")
    assert not valid_vault_folder("/", "http://insecure.example.invalid/")


@pytest.mark.parametrize("bad", ["", "//", "..", "a//b", "/a", "a/", ".", "a/../b", 3, None])
def test_other_folder_shapes_stay_invalid(bad):
    assert not valid_vault_folder(bad, GENERIC)


def test_named_folder_behaviour_is_unchanged():
    assert valid_vault_folder("ObsidianVault", NEXTCLOUD)
    assert valid_vault_folder("Team Vault/Notes", GENERIC)


def test_collection_url_adds_no_prefix_for_root_and_one_for_named():
    assert vault_collection_url(GENERIC, "/") == GENERIC
    assert vault_collection_url(GENERIC_SUB, "/") == GENERIC_SUB
    assert vault_collection_url(GENERIC, "A B/C") == GENERIC + "A%20B/C/"
    assert vault_folder_parts(ROOT_VAULT) == []
    assert vault_folder_parts("A/B") == ["A", "B"]


def _nextcloud(url, folder):
    return {"webdav_url": url, "vault_folder": folder,
            "nextcloud_user": "u", "nextcloud_password": "p"}


def test_nextcloud_connection_validation_accepts_root_only_when_generic():
    assert setup.validate_nextcloud(_nextcloud(GENERIC, "/"))["vault_folder"] == "/"
    with pytest.raises(ValueError):
        setup.validate_nextcloud(_nextcloud(NEXTCLOUD, "/"))
    with pytest.raises(ValueError):
        setup.validate_nextcloud(_nextcloud(GENERIC, ""))


def test_full_connection_validation_accepts_root_only_when_generic():
    base = {"git_url": "https://git.example.invalid/o/r.git", "git_user": "g",
            "git_token": "x", "branch": "main",
            **_nextcloud(GENERIC, "/")}
    assert set(base) == setup.FIELDS
    assert setup.validate(base)["vault_folder"] == "/"
    with pytest.raises(ValueError):
        setup.validate({**base, "webdav_url": NEXTCLOUD})


def test_field_level_accepts_root_marker():
    _validate_field("nextcloud", "vault_folder", "/")
    for bad in ("", "..", "//"):
        with pytest.raises(FieldError):
            _validate_field("nextcloud", "vault_folder", bad)


def test_rclone_source_accepts_root_marker(tmp_path):
    cfg = tmp_path / "rclone.conf"
    cfg.write_text("[nextcloud]\n")
    setup.NextcloudSource("nextcloud:/", cfg).validate()
    setup.NextcloudSource("nextcloud:Vault", cfg).validate()
    for bad in ("nextcloud:", "nextcloud://", "nextcloud:/a", "nextcloud:.."):
        with pytest.raises(ValueError):
            setup.NextcloudSource(bad, cfg).validate()


def test_snapshot_connections_accept_root_marker(tmp_path):
    cfg = tmp_path / "rclone.conf"; cfg.write_text("x")
    policy = tmp_path / "policy"; policy.write_text("x")

    def conn(remote):
        return Connections(state=tmp_path, rclone_remote=remote,
                           git_remote="https://git.example.invalid/o/r.git",
                           branch="main", policy=policy, rclone_config=cfg)
    conn("nextcloud:/").validate()
    conn("nextcloud:Vault").validate()
    for bad in ("nextcloud:", "nextcloud:/a", "nextcloud://"):
        with pytest.raises(ValueError):
            conn(bad).validate()


def test_project_notes_source_has_no_double_slash(monkeypatch, tmp_path):
    from obsidian_automation import github_daily_setup as daily
    seen = []

    class R:
        returncode = 0
    monkeypatch.setattr(daily.subprocess, "run",
                        lambda argv, **k: (seen.append(argv), R())[1])
    cfg = tmp_path / "rclone.conf"; cfg.write_text("x")
    policy = tmp_path / "policy"; policy.write_text("x")
    target = tmp_path / "t"; target.mkdir()
    for remote, expected in (("nextcloud:/", "nextcloud:/10-Project"),
                             ("nextcloud:Vault", "nextcloud:Vault/10-Project")):
        conn = Connections(state=tmp_path, rclone_remote=remote,
                           git_remote="https://git.example.invalid/o/r.git",
                           branch="main", policy=policy, rclone_config=cfg)
        assert daily.pull_project_notes(conn, target, folder="10-Project")
        assert seen[-1][4] == expected


@pytest.mark.parametrize("url", [
    "https://host.example.invalid/nextcloud/remote.php/dav/files/alice/",
    "https://host.example.invalid/remote.php/webdav/",
    "https://host.example.invalid/remote.php/%64av/files/alice/",
    "https://host.example.invalid/Remote.PHP/dav/files/alice/",
    "https://host.example.invalid/nc/index.php/apps/x/",
    "https://host.example.invalid/dav/files/alice/",
])
def test_root_marker_cannot_be_unlocked_by_nextcloud_url_spelling(url):
    assert not valid_vault_folder("/", url)
    with pytest.raises(ValueError):
        setup.validate_nextcloud(_nextcloud(url, "/"))
    # a named folder under such a URL keeps working
    assert valid_vault_folder("ObsidianVault", url)
