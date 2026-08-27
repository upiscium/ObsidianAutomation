from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

from obsidian_automation.core_promotion import build_promotion_plan
from obsidian_automation.core_promotion_transport import (
    HTTPResponse,
    PromotionTransportConflict,
    PromotionTransportError,
    PromotionTransportNetworkError,
    execute_promotion,
    initialize_checkpoint,
    load_checkpoint,
    parse_receipt,
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


def _fixture(tmp_path: Path):
    repo = tmp_path / "core"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Promotion Transport Test")
    _git(repo, "config", "user.email", "promotion@example.invalid")
    (repo / "98-System/01-script").mkdir(parents=True)
    (repo / "98-System/01-script/task.js").write_bytes(b"version=1\n")
    (repo / "98-System/obsolete.md").write_bytes(b"obsolete\n")
    base = _commit(repo, "base")

    (repo / "98-System/01-script/task.js").write_bytes(b"version=2\n")
    (repo / "98-System/new/path").mkdir(parents=True)
    (repo / "98-System/new/path/feature.css").write_bytes(b".feature{}\n")
    (repo / "98-System/obsolete.md").unlink()
    head = _commit(repo, "head")

    config = tmp_path / "public.toml"
    config.write_text(
        """version = 1
strict_missing = true
include = ["98-System/**"]
exclude = ["98-System/.rclone-bisync/**"]
repository_owned = [".github/**", ".gitignore", "README.md", "LICENSE"]
""",
        encoding="utf-8",
    )
    plan = build_promotion_plan(repo, base_ref=base, head_ref=head, config_path=config)
    checkpoint = tmp_path / "checkpoint.json"
    policy_sha = hashlib.sha256(config.read_bytes()).hexdigest()
    initialize_checkpoint(
        checkpoint,
        core_commit=base,
        policy_sha256=policy_sha,
        initialized_at="2026-08-27T00:00:00Z",
    )
    receipts = tmp_path / "receipts"
    return repo, config, plan, checkpoint, receipts


def _base_remote(plan, repo: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for change in plan.changes:
        if change.before_sha256 is None:
            continue
        data = subprocess.run(
            ["git", "show", f"{plan.base_commit}:{change.path}"],
            cwd=repo,
            check=True,
            stdout=subprocess.PIPE,
        ).stdout
        files[change.path] = data
    return files


class FakeDAV:
    def __init__(self, files: dict[str, bytes]):
        self.files = dict(files)
        self.versions = {path: 1 for path in files}
        self.mutations: list[tuple[str, str, dict[str, str], bytes | None]] = []
        self.raise_after_apply_for: str | None = None
        self.concurrent_before_mutation: dict[str, bytes] = {}
        self.weak_etag_paths: set[str] = set()

    def _path(self, target_url: str) -> str:
        raw = unquote(urlsplit(target_url).path)
        marker = "/vault/"
        if marker not in raw:
            raise AssertionError(raw)
        return raw.split(marker, 1)[1]

    def _etag(self, path: str) -> str | None:
        if path not in self.files:
            return None
        tag = f'"v{self.versions.get(path, 1)}"'
        return f"W/{tag}" if path in self.weak_etag_paths else tag

    def __call__(
        self,
        *,
        method: str,
        target_url: str,
        username: str,
        password: str,
        headers,
        body,
        timeout: float,
        response_limit: int,
    ) -> HTTPResponse:
        assert username == "promoter"
        assert password == "secret"
        path = self._path(target_url)
        headers = dict(headers)
        if method == "GET":
            if path not in self.files:
                return HTTPResponse(404, b"", None)
            return HTTPResponse(200, self.files[path], self._etag(path))

        self.mutations.append((method, path, headers, body))
        if path in self.concurrent_before_mutation:
            self.files[path] = self.concurrent_before_mutation.pop(path)
            self.versions[path] = self.versions.get(path, 1) + 1

        current_etag = self._etag(path)
        if headers.get("If-None-Match") == "*" and path in self.files:
            return HTTPResponse(412, b"", current_etag)
        if "If-Match" in headers and headers["If-Match"] != current_etag:
            return HTTPResponse(412, b"", current_etag)

        if method == "PUT":
            assert body is not None
            assert headers.get("X-NC-WebDAV-AutoMkcol") == "1"
            self.files[path] = body
            self.versions[path] = self.versions.get(path, 0) + 1
            response = HTTPResponse(201 if current_etag is None else 204, b"", self._etag(path))
        elif method == "DELETE":
            self.files.pop(path, None)
            self.versions.pop(path, None)
            response = HTTPResponse(204, b"", None)
        else:
            raise AssertionError(method)

        if self.raise_after_apply_for == path:
            self.raise_after_apply_for = None
            raise PromotionTransportNetworkError("simulated lost response")
        return response


def test_transport_applies_create_update_delete_and_advances_checkpoint(tmp_path: Path) -> None:
    repo, config, plan, checkpoint, receipts = _fixture(tmp_path)
    dav = FakeDAV(_base_remote(plan, repo))

    receipt_sha, receipt_path, receipt, advanced = execute_promotion(
        plan=plan,
        core_repository=repo,
        config_path=config,
        base_url="https://nextcloud.example/vault",
        username="promoter",
        password="secret",
        checkpoint_path=checkpoint,
        receipt_directory=receipts,
        transport=dav,
    )

    assert len(receipt_sha) == 64
    assert receipt_path.read_bytes() == receipt.to_json_bytes()
    assert parse_receipt(receipt_path.read_bytes()) == receipt
    assert advanced.last_observed_core_commit == plan.head_commit
    assert load_checkpoint(checkpoint).last_observed_core_commit == plan.head_commit
    assert {item.result for item in receipt.outcomes} == {"applied"}

    for change in plan.changes:
        if change.action == "delete":
            assert change.path not in dav.files
        else:
            assert hashlib.sha256(dav.files[change.path]).hexdigest() == change.after_sha256

    update = next(item for item in dav.mutations if item[1].endswith("task.js"))
    assert update[2]["If-Match"].startswith('"')
    create = next(item for item in dav.mutations if item[1].endswith("feature.css"))
    assert create[2]["If-None-Match"] == "*"


def test_preflight_conflict_performs_no_mutation_and_keeps_checkpoint(tmp_path: Path) -> None:
    repo, config, plan, checkpoint, receipts = _fixture(tmp_path)
    files = _base_remote(plan, repo)
    files["98-System/01-script/task.js"] = b"local divergence\n"
    dav = FakeDAV(files)

    with pytest.raises(PromotionTransportConflict, match="no mutation attempted"):
        execute_promotion(
            plan=plan,
            core_repository=repo,
            config_path=config,
            base_url="https://nextcloud.example/vault",
            username="promoter",
            password="secret",
            checkpoint_path=checkpoint,
            receipt_directory=receipts,
            transport=dav,
        )

    assert dav.mutations == []
    assert load_checkpoint(checkpoint).last_observed_core_commit == plan.base_commit


def test_all_already_applied_advances_checkpoint_without_mutations(tmp_path: Path) -> None:
    repo, config, plan, checkpoint, receipts = _fixture(tmp_path)
    files: dict[str, bytes] = {}
    for change in plan.changes:
        if change.after_sha256 is None:
            continue
        files[change.path] = subprocess.run(
            ["git", "show", f"{plan.head_commit}:{change.path}"],
            cwd=repo,
            check=True,
            stdout=subprocess.PIPE,
        ).stdout
    dav = FakeDAV(files)

    _, _, receipt, advanced = execute_promotion(
        plan=plan,
        core_repository=repo,
        config_path=config,
        base_url="https://nextcloud.example/vault",
        username="promoter",
        password="secret",
        checkpoint_path=checkpoint,
        receipt_directory=receipts,
        transport=dav,
    )

    assert dav.mutations == []
    assert {item.result for item in receipt.outcomes} == {"already_applied"}
    assert advanced.last_observed_core_commit == plan.head_commit


def test_ambiguous_lost_response_recovers_from_remote_exact_bytes(tmp_path: Path) -> None:
    repo, config, plan, checkpoint, receipts = _fixture(tmp_path)
    dav = FakeDAV(_base_remote(plan, repo))
    target = next(change.path for change in plan.changes if change.action == "update")
    dav.raise_after_apply_for = target

    _, _, receipt, advanced = execute_promotion(
        plan=plan,
        core_repository=repo,
        config_path=config,
        base_url="https://nextcloud.example/vault",
        username="promoter",
        password="secret",
        checkpoint_path=checkpoint,
        receipt_directory=receipts,
        transport=dav,
    )

    by_path = {item.path: item.result for item in receipt.outcomes}
    assert by_path[target] == "recovered"
    assert advanced.last_observed_core_commit == plan.head_commit


def test_concurrent_update_is_rejected_by_if_match_and_checkpoint_stays_base(tmp_path: Path) -> None:
    repo, config, plan, checkpoint, receipts = _fixture(tmp_path)
    dav = FakeDAV(_base_remote(plan, repo))
    target = next(change.path for change in plan.changes if change.action == "update")
    dav.concurrent_before_mutation[target] = b"concurrent edit\n"

    with pytest.raises(PromotionTransportConflict):
        execute_promotion(
            plan=plan,
            core_repository=repo,
            config_path=config,
            base_url="https://nextcloud.example/vault",
            username="promoter",
            password="secret",
            checkpoint_path=checkpoint,
            receipt_directory=receipts,
            transport=dav,
        )

    assert load_checkpoint(checkpoint).last_observed_core_commit == plan.base_commit


def test_update_requires_strong_etag_before_any_mutation(tmp_path: Path) -> None:
    repo, config, plan, checkpoint, receipts = _fixture(tmp_path)
    dav = FakeDAV(_base_remote(plan, repo))
    target = next(change.path for change in plan.changes if change.action == "update")
    dav.weak_etag_paths.add(target)

    with pytest.raises(PromotionTransportError, match="strong ETag"):
        execute_promotion(
            plan=plan,
            core_repository=repo,
            config_path=config,
            base_url="https://nextcloud.example/vault",
            username="promoter",
            password="secret",
            checkpoint_path=checkpoint,
            receipt_directory=receipts,
            transport=dav,
        )

    assert dav.mutations == []


def test_checkpoint_must_match_plan_base(tmp_path: Path) -> None:
    repo, config, plan, checkpoint, receipts = _fixture(tmp_path)
    checkpoint.unlink()
    policy_sha = hashlib.sha256(config.read_bytes()).hexdigest()
    initialize_checkpoint(
        checkpoint,
        core_commit="f" * 40,
        policy_sha256=policy_sha,
        initialized_at="2026-08-27T00:00:00Z",
    )
    dav = FakeDAV(_base_remote(plan, repo))

    with pytest.raises(PromotionTransportError, match="base_commit"):
        execute_promotion(
            plan=plan,
            core_repository=repo,
            config_path=config,
            base_url="https://nextcloud.example/vault",
            username="promoter",
            password="secret",
            checkpoint_path=checkpoint,
            receipt_directory=receipts,
            transport=dav,
        )

    assert dav.mutations == []


def test_remote_http_is_rejected_before_transport(tmp_path: Path) -> None:
    repo, config, plan, checkpoint, receipts = _fixture(tmp_path)
    dav = FakeDAV(_base_remote(plan, repo))

    with pytest.raises(PromotionTransportError, match="HTTPS"):
        execute_promotion(
            plan=plan,
            core_repository=repo,
            config_path=config,
            base_url="http://nextcloud.example/vault",
            username="promoter",
            password="secret",
            checkpoint_path=checkpoint,
            receipt_directory=receipts,
            transport=dav,
        )

    assert dav.mutations == []
