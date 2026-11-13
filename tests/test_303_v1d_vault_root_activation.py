"""#303 V1/D restricted-contract activation, rollback and Job pin (synthetic)."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import json
import sqlite3
import stat
import threading

import pytest

from obsidian_automation import vault_root_activation as act
from obsidian_automation.snapshot_setup import SetupStore
from test_303_v1b2_dav_verifier import UNPROVEN
from test_303_v1b2_dav_verifier import ACCOUNT, VERIFIED, FakeNextcloud, oc_id

RESTRICTED = replace(VERIFIED, C="UNKNOWN")
NC = dict(webdav_url="https://nextcloud.invalid/remote.php/dav/files/synthetic_user/",
          vault_folder="Other", nextcloud_user="synthetic_user",
          nextcloud_password="NC-PRIVATE")


class FakeFence:
    def __init__(self, busy=False):
        self.busy = busy
        self.changes = 0
        self.held = False

    @contextmanager
    def quiesced(self):
        self.held = True
        try:
            yield self.busy
        finally:
            self.held = False

    def after_change(self):
        assert self.held
        self.changes += 1


@pytest.fixture
def store(tmp_path):
    s = SetupStore(tmp_path, obscure=lambda _: "encoded")
    assert s.save_nextcloud(dict(NC), probed=True)
    return s


def activate(store, nc, fence=None, **kw):
    args = dict(enabled=True, store=store, fence=fence or FakeFence(), account=ACCOUNT,
                transport=nc, requested="Vault/Notes", expected_leaf_id=oc_id(11),
                expected_revision=store.nextcloud_revision(),
                container_resource_id=oc_id(1), active_resource_id=oc_id(20),
                proof=RESTRICTED, clock=lambda: 1_700_000_000)
    args.update(kw)
    return act.activate_vault_root(**args)


def snapshot(store):
    return store.path.read_bytes(), store.rclone.read_bytes()


def test_disabled_and_unproven_do_nothing(store):
    nc = FakeNextcloud()
    before = snapshot(store)
    assert activate(store, nc, enabled=False).reason == "activation_disabled"
    assert activate(store, nc, proof=UNPROVEN).reason == "provider_unproven"
    assert activate(store, nc, proof=replace(RESTRICTED, R="UNKNOWN")).reason == \
        "provider_unproven"
    assert nc.calls == [] and snapshot(store) == before
    assert not (store.state / act.BINDING_FILE).exists()


def test_restricted_activation_changes_only_vault_folder(store):
    nc = FakeNextcloud()
    fence = FakeFence()
    old_config = json.loads(store.path.read_text())
    old_rclone = store.rclone.read_bytes()
    result = activate(store, nc, fence)
    assert result == act.ActivationResult("activated", "requires_job_reenable")
    new_config = json.loads(store.path.read_text())
    assert new_config == {**old_config, "vault_folder": "Vault/Notes"}
    assert store.rclone.read_bytes() == old_rclone
    assert fence.changes == 1
    assert {depth for _, depth in nc.calls} <= {"0", "1"}
    binding = store.state / act.BINDING_FILE
    assert stat.S_IMODE(binding.stat().st_mode) == 0o600
    data = json.loads(binding.read_text())
    assert data["current"] == {"path": "Vault/Notes", "chain": [oc_id(10), oc_id(11)],
                               "instance_id": ACCOUNT.instance_id}
    assert data["previous"] == {"path": "Other", "chain": None, "instance_id": None}
    assert data["outcome"] == "activated"
    assert "NC-PRIVATE" not in binding.read_text()


def test_full_proof_path_also_activates(store):
    assert activate(store, FakeNextcloud(), proof=VERIFIED).status == "activated"


def test_jobs_in_flight_blocks_before_provider_io(store):
    nc = FakeNextcloud()
    before = snapshot(store)
    assert activate(store, nc, FakeFence(busy=True)).reason == "jobs_in_flight"
    assert nc.calls == [] and snapshot(store) == before


def test_stale_revision_and_recreated_leaf_block(store):
    nc = FakeNextcloud()
    before = snapshot(store)
    assert activate(store, nc, expected_revision="rstale").reason == "stale_source"
    nc.nodes["Vault/Notes"] = (99, True, "")
    assert activate(store, nc).reason == "identity_unverified"
    assert snapshot(store) == before
    assert not (store.state / act.BINDING_FILE).exists()


def test_revision_rotated_by_concurrent_save_blocks(store):
    nc = FakeNextcloud()
    revision = store.nextcloud_revision()
    assert store.save_nextcloud({**NC, "nextcloud_password": "rotated"}, probed=True)
    assert activate(store, nc, expected_revision=revision).reason == "stale_source"


def test_invalid_requests():
    store = None
    for kw in ({"requested": "../x"}, {"requested": ".hidden"},
               {"expected_leaf_id": "bad id"}):
        args = dict(enabled=True, store=store, fence=FakeFence(), account=ACCOUNT,
                    transport=FakeNextcloud(), requested="Vault", expected_leaf_id=oc_id(10),
                    expected_revision="r1", container_resource_id=oc_id(1),
                    active_resource_id=oc_id(20), proof=RESTRICTED)
        args.update(kw)
        assert act.activate_vault_root(**args).reason == "invalid_request"


def test_cas_failure_restores_binding_and_config(store, monkeypatch):
    before = snapshot(store)
    monkeypatch.setattr(store, "replace_vault_folder", lambda *a: "stale_source")
    assert activate(store, FakeNextcloud()).reason == "stale_source"
    assert snapshot(store) == before
    assert not (store.state / act.BINDING_FILE).exists()
    # A blocked first attempt must not leave the sticky marker behind.
    assert not (store.state / act.PINNING_MARKER).exists()
    assert act.check_root_pin(store, lambda _: "unused") == "legacy_unpinned"


def test_blocked_later_attempt_keeps_existing_pin_and_marker(store, monkeypatch):
    assert activate(store, FakeNextcloud()).status == "activated"
    binding = (store.state / act.BINDING_FILE).read_bytes()
    revision = store.nextcloud_revision()
    monkeypatch.setattr(store, "replace_vault_folder", lambda *a: "stale_source")
    nc = FakeNextcloud()
    nc.nodes["Vault/Notes"] = (11, True, "")
    result = activate(store, nc, requested="Vault", expected_leaf_id=oc_id(10),
                      expected_revision=revision, active_resource_id=oc_id(11))
    assert result.reason == "stale_source"
    assert (store.state / act.BINDING_FILE).read_bytes() == binding
    assert (store.state / act.PINNING_MARKER).exists()
    assert act.check_root_pin(store, lambda _: oc_id(11)) == "ok"


def test_uncertain_write_leaves_jobs_fail_closed(store, monkeypatch):
    monkeypatch.setattr(store, "replace_vault_folder", lambda *a: "uncertain")
    assert activate(store, FakeNextcloud()).status == "uncertain"
    # Config still names the old folder, the pending pin names the new one.
    assert act.check_root_pin(store, lambda _: oc_id(11)) == "mismatch"


def test_pin_write_failure_leaves_config_untouched(store, monkeypatch):
    before = snapshot(store)
    monkeypatch.setattr(act, "_write_binding", lambda *a: False)
    assert activate(store, FakeNextcloud()).reason == "pin_unrecorded"
    assert snapshot(store) == before
    assert act.check_root_pin(store, lambda _: "unused") == "legacy_unpinned"


def test_rollback_restores_previous_folder_once(store):
    fence = FakeFence()
    assert activate(store, FakeNextcloud(), fence).status == "activated"
    result = act.rollback_vault_root(enabled=True, store=store, fence=fence)
    assert result == act.ActivationResult("rolled_back", "requires_job_reenable")
    assert store.load_nextcloud()["vault_folder"] == "Other"
    assert fence.changes == 2
    assert act.check_root_pin(store, lambda _: "unused") == "legacy_unpinned"
    assert act.rollback_vault_root(enabled=True, store=store,
                                   fence=fence).reason == "nothing_to_roll_back"


def test_rollback_refuses_when_config_diverged_or_busy(store):
    assert activate(store, FakeNextcloud()).status == "activated"
    assert act.rollback_vault_root(enabled=True, store=store,
                                   fence=FakeFence(busy=True)).reason == "jobs_in_flight"
    assert store.save_nextcloud({**NC, "vault_folder": "Elsewhere"}, probed=True)
    assert act.rollback_vault_root(enabled=True, store=store,
                                   fence=FakeFence()).reason == "binding_diverged"
    assert act.rollback_vault_root(enabled=False, store=store,
                                   fence=FakeFence()).reason == "activation_disabled"


def test_check_root_pin_states(store):
    assert act.check_root_pin(store, lambda _: "x") == "legacy_unpinned"
    assert activate(store, FakeNextcloud()).status == "activated"
    assert act.check_root_pin(store, lambda path: oc_id(11)) == "ok"
    assert act.check_root_pin(store, lambda path: oc_id(99)) == "mismatch"
    assert act.check_root_pin(store, lambda path: None) == "unavailable"

    def boom(_):
        raise OSError
    assert act.check_root_pin(store, boom) == "unavailable"
    binding = store.state / act.BINDING_FILE
    binding.chmod(0o644)
    assert act.check_root_pin(store, lambda path: oc_id(11)) == "mismatch"
    binding.chmod(0o600)
    binding.write_text("{not json")
    assert act.check_root_pin(store, lambda path: oc_id(11)) == "mismatch"


def test_replace_vault_folder_cas(store):
    revision = store.nextcloud_revision()
    assert revision and revision.startswith("r") and "NC-PRIVATE" not in revision
    assert store.replace_vault_folder("../x", revision) == "invalid"
    assert store.replace_vault_folder("New", "rwrong") == "stale_source"
    assert store.replace_vault_folder("New", revision) == "replaced"
    assert store.nextcloud_revision() != revision
    assert store.replace_vault_folder("Again", revision) == "stale_source"


class _Job:
    def __init__(self, busy=False):
        self._lock = threading.RLock()
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        self.db.execute("CREATE TABLE runs(status TEXT)")
        if busy:
            self.db.execute("INSERT INTO runs VALUES ('running')")
        self.saved = []


class _Formatter(_Job):
    def __init__(self, busy=False):
        super().__init__(busy)
        self._write_gate = threading.RLock()

    def connections_saved(self):
        self.saved.append(True)


class _Daily(_Job):
    def settings(self):
        return {"interval_seconds": 3600}

    def update(self, payload):
        self.saved.append(payload)


class _Snapshot(_Job):
    def connections_saved(self, generation=None):
        self.saved.append(generation)


def test_dashboard_fence_holds_all_locks_and_detects_busy(store):
    jobs = dict(snapshot_job=_Snapshot(), daily_job=_Daily(), formatter_job=_Formatter(),
                b3_observer_job=_Job())
    fence = act.DashboardJobFence(store, **jobs)
    with fence.quiesced() as busy:
        assert busy is False

        def other_thread(result):
            result.append(jobs["formatter_job"]._write_gate.acquire(blocking=False))
        result = []
        t = threading.Thread(target=other_thread, args=(result,))
        t.start()
        t.join()
        assert result == [False]  # writer gate is held
    fence.after_change()
    assert jobs["daily_job"].saved == [{"enabled": False, "interval_seconds": 3600}]
    assert jobs["formatter_job"].saved == [True]
    assert jobs["snapshot_job"].saved == []  # Nextcloud-only setup: no Gitea binding
    for name in jobs:
        busy_jobs = dict(jobs, **{name: type(jobs[name])(busy=True)})
        with act.DashboardJobFence(store, **busy_jobs).quiesced() as busy:
            assert busy is True


def test_activation_never_issues_non_propfind(store):
    nc = FakeNextcloud()
    assert activate(store, nc).status == "activated"
    assert all(href.startswith("/remote.php/dav/files/synthetic_user/")
               for href, _ in nc.calls)


def test_uncertain_write_keeps_previous_identity_for_later_rollback(store, monkeypatch):
    nc = FakeNextcloud()
    nc.nodes["Vault/Other2"] = (13, True, "")
    assert activate(store, nc).status == "activated"           # Other -> Vault/Notes
    with monkeypatch.context() as m:
        m.setattr(store, "replace_vault_folder", lambda *a: "uncertain")
        assert activate(store, nc, requested="Vault/Other2", expected_leaf_id=oc_id(13),
                        active_resource_id=oc_id(11)).status == "uncertain"
    # config still holds Vault/Notes; the pending pin names Vault/Other2
    assert activate(store, nc, requested="Vault", expected_leaf_id=oc_id(10),
                    active_resource_id=oc_id(11)).status == "activated"
    data = json.loads((store.state / act.BINDING_FILE).read_text())
    assert data["previous"] == {"path": "Vault/Notes", "chain": [oc_id(10), oc_id(11)],
                                "instance_id": ACCOUNT.instance_id}
    assert act.rollback_vault_root(enabled=True, store=store,
                                   fence=FakeFence()).status == "rolled_back"
    assert act.check_root_pin(store, lambda _: oc_id(11)) == "ok"


def test_uncertain_rollback_keeps_identity_of_root_still_in_config(store, monkeypatch):
    nc = FakeNextcloud()
    assert activate(store, nc).status == "activated"           # Other -> Vault/Notes
    assert act.rollback_vault_root(enabled=True, store=store,
                                   fence=FakeFence()).status == "rolled_back"
    assert activate(store, nc, active_resource_id=oc_id(20)).status == "activated"
    with monkeypatch.context() as m:
        m.setattr(store, "replace_vault_folder", lambda *a: "uncertain")
        assert act.rollback_vault_root(enabled=True, store=store,
                                       fence=FakeFence()).status == "uncertain"
    # config still holds Vault/Notes; its pin must survive the next activation
    assert activate(store, nc, requested="Vault", expected_leaf_id=oc_id(10),
                    active_resource_id=oc_id(11)).status == "activated"
    data = json.loads((store.state / act.BINDING_FILE).read_text())
    assert data["previous"]["chain"] == [oc_id(10), oc_id(11)]


def test_retry_after_uncertain_rollback_that_landed_is_refused(store, monkeypatch):
    assert activate(store, FakeNextcloud()).status == "activated"   # Other -> Vault/Notes
    real = store.replace_vault_folder

    def landed_but_uncertain(*a):
        assert real(*a) == "replaced"
        return "uncertain"
    with monkeypatch.context() as m:
        m.setattr(store, "replace_vault_folder", landed_but_uncertain)
        assert act.rollback_vault_root(enabled=True, store=store,
                                       fence=FakeFence()).status == "uncertain"
    assert store.load_nextcloud()["vault_folder"] == "Other"
    retry = act.rollback_vault_root(enabled=True, store=store, fence=FakeFence())
    assert retry == act.ActivationResult("blocked", "rollback_uncertain")
    assert store.load_nextcloud()["vault_folder"] == "Other"
    assert act.check_root_pin(store, lambda _: "unused") == "mismatch"  # pending


def test_rollback_after_landed_but_unfinalized_activation(store):
    class Failing(FakeFence):
        def after_change(self):
            raise RuntimeError("daily update failed")
    assert activate(store, FakeNextcloud(), Failing()).reason == "job_pause_failed"
    assert act.rollback_vault_root(enabled=True, store=store,
                                   fence=FakeFence()).status == "rolled_back"
    assert store.load_nextcloud()["vault_folder"] == "Other"


def test_unknown_binding_outcome_is_not_rolled_back(store):
    assert activate(store, FakeNextcloud()).status == "activated"
    path = store.state / act.BINDING_FILE
    data = json.loads(path.read_text())
    data["outcome"] = "pending"   # legacy / unknown value
    path.write_text(json.dumps(data))
    assert act.rollback_vault_root(enabled=True, store=store,
                                   fence=FakeFence()).reason == "nothing_to_roll_back"


def test_lost_binding_after_activation_fails_closed(store):
    assert activate(store, FakeNextcloud()).status == "activated"
    (store.state / act.BINDING_FILE).unlink()
    assert act.check_root_pin(store, lambda _: oc_id(11)) == "mismatch"


def test_pending_pin_is_not_trusted(store, monkeypatch):
    real = act._write_binding

    def only_pending(state, current, previous, outcome, clock, source=None):
        return (real(state, current, previous, outcome, clock, source)
                if outcome.startswith("pending") else False)
    monkeypatch.setattr(act, "_write_binding", only_pending)
    result = activate(store, FakeNextcloud())
    assert result == act.ActivationResult("uncertain", "pin_unfinalized")
    assert store.load_nextcloud()["vault_folder"] == "Vault/Notes"
    assert act.check_root_pin(store, lambda _: oc_id(11)) == "mismatch"


def test_job_pause_failure_is_uncertain_and_fail_closed(store):
    class Failing(FakeFence):
        def after_change(self):
            raise RuntimeError("daily update failed")
    assert activate(store, FakeNextcloud(), Failing()) == \
        act.ActivationResult("uncertain", "job_pause_failed")
    assert act.check_root_pin(store, lambda _: oc_id(11)) == "mismatch"


def test_fence_requires_snapshot_job(store):
    with pytest.raises(ValueError):
        act.DashboardJobFence(store, snapshot_job=None)
    with pytest.raises(TypeError):
        act.DashboardJobFence(store)


def test_pin_chain_must_belong_to_recorded_instance():
    assert act._pin({"path": "V", "chain": [oc_id(1)], "instance_id": "ocother"}) is None
    assert act._pin({"path": "V", "chain": [oc_id(1)], "instance_id": None}) is None
    assert act._pin({"path": "V", "chain": [], "instance_id": ACCOUNT.instance_id}) is None
    assert act._pin({"path": "V", "chain": None, "instance_id": None}) is not None
