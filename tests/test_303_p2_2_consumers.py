"""#303 Phase 2 / P2-2: Jobs read role folders through the mapping record.

Inside a Job the paths stay logical (today's names); only the Nextcloud pull
maps a role to its physical folder. With a record, each Job also pins the
role folders it uses.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from obsidian_automation import github_daily_setup, vault_format_source
from obsidian_automation import vault_bindings as vb
from obsidian_automation.github_daily_setup import DailySetupError
from obsidian_automation.vault_format_source import FormatterSource, FormatterSourceError
from obsidian_automation.vault_layout import DEFAULT_ROLE_FOLDERS, ROLES, remote_folder
from obsidian_automation.vault_root_activation import RootPin, RootPinRefused
from test_303_v1b2_dav_verifier import INSTANCE, oc_id
from test_github_daily_setup import TOKEN, make_setup, project_copy

ROLE_IDS = {r: oc_id(100 + i) for i, r in enumerate(ROLES)}
PIN = RootPin("ObsidianVault", (oc_id(70),), INSTANCE)


def write_record(state: Path, **roles) -> None:
    record = {"version": 1, "root_pin": [oc_id(70)],
              "roles": {**DEFAULT_ROLE_FOLDERS, **roles}, "role_ids": dict(ROLE_IDS)}
    path = state / vb.BINDINGS_FILE
    path.write_text(json.dumps(record))
    path.chmod(0o600)


def folders(state: Path, **roles) -> dict:
    """Server view: physical path below the root -> oc:id."""
    mapped = {**DEFAULT_ROLE_FOLDERS, **roles}
    return {f"ObsidianVault/{mapped[r]}": ROLE_IDS[r] for r in ROLES}


def check(state, roles, *, root="ok", pin=PIN, server=None, unmapped=()):
    server = folders(state) if server is None else server
    return vb.check_role_pins(state, roles, root, pin, server.get, unmapped)


# --- physical folder ----------------------------------------------------------

def test_physical_folder_defaults_and_mapping(tmp_path):
    assert vb.physical_folder(tmp_path, "system") == "98-System"
    write_record(tmp_path, system="Ops/System")
    assert vb.physical_folder(tmp_path, "system") == "Ops/System"
    assert vb.physical_folder(tmp_path, "project") == "10-Project"
    with pytest.raises(vb.BindingsError):
        vb.physical_folder(tmp_path, "nope")


def test_physical_folder_never_falls_back_on_a_bad_record(tmp_path):
    (tmp_path / vb.BINDINGS_FILE).write_text("{}")
    (tmp_path / vb.BINDINGS_FILE).chmod(0o600)
    with pytest.raises(vb.BindingsError):
        vb.physical_folder(tmp_path, "system")


def test_remote_folder_keyword_only_for_a_mapped_folder():
    assert remote_folder(None, "system") == {}
    assert remote_folder(lambda r: "98-System", "system") == {}
    assert remote_folder(lambda r: "Ops/System", "system") == {"folder": "Ops/System"}
    for bad in ("", None, 3):
        with pytest.raises(ValueError):
            remote_folder(lambda r, bad=bad: bad, "system")


# --- role pins ------------------------------------------------------------------

def test_no_record_keeps_the_root_guard_result_without_io(tmp_path):
    def no_io(_path):
        raise AssertionError("no provider I/O without a record")
    for root in ("ok", "legacy_unpinned", "mismatch", "unavailable"):
        assert vb.check_role_pins(tmp_path, ("system",), root, PIN, no_io) == root


def test_record_with_matching_role_ids_passes(tmp_path):
    write_record(tmp_path, system="Ops/System")
    assert check(tmp_path, ("system",), server=folders(tmp_path, system="Ops/System")) == "ok"


@pytest.mark.parametrize("case", [
    "legacy_root", "unpinned_pin", "other_root", "recreated_role", "missing_role",
    "bad_record", "unknown_role",
])
def test_record_refuses_unless_every_pin_matches(tmp_path, case):
    write_record(tmp_path)
    kw = {}
    roles = ("system", "project")
    if case == "legacy_root":
        kw["root"] = "legacy_unpinned"
    elif case == "unpinned_pin":
        kw["pin"] = RootPin("ObsidianVault", None, None)
    elif case == "other_root":
        kw["pin"] = RootPin("ObsidianVault", (oc_id(71),), INSTANCE)
    elif case == "recreated_role":
        kw["server"] = {**folders(tmp_path), "ObsidianVault/98-System": oc_id(999)}
    elif case == "missing_role":
        server = folders(tmp_path)
        del server["ObsidianVault/10-Project"]
        kw["server"] = server
    elif case == "bad_record":
        (tmp_path / vb.BINDINGS_FILE).chmod(0o644)
    else:
        roles = ("system", "elsewhere")
    expected = "unavailable" if case == "missing_role" else "mismatch"
    assert check(tmp_path, roles, **kw) == expected


def test_provider_error_is_unavailable(tmp_path):
    write_record(tmp_path)

    def boom(_path):
        raise OSError("down")
    assert vb.check_role_pins(tmp_path, ("system",), "ok", PIN, boom) == "unavailable"


def test_role_not_yet_mapping_aware_must_keep_todays_folder(tmp_path):
    write_record(tmp_path)
    assert check(tmp_path, ("project", "daily"), unmapped=("daily",)) == "ok"
    write_record(tmp_path, daily="Journal")
    server = folders(tmp_path, daily="Journal")
    assert check(tmp_path, ("project", "daily"), server=server, unmapped=("daily",)) == "mismatch"
    assert check(tmp_path, ("project",), server=server) == "ok"


def test_role_guard_wraps_the_root_guard(tmp_path):
    write_record(tmp_path)
    server = folders(tmp_path)
    root = SimpleNamespace(state=tmp_path, status=lambda: "ok", pin=lambda: PIN,
                           read_identity=server.get)
    guard = vb.RoleGuard(root, ("system",))
    assert guard.status() == "ok"
    assert guard.wrap(lambda x: x + 1)(1) == 2
    server["ObsidianVault/98-System"] = oc_id(999)
    assert guard.status() == "mismatch"
    with pytest.raises(RootPinRefused):
        guard()
    root.status = lambda: (_ for _ in ()).throw(RuntimeError("x"))
    assert guard.status() == "unavailable"


# --- Formatter pull -------------------------------------------------------------

class _Shared:
    def __init__(self, tmp_path):
        config = tmp_path / "rclone.conf"
        config.write_text("[nextcloud]\ntype = local\n")
        self.source = SimpleNamespace(rclone_remote="nextcloud:Vault",
                                      rclone_config=config, validate=lambda: None)

    def source_connections(self):
        return self.source

    def load_nextcloud(self):
        return {"webdav_url": "https://cloud.invalid/remote.php/dav/files/u/",
                "vault_folder": "Vault", "nextcloud_user": "u",
                "nextcloud_password": "SYNTHETIC"}


def _formatter(tmp_path, layout):
    state = tmp_path / "fmt"
    state.mkdir(mode=0o700)
    calls = []

    def copier(source, target, **kw):
        calls.append((target.name, kw))
        (target / "a.md").write_text("x\n")
        return True
    return FormatterSource(state, _Shared(tmp_path), copier=copier, layout=layout), calls


def test_formatter_pulls_the_mapped_folder_into_a_logical_mirror(tmp_path):
    source, calls = _formatter(tmp_path, lambda role: {"system": "Ops/System"}[role])
    plan = source()
    assert calls == [("98-System", {"folder": "Ops/System"})]
    text = json.dumps(plan)
    assert "98-System/a.md" in text and "Ops/System" not in text  # logical paths


def test_formatter_default_layout_keeps_the_two_argument_call(tmp_path):
    source, calls = _formatter(tmp_path, lambda role: DEFAULT_ROLE_FOLDERS[role])
    source()
    assert calls == [("98-System", {})]


def test_formatter_stops_on_a_bad_record(tmp_path):
    def bad(_role):
        raise vb.BindingsError("unreadable_record")
    source, calls = _formatter(tmp_path, bad)
    with pytest.raises(FormatterSourceError, match="formatter_source_pull_failed"):
        source()
    assert calls == []


def test_pull_system_markdown_uses_the_folder_argument(tmp_path, monkeypatch):
    target = tmp_path / "capture"
    target.mkdir()
    seen = []
    monkeypatch.setattr(vault_format_source, "_bounded_listing",
                        lambda command, _cwd: seen.append(command) or b"[]")
    source = SimpleNamespace(validate=lambda: None, rclone_remote="nextcloud:Vault",
                             rclone_config=tmp_path / "rclone.conf")
    assert vault_format_source.pull_system_markdown(source, target, folder="Ops/System")
    assert "nextcloud:Vault/Ops/System" in seen[0]


# --- B3/B4 project pull ---------------------------------------------------------

def test_project_pull_uses_the_mapped_folder_and_keeps_logical_paths(tmp_path):
    calls = []

    def copier(connections, destination, **kw):
        calls.append(kw)
        return project_copy(connections, destination)
    shared, setup = make_setup(tmp_path, layout=lambda role: {"project": "Work/Projects"}[role])
    setup.copier = copier
    assert setup.save({"github_token": TOKEN})
    with setup.project_source() as (config, _token):
        assert list((config.vault_root / "10-Project").rglob("*.md"))
    assert calls == [{"folder": "Work/Projects"}]


def test_project_pull_stops_on_a_bad_record(tmp_path):
    def bad(_role):
        raise vb.BindingsError("invalid_record")
    shared, setup = make_setup(tmp_path, layout=bad)
    assert setup.save({"github_token": TOKEN})
    with pytest.raises(DailySetupError, match="project_mirror_pull_failed"):
        with setup.project_source():
            pass


def test_pull_project_notes_uses_the_folder_argument(tmp_path, monkeypatch):
    target = tmp_path / "notes"
    target.mkdir()
    seen = []
    monkeypatch.setattr(github_daily_setup.subprocess, "run",
                        lambda command, **_kw: seen.append(command) or SimpleNamespace(returncode=0))
    connections = SimpleNamespace(validate=lambda: None, rclone_remote="nextcloud:Vault",
                                  rclone_config=tmp_path / "rclone.conf")
    assert github_daily_setup.pull_project_notes(connections, target, folder="Work/Projects")
    assert "nextcloud:Vault/Work/Projects" in seen[0]


# --- review follow-ups -------------------------------------------------------

def _root(tmp_path, server, pin=PIN):
    return SimpleNamespace(state=tmp_path, status=lambda: "ok", pin=lambda: pin,
                           read_identity=server.get)


def test_pull_uses_the_mapping_the_guard_verified(tmp_path):
    write_record(tmp_path, system="Ops/System")
    guard = vb.RoleGuard(_root(tmp_path, folders(tmp_path, system="Ops/System")), ("system",))

    def pull():
        write_record(tmp_path, system="Elsewhere")          # saved mid-run
        return vb.physical_folder(tmp_path, "system")
    assert guard.wrap(pull)() == "Ops/System"
    assert vb.physical_folder(tmp_path, "system") == "Elsewhere"   # outside the call


def test_pull_keeps_the_defaults_when_a_record_appears_mid_run(tmp_path):
    guard = vb.RoleGuard(_root(tmp_path, {}), ("system",))

    def pull():
        write_record(tmp_path, system="Ops/System")
        return vb.physical_folder(tmp_path, "system")
    assert guard.wrap(pull)() == "98-System"


def test_activation_during_the_check_is_unavailable(tmp_path):
    pins = iter([PIN, RootPin("Other", (oc_id(71),), INSTANCE)])
    root = SimpleNamespace(state=tmp_path, status=lambda: "ok", pin=lambda: next(pins),
                           read_identity=lambda _p: None)
    assert vb.RoleGuard(root, ("system",)).status() == "unavailable"


def test_failing_pin_read_is_unavailable_without_a_record(tmp_path):
    def boom():
        raise OSError("x")
    root = SimpleNamespace(state=tmp_path, status=lambda: "legacy_unpinned", pin=boom,
                           read_identity=lambda _p: None)
    assert vb.RoleGuard(root, ("system",)).status() == "unavailable"


def test_record_generation_and_fold(tmp_path):
    assert vb.record_generation(tmp_path) is None
    assert vb.fold_generation("g", None) == "g"
    write_record(tmp_path)
    first = vb.record_generation(tmp_path)
    write_record(tmp_path, system="Ops/System")
    assert vb.record_generation(tmp_path) not in (None, first)
    (tmp_path / vb.BINDINGS_FILE).chmod(0o644)
    assert vb.record_generation(tmp_path) == "unreadable"
    assert vb.fold_generation("g", "x") != "g"


def test_job_generations_change_only_with_a_record(tmp_path):
    state = tmp_path / "bindings"
    state.mkdir()
    shared, setup = make_setup(tmp_path,
                               layout_generation=lambda: vb.record_generation(state))
    source, _ = _formatter(tmp_path, None)
    source.layout_generation = lambda: vb.record_generation(state)
    plain_daily, plain_fmt = setup.source_fingerprint(), source.generation()
    setup.layout_generation = source.layout_generation = None
    assert (setup.source_fingerprint(), source.generation()) == (plain_daily, plain_fmt)
    setup.layout_generation = source.layout_generation = lambda: vb.record_generation(state)
    write_record(state)
    assert setup.source_fingerprint() != plain_daily
    assert source.generation() != plain_fmt


def test_job_guard_wiring():
    from obsidian_automation import snapshot_app
    import inspect
    guards = snapshot_app.job_guards(object())
    assert (guards["daily"]._roles, guards["daily"]._unmapped) == (
        ("project", "daily"), ())  # P2-3: B4 writes through the mapping
    assert (guards["b3"]._roles, guards["b3"]._unmapped) == (("project",), ())
    assert (guards["formatter"]._roles, guards["formatter"]._unmapped) == (("system",), ())
    source = inspect.getsource(snapshot_app.main)
    assert "guard_daily_runner(daily_guard," in source
    assert "guard_formatter_runner(system_guard," in source
    assert source.count("project_guard.wrap(") == 2
    assert "root_guard.wrap(" not in source


def test_generation_inside_a_guarded_call_names_the_verified_mapping(tmp_path):
    write_record(tmp_path, system="Ops/System")
    on_file = vb.record_generation(tmp_path)
    guard = vb.RoleGuard(_root(tmp_path, folders(tmp_path, system="Ops/System")), ("system",))

    def pull():
        write_record(tmp_path, system="Elsewhere")          # saved mid-run
        return vb.record_generation(tmp_path)
    assert guard.wrap(pull)() == on_file                     # same digest as the file
    assert vb.record_generation(tmp_path) != on_file
    empty = tmp_path / "empty"
    empty.mkdir()
    no_record = vb.RoleGuard(_root(empty, {}), ("system",))
    assert no_record.wrap(lambda: (write_record(empty), vb.record_generation(empty))[1])() is None
