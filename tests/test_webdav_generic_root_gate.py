"""Generic DAV must not inherit Nextcloud OC-ID/OCS root-pinning authority."""
import pytest

from obsidian_automation.snapshot_setup import SetupStore
from obsidian_automation.vault_root_activation import JobRootGuard, RootPinRefused
from obsidian_automation.vault_root_enrollment import (
    context_provider, enroll_account, load_enrollment,
)
from obsidian_automation.vault_root_webui import VaultRootActions


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
    assert guard.status() == "provider_unsupported"
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


def test_legacy_unpinned_nextcloud_root_still_permitted(tmp_path):
    value = dict(SOURCE, webdav_url="https://cloud.example.invalid/remote.php/dav/files/user/")
    store = SetupStore(tmp_path, obscure=lambda _: "fixture-obscured")
    assert store.save_nextcloud(value, probed=True)
    guard = JobRootGuard(store, transport_factory=None)
    assert guard.status() == "legacy_unpinned"
    guard()
