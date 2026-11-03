"""#511 P1 exercises ONLY disposable local Git remotes, never live services."""
from __future__ import annotations

import dataclasses
import fcntl
import json
import os
import subprocess
from pathlib import Path

import pytest

import obsidian_automation.local_publication_preview as lp
from obsidian_automation.local_publication_preview import (
    PreviewConfig,
    PreviewError,
    SnapshotEvidence,
    prepare_local_preview,
)


def git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=path, check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def init_work(path: Path) -> None:
    path.mkdir()
    git(path, "init", "--quiet", "-b", "main")
    git(path, "config", "user.name", "Test")
    git(path, "config", "user.email", "test@example.invalid")


def commit(path: Path, message: str) -> str:
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", message)
    return git(path, "rev-parse", "HEAD")


def bare(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "--bare", "--quiet", "--initial-branch=main")
    return path


def publish_fixture(work: Path, remote: Path) -> str:
    git(work, "remote", "add", "origin", remote.as_uri())
    git(work, "push", "--quiet", "origin", "HEAD:main")
    return git(work, "rev-parse", "HEAD")


def advance(work: Path, name: str, value: str) -> str:
    path = work / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)
    new_sha = commit(work, "User update")
    git(work, "push", "--quiet", "origin", "HEAD:main")
    return new_sha


@pytest.fixture
def fixture(tmp_path: Path):
    source = tmp_path / "source"
    core = tmp_path / "core"
    source_remote = bare(tmp_path / "source.git")
    core_remote = bare(tmp_path / "core.git")
    init_work(source)
    init_work(core)
    (source / "98-System").mkdir()
    (core / "98-System").mkdir()
    (source / "98-System/view.js").write_text("new\n")
    (core / "98-System/view.js").write_text("old\n")
    validator = core / "98-System/99-dev/validate-repo.mjs"
    validator.parent.mkdir(parents=True)
    validator.write_text("process.exit(0);\n")
    tests_dir = core / "98-System/99-dev/test"
    tests_dir.mkdir()
    (tests_dir / "smoke.test.mjs").write_text(
        "import test from 'node:test'; test('smoke', () => {});\n"
    )
    commit(source, "Private snapshot fixture")
    commit(core, "Sync public projection from ObsidianVault")
    source_sha = publish_fixture(source, source_remote)
    core_sha = publish_fixture(core, core_remote)

    policy = tmp_path / "public-export.toml"
    policy.write_text(
        'version = 1\nstrict_missing = true\n'
        'include = ["98-System/view.js"]\n'
        'repository_owned = [".git", ".git/**", ".github/**", "README.md", "98-System/99-dev/**"]\n'
    )
    evidence = SnapshotEvidence("saved", "verify", source_sha, "verified")
    config = PreviewConfig(
        source_url=source_remote.as_uri(),
        core_url=core_remote.as_uri(),
        policy_path=policy,
        state_dir=tmp_path / "state",
        publisher_revision="a" * 40,
        allow_file_fixture=True,
    )
    return {
        "source": source, "core": core, "source_remote": source_remote,
        "core_remote": core_remote, "source_sha": source_sha,
        "core_sha": core_sha, "config": config, "evidence": evidence,
    }


def test_candidate_is_local_validated_and_remote_unmodified(fixture) -> None:
    data = fixture
    got = prepare_local_preview(data["evidence"], data["config"])
    assert got.status == "candidate_prepared"
    assert got.source_sha == data["source_sha"]
    assert got.core_before_sha == data["core_sha"]
    assert got.candidate_sha and got.candidate_sha != data["core_sha"]
    assert len(list(data["config"].state_dir.glob("receipts/*.json"))) == 1
    assert git(data["core_remote"], "rev-parse", "main") == data["core_sha"]
    assert git(data["source_remote"], "rev-parse", "main") == data["source_sha"]
    local_core = data["config"].state_dir / "runs" / got.run_key / "core"
    assert git(local_core, "rev-parse", "HEAD") == got.candidate_sha
    assert git(local_core, "remote") == ""  # Preview worktree cannot push to origin.
    assert git(local_core, "show", "HEAD:98-System/view.js") == "new"
    assert git(local_core, "rev-list", "--parents", "-n", "1", "HEAD").split() == [
        got.candidate_sha, data["core_sha"]
    ]
    receipt = data["config"].state_dir / "receipts" / f"{got.run_key}.json"
    assert receipt.stat().st_mode & 0o777 == 0o600
    assert "git_url" not in receipt.read_text()
    assert "token" not in receipt.read_text()
    assert (data["config"].state_dir / "runs" / got.run_key / "intent.json").stat().st_mode & 0o777 == 0o600


def test_duplicate_reuses_candidate_without_second_commit(fixture) -> None:
    first = prepare_local_preview(fixture["evidence"], fixture["config"])
    second = prepare_local_preview(fixture["evidence"], fixture["config"])
    assert second.status == "already_prepared"
    assert second.candidate_sha == first.candidate_sha
    assert second.run_key == first.run_key
    assert len(list(fixture["config"].state_dir.glob("runs/*"))) == 1
    assert git(fixture["core_remote"], "rev-parse", "main") == fixture["core_sha"]


def test_no_change_records_no_candidate_and_deduplicates(fixture) -> None:
    # Mirror source matches the current Core on all managed paths.
    new = advance(fixture["source"], "98-System/view.js", "old\n")
    evidence = dataclasses.replace(fixture["evidence"], status="no_changes", remote_ref=new)
    first = prepare_local_preview(evidence, fixture["config"])
    second = prepare_local_preview(evidence, fixture["config"])
    assert first.status == "no_changes"
    assert first.candidate_sha is None
    assert second.status == "already_prepared"
    assert second.candidate_sha is None
    assert git(fixture["core_remote"], "rev-parse", "main") == fixture["core_sha"]


@pytest.mark.parametrize(
    "changed",
    [
        {"status": "failed"},
        {"status": "unconfirmed"},
        {"stage": "push"},
        {"stage": "sync"},
        {"gitea_write": "unverified"},
        {"remote_ref": None},
        {"remote_ref": "a" * 39},
    ],
)
def test_unverified_source_is_refused_before_any_work(fixture, changed) -> None:
    evidence = dataclasses.replace(fixture["evidence"], **changed)
    with pytest.raises(PreviewError):
        prepare_local_preview(evidence, fixture["config"])
    assert not fixture["config"].state_dir.exists()


def test_stale_snapshot_does_not_checkout_or_publish(fixture) -> None:
    advance(fixture["source"], "98-System/view.js", "newer\n")
    with pytest.raises(PreviewError, match="snapshot_ref_mismatch"):
        prepare_local_preview(fixture["evidence"], fixture["config"])
    assert list(fixture["config"].state_dir.glob("runs/*")) == []
    assert git(fixture["core_remote"], "rev-parse", "main") == fixture["core_sha"]


def test_core_reviewed_managed_drift_fails_closed(fixture) -> None:
    reviewed_sha = advance(fixture["core"], "98-System/view.js", "reviewed change\n")
    with pytest.raises(PreviewError, match="projection_rejected"):
        prepare_local_preview(fixture["evidence"], fixture["config"])
    assert git(fixture["core_remote"], "rev-parse", "main") == reviewed_sha
    assert list(fixture["config"].state_dir.glob("receipts/*")) == []
    assert len(list(fixture["config"].state_dir.glob("runs/*/intent.json"))) == 1
    failure = next(fixture["config"].state_dir.glob("runs/*/failure.json"))
    assert failure.stat().st_mode & 0o777 == 0o600
    assert json.loads(failure.read_text())["reason"] == "projection_rejected"


def test_node_validation_error_cannot_publish(fixture) -> None:
    reviewed_sha = advance(
        fixture["core"], "98-System/99-dev/validate-repo.mjs",
        "console.error('SECRET_CANARY_DO_NOT_LOG'); process.exit(2);\n",
    )
    with pytest.raises(PreviewError, match="projection_rejected"):
        prepare_local_preview(fixture["evidence"], fixture["config"])
    assert git(fixture["core_remote"], "rev-parse", "main") == reviewed_sha
    assert list(fixture["config"].state_dir.glob("receipts/*")) == []
    for record in fixture["config"].state_dir.rglob("*.json"):
        assert "SECRET_CANARY_DO_NOT_LOG" not in record.read_text()


def test_source_moves_after_candidate_generation(fixture, monkeypatch) -> None:
    original = lp._run_publisher

    def advance_before_readback(*args, **kwargs):
        original(*args, **kwargs)
        advance(fixture["source"], "98-System/new.md", "new, after clone\n")

    monkeypatch.setattr(lp, "_run_publisher", advance_before_readback)
    with pytest.raises(PreviewError, match="source_moved_after_preview"):
        prepare_local_preview(fixture["evidence"], fixture["config"])
    assert list(fixture["config"].state_dir.glob("receipts/*")) == []
    assert git(fixture["core_remote"], "rev-parse", "main") == fixture["core_sha"]


def test_core_moves_after_candidate_generation(fixture, monkeypatch) -> None:
    original = lp._run_publisher

    def change_core_before_readback(*args, **kwargs):
        original(*args, **kwargs)
        advance(fixture["core"], "README.md", "Reviewed independent change\n")

    monkeypatch.setattr(lp, "_run_publisher", change_core_before_readback)
    with pytest.raises(PreviewError, match="core_moved_after_preview"):
        prepare_local_preview(fixture["evidence"], fixture["config"])
    assert list(fixture["config"].state_dir.glob("receipts/*")) == []


def test_interrupted_prior_attempt_fails_closed_without_replay(fixture, monkeypatch) -> None:
    original = lp._clone_exact

    def stop_after_intent(*args, **kwargs):
        raise PreviewError("synthetic_interruption")

    monkeypatch.setattr(lp, "_clone_exact", stop_after_intent)
    with pytest.raises(PreviewError, match="synthetic_interruption"):
        prepare_local_preview(fixture["evidence"], fixture["config"])
    assert len(list(fixture["config"].state_dir.glob("runs/*/intent.json"))) == 1
    failure = next(fixture["config"].state_dir.glob("runs/*/failure.json"))
    assert json.loads(failure.read_text())["reason"] == "synthetic_interruption"
    monkeypatch.setattr(lp, "_clone_exact", original)
    with pytest.raises(PreviewError, match="unreconciled_prior_run"):
        prepare_local_preview(fixture["evidence"], fixture["config"])
    assert list(fixture["config"].state_dir.glob("receipts/*")) == []


def test_single_active_lock(fixture) -> None:
    path = fixture["config"].state_dir
    path.mkdir(mode=0o700)
    fd = os.open(path / "publisher.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(PreviewError, match="busy"):
            prepare_local_preview(fixture["evidence"], fixture["config"])
        assert list(path.glob("runs/*")) == []
    finally:
        os.close(fd)


def test_corrupt_prior_receipt_is_not_trusted(fixture) -> None:
    result = prepare_local_preview(fixture["evidence"], fixture["config"])
    receipt = fixture["config"].state_dir / "receipts" / f"{result.run_key}.json"
    payload = json.loads(receipt.read_text())
    payload["candidate_sha"] = "b" * 40
    receipt.write_text(json.dumps(payload))
    with pytest.raises(PreviewError, match="invalid_existing_receipt"):
        prepare_local_preview(fixture["evidence"], fixture["config"])
    payload["candidate_sha"] = result.candidate_sha
    payload["status"] = "published_verified"
    receipt.write_text(json.dumps(payload))
    with pytest.raises(PreviewError, match="invalid_existing_receipt"):
        prepare_local_preview(fixture["evidence"], fixture["config"])


def test_policy_revision_changes_run_identity(fixture) -> None:
    original = prepare_local_preview(fixture["evidence"], fixture["config"])
    policy = fixture["config"].policy_path
    policy.write_text(policy.read_text() + "\n# test-only revision change\n")
    second = prepare_local_preview(fixture["evidence"], fixture["config"])
    assert original.status == "candidate_prepared"
    assert second.status == "candidate_prepared"
    assert second.run_key != original.run_key
    assert git(fixture["core_remote"], "rev-parse", "main") == fixture["core_sha"]


@pytest.mark.parametrize(
    "source, core, fixture_mode",
    [
        ("file:///tmp/private.git", "file:///tmp/core.git", False),
        ("https://token@internal.example/private.git", "https://github.com/upiscium/ObsidianCore.git", False),
        ("https://internal.example/private.git", "https://example.com/core.git", False),
        ("https://internal.example/private.git?token=xyz", "https://github.com/upiscium/ObsidianCore.git", False),
        ("https://internal.example/private.git", "https://github.com/upiscium/ObsidianCore.git#bad", False),
    ],
)
def test_unsafe_or_unapproved_repository_rejected_without_network(
    tmp_path: Path, source: str, core: str, fixture_mode: bool
) -> None:
    policy = tmp_path / "policy.toml"
    policy.write_text('version = 1\ninclude = ["98-System/**"]\n')
    config = PreviewConfig(
        source, core, policy, tmp_path / "state", "a" * 40, fixture_mode
    )
    with pytest.raises(PreviewError, match="invalid_repository|unexpected_core_repository"):
        prepare_local_preview(SnapshotEvidence("saved", "verify", "b" * 40, "verified"), config)
    # Validation occurs before creating any state or making remote requests.
    assert not config.state_dir.exists()


def test_invalid_policy_and_unpinned_revision_rejected_early(fixture) -> None:
    config = fixture["config"]
    with pytest.raises(PreviewError, match="invalid_sha"):
        prepare_local_preview(fixture["evidence"],
                              dataclasses.replace(config, publisher_revision="main"))
    with pytest.raises(PreviewError, match="invalid_policy"):
        prepare_local_preview(fixture["evidence"],
                              dataclasses.replace(config, policy_path=config.state_dir / "missing"))
    assert not config.state_dir.exists()


def test_cached_candidate_requires_unchanged_worktree_and_policy(fixture) -> None:
    result = prepare_local_preview(fixture["evidence"], fixture["config"])
    run_dir = fixture["config"].state_dir / "runs" / result.run_key
    (run_dir / "core" / "98-System/view.js").write_text("tampered\n")
    with pytest.raises(PreviewError, match="invalid_existing_receipt"):
        prepare_local_preview(fixture["evidence"], fixture["config"])
    # Even after restoring Core, changed frozen policy invalidates reuse.
    git(run_dir / "core", "restore", "98-System/view.js")
    (run_dir / "policy.toml").write_text("version=1\ninclude=[]\n")
    with pytest.raises(PreviewError, match="invalid_existing_receipt"):
        prepare_local_preview(fixture["evidence"], fixture["config"])


def test_mutable_original_policy_cannot_change_frozen_run(fixture, monkeypatch) -> None:
    policy = fixture["config"].policy_path
    original_policy = policy.read_bytes()
    original = lp._clone_exact
    counter = 0

    def move_policy_after_freeze(*args, **kwargs):
        nonlocal counter
        counter += 1
        if counter == 1:
            policy.write_text('version=1\ninclude=["bogus/**"]\n')
        return original(*args, **kwargs)

    monkeypatch.setattr(lp, "_clone_exact", move_policy_after_freeze)
    got = prepare_local_preview(fixture["evidence"], fixture["config"])
    assert got.status == "candidate_prepared"
    frozen = fixture["config"].state_dir / "runs" / got.run_key / "policy.toml"
    assert frozen.read_bytes() == original_policy
    assert git(fixture["core_remote"], "rev-parse", "main") == fixture["core_sha"]


def test_git_credentials_are_not_inherited_into_projection_process(fixture, monkeypatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "SYNTHETIC_SECRET_CANARY")
    monkeypatch.setenv("GIT_ASKPASS", "/tmp/bad-helper")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/bad-agent")
    original = lp._git

    def reject_env_leak(args, *, env, cwd, **kwargs):
        assert "GH_TOKEN" not in env
        assert "GIT_ASKPASS" not in env
        assert "SSH_AUTH_SOCK" not in env
        assert "GIT_CONFIG_GLOBAL" in env
        return original(args, env=env, cwd=cwd, **kwargs)

    monkeypatch.setattr(lp, "_git", reject_env_leak)
    got = prepare_local_preview(fixture["evidence"], fixture["config"])
    assert got.status == "candidate_prepared"
    for receipt in fixture["config"].state_dir.rglob("*.json"):
        assert "SYNTHETIC_SECRET_CANARY" not in receipt.read_text()


def test_invalid_https_port_is_a_finite_rejection(fixture) -> None:
    config = dataclasses.replace(
        fixture["config"], source_url="https://example.invalid:bogus/repo.git",
        allow_file_fixture=False,
    )
    with pytest.raises(PreviewError, match="invalid_repository"):
        prepare_local_preview(fixture["evidence"], config)


def test_validator_generated_repository_owned_file_is_not_in_candidate(fixture) -> None:
    """A successful Node validator must not add unrelated files to Core."""
    script = (
        "import { writeFileSync } from 'node:fs'; "
        "writeFileSync('README.md', 'UNEXPECTED_REPOSITORY_OWNED\\n');\n"
    )
    reviewed = advance(fixture["core"], "98-System/99-dev/validate-repo.mjs", script)
    with pytest.raises(PreviewError, match="candidate_out_of_scope"):
        prepare_local_preview(fixture["evidence"], fixture["config"])
    assert git(fixture["core_remote"], "rev-parse", "main") == reviewed
    assert list(fixture["config"].state_dir.glob("receipts/*.json")) == []
    failure = next(fixture["config"].state_dir.glob("runs/*/failure.json"))
    assert json.loads(failure.read_text())["reason"] == "candidate_out_of_scope"


def test_source_mutation_after_validator_fails_closed(fixture, monkeypatch) -> None:
    original = lp._run_publisher

    def changes_source(*args, **kwargs):
        original(*args, **kwargs)
        source = args[0]
        (source / "98-System/view.js").write_text("UNEXPECTED_SOURCE_MUTATION\n")

    monkeypatch.setattr(lp, "_run_publisher", changes_source)
    with pytest.raises(PreviewError, match="source_worktree_mutated"):
        prepare_local_preview(fixture["evidence"], fixture["config"])
    assert list(fixture["config"].state_dir.glob("receipts/*.json")) == []
    assert git(fixture["core_remote"], "rev-parse", "main") == fixture["core_sha"]


def test_frozen_policy_mutation_after_validator_fails_closed(fixture, monkeypatch) -> None:
    original = lp._run_publisher

    def changes_frozen_policy(*args, **kwargs):
        original(*args, **kwargs)
        args[2].write_text("version=1\ninclude=[]\n")

    monkeypatch.setattr(lp, "_run_publisher", changes_frozen_policy)
    with pytest.raises(PreviewError, match="policy_mutated"):
        prepare_local_preview(fixture["evidence"], fixture["config"])
    assert list(fixture["config"].state_dir.glob("receipts/*.json")) == []


def test_validator_rewriting_managed_file_is_rejected_before_receipt(fixture) -> None:
    """Allowlisted *path* alone is insufficient if validator changes its bytes."""
    validator_script = (
        "import { writeFileSync } from 'node:fs'; "
        "writeFileSync('98-System/view.js', 'UNEXPECTED_MANAGED_BYTES\\n');\n"
    )
    reviewed_head = advance(
        fixture["core"], "98-System/99-dev/validate-repo.mjs", validator_script
    )
    with pytest.raises(PreviewError, match="candidate_source_diverged"):
        prepare_local_preview(fixture["evidence"], fixture["config"])
    assert git(fixture["core_remote"], "rev-parse", "main") == reviewed_head
    assert list(fixture["config"].state_dir.glob("receipts/*.json")) == []
    failure = next(fixture["config"].state_dir.glob("runs/*/failure.json"))
    assert json.loads(failure.read_text())["reason"] == "candidate_source_diverged"


def test_source_convergence_check_rejects_a_clean_generated_wrong_tree(fixture) -> None:
    """A clean, marked local commit can be wrong even when only managed paths differ."""
    result = prepare_local_preview(fixture["evidence"], fixture["config"])
    run_dir = fixture["config"].state_dir / "runs" / result.run_key
    core = run_dir / "core"
    source = run_dir / "source"
    assert result.candidate_sha
    (core / "98-System/view.js").write_text("UNEXPECTED_MANAGED_BYTES\n")
    # The explicit convergence predicate must reject tampered bytes even if
    # the path is allowed and the existing candidate SHA remains unchanged.
    with pytest.raises(PreviewError, match="candidate_source_diverged"):
        lp._assert_projection_converged(source, core, run_dir / "policy.toml")
    assert git(fixture["core_remote"], "rev-parse", "main") == fixture["core_sha"]


@pytest.mark.parametrize("repository", ["source", "core"])
def test_cached_git_metadata_symlink_is_rejected(fixture, repository: str) -> None:
    """Do not trust a cached checkout redirected to a different .git directory."""
    result = prepare_local_preview(fixture["evidence"], fixture["config"])
    clone = fixture["config"].state_dir / "runs" / result.run_key / repository
    metadata = clone / ".git"
    relocated = clone / ".git-original"
    metadata.rename(relocated)
    metadata.symlink_to(relocated, target_is_directory=True)
    with pytest.raises(PreviewError, match="invalid_existing_receipt"):
        prepare_local_preview(fixture["evidence"], fixture["config"])
    assert git(fixture["core_remote"], "rev-parse", "main") == fixture["core_sha"]


def test_run_directory_creation_race_is_a_finite_failure(fixture, monkeypatch) -> None:
    original = Path.mkdir

    def simulate_concurrent_creation(path, *args, **kwargs):
        if path.parent.name == "runs" and len(path.name) == 64:
            original(path, *args, **kwargs)
            raise FileExistsError("synthetic concurrent directory creation")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", simulate_concurrent_creation)
    with pytest.raises(PreviewError, match="unreconciled_prior_run"):
        prepare_local_preview(fixture["evidence"], fixture["config"])
    assert list(fixture["config"].state_dir.glob("runs/*/intent.json")) == []
    assert list(fixture["config"].state_dir.glob("receipts/*.json")) == []
    assert git(fixture["core_remote"], "rev-parse", "main") == fixture["core_sha"]


def test_validator_unmanaged_leading_whitespace_path_rejected(fixture) -> None:
    """Git NUL-delimited paths must retain leading whitespace, not be stripped."""
    script = (
        "import { mkdirSync, writeFileSync } from 'node:fs'; "
        "mkdirSync(' 98-System', { recursive: true }); "
        "writeFileSync(' 98-System/view.js', 'UNMANAGED\\n');\n"
    )
    prior = advance(fixture["core"], "98-System/99-dev/validate-repo.mjs", script)
    with pytest.raises(PreviewError, match="candidate_out_of_scope"):
        prepare_local_preview(fixture["evidence"], fixture["config"])
    assert git(fixture["core_remote"], "rev-parse", "main") == prior
    assert not list(fixture["config"].state_dir.glob("receipts/*.json"))


def test_many_managed_path_changes_are_not_limited_to_4k_git_output(fixture) -> None:
    """A bounded large NUL-delimited diff must not fail for normal bulk updates."""
    from dataclasses import replace

    policy = fixture["config"].policy_path
    policy.write_text(
        'version = 1\nstrict_missing = true\n'
        'include = ["98-System/**"]\n'
        'repository_owned = [".git", ".git/**", ".github/**", "README.md", "98-System/99-dev/**"]\n'
    )
    source = fixture["source"]
    for i in range(160):
        (source / "98-System" / f"long-managed-name-{i:04d}-abcdefghijklmnopqrstuvwxyz.md").write_text(
            f"managed-{i}\n"
        )
    new_source = commit(source, "Synthetic bulk update")
    git(source, "push", "--quiet", "origin", "HEAD:main")
    new_evidence = replace(fixture["evidence"], remote_ref=new_source)
    result = prepare_local_preview(new_evidence, fixture["config"])
    assert result.status == "candidate_prepared"
    assert git(fixture["core_remote"], "rev-parse", "main") == fixture["core_sha"]
    assert len(list(fixture["config"].state_dir.glob("receipts/*.json"))) == 1


def test_git_timeout_is_a_finite_failure(tmp_path: Path, monkeypatch) -> None:
    """Do not leak subprocess exceptions or consider an unknown Git result success."""

    def timed_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["git", "status"], timeout=120)

    monkeypatch.setattr(lp.subprocess, "run", timed_out)
    with pytest.raises(PreviewError, match="git_unavailable_or_timeout"):
        lp._git(["status", "--porcelain"], env={}, cwd=tmp_path)


def test_git_path_inventory_output_cap_is_fail_closed(tmp_path: Path, monkeypatch) -> None:
    """The larger path allowance is still a finite bound, not unbounded output."""
    def oversized_output(*args, **kwargs):
        return subprocess.CompletedProcess(
            ["git", "diff"], 0, stdout=b"x" * (1024 * 1024 + 1), stderr=b""
        )

    monkeypatch.setattr(lp.subprocess, "run", oversized_output)
    with pytest.raises(PreviewError, match="git_failed"):
        lp._git(
            ["diff", "--name-only", "-z"], env={}, cwd=tmp_path,
            max_output_bytes=1024 * 1024, preserve_output=True,
        )


def test_publisher_timeout_kills_process_group_and_fails_closed(
    tmp_path: Path, monkeypatch
) -> None:
    """An over-budget validator is never treated as a completed projection."""
    calls: list[tuple[int, int]] = []

    class TimedOutPublisher:
        pid = 987654
        times_waited = 0

        def wait(self, timeout=None):
            self.times_waited += 1
            if self.times_waited == 1:
                raise subprocess.TimeoutExpired(cmd="synthetic-publisher", timeout=timeout)
            return -9

    monkeypatch.setattr(lp.subprocess, "Popen", lambda *args, **kwargs: TimedOutPublisher())
    monkeypatch.setattr(lp.os, "killpg", lambda pid, sig: calls.append((pid, sig)))
    with pytest.raises(PreviewError, match="projection_timeout"):
        lp._run_publisher(tmp_path, tmp_path, tmp_path / "policy.toml",
                          env={}, cwd=tmp_path)
    assert calls == [(987654, lp.signal.SIGKILL)]
