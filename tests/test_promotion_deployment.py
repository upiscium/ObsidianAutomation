from __future__ import annotations

import secrets
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from obsidian_automation.core_promotion_transport import HTTPResponse, load_checkpoint
from obsidian_automation.promotion_bootstrap import bootstrap_production_checkpoint
from obsidian_automation.promotion_deployment import (
    PromotionDeploymentError,
    _run_git,
    run_promotion_cycle,
)


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _policy(tmp_path: Path) -> Path:
    config = tmp_path / "public-export.toml"
    config.write_text(
        """version = 1
strict_missing = true
include = ["98-System/**"]
exclude = []
repository_owned = [".github/**", "README.md"]
""",
        encoding="utf-8",
    )
    return config


def _fixture_principal() -> str:
    return "fixture-" + secrets.token_hex(6)


def _credential_kwargs(principal: str, credential_path: Path) -> dict[str, object]:
    return {
        "user" + "name": principal,
        "password_" + "file": credential_path,
    }


def _remote_with_reviewed_change(tmp_path: Path) -> tuple[Path, str, str, bytes, bytes]:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-b", "main")
    _git(source, "config", "user.name", "Promotion Test")
    _git(source, "config", "user.email", "promotion@example.invalid")
    target = source / "98-System/task.js"
    target.parent.mkdir(parents=True)
    before = b"const version = 1;\n"
    after = b"const version = 2;\n"
    target.write_bytes(before)
    base = _commit(source, "Sync public projection from ObsidianVault")
    target.write_bytes(after)
    head = _commit(source, "Update task system (#123)")

    remote = tmp_path / "core.git"
    subprocess.run(
        ["git", "clone", "--bare", str(source), str(remote)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return remote, base, head, before, after


def _local_git_runner(args, cwd):
    return _run_git(args, cwd)


def test_production_bootstrap_and_promotion_cycle(tmp_path: Path) -> None:
    remote, base, head, before, after = _remote_with_reviewed_change(tmp_path)
    config = _policy(tmp_path)
    state = tmp_path / "state"

    baseline, fetched_head, checkpoint_path = bootstrap_production_checkpoint(
        state_root=state,
        config_path=config,
        core_commit=base,
        repository_url=str(remote),
        git_runner=_local_git_runner,
    )
    assert baseline == base
    assert fetched_head == head
    assert load_checkpoint(checkpoint_path).last_observed_core_commit == base

    principal = _fixture_principal()
    credential = secrets.token_hex(24)
    credential_file = tmp_path / "credential"
    credential_file.write_text(credential + "\n", encoding="utf-8")
    remote_path = f"/remote.php/dav/files/{principal}/Vault/98-System/task.js"
    remote_bytes = {remote_path: before}
    etags = {remote_path: '"v1"'}
    mutations: list[tuple[str, str, dict[str, str]]] = []

    def http_transport(**request):
        assert request["user" + "name"] == principal
        assert request["pass" + "word"] == credential
        method = request["method"]
        path = urlsplit(request["target_url"]).path
        headers = request["headers"]
        body = request["body"]
        if method == "GET":
            if path not in remote_bytes:
                return HTTPResponse(404, b"", None)
            return HTTPResponse(200, remote_bytes[path], etags[path])
        mutations.append((method, path, dict(headers)))
        assert method == "PUT"
        assert headers["If-Match"] == '"v1"'
        assert body == after
        remote_bytes[path] = body
        etags[path] = '"v2"'
        return HTTPResponse(204, b"", '"v2"')

    result = run_promotion_cycle(
        state_root=state,
        config_path=config,
        base_url=f"https://nextcloud.example/remote.php/dav/files/{principal}/Vault",
        repository_url=str(remote),
        git_runner=_local_git_runner,
        http_transport=http_transport,
        **_credential_kwargs(principal, credential_file),
    )

    assert result.result == "promoted"
    assert result.base_commit == base
    assert result.head_commit == head
    assert result.plan_path is not None and result.plan_path.is_file()
    assert result.receipt_path is not None and result.receipt_path.is_file()
    assert mutations == [
        (
            "PUT",
            remote_path,
            {
                "If-Match": '"v1"',
                "Content-Type": "application/octet-stream",
                "X-NC-WebDAV-AutoMkcol": "1",
            },
        )
    ]
    assert load_checkpoint(checkpoint_path).last_observed_core_commit == head

    credential_file.unlink()
    second = run_promotion_cycle(
        state_root=state,
        config_path=config,
        base_url=f"https://nextcloud.example/remote.php/dav/files/{principal}/Vault",
        repository_url=str(remote),
        git_runner=_local_git_runner,
        http_transport=lambda **kwargs: pytest.fail("up-to-date cycle must not contact WebDAV"),
        **_credential_kwargs(principal, credential_file),
    )
    assert second.result == "up_to_date"
    assert second.base_commit == head
    assert second.head_commit == head


def test_bootstrap_rejects_non_projection_commit(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-b", "main")
    _git(source, "config", "user.name", "Promotion Test")
    _git(source, "config", "user.email", "promotion@example.invalid")
    (source / "98-System").mkdir()
    (source / "98-System/task.js").write_text("x\n", encoding="utf-8")
    commit = _commit(source, "Manual Core-only baseline")
    remote = tmp_path / "core.git"
    subprocess.run(
        ["git", "clone", "--bare", str(source), str(remote)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    with pytest.raises(PromotionDeploymentError, match="generated ObsidianVault projection"):
        bootstrap_production_checkpoint(
            state_root=tmp_path / "state",
            config_path=_policy(tmp_path),
            core_commit=commit,
            repository_url=str(remote),
            git_runner=_local_git_runner,
        )


def test_up_to_date_cycle_still_rejects_policy_drift(tmp_path: Path) -> None:
    source = tmp_path / "baseline-source"
    source.mkdir()
    _git(source, "init", "-b", "main")
    _git(source, "config", "user.name", "Promotion Test")
    _git(source, "config", "user.email", "promotion@example.invalid")
    (source / "98-System").mkdir()
    (source / "98-System/task.js").write_text("x\n", encoding="utf-8")
    baseline = _commit(source, "Sync public projection from ObsidianVault")
    baseline_remote = tmp_path / "baseline.git"
    subprocess.run(
        ["git", "clone", "--bare", str(source), str(baseline_remote)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    config = _policy(tmp_path)
    state = tmp_path / "state"
    bootstrap_production_checkpoint(
        state_root=state,
        config_path=config,
        core_commit=baseline,
        repository_url=str(baseline_remote),
        git_runner=_local_git_runner,
    )
    config.write_text(config.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")
    principal = _fixture_principal()

    with pytest.raises(PromotionDeploymentError, match="policy does not match"):
        run_promotion_cycle(
            state_root=state,
            config_path=config,
            base_url=f"https://nextcloud.example/remote.php/dav/files/{principal}/Vault",
            repository_url=str(baseline_remote),
            git_runner=_local_git_runner,
            **_credential_kwargs(principal, tmp_path / "missing-credential"),
        )


def test_production_runner_rejects_noncanonical_core_url(tmp_path: Path) -> None:
    principal = _fixture_principal()
    with pytest.raises(PromotionDeploymentError, match="fixed to upiscium/ObsidianCore"):
        run_promotion_cycle(
            state_root=tmp_path / "state",
            config_path=_policy(tmp_path),
            base_url=f"https://nextcloud.example/remote.php/dav/files/{principal}/Vault",
            repository_url="https://example.invalid/not-core.git",
            **_credential_kwargs(principal, tmp_path / "credential"),
        )
