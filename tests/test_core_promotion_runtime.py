"""Core Promotion runtime acceptance with synthetic credentials and no remote I/O."""
from types import SimpleNamespace

import pytest

from obsidian_automation import core_promotion_runtime as module
from obsidian_automation.core_promotion import CORE_REPOSITORY, PROMOTION_POLICY_VERSION
from obsidian_automation.core_promotion_runtime import CorePromotionRuntime, Invalid


class SyntheticSharedStore:
    def __init__(self, root):
        self.state = root
        self.path = root / "connections.json"
        self.path.write_bytes(b'{"mock":"synthetic-only"}')
        self.connection = dict(
            webdav_url="https://cloud.invalid/remote.php/dav/files/mock/",
            vault_folder="My Vault",
            nextcloud_user="mock",
            nextcloud_password="SYNTHETIC-NO-LIVE-PASSWORD",
        )

    def load_nextcloud(self):
        return dict(self.connection)


def ready(tmp_path, monkeypatch):
    shared = SyntheticSharedStore(tmp_path)
    runtime = CorePromotionRuntime(tmp_path, shared, lambda: None)
    runtime.policy_path.write_bytes(runtime.policy_bytes)
    runtime._ensure_target_binding(runtime._target_identity(), "a" * 40)
    monkeypatch.setattr(module, "load_checkpoint", lambda _: SimpleNamespace(
        source_repository=CORE_REPOSITORY, policy_version=PROMOTION_POLICY_VERSION,
        policy_sha256=runtime.policy_sha256,
    ))
    return runtime, shared


def test_missing_checkpoint_and_policy_refuse_automatic_run(tmp_path):
    store = SyntheticSharedStore(tmp_path)
    runtime = CorePromotionRuntime(tmp_path, store, lambda: None)
    assert runtime.configured() is False
    assert runtime.source_generation() is None
    with pytest.raises(Invalid):
        runtime.run()


def test_verified_private_password_is_ephemeral_and_status_is_bounded(tmp_path, monkeypatch):
    runtime, _ = ready(tmp_path, monkeypatch)
    assert runtime.configured() is True
    seen = {}

    def fake_promotion_cycle(**kwargs):
        password_file = kwargs["password_file"]
        seen["path"] = password_file
        assert password_file.read_text() == "SYNTHETIC-NO-LIVE-PASSWORD"
        assert password_file.stat().st_mode & 0o777 == 0o600
        assert kwargs["base_url"] == (
            "https://cloud.invalid/remote.php/dav/files/mock/My%20Vault/"
        )
        assert kwargs["username"] == "mock"
        assert kwargs["config_path"] == runtime.policy_path
        assert kwargs["state_root"] == runtime.root
        return SimpleNamespace(
            result="promoted", base_commit="a"*40, head_commit="b"*40,
            plan_sha256="c"*64, receipt_sha256="d"*64,
        )

    monkeypatch.setattr(module, "run_promotion_cycle", fake_promotion_cycle)
    result = runtime.run()
    assert result == dict(status="promoted", base_commit="a"*40,
                          head_commit="b"*40, plan_sha256="c"*64,
                          receipt_sha256="d"*64)
    assert not seen["path"].exists()
    assert "SYNTHETIC-NO-LIVE-PASSWORD" not in str(runtime.status())


def test_connection_rotation_rechecked_before_any_effect(tmp_path, monkeypatch):
    runtime, shared = ready(tmp_path, monkeypatch)
    call_count = [0]
    old_guard = runtime.guard

    def rotate():
        shared.path.write_bytes(b'{"mock":"rotated"}')
        return old_guard()
    runtime.guard = rotate

    monkeypatch.setattr(module, "run_promotion_cycle", lambda **kw:
                        call_count.__setitem__(0, call_count[0]+1))
    with pytest.raises(Invalid):
        runtime.run()
    assert call_count == [0]


def test_mismatched_policy_or_system_mapping_refuses(tmp_path, monkeypatch):
    runtime, _ = ready(tmp_path, monkeypatch)
    runtime.policy_path.write_bytes(b"not-the-reviewed-policy")
    assert not runtime.configured()
    monkeypatch.setattr(module, "physical_folder", lambda state, role: "SomewhereElse")
    runtime.policy_path.write_bytes(runtime.policy_bytes)
    assert not runtime.configured()


def test_bootstrap_explicit_sha_and_no_canonical_put(tmp_path, monkeypatch):
    store = SyntheticSharedStore(tmp_path)
    runtime = CorePromotionRuntime(tmp_path, store, lambda: None)
    calls = []
    def fake_bootstrap(**kwargs):
        calls.append(kwargs)
        kwargs["before_checkpoint"]("a"*40, "b"*40, runtime.policy_sha256)
        runtime.checkpoint_path.write_text("synthetic-checkpoint")
        return "a"*40, "b"*40, runtime.checkpoint_path
    monkeypatch.setattr(module, "bootstrap_production_checkpoint", fake_bootstrap)
    with pytest.raises(Invalid):
        runtime.bootstrap({"core_commit": "latest", "policy_sha256": runtime.policy_sha256})
    result = runtime.bootstrap({"core_commit":"a"*40, "policy_sha256":runtime.policy_sha256})
    assert result["baseline_commit"] == "a"*40
    assert len(calls) == 1
    assert runtime.policy_path.read_bytes() == runtime.policy_bytes
    assert runtime._target_bound() is True
    assert runtime._binding()["core_commit"] == "a" * 40
    with pytest.raises(Invalid):
        runtime.bootstrap({"core_commit":"a"*40, "policy_sha256":runtime.policy_sha256})


def test_checkpoint_bound_to_exact_vault_target_and_sticky_quarantine(tmp_path, monkeypatch):
    runtime, shared = ready(tmp_path, monkeypatch)
    original = dict(shared.connection)
    assert runtime.configured() is True
    assert runtime._target_bound() is True

    # Moving to another Vault with the same Core policy may not reuse a
    # checkpoint even for a manual Run Once while the scheduler is OFF.
    shared.connection["vault_folder"] = "Other Vault"
    assert runtime.configured() is False
    assert runtime.status()["requires_reauthorization"] is True
    with pytest.raises(Invalid):
        runtime.run()

    # UI-controlled A->B->A rotation burns the old target authority
    # permanently, even if the saved connection later looks like A again.
    shared.connection = dict(original)
    assert runtime.quarantine_for_target_change(
        {**original, "vault_folder": "Other Vault"}
    ) is True
    assert runtime.quarantine_path.read_bytes() == b"target_changed\n"
    assert runtime.configured() is False
    shared.connection["vault_folder"] = "Other Vault"
    shared.connection["vault_folder"] = original["vault_folder"]
    assert runtime.configured() is False
    with pytest.raises(Invalid):
        runtime.run()


def test_same_target_credential_rotation_is_not_target_change(tmp_path, monkeypatch):
    runtime, shared = ready(tmp_path, monkeypatch)
    candidate = {**shared.connection, "nextcloud_password": "ROTATED-SYNTHETIC"}
    assert runtime.quarantine_for_target_change(candidate) is False
    shared.connection.update(candidate)
    assert runtime.configured() is True
    assert not runtime.quarantine_path.exists()


def test_target_binding_cannot_be_replaced_or_retargeted(tmp_path, monkeypatch):
    runtime, shared = ready(tmp_path, monkeypatch)
    original = runtime._target_identity()
    assert runtime.target_binding_path.stat().st_mode & 0o777 == 0o600
    runtime._ensure_target_binding(original, "a" * 40)
    with pytest.raises(Invalid):
        runtime._ensure_target_binding(original, "b" * 40)
    shared.connection["vault_folder"] = "new"
    with pytest.raises(Invalid):
        runtime._ensure_target_binding(runtime._target_identity(), "a" * 40)


def test_invalid_generated_baseline_must_not_seal_target(tmp_path, monkeypatch):
    from obsidian_automation.promotion_deployment import PromotionDeploymentError

    store = SyntheticSharedStore(tmp_path)
    runtime = CorePromotionRuntime(tmp_path, store, lambda: None)

    def invalid_baseline(**kwargs):
        # The real bootstrap rejects non-generated or nonancestor commits
        # before invoking its new before_checkpoint callback.
        raise PromotionDeploymentError("not_a_generated_projection")

    monkeypatch.setattr(module, "bootstrap_production_checkpoint", invalid_baseline)
    with pytest.raises(PromotionDeploymentError):
        runtime.bootstrap({"core_commit":"a"*40, "policy_sha256":runtime.policy_sha256})
    assert not runtime.checkpoint_path.exists()
    assert not runtime.target_binding_path.exists()


def test_legacy_or_malformed_target_seal_does_not_adopt_checkpoint(
    tmp_path, monkeypatch,
):
    runtime, _ = ready(tmp_path, monkeypatch)
    assert runtime.configured()
    runtime.target_binding_path.unlink()
    assert not runtime.configured()
    runtime.target_binding_path.write_text("{bad-json")
    assert not runtime.configured()
    runtime.target_binding_path.write_text('{"untrusted":true}')
    assert not runtime.configured()


def test_root_pin_replacement_invalidates_sealed_target(tmp_path, monkeypatch):
    from obsidian_automation.vault_root_activation import RootPin

    runtime, _ = ready(tmp_path, monkeypatch)
    assert runtime.configured()
    monkeypatch.setattr(module, "current_pin", lambda state: RootPin(
        path="My Vault", chain=("external-instance-id",), instance_id="external-instance-id",
    ))
    assert runtime.configured() is False
    with pytest.raises(Invalid):
        runtime.run()


def test_crash_after_target_seal_before_checkpoint_is_replay_safe(
    tmp_path, monkeypatch,
):
    store = SyntheticSharedStore(tmp_path)
    runtime = CorePromotionRuntime(tmp_path, store, lambda: None)

    def crash_after_seal(**kwargs):
        kwargs["before_checkpoint"]("a"*40, "b"*40, runtime.policy_sha256)
        raise OSError("synthetic_process_crash_before_checkpoint")

    monkeypatch.setattr(module, "bootstrap_production_checkpoint", crash_after_seal)
    request = {"core_commit": "a"*40, "policy_sha256": runtime.policy_sha256}
    with pytest.raises(OSError):
        runtime.bootstrap(request)
    assert runtime._target_bound()
    assert not runtime.checkpoint_path.exists()

    # A different baseline cannot reuse this interrupted authorization.
    with pytest.raises(Invalid):
        runtime.bootstrap({**request, "core_commit": "c"*40})

    def finish(**kwargs):
        kwargs["before_checkpoint"]("a"*40, "b"*40, runtime.policy_sha256)
        runtime.checkpoint_path.write_text("synthetic-only")
        return "a"*40, "b"*40, runtime.checkpoint_path

    monkeypatch.setattr(module, "bootstrap_production_checkpoint", finish)
    assert runtime.bootstrap(request)["initialized"] is True
    assert runtime._binding()["core_commit"] == "a"*40
