"""Generic DAV must not inherit Nextcloud OC-ID/OCS root-pinning authority."""
import pytest

from obsidian_automation.snapshot_setup import SetupStore
from obsidian_automation.vault_root_activation import JobRootGuard, RootPinRefused
from obsidian_automation.vault_root_enrollment import (
    context_provider, enroll_account, load_enrollment,
)
from obsidian_automation.vault_root_webui import VaultRootActions
from obsidian_automation.generic_webdav_root import (
    enroll as enroll_generic, check as check_generic, is_enrolled,
)


SOURCE = {
    "webdav_url": "https://dav.example.invalid/webdav/",
    "vault_folder": "Vault",
    "nextcloud_user": "fixture-user",
    "nextcloud_password": "synthetic-passphrase",
}


def test_generic_dav_has_no_unverified_nextcloud_root_authority(tmp_path):
    store = SetupStore(tmp_path, obscure=lambda _: "fixture-obscured")
    assert store.save_nextcloud(SOURCE, probed=True)
    calls = []

    def never_factory(*args, **kwargs):
        calls.append(("transport", args, kwargs))
        raise AssertionError("Nextcloud-only OCS/OC-ID provider I/O forbidden")

    guard = JobRootGuard(store, transport_factory=never_factory)
    assert guard.status() == "provider_unverified"
    with pytest.raises(RootPinRefused, match="vault_root_pin_refused"):
        guard()
    assert load_enrollment(store) is None
    assert context_provider(
        store, never_factory, jobs_busy=lambda: False,
    )() == "provider_unsupported"

    actions = VaultRootActions(store, fence_factory=lambda: pytest.fail("unexpected fence"),
                               transport_factory=never_factory)
    assert not actions.enrolled()
    assert actions.activate(
        "Vault", "synthetic-id", "rev", ctx=None,
    ).reason == "provider_unsupported"
    assert actions.rollback().reason == "provider_unsupported"
    assert actions.can_rollback() is False
    assert not calls


def test_explicit_generic_path_enrollment_requires_fresh_collection(tmp_path):
    store=SetupStore(tmp_path, obscure=lambda _:"fixture-obscured")
    assert store.save_nextcloud(SOURCE,probed=True)
    assert not is_enrolled(store)
    actions=VaultRootActions(
        store, fence_factory=lambda: pytest.fail("unexpected provider effect"),
        generic_probe=lambda saved: saved["vault_folder"]=="Vault",
    )
    assert actions.is_generic()
    assert actions.enroll()=="enrolled"
    assert actions.enrolled()
    proof=store.state/"generic-webdav-root.json"
    assert proof.is_file() and proof.stat().st_mode & 0o777==0o600
    assert SOURCE["nextcloud_password"] not in proof.read_text()
    guard=JobRootGuard(store,generic_probe=lambda saved: True)
    assert guard.status()=="ok"
    guard()
    assert check_generic(store,probe=lambda _:False)=="provider_unavailable"
    assert JobRootGuard(store,generic_probe=lambda _:False).status()=="provider_unavailable"
    with pytest.raises(RootPinRefused):
        JobRootGuard(store,generic_probe=lambda _:False)()

    # Across restart the registered exact URL, user and root are durable.
    restarted=SetupStore(tmp_path)
    assert is_enrolled(restarted)
    assert JobRootGuard(restarted,generic_probe=lambda _:True).status()=="ok"
    assert actions.activate("Vault","fake","rev",ctx=None).reason=="provider_unsupported"
    assert actions.rollback().reason=="provider_unsupported"


def test_generic_path_enrollment_fails_closed_on_source_change(tmp_path):
    store=SetupStore(tmp_path,obscure=lambda _:"fixture-obscured")
    assert store.save_nextcloud(SOURCE,probed=True)
    assert enroll_generic(store,probe=lambda _:True)=="enrolled"
    assert is_enrolled(store)
    changed=dict(SOURCE,vault_folder="Other")
    assert store.save_nextcloud(changed,probed=True)
    assert not is_enrolled(store)
    assert check_generic(store,probe=lambda _:True)=="provider_unverified"
    assert JobRootGuard(store,generic_probe=lambda _:True).status()=="provider_unverified"


def test_concurrent_source_change_blocks_generic_root_enrollment(tmp_path):
    store=SetupStore(tmp_path,obscure=lambda _:"fixture-obscured")
    assert store.save_nextcloud(SOURCE,probed=True)
    def mutate_on_probe(_saved):
        assert store.save_nextcloud(dict(SOURCE,vault_folder="Changed"),probed=True)
        return True
    assert enroll_generic(store,probe=mutate_on_probe)=="source_changed"
    assert not is_enrolled(store)


def test_malformed_generic_root_record_never_authorizes_writer(tmp_path):
    store=SetupStore(tmp_path,obscure=lambda _:"fixture-obscured")
    assert store.save_nextcloud(SOURCE,probed=True)
    p=store.state/"generic-webdav-root.json"
    p.symlink_to(tmp_path/"elsewhere")
    assert not is_enrolled(store)
    with pytest.raises(RootPinRefused):
        JobRootGuard(store,generic_probe=lambda _:True)()


def test_legacy_unpinned_nextcloud_root_still_permitted(tmp_path):
    value = dict(SOURCE, webdav_url="https://cloud.example.invalid/remote.php/dav/files/user/")
    store = SetupStore(tmp_path, obscure=lambda _: "fixture-obscured")
    assert store.save_nextcloud(value, probed=True)
    guard = JobRootGuard(store, transport_factory=None)
    assert guard.status() == "legacy_unpinned"
    guard()


def test_generic_root_ui_explicit_enroll_but_no_ocid_picker(tmp_path):
    from obsidian_automation.vault_root_selection import VaultRootSelectionService

    store=SetupStore(tmp_path,obscure=lambda _:"fixture-obscured")
    assert store.save_nextcloud(SOURCE,probed=True)
    calls=[]
    actions=VaultRootActions(
        store,fence_factory=lambda: pytest.fail("no writer/activation"),
        transport_factory=lambda *_: pytest.fail("no Nextcloud OCS transport"),
        generic_probe=lambda saved:(calls.append("propfind") or True),
    )
    picker=VaultRootSelectionService(
        context=lambda:"provider_unsupported",transport=None,actions=actions,
    )
    before=picker.status()
    assert before["provider"]=="generic_unverified"
    assert before["enrolled"] is False
    assert before["activation"]=="not_available"
    code,result=picker.enroll({})
    assert code==200 and result["status"]=="enrolled"
    assert calls==["propfind"]
    after=picker.status()
    assert after["provider"]=="generic_https_path"
    assert after["enrolled"] is True
    assert after["activation"]=="not_available"
    assert after["can_rollback"] is False
    assert after["binding"]=="available"
    assert after["active_label"]=="Vault"


def test_generic_root_enrollment_not_resurrected_by_a_b_a_connection_change(tmp_path):
    store=SetupStore(tmp_path,obscure=lambda _:"fixture-obscured")
    assert store.save_nextcloud(SOURCE,probed=True)
    first_epoch=store.webdav_epoch.read_text()
    assert enroll_generic(store,probe=lambda _:True)=="enrolled"
    assert is_enrolled(store)
    assert store.save_nextcloud(dict(SOURCE,vault_folder="Other"),probed=True)
    assert not is_enrolled(store)
    assert store.save_nextcloud(SOURCE,probed=True)
    assert not is_enrolled(store), "returning to the same URL must require new enrollment"
    assert store.webdav_epoch.read_text()!=first_epoch
    assert enroll_generic(store,probe=lambda _:True)=="enrolled"
    assert is_enrolled(store)


def test_generic_root_refuses_unreadable_prior_nextcloud_pin(tmp_path):
    from obsidian_automation.snapshot_setup import _atomic
    store=SetupStore(tmp_path,obscure=lambda _:"fixture-obscured")
    assert store.save_nextcloud(SOURCE,probed=True)
    _atomic(store.state/"vault_root_pinning.enabled","1\n")
    # Presence of a prior pin marker without a valid reconciled binding
    # is an uncertain former identity and must never downgrade to URL trust.
    assert enroll_generic(store,probe=lambda _:True)=="prior_root_pin_requires_reset"
    assert not is_enrolled(store)
    assert JobRootGuard(store,generic_probe=lambda _:True).status()=="provider_unverified"


def test_generic_proof_fails_closed_when_epoch_file_unreadable(tmp_path):
    store=SetupStore(tmp_path,obscure=lambda _:"fixture-obscured")
    assert store.save_nextcloud(SOURCE,probed=True)
    assert enroll_generic(store,probe=lambda _:True)=="enrolled"
    assert is_enrolled(store)
    store.webdav_epoch.chmod(0o644)
    assert not is_enrolled(store)
    assert check_generic(store,probe=lambda _:True)=="provider_unverified"


def test_normal_connection_saved_record_without_pin_marker_can_enroll(tmp_path):
    """Copilot R1: a legitimate fresh generic config can have an unpinned binding."""
    from obsidian_automation.vault_root_activation import (
        _write_binding, _source_digest, RootPin,
    )
    store=SetupStore(tmp_path, obscure=lambda _:"fixture-obscured")
    assert store.save_nextcloud(SOURCE, probed=True)
    # This may be left by the connection-field save/fence even if OC-ID
    # enrollment has never started. It must not be mistaken for a prior pin.
    assert _write_binding(
        store.state,RootPin(SOURCE["vault_folder"],None,None),
        None,"connection_saved",clock=lambda:1.0,
        source=_source_digest(SOURCE))
    assert not (tmp_path/"vault_root_pinning.enabled").exists()
    assert enroll_generic(store,probe=lambda _:True)=="enrolled"
    assert is_enrolled(store)
    assert JobRootGuard(store,generic_probe=lambda _:True).status()=="ok"


def test_unpinned_record_without_marker_must_still_match_source_and_not_be_symlinked(tmp_path):
    from obsidian_automation.vault_root_activation import (
        _write_binding, _source_digest, RootPin,
    )
    store=SetupStore(tmp_path, obscure=lambda _:"fixture-obscured")
    assert store.save_nextcloud(SOURCE, probed=True)
    assert _write_binding(
        store.state,RootPin("SomeOtherVault",None,None),
        None,"connection_saved",clock=lambda:1.0,
        source=_source_digest(SOURCE))
    assert enroll_generic(store,probe=lambda _:True)=="prior_root_pin_requires_reset"
    assert not is_enrolled(store)


def test_present_corrupt_pin_marker_never_downgrades_to_unpinned_generic(tmp_path):
    from obsidian_automation.vault_root_activation import (
        _write_binding, _source_digest, RootPin,
    )
    store=SetupStore(tmp_path, obscure=lambda _:"fixture-obscured")
    assert store.save_nextcloud(SOURCE, probed=True)
    assert _write_binding(
        store.state,RootPin(SOURCE["vault_folder"],None,None),
        None,"connection_saved",clock=lambda:1.0,
        source=_source_digest(SOURCE))
    marker=tmp_path/"vault_root_pinning.enabled"
    marker.write_text("not a trusted pin")
    marker.chmod(0o600)
    assert enroll_generic(store,probe=lambda _:True)=="prior_root_pin_requires_reset"
    assert not is_enrolled(store)


def test_switch_from_pinned_nextcloud_to_generic_root_can_enroll_via_real_save_path(tmp_path):
    """Production flow: an old OC-ID pin + marker exist, the operator saves a
    generic connection (reset_pin_after_connection_save), then enrolls."""
    from obsidian_automation.vault_root_activation import (
        _write_binding, _source_digest, RootPin, reset_pin_after_connection_save,
    )
    store=SetupStore(tmp_path, obscure=lambda _:"fixture-obscured")
    nextcloud={**SOURCE,
               "webdav_url":"https://nas.example.invalid/remote.php/dav/files/u/",
               "vault_folder":"ObsidianVault"}
    assert store.save_nextcloud(nextcloud, probed=True)
    assert _write_binding(
        store.state,RootPin("ObsidianVault",("0000001ocabc",),"ocabc"),None,
        "activated",clock=lambda:1.0,source=_source_digest(nextcloud))
    marker=tmp_path/"vault_root_pinning.enabled"
    marker.write_bytes(b"1\n"); marker.chmod(0o600)
    generic={**SOURCE,"webdav_url":"https://vault.example.invalid/","vault_folder":"/",
             "nextcloud_user":"vault-oa"}
    assert store.save_nextcloud(generic, probed=True)
    assert reset_pin_after_connection_save(store, clock=lambda:2.0)
    assert not is_enrolled(store)
    assert enroll_generic(store,probe=lambda _:True)=="enrolled"
    assert is_enrolled(store)
    assert JobRootGuard(store,generic_probe=lambda _:True).status()=="ok"
