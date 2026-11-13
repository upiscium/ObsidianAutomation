"""#303 scope V1-S2: a received user share may hold the Vault root (synthetic).

Fixture shape mirrors P3-SR (Nextcloud 35.0.2): the share root reports
mount-type "shared" + is-mount-root "true"; items inside it "shared" + "false";
the share root's oc:id is the owner's folder ID.
"""
from __future__ import annotations

import json

import pytest

from obsidian_automation import vault_root_activation as act
from obsidian_automation import vault_root_dav_verifier as v
from obsidian_automation import vault_root_enrollment as en
from obsidian_automation.snapshot_setup import SetupStore
from obsidian_automation.vault_root_selection import VaultRootSelectionService
from test_303_v1_a_account_enrollment import EnrollServer
from test_303_v1b2_dav_verifier import ACCOUNT, PREFIX, FakeNextcloud, oc_id, selection
from test_303_v1d_vault_root_activation import NC, RESTRICTED, FakeFence


def share_nc():
    nc = FakeNextcloud()
    nc.nodes.update({
        "ObsidianVault": (70, True, "shared"),             # received share root
        "ObsidianVault/Notes": (71, True, "shared"),       # inside the share
        "ObsidianVault/Notes/a.md": (72, False, "shared"),
        "ObsidianVault/Nested": (73, True, "shared"),      # nested share root (below)
        "ReadOnly": (80, True, "shared"),                  # read-only share
        "ReadOnly/Inner": (81, True, "shared"),
    })
    nc.perms["ReadOnly"] = "SGDNV"
    nc.perms["ReadOnly/Inner"] = "SGDNV"
    return nc


def nested(nc):
    # Make "Nested" report is-mount-root true although its parent is shared.
    original = nc.row

    def row(path):
        text = original(path)
        if path == "ObsidianVault/Nested":
            text = text.replace("<nc:is-mount-root>false<", "<nc:is-mount-root>true<")
        return text
    nc.row = row


def preview(nc, path, **kw):
    sel = selection(path, active="Other", active_resource_id=oc_id(20), **kw)
    return v.verify_root_chain(sel, ACCOUNT, nc, source_revision=lambda: "rev1",
                               proof=RESTRICTED, restricted_coherence=True)


def test_share_root_and_folder_inside_it_are_valid():
    nc = share_nc()
    result, chain = preview(nc, "ObsidianVault")
    assert result.status == "candidate_valid" and chain == (oc_id(70),)
    result, chain = preview(nc, "ObsidianVault/Notes")
    assert result.status == "candidate_valid" and chain == (oc_id(70), oc_id(71))


def test_nested_share_root_is_refused():
    nc = share_nc()
    nested(nc)
    assert preview(nc, "ObsidianVault/Nested")[0].reason == "unsafe_target"


@pytest.mark.parametrize("path", ["ReadOnly", "ReadOnly/Inner"])
def test_share_without_write_permission_is_refused(path):
    assert preview(share_nc(), path)[0].reason == "unsafe_target"


def test_other_mount_types_stay_refused():
    nc = share_nc()
    assert preview(nc, "Shared")[0].reason == "unsafe_target"        # external storage


def test_missing_is_mount_root_is_unverified():
    nc = share_nc()
    nc.row_hook = lambda path, t: t.replace("<nc:is-mount-root>true</nc:is-mount-root>", "") \
        if path == "ObsidianVault" else t
    assert preview(nc, "ObsidianVault")[0].reason == "alias_unverified"


def test_malformed_permissions_are_rejected():
    nc = share_nc()
    nc.perms["ObsidianVault"] = "S G"
    assert preview(nc, "ObsidianVault")[0].reason == "invalid_dav_response"


def test_v1_s1_switch_restores_share_refusal(monkeypatch):
    monkeypatch.setattr(v, "SHARE_ROOTS_ACCEPTED", False)
    nc = share_nc()
    assert preview(nc, "ObsidianVault")[0].reason == "unsafe_target"
    assert v.read_root_status(ACCOUNT, nc, "ObsidianVault", proof=RESTRICTED) == \
        (None, "unsupported_mount")


def test_listing_offers_share_roots_and_stays_inside_a_share():
    nc = share_nc()
    nested(nc)
    top = v.list_candidate_directories(ACCOUNT, nc, parent="", parent_resource_id=oc_id(1),
                                       expected_revision="rev1", source_revision=lambda: "rev1",
                                       proof=RESTRICTED, restricted_coherence=True)
    names = [n for n, _ in top.directories]
    assert "ObsidianVault" in names and "ReadOnly" in names and "Shared" not in names
    inner = v.list_candidate_directories(ACCOUNT, nc, parent="ObsidianVault",
                                         parent_resource_id=oc_id(70), expected_revision="rev1",
                                         source_revision=lambda: "rev1", proof=RESTRICTED,
                                         restricted_coherence=True)
    assert [n for n, _ in inner.directories] == ["Notes"]          # nested root hidden


def test_read_root_status_accepts_share_roots_and_inner_folders():
    nc = share_nc()
    assert v.read_root_status(ACCOUNT, nc, "ObsidianVault", proof=RESTRICTED) == \
        (oc_id(70), "ok")
    assert v.read_root_status(ACCOUNT, nc, "ObsidianVault/Notes", proof=RESTRICTED) == \
        (oc_id(71), "ok")
    assert v.read_root_status(ACCOUNT, nc, "Shared", proof=RESTRICTED) == \
        (None, "unsupported_mount")


def test_production_shape_end_to_end(tmp_path):
    """The saved Vault is a received share root (as on obsidian-control):
    enroll, list inside it, switch to a folder inside it, Job guard passes."""
    store = SetupStore(tmp_path, obscure=lambda _: "encoded")
    assert store.save_nextcloud({**NC, "vault_folder": "ObsidianVault"}, probed=True)
    assert en.enroll_account(store, lambda saved: EnrollServer(), proof=RESTRICTED)[0] == "enrolled"
    nc = share_nc()
    ctx_provider = en.context_provider(store, lambda saved: nc, jobs_busy=lambda: False,
                                       proof=RESTRICTED)
    ctx = ctx_provider()
    assert ctx.active == "ObsidianVault" and ctx.active_resource_id == oc_id(70)
    svc = VaultRootSelectionService(context=ctx_provider, transport=nc, proof=RESTRICTED,
                                    restricted_coherence=True)
    top = {c["label"]: c["handle"] for c in svc.candidates({})[1]["candidates"]}
    assert svc.preview({"candidate": top["ObsidianVault"]})[1]["reason"] == "already_selected"
    inner = {c["label"]: c["handle"]
             for c in svc.candidates({"parent": top["ObsidianVault"]})[1]["candidates"]}
    assert svc.preview({"candidate": inner["Notes"]})[1]["status"] == "candidate_valid"
    result = act.activate_vault_root(
        enabled=True, store=store, fence=FakeFence(), account=ctx.account, transport=nc,
        requested="ObsidianVault/Notes", expected_leaf_id=oc_id(71),
        expected_revision=store.nextcloud_revision(),
        container_resource_id=ctx.container_resource_id,
        active_resource_id=ctx.active_resource_id, proof=RESTRICTED)
    assert result.status == "activated"
    data = json.loads((store.state / act.BINDING_FILE).read_text())
    assert data["current"]["chain"] == [oc_id(70), oc_id(71)]
    guard = act.JobRootGuard(store, transport_factory=lambda saved: nc, proof=RESTRICTED)
    assert guard.status() == "ok"
    # Owner deletes and recreates the shared folder: the Job refuses.
    nc.nodes["ObsidianVault/Notes"] = (99, True, "shared")
    assert guard.status() == "mismatch"


def test_in_share_row_at_a_local_level_is_refused():
    nc = share_nc()
    nc.nodes["Loose"] = (90, True, "shared")
    nc.row_hook = lambda path, t: t.replace("<nc:is-mount-root>true<", "<nc:is-mount-root>false<") \
        if path == "Loose" else t
    assert preview(nc, "Loose")[0].reason == "unsafe_target"
    top = v.list_candidate_directories(ACCOUNT, nc, parent="", parent_resource_id=oc_id(1),
                                       expected_revision="rev1", source_revision=lambda: "rev1",
                                       proof=RESTRICTED, restricted_coherence=True)
    assert "Loose" not in [n for n, _ in top.directories]


def test_local_folder_inside_a_share_is_refused():
    nc = share_nc()
    nc.nodes["ObsidianVault/Local"] = (74, True, "")
    assert preview(nc, "ObsidianVault/Local")[0].reason == "unsafe_target"


@pytest.mark.parametrize("perms", ["SGDNV", ""])
def test_read_only_inner_folder_under_writable_share_is_refused(perms):
    nc = share_nc()
    nc.perms["ObsidianVault/Notes"] = perms
    assert preview(nc, "ObsidianVault/Notes")[0].reason == "unsafe_target"


def test_folder_that_describes_itself_differently_is_refused():
    nc = share_nc()
    # The listing says share root, but the folder's own row says local.
    nc.row_hook = lambda path, t, depth=None: t
    original = nc.propfind

    def propfind(origin, href, depth, body, **kw):
        response = original(origin, href, depth, body, **kw)
        if href.endswith("/ObsidianVault/") and depth == "1":
            from dataclasses import replace as _replace
            text = response.body.decode()
            first = text.index("<nc:mount-type>shared</nc:mount-type>")
            text = text[:first] + "<nc:mount-type></nc:mount-type>" + \
                text[first + len("<nc:mount-type>shared</nc:mount-type>"):]
            return _replace(response, body=text.encode())
        return response
    nc.propfind = propfind
    assert preview(nc, "ObsidianVault/Notes")[0].reason == "identity_unverified"



@pytest.mark.parametrize("owner", ["alice@remote.example", "alice@remote.example/sub", "", "a b"])
def test_share_with_non_local_owner_is_refused(owner):
    # P3-OW (2026-11-13): local shares report the sharing user's plain ID; a
    # federated owner looks like "user@host" and is refused.
    nc = share_nc()
    for path in ("ObsidianVault", "ObsidianVault/Notes"):
        nc.owners[path] = owner
    assert preview(nc, "ObsidianVault")[0].reason == "unsafe_target"
    top = v.list_candidate_directories(ACCOUNT, nc, parent="", parent_resource_id=oc_id(1),
                                       expected_revision="rev1", source_revision=lambda: "rev1",
                                       proof=RESTRICTED, restricted_coherence=True)
    assert "ObsidianVault" not in [n for n, _ in top.directories]
    assert v.read_root_status(ACCOUNT, nc, "ObsidianVault", proof=RESTRICTED) == \
        (None, "unsupported_mount")


def test_missing_owner_id_is_unverified():
    nc = share_nc()
    nc.row_hook = lambda path, t: t.replace("<oc:owner-id>owner_user</oc:owner-id>", "") \
        if path == "ObsidianVault" else t
    assert preview(nc, "ObsidianVault")[0].reason == "identity_unverified"
