"""#511 P2/A: real disposable B2 SQLite + file Git remote E2E; NO live endpoint."""
from __future__ import annotations

import dataclasses
import fcntl
import json
import os
import sqlite3
import subprocess
import uuid
from pathlib import Path

import pytest

import obsidian_automation.local_publication_delivery as delivery
import obsidian_automation.local_publication_preview as preview
from obsidian_automation.snapshot_job import SnapshotJob


def git(path: Path, *args: str) -> str:
    p = subprocess.run(
        ["git", *args], cwd=path, capture_output=True, text=True, check=True
    )
    return p.stdout.strip()


def init_work(path: Path) -> None:
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "Fixture")
    git(path, "config", "user.email", "test@example.invalid")


def commit(path: Path, label: str) -> str:
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", label)
    return git(path, "rev-parse", "HEAD")


def advance(path: Path, relative: str, content: str) -> str:
    target = path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    new = commit(path, "reviewed change")
    git(path, "push", "-q", "origin", "HEAD:main")
    return new


def seed_snapshot(state: Path, remote_ref: str, *, status: str = "saved") -> str:
    model = SnapshotJob(
        state, lambda run_id: (status, "verify", remote_ref),
        connection_generation="fixture-generation-r1",
    )
    try:
        outcome, record = model.submit(uuid.uuid4().hex, trigger="scheduled")
        assert outcome == "accepted" and record
        return record["run_id"]
    finally:
        # Close waits for the real SnapshotJob worker and persists its result.
        model.close()


@pytest.fixture
def fixture(tmp_path: Path):
    tmp_path.chmod(0o700)
    marker = tmp_path / ".oa511-p2-disposable-fixture"
    marker.write_text(delivery._MARKER)
    marker.chmod(0o600)
    private_git = tmp_path / "source.git"
    public_git = tmp_path / "core.git"
    private_git.mkdir()
    public_git.mkdir()
    git(private_git, "init", "--bare", "-q", "--initial-branch=main")
    git(public_git, "init", "--bare", "-q", "--initial-branch=main")
    src = tmp_path / "private_work"
    core = tmp_path / "public_work"
    init_work(src)
    init_work(core)
    (src / "98-System").mkdir()
    (core / "98-System").mkdir()
    (src / "98-System/view.js").write_text("new\n")
    (core / "98-System/view.js").write_text("old\n")
    scripts = core / "98-System/99-dev"
    (scripts / "test").mkdir(parents=True)
    (scripts / "validate-repo.mjs").write_text("process.exit(0);\n")
    (scripts / "test/smoke.test.mjs").write_text(
        "import test from 'node:test'; test('ok', () => {});\n"
    )
    commit(src, "canonical private snapshot")
    commit(core, "Sync public projection from ObsidianVault")
    git(src, "remote", "add", "origin", private_git.as_uri())
    git(core, "remote", "add", "origin", public_git.as_uri())
    git(src, "push", "-q", "origin", "HEAD:main")
    git(core, "push", "-q", "origin", "HEAD:main")
    src_sha, core_sha = git(src, "rev-parse", "HEAD"), git(core, "rev-parse", "HEAD")
    b2_state = tmp_path / "b2-state"
    run_id = seed_snapshot(b2_state, src_sha)
    policy = tmp_path / "public-export.toml"
    policy.write_text(
        'version = 1\nstrict_missing = true\ninclude = ["98-System/view.js"]\n'
        'repository_owned = [".git", ".git/**", ".github/**", "README.md", '
        '"98-System/99-dev/**"]\n'
    )
    config = delivery.FixtureDeliveryConfig(
        preview=preview.PreviewConfig(
            private_git.as_uri(), public_git.as_uri(), policy,
            tmp_path / "p2-state", "a" * 40, allow_file_fixture=True,
        ),
        b2_state=b2_state,
        b2_run_id=run_id,
        b2_connection_generation="fixture-generation-r1",
        fixture_root=tmp_path,
        enabled=True,
    )
    return {
        "config": config, "source": src, "core": core,
        "source_git": private_git, "core_git": public_git,
        "source_sha": src_sha, "core_sha": core_sha,
    }


def config_result(fixture):
    return delivery.deliver_disposable_fixture(fixture["config"])


def state_path(fixture) -> Path:
    cfg = fixture["config"]
    return cfg.preview.state_dir / "deliveries" / cfg.b2_run_id


def test_fixture_opt_in_refuses_before_any_action(fixture) -> None:
    cfg = fixture["config"]
    with pytest.raises(delivery.DeliveryError, match="fixture_disabled"):
        delivery.deliver_disposable_fixture(dataclasses.replace(cfg, enabled=False))
    assert not cfg.preview.state_dir.exists()
    assert git(fixture["core_git"], "rev-parse", "main") == fixture["core_sha"]


def test_real_https_destination_cannot_be_pushed_even_when_enabled(fixture) -> None:
    cfg = fixture["config"]
    changed = dataclasses.replace(
        cfg, preview=dataclasses.replace(
            cfg.preview, core_url="https://github.com/upiscium/ObsidianCore.git"
        )
    )
    with pytest.raises(delivery.DeliveryError, match="fixture_url_only"):
        delivery.deliver_disposable_fixture(changed)
    assert not cfg.preview.state_dir.exists()


def test_wrong_source_repository_is_refused(fixture, tmp_path) -> None:
    cfg = fixture["config"]
    changed = dataclasses.replace(
        cfg, preview=dataclasses.replace(
            cfg.preview, source_url=(tmp_path / "core.git").as_uri()
        )
    )
    with pytest.raises(delivery.DeliveryError, match="wrong_fixture_repository"):
        delivery.deliver_disposable_fixture(changed)


def test_latest_verified_b2_run_required(fixture) -> None:
    cfg = fixture["config"]
    assert delivery.read_verified_b2_run(
        cfg.b2_state, run_id=cfg.b2_run_id,
        connection_generation=cfg.b2_connection_generation,
    ).remote_ref == fixture["source_sha"]
    with pytest.raises(delivery.DeliveryError, match="b2_binding_unverified"):
        delivery.deliver_disposable_fixture(
            dataclasses.replace(cfg, b2_connection_generation="wrong")
        )
    with pytest.raises(delivery.DeliveryError, match="b2_not_latest"):
        delivery.deliver_disposable_fixture(
            dataclasses.replace(cfg, b2_run_id="b" * 32)
        )


@pytest.mark.parametrize("field,value", [
    ("status", "failed"), ("status", "unconfirmed"),
    ("stage", "push"), ("remote_ref", None),
])
def test_b2_failure_record_never_reaches_git(fixture, field, value) -> None:
    cfg = fixture["config"]
    with sqlite3.connect(cfg.b2_state / "snapshot.sqlite3") as db:
        db.execute(f"UPDATE runs SET {field}=? WHERE run_id=?",
                   (value, cfg.b2_run_id))
    with pytest.raises(delivery.DeliveryError, match="b2_run_not_verified"):
        config_result(fixture)
    assert not cfg.preview.state_dir.exists()
    assert git(fixture["core_git"], "rev-parse", "main") == fixture["core_sha"]


def test_verified_candidate_push_has_independent_commit_tree_readback(fixture) -> None:
    got = config_result(fixture)
    assert got.status == "published_verified"
    assert got.source_sha == fixture["source_sha"]
    assert got.core_before_sha == fixture["core_sha"]
    assert got.candidate_sha and got.candidate_sha != got.core_before_sha
    assert git(fixture["core_git"], "rev-parse", "main") == got.candidate_sha
    assert git(fixture["source_git"], "rev-parse", "main") == fixture["source_sha"]
    assert git(fixture["core_git"], "show", "main:98-System/view.js") == "new"
    assert git(fixture["core_git"], "rev-list", "--parents", "-n", "1", "main").split() == [
        got.candidate_sha, fixture["core_sha"]
    ]
    intent = state_path(fixture) / "intent.json"
    receipt = state_path(fixture) / "result.json"
    for record in (intent, receipt):
        assert record.stat().st_mode & 0o777 == 0o600
        assert fixture["config"].preview.source_url not in record.read_text()
        assert fixture["config"].preview.core_url not in record.read_text()
        assert "98-System/view.js" not in record.read_text()
        assert "secret" not in record.read_text().lower()
    assert json.loads(receipt.read_text())["status"] == "published_verified"


def test_repeat_run_is_idempotent_no_second_push(fixture) -> None:
    first = config_result(fixture)
    second = config_result(fixture)
    assert second.status == "already_published_verified"
    assert second.candidate_sha == first.candidate_sha
    assert git(fixture["core_git"], "rev-parse", "main") == first.candidate_sha
    assert git(fixture["core_git"], "rev-list", "--count",
               f"{fixture['core_sha']}..main") == "1"


def test_no_change_snapshot_never_emits_core_push(fixture) -> None:
    cfg = fixture["config"]
    # Move private snapshot to the existing public managed bytes.
    source_sha = advance(fixture["source"], "98-System/view.js", "old\n")
    run = seed_snapshot(cfg.b2_state, source_sha, status="no_changes")
    config = dataclasses.replace(cfg, b2_run_id=run)
    first = delivery.deliver_disposable_fixture(config)
    second = delivery.deliver_disposable_fixture(config)
    assert first.status == "no_changes_verified"
    assert first.candidate_sha is None
    assert second.status == "already_no_changes_verified"
    assert git(fixture["core_git"], "rev-parse", "main") == fixture["core_sha"]
    assert not (cfg.preview.state_dir / "deliveries" / run / "reconcile.json").exists()


def test_core_remote_moves_after_preview_and_blocks_push(fixture, monkeypatch) -> None:
    original = preview.prepare_local_preview

    def advance_core_after_preview(evidence, config):
        result = original(evidence, config)
        advance(fixture["core"], "README.md", "reviewed independent change\n")
        return result

    monkeypatch.setattr(preview, "prepare_local_preview", advance_core_after_preview)
    first = config_result(fixture)
    assert first.status == "blocked_conflict"
    assert first.candidate_sha
    assert git(fixture["core_git"], "rev-parse", "main") != first.candidate_sha
    assert config_result(fixture).status == "blocked_conflict"


def test_snapshot_ref_moved_before_preview_blocks_push(fixture) -> None:
    advance(fixture["source"], "98-System/view.js", "newer\n")
    with pytest.raises(preview.PreviewError, match="snapshot_ref_mismatch"):
        config_result(fixture)
    assert git(fixture["core_git"], "rev-parse", "main") == fixture["core_sha"]
    assert not list(fixture["config"].preview.state_dir.glob("deliveries/*"))


def test_lost_ack_after_push_is_independently_read_back(fixture, monkeypatch) -> None:
    """No retry: the first push applied but transport appeared to fail."""
    original = preview._git
    pushes = []

    def lost_ack(args, *, env, cwd, **kwargs):
        if args[0] == "push":
            pushes.append(tuple(args))
            original(args, env=env, cwd=cwd, **kwargs)
            raise preview.PreviewError("synthetic_lost_ack")
        return original(args, env=env, cwd=cwd, **kwargs)

    monkeypatch.setattr(preview, "_git", lost_ack)
    first = config_result(fixture)
    assert first.status == "published_verified"
    assert first.candidate_sha == git(fixture["core_git"], "rev-parse", "main")
    assert len(pushes) == 1
    second = config_result(fixture)
    assert second.status == "already_published_verified"
    assert len(pushes) == 1


def test_network_failure_before_push_is_unknown_without_retry(fixture, monkeypatch) -> None:
    original = preview._git
    pushes = []

    def connection_failed(args, *, env, cwd, **kwargs):
        if args[0] == "push":
            pushes.append(tuple(args))
            raise preview.PreviewError("synthetic_network_unknown")
        return original(args, env=env, cwd=cwd, **kwargs)

    monkeypatch.setattr(preview, "_git", connection_failed)
    first = config_result(fixture)
    assert first.status == "unknown"
    assert first.candidate_sha is not None
    assert git(fixture["core_git"], "rev-parse", "main") == fixture["core_sha"]
    monkeypatch.setattr(preview, "_git", original)
    second = config_result(fixture)
    assert second.status == "unknown"
    assert len(pushes) == 1
    assert git(fixture["core_git"], "rev-parse", "main") == fixture["core_sha"]
    assert json.loads((state_path(fixture) / "result.json").read_text())["status"] == "unknown"


def test_later_reconciliation_of_unknown_is_read_only(fixture, monkeypatch) -> None:
    original = preview._git

    def no_send(args, *, env, cwd, **kwargs):
        if args[0] == "push":
            raise preview.PreviewError("synthetic_delivery_unknown")
        return original(args, env=env, cwd=cwd, **kwargs)

    monkeypatch.setattr(preview, "_git", no_send)
    first = config_result(fixture)
    assert first.status == "unknown"
    monkeypatch.setattr(preview, "_git", original)
    assert config_result(fixture).status == "unknown"
    intent = json.loads((state_path(fixture) / "intent.json").read_text())
    candidate = str(intent["candidate_sha"])
    local_core = fixture["config"].preview.state_dir / "runs" / intent["run_key"] / "core"
    # Simulate an external/late application to this disposable file remote.
    git(local_core, "push", "--no-verify",
        fixture["config"].preview.core_url, f"{candidate}:refs/heads/main")
    reconciled = config_result(fixture)
    assert reconciled.status == "reconciled_verified"
    assert reconciled.candidate_sha == candidate
    assert git(fixture["core_git"], "rev-parse", "main") == candidate
    assert config_result(fixture).status == "already_published_verified"
    record = json.loads((state_path(fixture) / "reconcile.json").read_text())
    assert record["status"] == "reconciled_verified"


def test_successful_push_with_unavailable_readback_is_unknown(fixture, monkeypatch) -> None:
    original = delivery._readback

    def transient_readback(*args, **kwargs):
        raise preview.PreviewError("readback_unavailable")

    monkeypatch.setattr(delivery, "_readback", transient_readback)
    first = config_result(fixture)
    assert first.status == "unknown"
    assert first.candidate_sha == git(fixture["core_git"], "rev-parse", "main")
    monkeypatch.setattr(delivery, "_readback", original)
    assert config_result(fixture).status == "reconciled_verified"


def test_crash_after_intent_does_not_automatically_repush(fixture, monkeypatch) -> None:
    original = preview._git

    def crash_before_send(args, *, env, cwd, **kwargs):
        if args[0] == "push":
            raise KeyboardInterrupt("synthetic interruption before push")
        return original(args, env=env, cwd=cwd, **kwargs)

    monkeypatch.setattr(preview, "_git", crash_before_send)
    with pytest.raises(KeyboardInterrupt):
        config_result(fixture)
    assert (state_path(fixture) / "intent.json").is_file()
    assert not (state_path(fixture) / "result.json").exists()
    monkeypatch.setattr(preview, "_git", original)
    assert config_result(fixture).status == "unknown"
    assert git(fixture["core_git"], "rev-parse", "main") == fixture["core_sha"]


def test_delivery_lock_prevents_concurrent_mutation(fixture) -> None:
    state = fixture["config"].preview.state_dir
    state.mkdir(mode=0o700)
    fd = os.open(state / "delivery.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(delivery.DeliveryError, match="delivery_busy"):
            config_result(fixture)
    finally:
        os.close(fd)
    assert git(fixture["core_git"], "rev-parse", "main") == fixture["core_sha"]
    assert not list((state / "deliveries").glob("*"))


def test_tampered_policy_prevents_cached_delivery_reuse(fixture) -> None:
    first = config_result(fixture)
    assert first.status == "published_verified"
    policy = fixture["config"].preview.policy_path
    policy.write_text(policy.read_text() + "\n# new policy revision\n")
    with pytest.raises(delivery.DeliveryError, match="delivery_authority_changed"):
        config_result(fixture)


def test_b2_connection_write_downgrade_refuses_to_deliver(fixture) -> None:
    cfg = fixture["config"]
    with sqlite3.connect(cfg.b2_state / "snapshot.sqlite3") as db:
        db.execute("UPDATE connection_write SET verified=0 WHERE id=1")
    with pytest.raises(delivery.DeliveryError, match="b2_binding_unverified"):
        config_result(fixture)
    assert not cfg.preview.state_dir.exists()


def test_b2_source_destination_binding_mismatch_refuses_to_deliver(fixture) -> None:
    cfg = fixture["config"]
    with sqlite3.connect(cfg.b2_state / "snapshot.sqlite3") as db:
        db.execute("UPDATE settings SET destination_name='other' WHERE id=1")
    with pytest.raises(delivery.DeliveryError, match="b2_binding_unverified"):
        config_result(fixture)


def test_untrusted_b2_db_symlink_refuses_delivery(fixture) -> None:
    cfg = fixture["config"]
    db = cfg.b2_state / "snapshot.sqlite3"
    safe = cfg.b2_state / "safe-original.db"
    db.rename(safe)
    db.symlink_to(safe)
    with pytest.raises(delivery.DeliveryError, match="untrusted_b2_state"):
        config_result(fixture)


def test_missing_disposable_marker_blocks_all_effects(fixture) -> None:
    marker = fixture["config"].fixture_root / ".oa511-p2-disposable-fixture"
    marker.unlink()
    with pytest.raises(delivery.DeliveryError, match="untrusted_fixture_root"):
        config_result(fixture)
    assert git(fixture["core_git"], "rev-parse", "main") == fixture["core_sha"]
    assert not fixture["config"].preview.state_dir.exists()


def test_fixture_state_cannot_escape_disposable_root(fixture, tmp_path) -> None:
    cfg = fixture["config"]
    outside = tmp_path.parent / "not-a-fixture-preview"
    changed = dataclasses.replace(
        cfg, preview=dataclasses.replace(cfg.preview, state_dir=outside)
    )
    with pytest.raises(delivery.DeliveryError, match="fixture_out_of_bounds"):
        delivery.deliver_disposable_fixture(changed)
    assert not outside.exists()
    assert git(fixture["core_git"], "rev-parse", "main") == fixture["core_sha"]


def test_fixture_ancestor_symlink_outside_root_rejected(fixture, tmp_path) -> None:
    cfg = fixture["config"]
    external = tmp_path.parent
    redirected = tmp_path / "escape"
    redirected.symlink_to(external, target_is_directory=True)
    changed = dataclasses.replace(
        cfg, preview=dataclasses.replace(
            cfg.preview, state_dir=redirected / "p2-state"
        )
    )
    with pytest.raises(delivery.DeliveryError, match="fixture_out_of_bounds"):
        delivery.deliver_disposable_fixture(changed)
    assert git(fixture["core_git"], "rev-parse", "main") == fixture["core_sha"]


def test_untrusted_fixture_root_mode_refused(fixture) -> None:
    root = fixture["config"].fixture_root
    root.chmod(0o755)
    try:
        with pytest.raises(delivery.DeliveryError, match="untrusted_fixture_root"):
            config_result(fixture)
    finally:
        root.chmod(0o700)
    assert not fixture["config"].preview.state_dir.exists()


def test_newer_failed_b2_run_blocks_older_success(fixture) -> None:
    cfg = fixture["config"]
    # A newer failed Snapshot may invalidate old source freshness.
    seed_snapshot(cfg.b2_state, fixture["source_sha"], status="failed")
    with pytest.raises(delivery.DeliveryError, match="b2_not_latest"):
        config_result(fixture)
    assert git(fixture["core_git"], "rev-parse", "main") == fixture["core_sha"]


def test_b2_connection_generation_rotation_revokes_old_admission(fixture) -> None:
    cfg = fixture["config"]
    model = SnapshotJob(
        cfg.b2_state, lambda _: ("saved", "verify", fixture["source_sha"]),
        connection_generation="fixture-generation-rotated",
    )
    model.close()
    with pytest.raises(delivery.DeliveryError, match="b2_binding_unverified"):
        config_result(fixture)
    assert not cfg.preview.state_dir.exists()


def test_source_ref_moves_after_preview_blocks_fixture_push(fixture, monkeypatch) -> None:
    original = preview.prepare_local_preview

    def change_source_after_preview(evidence, cfg):
        result = original(evidence, cfg)
        advance(fixture["source"], "98-System/view.js", "updated by new B2\n")
        return result

    monkeypatch.setattr(preview, "prepare_local_preview", change_source_after_preview)
    got = config_result(fixture)
    assert got.status == "blocked_conflict"
    assert git(fixture["core_git"], "rev-parse", "main") == fixture["core_sha"]


def test_non_fast_forward_race_does_not_overwrite_reviewed_core(fixture, monkeypatch) -> None:
    original = preview._git
    race_heads = []

    def concurrent_core_update(args, *, env, cwd, **kwargs):
        if args[0] == "push":
            race_heads.append(
                advance(fixture["core"], "README.md", "reviewed while delivering\n")
            )
        return original(args, env=env, cwd=cwd, **kwargs)

    monkeypatch.setattr(preview, "_git", concurrent_core_update)
    result = config_result(fixture)
    assert race_heads
    assert result.status == "unknown"  # Not a proof of no write or of success.
    assert git(fixture["core_git"], "rev-parse", "main") == race_heads[0]
    assert git(fixture["core_git"], "rev-parse", "main") != result.candidate_sha
    # A second call must NOT resend the rejected/ambiguous mutation.
    assert config_result(fixture).status == "unknown"


def test_git_push_always_uses_non_force_single_refspec(fixture, monkeypatch) -> None:
    original = preview._git
    actual = []

    def record_push(args, *, env, cwd, **kwargs):
        if args[0] == "push":
            actual.append(tuple(args))
        return original(args, env=env, cwd=cwd, **kwargs)

    monkeypatch.setattr(preview, "_git", record_push)
    result = config_result(fixture)
    assert result.status == "published_verified"
    assert actual == [(
        "push", "--porcelain", "--no-verify",
        fixture["config"].preview.core_url,
        f"{result.candidate_sha}:refs/heads/main",
    )]
    assert not any(arg.startswith("+") or "--force" in arg for arg in actual[0])


def test_connection_revoked_after_p1_validation_blocks_push(fixture, monkeypatch) -> None:
    original = preview.prepare_local_preview

    def revoke_after_preview(evidence, cfg):
        result = original(evidence, cfg)
        with sqlite3.connect(fixture["config"].b2_state / "snapshot.sqlite3") as db:
            db.execute("UPDATE connection_write SET verified=0 WHERE id=1")
        return result

    monkeypatch.setattr(preview, "prepare_local_preview", revoke_after_preview)
    with pytest.raises(delivery.DeliveryError, match="b2_binding_unverified"):
        config_result(fixture)
    assert git(fixture["core_git"], "rev-parse", "main") == fixture["core_sha"]
    assert not list(fixture["config"].preview.state_dir.glob("deliveries/*"))


def test_new_latest_b2_run_after_preview_blocks_old_snapshot(fixture, monkeypatch) -> None:
    original = preview.prepare_local_preview

    def new_b2_run_after_preview(evidence, cfg):
        result = original(evidence, cfg)
        seed_snapshot(fixture["config"].b2_state, fixture["source_sha"])
        return result

    monkeypatch.setattr(preview, "prepare_local_preview", new_b2_run_after_preview)
    with pytest.raises(delivery.DeliveryError, match="b2_not_latest"):
        config_result(fixture)
    assert git(fixture["core_git"], "rev-parse", "main") == fixture["core_sha"]


def test_policy_mutation_after_preview_blocks_push(fixture, monkeypatch) -> None:
    original = preview.prepare_local_preview

    def mutable_policy_after_preview(evidence, cfg):
        result = original(evidence, cfg)
        cfg.policy_path.write_text(cfg.policy_path.read_text() + "# R2\n")
        return result

    monkeypatch.setattr(preview, "prepare_local_preview", mutable_policy_after_preview)
    with pytest.raises(delivery.DeliveryError, match="policy_changed_before_delivery"):
        config_result(fixture)
    assert git(fixture["core_git"], "rev-parse", "main") == fixture["core_sha"]


def test_push_does_not_inherit_ambient_tokens(fixture, monkeypatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "UNSAFE_CANARY")
    monkeypatch.setenv("GIT_ASKPASS", "/tmp/untrusted")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/untrusted-agent")
    original = preview._git
    observed = 0

    def check_push(args, *, env, cwd, **kwargs):
        nonlocal observed
        if args[0] == "push":
            observed += 1
            assert "GH_TOKEN" not in env
            assert "GIT_ASKPASS" not in env
            assert "SSH_AUTH_SOCK" not in env
            assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
        return original(args, env=env, cwd=cwd, **kwargs)

    monkeypatch.setattr(preview, "_git", check_push)
    assert config_result(fixture).status == "published_verified"
    assert observed == 1
    for doc in state_path(fixture).glob("*.json"):
        assert "UNSAFE_CANARY" not in doc.read_text()


def test_remote_moves_during_independent_readback_is_not_current_success(
    fixture, monkeypatch,
) -> None:
    original = preview._git
    altered = []

    def move_after_local_tree(args, *, env, cwd, **kwargs):
        result = original(args, env=env, cwd=cwd, **kwargs)
        if (
            not altered and args[0] == "rev-parse" and len(args) == 2
            and args[1].endswith("^{tree}") and Path(cwd).name == "core"
        ):
            # The candidate was pushed; another synthetic client now
            # advances main before the final live-ref stability check.
            git(fixture["core"], "fetch", "-q", "origin", "main")
            git(fixture["core"], "reset", "-q", "--hard", "FETCH_HEAD")
            altered.append(advance(
                fixture["core"], "README.md", "independent reviewed change\n"
            ))
        return result

    monkeypatch.setattr(preview, "_git", move_after_local_tree)
    first = config_result(fixture)
    assert altered
    assert first.status == "unknown"
    assert git(fixture["core_git"], "rev-parse", "main") == altered[0]
    assert first.candidate_sha != altered[0]
    # No automatic second send when latest ref is now different.
    assert config_result(fixture).status == "unknown"


@pytest.mark.parametrize("nested_parent", ["source.git", "core.git", "b2-state"])
def test_fixture_delivery_state_cannot_live_inside_git_or_b2_tree(
    fixture, nested_parent: str,
) -> None:
    """Fixture state must never modify bare Git internals or the B2 owner's tree."""
    cfg = fixture["config"]
    nested_state = cfg.fixture_root / nested_parent / "publisher-state"
    changed = dataclasses.replace(
        cfg, preview=dataclasses.replace(cfg.preview, state_dir=nested_state)
    )
    with pytest.raises(delivery.DeliveryError, match="fixture_out_of_bounds"):
        delivery.deliver_disposable_fixture(changed)
    assert not nested_state.exists()
    assert git(fixture["core_git"], "rev-parse", "main") == fixture["core_sha"]
    assert git(fixture["source_git"], "rev-parse", "main") == fixture["source_sha"]


@pytest.mark.parametrize("repository_name", ["source.git", "core.git"])
def test_nested_repository_endpoint_cannot_target_b2_tree(
    fixture, repository_name: str,
) -> None:
    """A basename match alone must not admit a nested Git endpoint."""
    cfg = fixture["config"]
    nested = cfg.b2_state / repository_name
    nested.mkdir()
    changed_preview = dataclasses.replace(
        cfg.preview,
        **{
            "source_url" if repository_name == "source.git" else "core_url":
                nested.as_uri()
        },
    )
    changed = dataclasses.replace(cfg, preview=changed_preview)
    with pytest.raises(delivery.DeliveryError, match="wrong_fixture_repository"):
        delivery.deliver_disposable_fixture(changed)
    assert not cfg.preview.state_dir.exists()
    assert git(fixture["core_git"], "rev-parse", "main") == fixture["core_sha"]
    assert git(fixture["source_git"], "rev-parse", "main") == fixture["source_sha"]
