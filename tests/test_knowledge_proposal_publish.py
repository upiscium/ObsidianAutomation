"""B1-lite phase 2: create-only Vault delivery of a proposal. Fully synthetic WebDAV."""
from __future__ import annotations

import json
from urllib.parse import unquote, urlsplit

import pytest

from obsidian_automation import knowledge_proposal as kp
from obsidian_automation import knowledge_proposal_publish as pub
from obsidian_automation.core_promotion_transport import (
    HTTPResponse, PromotionTransportNetworkError,
)
from obsidian_automation.knowledge_proposal import Proposal
from obsidian_automation.knowledge_proposal_publish import (
    MAX_PROPOSAL_BYTES, ProposalJournal, PublishError, publish_proposal, recipe_key,
)
from obsidian_automation.snapshot_setup import SetupStore
from obsidian_automation.vault_root_activation import RootPinRefused

SOURCE = {"webdav_url": "https://cloud.invalid/remote.php/dav/files/user/",
          "vault_folder": "Vault", "nextcloud_user": "user",
          "nextcloud_password": "NC-SYNTHETIC"}
ROOT = "/remote.php/dav/files/user/Vault/"
NOTE = ("---\ntype: knowledge-proposal\nstatus: proposed\ncategory: x\nmaturity: draft\n"
        "source_type: y\n---\n\n```meta-bind-embed\n[[knowledge-proposal-meta]]\n```\n\n"
        "A durable note about WebDAV.\n").encode()
PATH = "04-AI/Proposals/2026-11-26-webdav-note.md"


def make_proposal(content=NOTE, path=PATH):
    return Proposal(target_path=path, content=content, context_sha256="a" * 64,
                    proposal_sha256="b" * 64, generation_sha256="c" * 64,
                    model_identifier="m", source_sha256="d" * 64)


class FakeVault:
    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.dirs = {""}
        self.calls: list[tuple] = []
        self.put_raises = False
        self.store_instead: bytes | None = None
        self.weak = False
        self.before_get = None

    def _rel(self, url):
        raw = unquote(urlsplit(url).path)
        assert raw.startswith(ROOT), raw
        return raw[len(ROOT):]

    def __call__(self, *, method, target_url, username, password, headers, body,
                 timeout, response_limit):
        assert (username, password) == ("user", "NC-SYNTHETIC")
        rel = self._rel(target_url)
        self.calls.append((method, rel, dict(headers or {}), body))
        if method == "PROPFIND":
            if rel.rstrip("/") not in {d.rstrip("/") for d in self.dirs} and rel != "":
                return HTTPResponse(404, b"", None)
            href = urlsplit(target_url).path
            xml = ('<d:multistatus xmlns:d="DAV:"><d:response>'
                   f'<d:href>{href}</d:href><d:propstat><d:prop>'
                   '<d:resourcetype><d:collection/></d:resourcetype></d:prop>'
                   '<d:status>HTTP/1.1 200 OK</d:status></d:propstat>'
                   '</d:response></d:multistatus>').encode()
            return HTTPResponse(207, xml, None)
        if method == "MKCOL":
            parent = "/".join(rel.rstrip("/").split("/")[:-1])
            if parent and parent not in {d.rstrip("/") for d in self.dirs}:
                return HTTPResponse(409, b"", None)
            if rel in self.dirs:
                return HTTPResponse(405, b"", None)
            self.dirs.add(rel)
            return HTTPResponse(201, b"", None)
        if method == "PUT":
            assert headers["If-None-Match"] == "*"
            if rel in self.files:
                return HTTPResponse(412, b"", None)
            if self.put_raises:
                self.files[rel] = body
                raise PromotionTransportNetworkError("synthetic loss after effect")
            self.files[rel] = self.store_instead if self.store_instead is not None else body
            return HTTPResponse(201, b"", None)
        if method == "GET":
            if self.before_get:
                self.before_get()
            if rel not in self.files:
                return HTTPResponse(404, b"", None)
            return HTTPResponse(200, self.files[rel], 'W/"x"' if self.weak else '"strong"')
        raise AssertionError(method)


@pytest.fixture
def store(tmp_path):
    shared = SetupStore(tmp_path / "state", probe=lambda _: True, obscure=lambda _: "encoded")
    shared.nextcloud_probe = lambda _: True
    assert shared.save_nextcloud(dict(SOURCE))
    return shared


def publish(store, dav, proposal=None, **kwargs):
    return publish_proposal(proposal or make_proposal(), shared=store, transport=dav, **kwargs)


def test_creates_parents_and_the_note_with_a_conditional_put_and_readback(store):
    dav = FakeVault()
    receipt = publish(store, dav)
    assert receipt["outcome"] == "created" and receipt["vault_write"] is True
    assert dav.files[PATH] == NOTE
    assert [c[1] for c in dav.calls if c[0] == "MKCOL"] == ["04-AI/", "04-AI/Proposals/"]
    puts = [c for c in dav.calls if c[0] == "PUT"]
    assert len(puts) == 1 and puts[0][2]["If-None-Match"] == "*"
    assert dav.calls[-1][0] == "GET"  # the result is only reported after a readback


def test_an_existing_different_file_is_never_overwritten(store):
    dav = FakeVault()
    dav.dirs |= {"04-AI/", "04-AI/Proposals/"}
    dav.files[PATH] = b"mine, do not touch"
    with pytest.raises(PublishError, match="proposal_exists"):
        publish(store, dav)
    assert dav.files[PATH] == b"mine, do not touch"


def test_identical_existing_bytes_are_reported_as_already_present_without_a_write(store):
    dav = FakeVault()
    dav.dirs |= {"04-AI/", "04-AI/Proposals/"}
    dav.files[PATH] = NOTE
    receipt = publish(store, dav)
    assert receipt["outcome"] == "already_present" and receipt["vault_write"] is False


def test_a_lost_response_after_the_write_is_recovered_by_the_readback(store):
    dav = FakeVault()
    dav.put_raises = True
    assert publish(store, dav)["outcome"] == "recovered"
    assert dav.files[PATH] == NOTE


def test_a_lost_response_with_nothing_written_is_unconfirmed_not_success(store):
    class Lossy(FakeVault):
        def __call__(self, **kwargs):
            if kwargs["method"] == "PUT":
                self.calls.append(("PUT", self._rel(kwargs["target_url"]), {}, None))
                raise PromotionTransportNetworkError("synthetic loss before effect")
            return super().__call__(**kwargs)
    dav = Lossy()
    with pytest.raises(PublishError, match="outcome_unconfirmed"):
        publish(store, dav)
    assert PATH not in dav.files


def test_a_readback_that_does_not_match_is_never_reported_as_success(store):
    dav = FakeVault()
    dav.store_instead = NOTE + b"tampered"
    with pytest.raises(PublishError, match="readback_mismatch"):
        publish(store, dav)


def test_a_weak_etag_is_fine_because_the_exact_bytes_are_what_prove_the_create(store):
    dav = FakeVault()
    dav.weak = True  # Apache without FileETag Digest answers a fresh write this way
    assert publish(store, dav)["outcome"] == "created"


def test_a_second_managed_embed_or_a_missing_one_is_refused(store):
    dav = FakeVault()
    embed = kp.PROPOSAL_EMBED.encode()
    with pytest.raises(PublishError, match="active_content"):
        publish(store, dav, make_proposal(content=NOTE + embed))
    with pytest.raises(PublishError, match="not_a_proposal"):
        publish(store, dav, make_proposal(content=NOTE.replace(embed, b"")))
    assert dav.calls == []


def test_a_refused_root_pin_stops_before_any_request(store):
    dav = FakeVault()

    def refuse():
        raise RootPinRefused("vault_root_pin_refused")

    with pytest.raises(PublishError, match="vault_root_refused"):
        publish(store, dav, guard=refuse)
    assert dav.calls == []


@pytest.mark.parametrize("path", [
    "11-Knowledge/x.md", "04-AI/x.md", "04-AI/Proposals/sub/x.md", "04-AI/Proposals/../x.md",
    "04-AI/Proposals/x.txt", "04-AI/Proposals/.hidden.md", "/04-AI/Proposals/x.md",
    "04-AI/Proposals/", "04-AI/Proposals/a..b.md",
])
def test_only_a_markdown_file_directly_in_the_fixed_folder_is_admitted(store, path):
    dav = FakeVault()
    with pytest.raises(PublishError, match="invalid_target"):
        publish(store, dav, make_proposal(path=path))
    assert dav.calls == []


@pytest.mark.parametrize("content,code", [
    (b"", "invalid_content"),
    (b"\xff\xfe not utf8", "invalid_content"),
    (b"x" * (MAX_PROPOSAL_BYTES + 1), "invalid_content"),
    (b"just text", "not_a_proposal"),
    (b"---\ntype: note\n---\nbody\n", "not_a_proposal"),
    (NOTE + b"\n```dataview\nLIST\n```\n", "active_content"),
    (NOTE + b"\n[x](obsidian://open)\n", "active_content"),
])
def test_a_bad_proposal_is_refused_before_any_request(store, content, code):
    dav = FakeVault()
    with pytest.raises(PublishError, match=code):
        publish(store, dav, make_proposal(content=content))
    assert dav.calls == []


def test_an_unconfigured_vault_source_is_refused(tmp_path):
    empty = SetupStore(tmp_path / "empty", probe=lambda _: True, obscure=lambda _: "e")
    dav = FakeVault()
    with pytest.raises(PublishError, match="source_not_configured"):
        publish(empty, dav)
    assert dav.calls == []


def test_a_source_swapped_during_the_write_is_not_reported_as_success(store):
    dav = FakeVault()
    other = dict(SOURCE, vault_folder="Other")

    def swap():
        assert store.save_nextcloud(other)

    dav.before_get = swap
    with pytest.raises(PublishError, match="source_changed"):
        publish(store, dav)


def test_error_codes_never_carry_credentials_or_note_text(store):
    dav = FakeVault()
    dav.files[PATH] = b"x"
    dav.dirs |= {"04-AI/", "04-AI/Proposals/"}
    with pytest.raises(PublishError) as exc:
        publish(store, dav)
    assert "NC-SYNTHETIC" not in str(exc.value) and "durable note" not in str(exc.value)


def test_journal_remembers_a_proposal_and_the_key_depends_on_input_and_model(tmp_path):
    journal = ProposalJournal(tmp_path / "kp")
    try:
        key = recipe_key("d" * 64, "model-a")
        assert journal.lookup(key) is None
        journal.record(key, {"target_path": PATH, "content_sha256": "e" * 64})
        journal.record(key, {"target_path": "04-AI/Proposals/other.md", "content_sha256": "f" * 64})
        assert journal.lookup(key)["target_path"] == PATH  # the first record stands
        assert recipe_key("d" * 64, "model-b") != key
        assert recipe_key("1" * 64, "model-a") != key
    finally:
        journal.close()


# ------------------------------------------------------------------- CLI

def _cli_zip_args(tmp_path):
    return ["propose", "--zip", str(tmp_path / "x.zip"), "--provider", "claude", "--index", "0",
            "--ai-root", str(tmp_path / "ai"), "--base-url", "https://llm.invalid/v1",
            "--model", "m"]


def test_cli_publish_and_out_cannot_be_combined_and_state_must_be_absolute(tmp_path, capsys):
    base = _cli_zip_args(tmp_path)
    for extra in (["--publish"], ["--publish", "--state", "relative"],
                  ["--publish", "--state", str(tmp_path), "--out", str(tmp_path / "o")],
                  ["--state", str(tmp_path), "--out", str(tmp_path / "o")]):
        assert kp.main(base + extra) == 1
        assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["error_code"] == "invalid_arguments"


def test_cli_publish_creates_once_and_never_regenerates_the_same_input(tmp_path, monkeypatch, capsys):
    calls = {"propose": 0, "publish": 0}

    def fake_propose(ai_root, source, **kwargs):
        calls["propose"] += 1
        return make_proposal()

    def fake_publish(proposal, *, shared, guard=None, **kwargs):
        calls["publish"] += 1
        assert guard is not None
        return {"status": "published", "outcome": "created", "target_path": PATH,
                "content_sha256": "e" * 64, "bytes": len(NOTE), "vault_write": True}

    monkeypatch.setattr(kp, "propose", fake_propose)
    monkeypatch.setattr(pub, "publish_proposal", fake_publish)
    args = argparse_namespace(tmp_path)
    source = kp.SourceText("private-claude", "a conversation")
    assert kp._publish(args, source, None) == 0
    first = json.loads(capsys.readouterr().out)
    assert first == {"bytes": len(NOTE), "file": "2026-11-26-webdav-note.md",
                     "outcome": "created", "status": "proposed", "vault_write": True}
    assert kp._publish(args, source, None) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["status"] == "already_proposed" and second["vault_write"] is False
    assert calls == {"propose": 1, "publish": 1}


def test_cli_publish_failure_is_reported_and_not_journaled(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(kp, "propose", lambda *a, **k: make_proposal())

    def failing(proposal, **kwargs):
        raise PublishError("outcome_unconfirmed")

    monkeypatch.setattr(pub, "publish_proposal", failing)
    args = argparse_namespace(tmp_path)
    source = kp.SourceText("private-claude", "a conversation")
    assert kp._publish(args, source, None) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["error_code"] == "outcome_unconfirmed" and report["vault_write"] == "unknown"
    journal = ProposalJournal(tmp_path / "state" / "knowledge-proposals")
    try:
        import hashlib
        key = recipe_key(hashlib.sha256(b"a conversation").hexdigest(), "m")
        assert journal.lookup(key) is None  # a failed attempt can be retried
    finally:
        journal.close()


def argparse_namespace(tmp_path):
    import argparse
    return argparse.Namespace(state=tmp_path / "state", ai_root=tmp_path / "ai",
                              base_url="https://llm.invalid/v1", model="m", timeout=5.0)
