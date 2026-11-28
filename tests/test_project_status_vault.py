"""B3-lite step 2a: Project note listing and the conditional one-line status write."""
from __future__ import annotations

import hashlib
from urllib.parse import quote, unquote, urlsplit

import pytest

from obsidian_automation.core_promotion_transport import (
    HTTPResponse, PromotionTransportNetworkError,
)
from obsidian_automation.project_status_vault import (
    ProjectVaultError, list_project_notes, read_note, set_status,
)
from obsidian_automation.vault_layout import role_folder
from obsidian_automation.vault_root_activation import RootPinRefused
from test_knowledge_proposal_publish import ROOT, SOURCE, store  # noqa: F401 (fixture)

FOLDER = role_folder("project")
NOTE = "---\ntype: project\nstatus: running\ngithub_repo: a/b\ngithub_watch: true\n---\n# T\n"


def etag_of(data: bytes) -> str:
    return '"' + hashlib.sha256(data).hexdigest()[:16] + '"'


class Vault:
    """Synthetic WebDAV with strong content ETags and If-Match."""

    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.calls: list[tuple] = []
        self.weak = False
        self.put_raises = False
        self.put_ignored = False
        self.before_put = None

    def __call__(self, *, method, target_url, username, password, headers, body,
                 timeout, response_limit):
        assert (username, password) == ("user", "NC-SYNTHETIC")
        rel = unquote(urlsplit(target_url).path)[len(ROOT):]
        self.calls.append((method, rel, dict(headers or {})))
        if method == "PROPFIND":
            prefix = rel if rel.endswith("/") else rel + "/"
            dirs, entries = set(), []
            for name in sorted(self.files):
                if name.startswith(prefix):
                    rest = name[len(prefix):]
                    if "/" in rest:
                        dirs.add(rest.split("/")[0])
                    else:
                        entries.append(name)
            if not entries and not dirs and not any(n.startswith(prefix) for n in self.files):
                return HTTPResponse(404, b"", None)
            out = [f'<d:response><d:href>{ROOT}{quote(prefix)}</d:href><d:propstat><d:prop>'
                   '<d:resourcetype><d:collection/></d:resourcetype></d:prop></d:propstat></d:response>']
            for d in sorted(dirs):
                out.append(f'<d:response><d:href>{ROOT}{quote(prefix + d)}/</d:href><d:propstat><d:prop>'
                           '<d:resourcetype><d:collection/></d:resourcetype></d:prop></d:propstat></d:response>')
            for name in entries:
                out.append(f'<d:response><d:href>{ROOT}{quote(name)}</d:href><d:propstat><d:prop>'
                           '<d:resourcetype/></d:prop></d:propstat></d:response>')
            return HTTPResponse(207, ('<d:multistatus xmlns:d="DAV:">' + "".join(out)
                                      + "</d:multistatus>").encode(), None)
        if method == "GET":
            if rel not in self.files:
                return HTTPResponse(404, b"", None)
            data = self.files[rel]
            return HTTPResponse(200, data, "W/" + etag_of(data) if self.weak else etag_of(data))
        if method == "PUT":
            if self.before_put:
                self.before_put()
            if rel not in self.files or headers.get("If-Match") != etag_of(self.files[rel]):
                return HTTPResponse(412, b"", None)
            if self.put_raises:
                self.files[rel] = body
                raise PromotionTransportNetworkError("synthetic loss after effect")
            if not self.put_ignored:
                self.files[rel] = body
            return HTTPResponse(204, b"", None)
        raise AssertionError(method)


@pytest.fixture
def vault():
    v = Vault()
    v.files[f"{FOLDER}/a.md"] = NOTE.encode()
    v.files[f"{FOLDER}/sub/b.md"] = NOTE.encode()
    v.files[f"{FOLDER}/.hidden.md"] = NOTE.encode()
    v.files[f"{FOLDER}/readme.txt"] = b"x"
    v.files["Other/c.md"] = NOTE.encode()
    return v


def test_listing_is_confined_recursive_and_skips_hidden_and_non_markdown(store, vault):
    assert list_project_notes(shared=store, transport=vault) == [f"{FOLDER}/a.md", f"{FOLDER}/sub/b.md"]
    assert all(c[0] == "PROPFIND" for c in vault.calls)


def test_an_absent_project_folder_is_an_empty_list(store):
    assert list_project_notes(shared=store, transport=Vault()) == []


def test_set_status_changes_one_line_with_if_match_and_reads_back(store, vault):
    path = f"{FOLDER}/a.md"
    result = set_status(path, "running", "planning", shared=store, transport=vault)
    assert result["outcome"] == "updated"
    assert vault.files[path] == NOTE.replace("status: running", "status: planning").encode()
    put = next(c for c in vault.calls if c[0] == "PUT")
    assert put[2]["If-Match"] == etag_of(NOTE.encode())
    assert vault.calls[-1][0] == "GET"  # exact-byte readback came last


def test_an_edit_by_the_owner_between_read_and_write_is_never_overwritten(store, vault):
    path = f"{FOLDER}/a.md"
    owner = (NOTE + "owner edit\n").encode()

    def edit():
        vault.files[path] = owner
    vault.before_put = edit
    with pytest.raises(ProjectVaultError, match="note_changed"):
        set_status(path, "running", "planning", shared=store, transport=vault)
    assert vault.files[path] == owner


def test_expected_bytes_that_no_longer_match_stop_before_any_write(store, vault):
    path = f"{FOLDER}/a.md"
    seen = read_note(path, shared=store, transport=vault)
    vault.files[path] = (NOTE + "x").encode()
    with pytest.raises(ProjectVaultError, match="note_changed"):
        set_status(path, "running", "planning", shared=store, transport=vault, expected=seen)
    assert not any(c[0] == "PUT" for c in vault.calls)


def test_weak_etag_and_unsafe_notes_are_refused(store, vault):
    path = f"{FOLDER}/a.md"
    vault.weak = True
    with pytest.raises(ProjectVaultError, match="etag_not_strong"):
        set_status(path, "running", "planning", shared=store, transport=vault)
    vault.weak = False
    vault.files[path] = b"no front matter\n"
    with pytest.raises(ProjectVaultError, match="status_edit_refused"):
        set_status(path, "running", "planning", shared=store, transport=vault)
    vault.files[path] = NOTE.encode()
    with pytest.raises(ProjectVaultError, match="status_edit_refused"):
        set_status(path, "stable", "planning", shared=store, transport=vault)  # not the value read
    assert not any(c[0] == "PUT" for c in vault.calls)


@pytest.mark.parametrize("bad", ["../x.md", f"{FOLDER}/../x.md", "/abs.md", f"{FOLDER}/a.txt",
                                 f"{FOLDER}/.h.md", "Other/c.md", f"{FOLDER}x/a.md", 5])
def test_the_path_guard_confines_every_operation(store, vault, bad):
    with pytest.raises(ProjectVaultError, match="invalid_target"):
        set_status(bad, "running", "planning", shared=store, transport=vault)
    with pytest.raises(ProjectVaultError, match="invalid_target"):
        read_note(bad, shared=store, transport=vault)
    assert vault.calls == []


def test_a_lost_response_after_a_successful_write_is_recovered_by_readback(store, vault):
    vault.put_raises = True
    result = set_status(f"{FOLDER}/a.md", "running", "planning", shared=store, transport=vault)
    assert result["outcome"] == "recovered"


def test_a_write_that_silently_did_not_happen_is_a_mismatch_not_a_success(store, vault):
    vault.put_ignored = True
    with pytest.raises(ProjectVaultError, match="readback_mismatch"):
        set_status(f"{FOLDER}/a.md", "running", "planning", shared=store, transport=vault)


def test_the_root_pin_guard_must_pass_and_the_connection_must_not_change(store, vault):
    def refuse():
        raise RootPinRefused("x")
    with pytest.raises(ProjectVaultError, match="vault_root_refused"):
        list_project_notes(shared=store, guard=refuse, transport=vault)
    assert vault.calls == []
    path = f"{FOLDER}/a.md"

    def swap():
        changed = dict(SOURCE, vault_folder="Other")
        assert store.save_nextcloud(changed)
    vault.before_put = swap
    with pytest.raises(ProjectVaultError, match="source_changed"):
        set_status(path, "running", "planning", shared=store, transport=vault)
