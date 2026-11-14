"""#303 Phase 2 / P2-1: mapping record, validation and read-only Preview."""
from __future__ import annotations

import json
import os

import pytest

from obsidian_automation import vault_bindings as vb
from obsidian_automation.vault_layout import DEFAULT_ROLE_FOLDERS, ROLES
from obsidian_automation.vault_root_activation import RootPin
from obsidian_automation.vault_root_selection import TrustedRootContext
from test_303_v1b2_dav_verifier import ACCOUNT, INSTANCE, FakeNextcloud, UNPROVEN, oc_id
from test_303_v1d_vault_root_activation import RESTRICTED

ROLE_IDS = {r: 100 + i for i, r in enumerate(ROLES)}


def vault_nc(*, shared: bool = False, mount: str = "") -> FakeNextcloud:
    nc = FakeNextcloud()
    m = "shared" if shared else mount
    nc.nodes["ObsidianVault"] = (70, True, m)
    for role, folder in DEFAULT_ROLE_FOLDERS.items():
        nc.nodes["ObsidianVault/" + folder] = (ROLE_IDS[role], True, m)
    nc.nodes["ObsidianVault/Journal"] = (200, True, m)
    nc.nodes["ObsidianVault/Journal/Inner"] = (201, True, m)
    nc.nodes["ObsidianVault/note.md"] = (202, False, m)
    return nc


def ctx(**kw) -> TrustedRootContext:
    base = dict(account=ACCOUNT, active="ObsidianVault", active_resource_id=oc_id(70),
                container_resource_id=oc_id(1), source_revision="rev1", jobs_in_flight=False)
    base.update(kw)
    return TrustedRootContext(**base)


PIN = RootPin("ObsidianVault", (oc_id(70),), INSTANCE)


def run(roles, nc=None, *, current=None, context=None, pin=PIN, revision=lambda: "rev1",
        proof=RESTRICTED):
    return vb.preview_bindings(
        roles, current=current or vb.default_bindings(),
        context=ctx() if context is None else context, root_pin=pin,
        transport=vault_nc() if nc is None else nc, revision=revision, proof=proof,
        restricted_coherence=True)


def roles(**changes) -> dict:
    return {**DEFAULT_ROLE_FOLDERS, **changes}


def stored(**changes) -> dict:
    return {"version": 1, "root_pin": [oc_id(70)], "roles": roles(),
            "role_ids": {r: oc_id(ROLE_IDS[r]) for r in ROLES}, **changes}


def write_private(path, data, mode=0o600):
    path.write_text(json.dumps(data))
    path.chmod(mode)


# --- record and validation --------------------------------------------------

def test_missing_record_means_todays_names(tmp_path):
    loaded = vb.load_bindings(tmp_path)
    assert loaded.is_default and dict(loaded.roles) == dict(DEFAULT_ROLE_FOLDERS)


def test_stored_record_round_trips(tmp_path):
    write_private(tmp_path / vb.BINDINGS_FILE, stored(roles=roles(daily="Journal")))
    loaded = vb.load_bindings(tmp_path)
    assert not loaded.is_default and loaded.roles["daily"] == "Journal"
    assert loaded.root_pin == (oc_id(70),)
    assert vb.bindings_json(loaded) == stored(roles=roles(daily="Journal"))


@pytest.mark.parametrize("problem", ["mode", "symlink", "fifo", "not_json", "too_big"])
def test_unreadable_record_fails_closed(tmp_path, problem):
    path = tmp_path / vb.BINDINGS_FILE
    if problem == "mode":
        write_private(path, stored(), mode=0o644)
    elif problem == "symlink":
        write_private(tmp_path / "real.json", stored())
        path.symlink_to(tmp_path / "real.json")
    elif problem == "fifo":
        os.mkfifo(path, 0o600)
    elif problem == "not_json":
        path.write_text("{")
        path.chmod(0o600)
    else:
        path.write_text(json.dumps({**stored(), "pad": "x" * 70000}))
        path.chmod(0o600)
    with pytest.raises(vb.BindingsError) as err:
        vb.load_bindings(tmp_path)
    assert err.value.reason == "unreadable_record"


@pytest.mark.parametrize("record", [
    [],
    {**stored(), "extra": 1},
    stored(version=2),
    stored(version=True),
    stored(root_pin=[]),
    stored(root_pin=["../x"]),
    stored(role_ids={r: oc_id(5) for r in ROLES}),                 # one folder twice
    stored(role_ids={**{r: oc_id(ROLE_IDS[r]) for r in ROLES}, "daily": oc_id(70)}),
    stored(role_ids={r: oc_id(ROLE_IDS[r]) for r in ROLES if r != "ai"}),
])
def test_invalid_record_is_rejected(tmp_path, record):
    write_private(tmp_path / vb.BINDINGS_FILE, record)
    with pytest.raises(vb.BindingsError):
        vb.load_bindings(tmp_path)


@pytest.mark.parametrize("mapping, reason, role", [
    ({r: DEFAULT_ROLE_FOLDERS[r] for r in ROLES if r != "ai"}, "invalid_roles", None),
    ({**roles(), "extra": "X"}, "invalid_roles", None),
    (roles(daily="/abs"), "invalid_path", "daily"),
    (roles(daily="a/../b"), "invalid_path", "daily"),
    (roles(daily=".hidden"), "invalid_path", "daily"),
    (roles(daily=""), "invalid_path", "daily"),
    (roles(daily="日記"), "invalid_path", "daily"),
    (roles(daily=1), "invalid_path", "daily"),
    (roles(daily="/".join("a" * 17)), "invalid_path", "daily"),
    (roles(idea="00-DailyNote"), "duplicate_folder", None),
    (roles(idea="00-dailynote"), "duplicate_folder", None),
    (roles(idea="00-DailyNote/Ideas"), "nested_folder", "idea"),
    (roles(daily="11-knowledge/Daily"), "nested_folder", "daily"),
])
def test_role_path_rules(mapping, reason, role):
    with pytest.raises(vb.BindingsError) as err:
        vb.validate_role_paths(mapping)
    assert err.value.reason == reason
    if role is not None:
        assert err.value.role == role


def test_sibling_prefix_is_not_nesting():
    assert vb.validate_role_paths(roles(daily="Daily", idea="DailyIdeas"))["idea"] == "DailyIdeas"


# --- Preview ------------------------------------------------------------------

def test_preview_of_todays_names_is_valid_and_unchanged():
    result, record = run(roles())
    assert result["status"] == "valid" and result["changed"] is False
    assert all(s["status"] == "ok" and not s["changed"] for s in result["roles"].values())
    assert record.root_pin == (oc_id(70),)
    assert dict(record.role_ids) == {r: oc_id(ROLE_IDS[r]) for r in ROLES}
    assert result["writes_vault"] is False and result["activation_allowed"] is False


def test_preview_reports_changed_role_and_nested_folder():
    result, record = run(roles(daily="Journal/Inner"))
    assert result["status"] == "valid" and result["changed"] is True
    assert result["roles"]["daily"] == {"folder": "Journal/Inner", "status": "ok",
                                        "changed": True}
    assert record.role_ids["daily"] == oc_id(201)


def test_preview_is_read_only_and_redacted():
    nc = vault_nc()
    result, _ = run(roles(daily="Journal"), nc)
    # The transport offers only PROPFIND; every call is a bounded Depth 0/1 read.
    assert nc.calls and {depth for _, depth in nc.calls} <= {"0", "1"}
    text = json.dumps(result)
    assert INSTANCE not in text and "remote.php" not in text and "etag" not in text


@pytest.mark.parametrize("folder, reason", [
    ("Missing", "candidate_missing"),
    ("note.md", "unsafe_target"),
])
def test_preview_refuses_a_role_that_is_not_a_folder(folder, reason):
    result, record = run(roles(daily=folder))
    assert result["status"] == "blocked" and record is None
    assert result["roles"]["daily"]["status"] == reason
    assert result["roles"]["idea"]["status"] == "ok"


def test_preview_inside_a_received_share():
    result, record = run(roles(daily="Journal"), vault_nc(shared=True))
    assert result["status"] == "valid" and record.role_ids["daily"] == oc_id(200)


def test_preview_refuses_external_storage_role():
    nc = vault_nc()
    nc.nodes["ObsidianVault/Ext"] = (300, True, "external")
    result, _ = run(roles(daily="Ext"), nc)
    assert result["roles"]["daily"]["status"] == "unsafe_target"


def test_preview_refuses_two_names_for_one_server_folder():
    # An ID seen twice on one walked path is already refused by the V1 walk
    # (identity_alias). This covers two branches where neither walk sees the
    # other's folder.
    nc = vault_nc()
    nc.nodes["ObsidianVault/Ideas"] = (210, True, "")
    nc.nodes["ObsidianVault/Ideas/Deep"] = (500, True, "")
    nc.nodes["ObsidianVault/Journal/Inner"] = (500, True, "")
    result, record = run(roles(daily="Journal/Inner", idea="Ideas/Deep"), nc)
    assert record is None and result["status"] == "blocked"
    assert result["roles"]["daily"]["status"] == "duplicate_folder"
    assert result["roles"]["idea"]["status"] == "duplicate_folder"
    assert result["roles"]["project"]["status"] == "ok"


def test_preview_validates_shape_before_any_io():
    nc = vault_nc()
    result, record = run(roles(idea="00-DailyNote"), nc)
    assert (result["status"], result["reason"], record) == ("blocked", "duplicate_folder", None)
    assert nc.calls == []


@pytest.mark.parametrize("kw, reason", [
    (dict(context="not_enrolled"), "not_enrolled"),
    (dict(pin=None), "root_unpinned"),
    (dict(pin=RootPin("ObsidianVault", None, None)), "root_unpinned"),
    (dict(pin=RootPin("Other", (oc_id(70),), INSTANCE)), "root_pin_mismatch"),
    (dict(pin=RootPin("ObsidianVault", (oc_id(71),), INSTANCE)), "root_pin_mismatch"),
])
def test_preview_needs_a_pinned_active_root(kw, reason):
    nc = vault_nc()
    result, record = run(roles(), nc, **kw)
    assert (result["status"], result["reason"], record) == ("blocked", reason, None)
    assert nc.calls == []


def test_preview_refuses_a_stored_mapping_for_another_root():
    current = vb.parse_bindings(stored(root_pin=[oc_id(99)]))
    result, _ = run(roles(), current=current)
    assert result["reason"] == "root_changed"


def test_preview_marks_changes_against_the_stored_mapping():
    current = vb.parse_bindings(stored(roles=roles(daily="Journal"),
                                       role_ids={**{r: oc_id(ROLE_IDS[r]) for r in ROLES},
                                                 "daily": oc_id(200)}))
    result, _ = run(roles(), current=current)
    assert result["roles"]["daily"]["changed"] is True
    assert result["roles"]["idea"]["changed"] is False


def test_preview_refuses_a_root_recreated_under_the_same_name():
    nc = vault_nc()
    nc.nodes["ObsidianVault"] = (90, True, "")
    result, record = run(roles(), nc, context=ctx(active_resource_id=oc_id(70)))
    assert record is None and result["status"] == "blocked"


def test_preview_stops_on_stale_connection_and_unproven_provider():
    result, record = run(roles(), revision=lambda: "rev2")
    assert record is None and result["roles"]["daily"]["status"] == "stale_source"
    result, record = run(roles(), proof=UNPROVEN)
    assert record is None and result["roles"]["daily"]["status"] == "provider_unproven"


def test_record_from_preview_round_trips(tmp_path):
    _, record = run(roles(daily="Journal"))
    write_private(tmp_path / vb.BINDINGS_FILE, vb.bindings_json(record))
    assert vb.load_bindings(tmp_path) == record


def test_defaults_are_never_stored():
    with pytest.raises(vb.BindingsError):
        vb.bindings_json(vb.default_bindings())


# --- review follow-ups -------------------------------------------------------

def test_role_may_not_be_another_share_below_a_local_root():
    nc = vault_nc()
    nc.nodes["ObsidianVault/Foreign"] = (300, True, "shared")   # someone else's share
    result, record = run(roles(daily="Foreign"), nc)
    assert record is None and result["roles"]["daily"]["status"] == "unsafe_target"


def test_role_may_not_cross_into_a_nested_share_inside_a_shared_root():
    nc = vault_nc(shared=True)
    nc.nodes["ObsidianVault/Nested"] = (301, True, "shared")
    original = nc.row

    def row(path):
        text = original(path)
        if path == "ObsidianVault/Nested":
            text = text.replace("<nc:is-mount-root>false<", "<nc:is-mount-root>true<")
        return text
    nc.row = row
    result, record = run(roles(daily="Nested"), nc)
    assert record is None and result["roles"]["daily"]["status"] == "unsafe_target"


def test_v1_root_walk_still_accepts_a_share_by_default():
    from obsidian_automation import vault_root_dav_verifier as v
    from test_303_v1b2_dav_verifier import selection
    nc = vault_nc()
    nc.nodes["ObsidianVault/Foreign"] = (300, True, "shared")
    sel = selection("ObsidianVault/Foreign", active="Other", active_resource_id=oc_id(20))
    result, chain = v.verify_root_chain(sel, ACCOUNT, nc, source_revision=lambda: "rev1",
                                        proof=RESTRICTED, restricted_coherence=True)
    assert result.status == "candidate_valid" and chain == (oc_id(70), oc_id(300))
    for bad in (-1, True, "1"):
        result, chain = v.verify_root_chain(
            sel, ACCOUNT, nc, source_revision=lambda: "rev1", proof=RESTRICTED,
            restricted_coherence=True, no_share_root_below=bad)
        assert (result.reason, chain) == ("invalid_request", None)


def test_roles_nested_on_the_server_are_refused():
    nc = vault_nc()
    nc.nodes["ObsidianVault/Ideas"] = (210, True, "")
    nc.nodes["ObsidianVault/Ideas/X"] = (201, True, "")        # same folder as Journal/Inner
    nc.nodes["ObsidianVault/Ideas/X/Deep"] = (211, True, "")
    result, record = run(roles(daily="Journal/Inner", idea="Ideas/X/Deep"), nc)
    assert record is None
    assert result["roles"]["daily"]["status"] == "nested_folder"
    assert result["roles"]["idea"]["status"] == "nested_folder"
    assert result["roles"]["project"]["status"] == "ok"


def test_recreated_role_folder_under_the_same_name_is_refused():
    current = vb.parse_bindings(stored())
    nc = vault_nc()
    nc.nodes["ObsidianVault/00-DailyNote"] = (400, True, "")
    result, record = run(roles(), nc, current=current)
    assert record is None and result["roles"]["daily"]["status"] == "identity_changed"
    assert result["roles"]["idea"]["status"] == "ok"


def test_too_long_combined_path_is_refused_before_io():
    nc = vault_nc()
    long_root = "R" * 90
    result, record = run(roles(), nc, context=ctx(active=long_root),
                         pin=RootPin(long_root, (oc_id(70),), INSTANCE))
    assert (result["reason"], result["role"], record) == ("path_too_long", "daily", None)
    deep_root = "/".join("d" * 16)
    result, _ = run(roles(), nc, context=ctx(active=deep_root),
                    pin=RootPin(deep_root, (oc_id(70),), INSTANCE))
    assert result["reason"] == "path_too_long"
    assert nc.calls == []


def test_two_level_root_pin_uses_the_whole_prefix():
    nc = vault_nc()
    nc.nodes["Parent"] = (60, True, "")
    for path in [p for p in nc.nodes if p.startswith("ObsidianVault")]:
        nc.nodes["Parent/" + path] = nc.nodes.pop(path)
    pin = RootPin("Parent/ObsidianVault", (oc_id(60), oc_id(70)), INSTANCE)
    context = ctx(active="Parent/ObsidianVault")
    result, record = run(roles(), nc, context=context, pin=pin)
    assert result["status"] == "valid" and record.root_pin == (oc_id(60), oc_id(70))
    nc.nodes["Parent"] = (61, True, "")                           # parent recreated
    result, record = run(roles(), nc, context=context, pin=pin)
    assert record is None and result["roles"]["daily"]["status"] == "root_changed"


def test_record_with_repeated_pin_ids_is_rejected(tmp_path):
    write_private(tmp_path / vb.BINDINGS_FILE, stored(root_pin=[oc_id(70), oc_id(70)]))
    with pytest.raises(vb.BindingsError):
        vb.load_bindings(tmp_path)


def test_local_root_inside_a_share_keeps_roles_inside_that_share():
    nc = vault_nc(shared=True)
    for path in [p for p in nc.nodes if p.startswith("ObsidianVault")]:
        nc.nodes["Team/" + path] = nc.nodes.pop(path)
    nc.nodes["Team"] = (50, True, "shared")                     # the received share
    pin = RootPin("Team/ObsidianVault", (oc_id(50), oc_id(70)), INSTANCE)
    context = ctx(active="Team/ObsidianVault")
    result, record = run(roles(), nc, context=context, pin=pin)
    assert result["status"] == "valid" and record.root_pin == (oc_id(50), oc_id(70))
    nc.nodes["Team/ObsidianVault/Nested"] = (302, True, "shared")
    original = nc.row

    def row(path):
        text = original(path)
        if path == "Team/ObsidianVault/Nested":
            text = text.replace("<nc:is-mount-root>false<", "<nc:is-mount-root>true<")
        return text
    nc.row = row
    result, record = run(roles(daily="Nested"), nc, context=context, pin=pin)
    assert record is None and result["roles"]["daily"]["status"] == "unsafe_target"
