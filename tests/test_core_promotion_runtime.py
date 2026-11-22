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
        runtime.checkpoint_path.write_text("synthetic-checkpoint")
        return "a"*40, "b"*40, runtime.checkpoint_path
    monkeypatch.setattr(module, "bootstrap_production_checkpoint", fake_bootstrap)
    with pytest.raises(Invalid):
        runtime.bootstrap({"core_commit": "latest", "policy_sha256": runtime.policy_sha256})
    result = runtime.bootstrap({"core_commit":"a"*40, "policy_sha256":runtime.policy_sha256})
    assert result["baseline_commit"] == "a"*40
    assert len(calls) == 1
    assert runtime.policy_path.read_bytes() == runtime.policy_bytes
    with pytest.raises(Invalid):
        runtime.bootstrap({"core_commit":"a"*40, "policy_sha256":runtime.policy_sha256})
