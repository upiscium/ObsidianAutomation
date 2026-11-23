"""#601: fail-closed generic DAV parent creation with no production I/O."""
from urllib.parse import urlsplit

import pytest

from obsidian_automation.core_promotion_transport import (
    HTTPResponse,
    PromotionTransportConflict,
    PromotionTransportNetworkError,
    PromotionTransportError,
    ensure_parent_collections,
    execute_promotion,
    load_checkpoint,
)
from test_core_promotion_transport import _fixture, _base_remote, FakeDAV

BASE = "https://dav.example.invalid/vault"
CHILD = "98-System/new/path/file.css"


def check(dav, path=CHILD):
    return ensure_parent_collections(
        base_url=BASE,
        path=path,
        username="promoter",
        password="secret",
        timeout=5,
        transport=dav,
    )


def fixture_dav():
    # Only the configured Vault exists initially.
    return FakeDAV({})


def test_nested_parent_created_in_ascending_order_and_verifiable():
    dav = fixture_dav()
    created = check(dav)
    assert created == ("98-System", "98-System/new", "98-System/new/path")
    assert [p for method, p, *_ in dav.mutations if method == "MKCOL"] == [
        "98-System/", "98-System/new/", "98-System/new/path/",
    ]
    # Repeat: no extra network mutations against already verified collections.
    assert check(dav) == ()
    assert len(dav.mutations) == 3


def test_missing_configured_root_never_created_or_auto_selected():
    dav = fixture_dav()
    dav.directories.remove("")
    with pytest.raises(PromotionTransportConflict, match="configured WebDAV root"):
        check(dav)
    assert dav.mutations == []


def test_parent_file_refuses_without_mkcol():
    class FileAsParent(FakeDAV):
        def __call__(self, **kwargs):
            path = self._path(kwargs["target_url"])
            if kwargs["method"] == "PROPFIND" and path == "98-System/":
                body = (
                    '<d:multistatus xmlns:d="DAV:"><d:response>'
                    '<d:href>/vault/98-System/</d:href>'
                    '<d:propstat><d:prop><d:resourcetype/>'
                    '</d:prop><d:status>HTTP/1.1 200 OK</d:status>'
                    '</d:propstat></d:response></d:multistatus>'
                ).encode()
                return HTTPResponse(207, body, None)
            return super().__call__(**kwargs)
    dav = FileAsParent({})
    with pytest.raises(PromotionTransportConflict, match="not a verified DAV collection"):
        check(dav)
    assert dav.mutations == []


def test_racing_mkcol_405_must_prove_actual_collection():
    class Concurrent(FakeDAV):
        def __call__(self, **kwargs):
            if kwargs["method"] == "MKCOL":
                super().__call__(**kwargs)  # peer wins the race, creates folder
                return HTTPResponse(405, b"", None)
            return super().__call__(**kwargs)
    dav = Concurrent({})
    assert check(dav) == ()
    assert "98-System/new/path/" in dav.directories


def test_mkcol_405_without_collection_remains_blocked():
    class ExistingFile(FakeDAV):
        def __call__(self, **kwargs):
            if kwargs["method"] == "MKCOL":
                self.mutations.append((kwargs["method"], self._path(kwargs["target_url"]),
                                       kwargs["headers"], kwargs["body"]))
                return HTTPResponse(405, b"", None)
            return super().__call__(**kwargs)
    dav = ExistingFile({})
    with pytest.raises(PromotionTransportNetworkError, match="could not be verified"):
        check(dav)
    assert not any(method=="PUT" for method,*_ in dav.mutations)


@pytest.mark.parametrize("status", [400, 401, 403, 409, 423, 500])
def test_refused_mkcol_fail_closed_without_put(status):
    class Refuse(FakeDAV):
        def __call__(self, **kwargs):
            if kwargs["method"] == "MKCOL":
                self.mutations.append((kwargs["method"], self._path(kwargs["target_url"]),
                                       kwargs["headers"], kwargs["body"]))
                return HTTPResponse(status, b"", None)
            return super().__call__(**kwargs)
    dav = Refuse({})
    with pytest.raises(PromotionTransportConflict, match="refused or conflicted"):
        check(dav)
    assert [method for method,*_ in dav.mutations] == ["MKCOL"]


def test_ambiguous_network_loss_awaits_explicit_replay():
    class Lost(FakeDAV):
        first = True
        def __call__(self, **kwargs):
            if kwargs["method"]=="MKCOL" and self.first:
                self.first = False
                super().__call__(**kwargs)  # side effect reached provider
                raise PromotionTransportNetworkError("synthetic lost response")
            return super().__call__(**kwargs)
    dav=Lost({})
    with pytest.raises(PromotionTransportNetworkError, match="no trustworthy result"):
        check(dav)
    assert dav.directories == {"", "98-System/"}
    # The later controlled attempt observes and converges safely.
    assert check(dav) == ("98-System/new", "98-System/new/path")
    assert [p for m,p,*_ in dav.mutations if m=="MKCOL"].count("98-System/")==1


def test_no_implicit_mkcol_without_full_preflight(tmp_path):
    repo, config, plan, checkpoint, receipts=_fixture(tmp_path)
    files=_base_remote(plan,repo)
    files["98-System/01-script/task.js"]=b"unapproved divergence\n"
    dav=FakeDAV(files)
    before=checkpoint.read_bytes()
    with pytest.raises(PromotionTransportConflict):
        execute_promotion(
            plan=plan, core_repository=repo, config_path=config,
            base_url="https://dav.example.invalid/vault",
            username="promoter",password="secret",
            checkpoint_path=checkpoint,receipt_directory=receipts,
            transport=dav,
        )
    assert checkpoint.read_bytes()==before
    assert dav.mutations==[]


def test_mkcol_failure_keeps_checkpoint_and_does_not_write_target(tmp_path):
    class Refuse(FakeDAV):
        def __call__(self, **kwargs):
            if kwargs["method"]=="MKCOL":
                return HTTPResponse(409,b"",None)
            return super().__call__(**kwargs)
    repo,config,plan,checkpoint,receipts=_fixture(tmp_path)
    dav=Refuse(_base_remote(plan,repo))
    baseline=checkpoint.read_bytes()
    with pytest.raises(PromotionTransportConflict,match="MKCOL"):
        execute_promotion(
            plan=plan,core_repository=repo,config_path=config,
            base_url="https://dav.example.invalid/vault",
            username="promoter",password="secret",
            checkpoint_path=checkpoint,receipt_directory=receipts,
            transport=dav,
        )
    assert checkpoint.read_bytes()==baseline
    assert not any("feature.css" in file for file in dav.files)
    assert not receipts.exists()


@pytest.mark.parametrize("bad", ["/escape.txt","../escape.txt","foo//bar", ""] )
def test_unsafe_target_paths_rejected_without_provider_io(bad):
    class Never:
        def __call__(self,**kwargs):raise AssertionError("network I/O not allowed")
    with pytest.raises(PromotionTransportError):
        ensure_parent_collections(base_url=BASE, path=bad,
                                  username="promoter",password="secret",
                                  timeout=5,transport=Never())



def test_parent_transport_exceptions_are_redacted():
    class Fails:
        def __call__(self, **kwargs):
            raise OSError("should-not-be-in-error-diagnostics")
    with pytest.raises(PromotionTransportNetworkError) as exc:
        check(Fails())
    assert "should-not-be-in-error-diagnostics" not in str(exc.value)


def test_double_encoded_git_parent_names_are_rejected_before_any_io():
    class Never:
        def __call__(self, **kwargs):
            raise AssertionError("provider I/O was incorrectly allowed")
    with pytest.raises(PromotionTransportError, match="ambiguous encoded parent path"):
        check(Never(), "98-System/%2e%2e/notes.md")


def test_percent_in_final_filename_is_allowed_and_sent_as_25():
    dav = fixture_dav()
    assert check(dav, "Notes/100% done.md") == ("Notes",)


def test_percent_in_parent_still_rejected_but_filename_percent_ok():
    with pytest.raises(PromotionTransportError, match="ambiguous encoded parent path"):
        check(fixture_dav(), "Notes/50%/a.md")


def test_preflight_is_read_only_and_rejects_bad_ancestor_before_any_mutation(tmp_path):
    from obsidian_automation.core_promotion_transport import preflight_remote
    repo, config, plan, checkpoint, receipts = _fixture(tmp_path)
    dav = FakeDAV(_base_remote(plan, repo))
    dav.directories.remove("")  # configured root missing
    with pytest.raises(PromotionTransportConflict, match="configured WebDAV root"):
        preflight_remote(plan, base_url="https://dav.example.invalid/vault",
                         username="promoter", password="secret", transport=dav)
    assert dav.mutations == []
