"""#303 gate A: account enrollment and the trusted picker context (synthetic)."""
from __future__ import annotations

from dataclasses import replace
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
    def __init__(self, *, ocs_id="synthetic_user", fileid=1, instance=INSTANCE,
                 mount="", collection=True, status=207, redirected=False, mutate=None):
        self.ocs_id, self.fileid, self.instance = ocs_id, fileid, instance
        self.ocs_status, self.ocs_body = 200, None
        self.mount, self.collection = mount, collection
        self.status, self.redirected = status, redirected
        self.mutate = mutate
        self.calls = []

    def propfind(self, origin, href, depth, body, *, timeout, max_bytes):
        assert origin == ORIGIN and body == en.ENROLL_BODY and depth == "0"
        self.calls.append(href)
        rtype = "<d:collection/>" if self.collection else ""
        xml = ('<?xml version="1.0" encoding="utf-8"?><d:multistatus xmlns:d="DAV:" '
               'xmlns:oc="http://owncloud.org/ns" xmlns:nc="http://nextcloud.org/ns">'
               f'<d:response><d:href>{PREFIX}</d:href><d:propstat><d:prop>'
               f'<d:resourcetype>{rtype}</d:resourcetype>'
               f'<oc:fileid>{self.fileid}</oc:fileid>'
               f'<oc:id>{str(self.fileid).zfill(8)}{self.instance}</oc:id>'
               f'<nc:mount-type>{self.mount}</nc:mount-type>'
               '</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>'
               '</d:multistatus>')
        if self.mutate:
            xml = self.mutate(xml)
        return v.DavResponse(self.status, "application/xml; charset=utf-8", "",
                             self.redirected, xml.encode())

    def ocs_current_user(self, origin, *, timeout, max_bytes):
        assert origin == ORIGIN
        self.calls.append("ocs")
        body = self.ocs_body if self.ocs_body is not None else json.dumps(
            {"ocs": {"meta": {"status": "ok", "statuscode": 100},
                     "data": {"id": self.ocs_id}}}).encode()
        return v.DavResponse(self.ocs_status, "application/json", "", False, body)


@pytest.fixture
def store(tmp_path):
    s = SetupStore(tmp_path, obscure=lambda _: "encoded")
    assert s.save_nextcloud(dict(NC), probed=True)
    return s


def test_enrollment_refused_without_io_while_register_unproven(store):
    server = EnrollServer()
    from test_303_v1b2_dav_verifier import UNPROVEN
    assert en.enroll_account(store, lambda saved: server, proof=UNPROVEN) == (
        "provider_unproven", None)
    assert en.enroll_account(store, lambda saved: server,
                             proof=replace(RESTRICTED, A="UNKNOWN"))[0] == "provider_unproven"
    assert server.calls == []
    assert not (store.state / en.ENROLLMENT_FILE).exists()


def test_enrollment_records_instance_root_and_user(store):
    server = EnrollServer()
    status, record = en.enroll_account(store, lambda saved: server, proof=RESTRICTED,
                                       clock=lambda: 1_700_000_000)
    assert status == "enrolled"
    assert record.instance_id == INSTANCE and record.root_resource_id == oc_id(1)
    assert record.user_id == "synthetic_user" and server.calls == [PREFIX, "ocs"]
    path = store.state / en.ENROLLMENT_FILE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    text = path.read_text()
    assert "NC-PRIVATE" not in text and "nextcloud.invalid" not in text
    assert en.load_enrollment(store) == record


@pytest.mark.parametrize("server,reason", [
    (EnrollServer(ocs_id="someone_else"), "account_mismatch"),
    # P3-A: another user's files root answers 207 with no oc:fileid / oc:id
    (EnrollServer(mutate=lambda x: x.replace("<oc:fileid>1</oc:fileid>", "<oc:fileid/>")
                  .replace(f"<oc:id>00000001{INSTANCE}</oc:id>", "<oc:id/>")),
     "identity_unverified"),
    (EnrollServer(instance="BAD!"), "identity_unverified"),
    (EnrollServer(instance="UPPERCASE1"), "identity_unverified"),   # _INSTANCE regex
    (EnrollServer(mutate=lambda x: x.replace("<oc:fileid>1<", "<oc:fileid>2<")),
     "identity_unverified"),
    (EnrollServer(mutate=lambda x: x.replace("</d:multistatus>", x[x.index("<d:response>"):
                                             x.index("</d:multistatus>")] + "</d:multistatus>")),
     "account_unverified"),
    (EnrollServer(mutate=lambda x: x.replace(f"<d:href>{PREFIX}</d:href>",
                                             "<d:href>/remote.php/dav/files/other/</d:href>")),
     "account_unverified"),
    (EnrollServer(mount="shared"), "account_unverified"),
    (EnrollServer(collection=False), "account_unverified"),
    (EnrollServer(status=401), "provider_unavailable"),
    (EnrollServer(redirected=True), "provider_unavailable"),
])
def test_enrollment_fails_closed(store, server, reason):
    assert en.enroll_account(store, lambda saved: server, proof=RESTRICTED) == (reason, None)
    assert not (store.state / en.ENROLLMENT_FILE).exists()


def test_transport_error_is_unavailable(store):
    class Broken:
        def propfind(self, *a, **k):
            raise v.DavTransportError("timeout")
    assert en.enroll_account(store, lambda saved: Broken(), proof=RESTRICTED)[0] == "provider_unavailable"


@pytest.mark.parametrize("change", ["user", "url", "mode", "garbage"])
def test_record_is_only_valid_for_its_source(store, change):
    assert en.enroll_account(store, lambda saved: EnrollServer(), proof=RESTRICTED)[0] == "enrolled"
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
    assert en.enroll_account(store, lambda saved: EnrollServer(), proof=RESTRICTED)[0] == "enrolled"
    assert store.save_nextcloud({**NC, "nextcloud_password": "rotated"}, probed=True)
    assert en.load_enrollment(store) is not None


def provider(store, nc, busy=False, proof=RESTRICTED):
    return en.context_provider(store, lambda saved: nc, jobs_busy=lambda: busy, proof=proof)


def test_context_needs_enrollment(store):
    nc = FakeNextcloud()
    assert provider(store, nc)() == "not_enrolled"
    assert nc.calls == []


def test_context_reads_active_root_and_binds_account(store):
    assert en.enroll_account(store, lambda saved: EnrollServer(), proof=RESTRICTED)[0] == "enrolled"
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
    assert en.enroll_account(store, lambda saved: EnrollServer(), proof=RESTRICTED)[0] == "enrolled"
    assert activate(store, FakeNextcloud()).status == "activated"
    nc = FakeNextcloud()
    ctx = provider(store, nc)()
    assert ctx.active == "Vault/Notes" and ctx.active_resource_id == oc_id(11)
    assert nc.calls == []


def test_context_unavailable_when_active_root_unreadable(store):
    assert en.enroll_account(store, lambda saved: EnrollServer(), proof=RESTRICTED)[0] == "enrolled"
    from test_303_v1b2_dav_verifier import UNPROVEN
    assert provider(store, FakeNextcloud(), proof=UNPROVEN)() == "active_root_unreadable"


def test_picker_end_to_end_with_enrolled_context(store):
    assert en.enroll_account(store, lambda saved: EnrollServer(), proof=RESTRICTED)[0] == "enrolled"
    nc = FakeNextcloud()
    svc = VaultRootSelectionService(context=provider(store, nc), transport=nc,
                                    proof=RESTRICTED, restricted_coherence=True)
    listing = svc.candidates({})[1]
    vault = next(c for c in listing["candidates"] if c["label"] == "Vault")
    assert svc.preview({"candidate": vault["handle"]})[1]["status"] == "candidate_valid"
    assert json.dumps(listing).count(INSTANCE) == 0


def test_login_name_must_equal_user_id(tmp_path):
    s = SetupStore(tmp_path, obscure=lambda _: "encoded")
    assert s.save_nextcloud({**NC, "nextcloud_user": "login.alias"}, probed=True)
    assert en.enroll_account(s, lambda saved: EnrollServer(), proof=RESTRICTED) == \
        ("account_mismatch", None)


def test_transport_is_built_from_saved_credentials(store):
    seen = []
    assert en.enroll_account(store, lambda saved: seen.append(saved) or EnrollServer(),
                             proof=RESTRICTED)[0] == "enrolled"
    assert seen == [store.load_nextcloud()]


def test_reenrollment_refuses_a_different_instance(store):
    assert en.enroll_account(store, lambda saved: EnrollServer(), proof=RESTRICTED)[0] == "enrolled"
    before = (store.state / en.ENROLLMENT_FILE).read_bytes()
    assert en.enroll_account(store, lambda saved: EnrollServer(instance="ocother99"),
                             proof=RESTRICTED) == ("instance_changed", None)
    assert en.enroll_account(store, lambda saved: EnrollServer(fileid=2),
                             proof=RESTRICTED) == ("instance_changed", None)
    assert (store.state / en.ENROLLMENT_FILE).read_bytes() == before
    assert en.enroll_account(store, lambda saved: EnrollServer(), proof=RESTRICTED)[0] == "enrolled"


def test_symlinked_record_is_rejected(store, tmp_path):
    assert en.enroll_account(store, lambda saved: EnrollServer(), proof=RESTRICTED)[0] == "enrolled"
    path = store.state / en.ENROLLMENT_FILE
    target = tmp_path / "elsewhere.json"
    target.write_bytes(path.read_bytes())
    target.chmod(0o600)
    path.unlink()
    path.symlink_to(target)
    assert en.load_enrollment(store) is None


def test_pin_from_another_source_is_not_trusted(store):
    assert en.enroll_account(store, lambda saved: EnrollServer(), proof=RESTRICTED)[0] == "enrolled"
    assert activate(store, FakeNextcloud()).status == "activated"
    from obsidian_automation import vault_root_activation as act
    binding = store.state / act.BINDING_FILE
    data = json.loads(binding.read_text())
    data["source_sha256"] = "0" * 64
    binding.write_text(json.dumps(data))
    nc = FakeNextcloud()
    ctx = provider(store, nc)()
    assert nc.calls == [(PREFIX + "Vault/Notes/", "0")]      # re-read, pin ignored
    assert ctx.active_resource_id == oc_id(11)


def test_non_bool_busy_counts_as_busy(store):
    assert en.enroll_account(store, lambda saved: EnrollServer(), proof=RESTRICTED)[0] == "enrolled"
    ctx = en.context_provider(store, lambda saved: FakeNextcloud(), jobs_busy=lambda: None,
                              proof=RESTRICTED)()
    assert ctx.jobs_in_flight is True


def test_connection_saved_during_build_yields_no_context(store):
    assert en.enroll_account(store, lambda saved: EnrollServer(), proof=RESTRICTED)[0] == "enrolled"

    def factory(saved):
        assert store.save_nextcloud({**NC, "nextcloud_password": "rotated"}, probed=True)
        return FakeNextcloud()
    assert en.context_provider(store, factory, jobs_busy=lambda: False,
                               proof=RESTRICTED)() == "source_changed"


def test_fifo_records_are_rejected_without_blocking(store):
    import os
    import threading
    from obsidian_automation import vault_root_activation as act
    assert en.enroll_account(store, lambda saved: EnrollServer(), proof=RESTRICTED)[0] == "enrolled"
    for name in (en.ENROLLMENT_FILE, act.BINDING_FILE):
        path = store.state / name
        path.unlink(missing_ok=True)
        os.mkfifo(path, 0o600)
    results = []
    worker = threading.Thread(daemon=True, target=lambda: results.append((
        en.load_enrollment(store), act.current_pin(store.state),
        act.check_root_pin(store, lambda _: "x"))))
    worker.start()
    worker.join(5)
    assert not worker.is_alive(), "reading a FIFO record blocked"
    assert results == [(None, None, "mismatch")]


@pytest.mark.parametrize("body,status,reason", [
    (b'{"ocs":{"meta":{"statuscode":997},"data":{}}}', 200, "account_unverified"),
    (b'{"ocs":{"meta":{"statuscode":997},"data":{"id":"synthetic_user"}}}', 200,
     "account_unverified"),
    (b'{"ocs":{"meta":{"statuscode":100},"data":{"id":"a/b"}}}', 200, "account_unverified"),
    (b'not json', 200, "account_unverified"),
    (b'{}', 401, "provider_unavailable"),
])
def test_ocs_user_must_be_a_clean_ok_answer(store, body, status, reason):
    server = EnrollServer()
    server.ocs_body, server.ocs_status = body, status
    assert en.enroll_account(store, lambda saved: server, proof=RESTRICTED) == (reason, None)


def test_transport_without_ocs_read_cannot_enroll(store):
    class PropfindOnly:
        def propfind(self, *a, **k):
            pytest.fail("no I/O expected")
    assert en.enroll_account(store, lambda saved: PropfindOnly(), proof=RESTRICTED) == (
        "provider_unavailable", None)


def test_enroll_body_no_longer_asks_for_principal():
    assert b"current-user-principal" not in en.ENROLL_BODY


def test_ocs_user_must_match_the_url_user_segment(tmp_path):
    # Login equals the OCS ID, but the saved URL names another user's files root.
    other = "https://nextcloud.invalid/remote.php/dav/files/other_user/"
    s = SetupStore(tmp_path, obscure=lambda _: "encoded")
    assert s.save_nextcloud({**NC, "webdav_url": other}, probed=True)
    server = EnrollServer(mutate=lambda x: x.replace(PREFIX, "/remote.php/dav/files/other_user/"))
    assert en.enroll_account(s, lambda saved: server, proof=RESTRICTED) == ("account_mismatch", None)


@pytest.mark.parametrize("server", [
    EnrollServer(status=401),
    EnrollServer(mutate=lambda x: x.replace("<oc:fileid>1</oc:fileid>", "<oc:fileid/>")
                 .replace(f"<oc:id>00000001{INSTANCE}</oc:id>", "<oc:id/>")),
])
def test_ocs_is_not_called_when_the_root_read_fails(store, server):
    assert en.enroll_account(store, lambda saved: server, proof=RESTRICTED)[1] is None
    assert server.calls == [PREFIX]


def test_context_reports_a_shared_active_root(store):
    # Production finding (2026-11-13): the saved Vault is a received share.
    assert en.enroll_account(store, lambda saved: EnrollServer(), proof=RESTRICTED)[0] == "enrolled"
    nc = FakeNextcloud()
    nc.nodes["Other"] = (20, True, "shared")
    assert provider(store, nc)() == "active_root_shared"
    svc = VaultRootSelectionService(context=provider(store, nc), transport=nc,
                                    proof=RESTRICTED, restricted_coherence=True)
    assert svc.candidates({})[1]["reason"] == "active_root_shared"
    assert svc.preview({"candidate": "x"})[1]["reason"] == "active_root_shared"


def test_context_without_saved_connection(tmp_path):
    empty = SetupStore(tmp_path / "empty")
    assert en.context_provider(empty, lambda s: FakeNextcloud(), jobs_busy=lambda: False,
                               proof=RESTRICTED)() == "binding_unavailable"


def test_unknown_context_value_is_not_trusted():
    svc = VaultRootSelectionService(context=lambda: "made_up", transport=FakeNextcloud(),
                                    proof=RESTRICTED, restricted_coherence=True)
    assert svc.candidates({})[1]["reason"] == "binding_unavailable"
