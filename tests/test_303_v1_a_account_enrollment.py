"""#303 gate A: account enrollment and the trusted picker context (synthetic)."""
from __future__ import annotations

import json
import stat

import pytest

from obsidian_automation import vault_root_enrollment as en
from obsidian_automation import vault_root_dav_verifier as v
from obsidian_automation.snapshot_setup import SetupStore
from obsidian_automation.vault_root_selection import VaultRootSelectionService
from test_303_v1b2_dav_verifier import FakeNextcloud, INSTANCE, ORIGIN, PREFIX, oc_id
from test_303_v1d_vault_root_activation import NC, RESTRICTED, activate


class EnrollServer:
    def __init__(self, *, principal="synthetic_user", fileid=1, instance=INSTANCE,
                 mount="", collection=True, status=207, redirected=False):
        self.principal, self.fileid, self.instance = principal, fileid, instance
        self.mount, self.collection = mount, collection
        self.status, self.redirected = status, redirected
        self.calls = []

    def propfind(self, origin, href, depth, body, *, timeout, max_bytes):
        assert origin == ORIGIN and body == en.ENROLL_BODY and depth == "0"
        self.calls.append(href)
        rtype = "<d:collection/>" if self.collection else ""
        xml = ('<?xml version="1.0" encoding="utf-8"?><d:multistatus xmlns:d="DAV:" '
               'xmlns:oc="http://owncloud.org/ns" xmlns:nc="http://nextcloud.org/ns">'
               f'<d:response><d:href>{PREFIX}</d:href><d:propstat><d:prop>'
               f'<d:resourcetype>{rtype}</d:resourcetype>'
               '<d:current-user-principal><d:href>/remote.php/dav/principals/users/'
               f'{self.principal}/</d:href></d:current-user-principal>'
               f'<oc:fileid>{self.fileid}</oc:fileid>'
               f'<oc:id>{str(self.fileid).zfill(8)}{self.instance}</oc:id>'
               f'<nc:mount-type>{self.mount}</nc:mount-type>'
               '</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>'
               '</d:multistatus>')
        return v.DavResponse(self.status, "application/xml; charset=utf-8", "",
                             self.redirected, xml.encode())


@pytest.fixture
def store(tmp_path):
    s = SetupStore(tmp_path, obscure=lambda _: "encoded")
    assert s.save_nextcloud(dict(NC), probed=True)
    return s


def test_enrollment_refused_without_io_while_register_unproven(store):
    server = EnrollServer()
    assert en.enroll_account(store, server) == ("provider_unproven", None)
    assert en.enroll_account(store, server, proof=v.CURRENT_PROVIDER_PROOF)[0] == \
        "provider_unproven"
    assert server.calls == []
    assert not (store.state / en.ENROLLMENT_FILE).exists()


def test_enrollment_records_instance_root_and_user(store):
    server = EnrollServer()
    status, record = en.enroll_account(store, server, proof=RESTRICTED,
                                       clock=lambda: 1_700_000_000)
    assert status == "enrolled"
    assert record.instance_id == INSTANCE and record.root_resource_id == oc_id(1)
    assert record.user_id == "synthetic_user" and server.calls == [PREFIX]
    path = store.state / en.ENROLLMENT_FILE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    text = path.read_text()
    assert "NC-PRIVATE" not in text and "nextcloud.invalid" not in text
    assert en.load_enrollment(store) == record


@pytest.mark.parametrize("server,reason", [
    (EnrollServer(principal="someone_else"), "account_mismatch"),
    (EnrollServer(instance="BAD!"), "identity_unverified"),
    (EnrollServer(mount="shared"), "account_unverified"),
    (EnrollServer(collection=False), "account_unverified"),
    (EnrollServer(status=401), "provider_unavailable"),
    (EnrollServer(redirected=True), "provider_unavailable"),
])
def test_enrollment_fails_closed(store, server, reason):
    assert en.enroll_account(store, server, proof=RESTRICTED) == (reason, None)
    assert not (store.state / en.ENROLLMENT_FILE).exists()


def test_transport_error_is_unavailable(store):
    class Broken:
        def propfind(self, *a, **k):
            raise v.DavTransportError("timeout")
    assert en.enroll_account(store, Broken(), proof=RESTRICTED)[0] == "provider_unavailable"


@pytest.mark.parametrize("change", ["user", "url", "mode", "garbage"])
def test_record_is_only_valid_for_its_source(store, change):
    assert en.enroll_account(store, EnrollServer(), proof=RESTRICTED)[0] == "enrolled"
    path = store.state / en.ENROLLMENT_FILE
    if change == "user":
        assert store.save_nextcloud({**NC, "nextcloud_user": "other"}, probed=True)
    elif change == "url":
        assert store.save_nextcloud({**NC, "webdav_url":
                                     "https://other.invalid/remote.php/dav/files/synthetic_user/"},
                                    probed=True)
    elif change == "mode":
        path.chmod(0o644)
    else:
        path.write_text("{")
    assert en.load_enrollment(store) is None


def test_credential_only_change_keeps_enrollment(store):
    assert en.enroll_account(store, EnrollServer(), proof=RESTRICTED)[0] == "enrolled"
    assert store.save_nextcloud({**NC, "nextcloud_password": "rotated"}, probed=True)
    assert en.load_enrollment(store) is not None


def provider(store, nc, busy=False, proof=RESTRICTED):
    return en.context_provider(store, lambda saved: nc, jobs_busy=lambda: busy, proof=proof)


def test_context_needs_enrollment(store):
    nc = FakeNextcloud()
    assert provider(store, nc)() is None
    assert nc.calls == []


def test_context_reads_active_root_and_binds_account(store):
    assert en.enroll_account(store, EnrollServer(), proof=RESTRICTED)[0] == "enrolled"
    nc = FakeNextcloud()
    ctx = provider(store, nc)()
    assert ctx.account == v.TrustedDavAccount(ORIGIN, PREFIX, INSTANCE)
    assert ctx.active == "Other" and ctx.active_resource_id == oc_id(20)
    assert ctx.container_resource_id == oc_id(1)
    assert ctx.source_revision == store.nextcloud_revision()
    assert ctx.jobs_in_flight is False
    assert nc.calls == [(PREFIX + "Other/", "0")]
    assert provider(store, nc, busy=True)().jobs_in_flight is True


def test_context_uses_pin_when_pinned(store):
    assert en.enroll_account(store, EnrollServer(), proof=RESTRICTED)[0] == "enrolled"
    assert activate(store, FakeNextcloud()).status == "activated"
    nc = FakeNextcloud()
    ctx = provider(store, nc)()
    assert ctx.active == "Vault/Notes" and ctx.active_resource_id == oc_id(11)
    assert nc.calls == []


def test_context_unavailable_when_active_root_unreadable(store):
    assert en.enroll_account(store, EnrollServer(), proof=RESTRICTED)[0] == "enrolled"
    assert provider(store, FakeNextcloud(), proof=v.CURRENT_PROVIDER_PROOF)() is None


def test_picker_end_to_end_with_enrolled_context(store):
    assert en.enroll_account(store, EnrollServer(), proof=RESTRICTED)[0] == "enrolled"
    nc = FakeNextcloud()
    svc = VaultRootSelectionService(context=provider(store, nc), transport=nc,
                                    proof=RESTRICTED, restricted_coherence=True)
    listing = svc.candidates({})[1]
    vault = next(c for c in listing["candidates"] if c["label"] == "Vault")
    assert svc.preview({"candidate": vault["handle"]})[1]["status"] == "candidate_valid"
    assert json.dumps(listing).count(INSTANCE) == 0
