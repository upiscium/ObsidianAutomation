"""Synthetic provider-neutral DAV URL/probe and Nextcloud compatibility."""

import pytest

from obsidian_automation.connection_fields import FieldDrafts
from obsidian_automation.snapshot_setup import (
    SetupStore, _rclone_config, validate, validate_nextcloud,
)
from obsidian_automation.webdav_connection import (
    _collection_multistatus, probe_webdav_collection, rclone_vendor,
    validate_webdav_url, vault_collection_url,
)


def source(url="https://dav.example.invalid/webdav/"):
    return {
        "webdav_url": url, "vault_folder": "Vault",
        "nextcloud_user": "fixture-user",
        "nextcloud_password": "synthetic-passphrase",
    }


def full(url="https://dav.example.invalid/webdav/"):
    return {
        **source(url),
        "git_url": "https://git.example.invalid/owner/ObsidianVault.git",
        "git_user": "fixture-user",
        "git_token": "synthetic-token",
        "branch": "main",
    }


@pytest.mark.parametrize("url,vendor", [
    ("https://dav.example.invalid/webdav/", "other"),
    ("https://dav.example.invalid/", "other"),
    ("https://dav.example.invalid:8443/files/", "other"),
    ("https://dav.example.invalid/Vault%20Root/", "other"),
    ("https://cloud.example.invalid/remote.php/dav/files/account/", "nextcloud"),
])
def test_provider_neutral_https_directory_urls_and_vendor(url,vendor):
    assert validate_webdav_url(url)==url
    assert rclone_vendor(url)==vendor
    assert validate_nextcloud(source(url))["webdav_url"]==url
    assert validate(full(url))["webdav_url"]==url
    config=_rclone_config(source(url),"synthetic-obscured")
    assert "vendor = "+vendor+"\n" in config
    assert "url = "+url+"\n" in config
    assert "synthetic-passphrase" not in config


@pytest.mark.parametrize("url", [
    "http://dav.example.invalid/webdav/",
    "https://dav.example.invalid/webdav",
    "https://dav.example.invalid/a//b/",
    "https://dav.example.invalid/../vault/",
    "https://dav.example.invalid/a/./vault/",
    "https://dav.example.invalid/a/%2e%2e/vault/",
    "https://dav.example.invalid/a/%2f/vault/",
    "https://dav.example.invalid/a/%5c/vault/",
    "https://dav.example.invalid/a/%25/vault/",
    "https://dav.example.invalid/a/%QQ/",
    "https://user:secret@dav.example.invalid/webdav/",
    "https://dav.example.invalid/webdav/?x=1",
    "https://dav.example.invalid/webdav/#fragment",
    "https://dav.example.invalid:99999/webdav/",
    "https://dav.example.invalid/webdav/\n",
    r"https://dav.example.invalid\other/",
    "https://dav.example.invalid/web dav/",
    "",
])
def test_invalid_or_noncanonical_dav_url_rejected(url):
    with pytest.raises(ValueError):
        validate_webdav_url(url)
    with pytest.raises(ValueError):
        validate_nextcloud(source(url))
    with pytest.raises(ValueError):
        validate(full(url))


def xml(href="/webdav/Vault/",kind="collection",status="HTTP/1.1 200 OK"):
    return (
        '<d:multistatus xmlns:d="DAV:"><d:response>'
        f'<d:href>{href}</d:href><d:propstat><d:prop>'
        f'<d:resourcetype><d:{kind}/></d:resourcetype></d:prop>'
        f'<d:status>{status}</d:status></d:propstat>'
        '</d:response></d:multistatus>'
    ).encode()


def test_propfind_accepts_only_exact_collection():
    target="https://dav.example.invalid/webdav/Vault/"
    assert _collection_multistatus(xml(),target)
    assert _collection_multistatus(
        xml(href="https://dav.example.invalid/webdav/Vault/"),target)
    assert not _collection_multistatus(xml(href="/webdav/Other/"),target)
    assert not _collection_multistatus(
        xml(href="https://other.invalid/webdav/Vault/"),target)
    assert not _collection_multistatus(xml(kind="getcontentlength"),target)
    assert not _collection_multistatus(xml(status="HTTP/1.1 404 Not Found"),target)
    assert not _collection_multistatus(b"",target)
    assert not _collection_multistatus(b"<d:multistatus",target)
    assert not _collection_multistatus(xml()+b"x"*65537,target)
    rows=xml().replace(b"</d:multistatus>",b"</d:multistatus>")
    assert not _collection_multistatus(rows.replace(b"</d:response>",b"</d:response>"
                   b"<d:response><d:href>/webdav/Vault/</d:href></d:response>"),target)


def test_probe_one_bounded_no_redirect_propfind(monkeypatch):
    import obsidian_automation.webdav_connection as module
    seen=[]

    class Response:
        status=207
        headers={"Content-Type":"application/xml; charset=utf-8"}
        def __enter__(self): return self
        def __exit__(self,*_): return False
        def read(self,n):
            assert n==65537
            return xml()

    class Opener:
        def open(self,request,timeout):
            seen.append(request)
            assert timeout==8
            assert request.get_method()=="PROPFIND"
            assert request.get_header("Depth")=="0"
            assert request.full_url=="https://dav.example.invalid/webdav/Vault/"
            assert request.data==module.PROPFIND_BODY
            assert request.get_header("Authorization").startswith("Basic ")
            assert "synthetic-passphrase" not in str(request.headers)
            return Response()

    def factory(proxy,handler):
        assert proxy.proxies=={}
        assert handler.redirect_request(None,None,302,"Moved",{},
                                        "https://other.invalid/") is None
        return Opener()
    monkeypatch.setattr(module.urllib.request,"build_opener",factory)
    assert probe_webdav_collection(source())
    assert len(seen)==1


def test_webdav_store_restart_and_individual_save_without_gitea(tmp_path):
    store=SetupStore(tmp_path,obscure=lambda _:"obscured")
    store.nextcloud_probe=lambda _:True
    assert store.save_nextcloud(source())
    assert store.load_nextcloud()==source()
    assert "vendor = other" in store.rclone.read_text()
    assert "synthetic-passphrase" not in store.rclone.read_text()
    assert store.nextcloud_status()["configured"]
    assert store.connections() is None
    assert SetupStore(tmp_path).load_nextcloud()==source()
    assert FieldDrafts(store).status("nextcloud")["values"]=={
        "webdav_url":source()["webdav_url"],
        "vault_folder":"Vault","nextcloud_user":"fixture-user",
    }
    action,candidate=FieldDrafts(store).prepare("nextcloud",{
        "field":"webdav_url","value":"https://dav.example.invalid/another/"})
    assert action=="commit"
    assert candidate["webdav_url"].endswith("/another/")
    assert candidate["nextcloud_password"]=="synthetic-passphrase"
    assert store.load_nextcloud()==source()  # Preview does not change live data
